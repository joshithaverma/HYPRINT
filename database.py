"""
database.py — SQLite persistence for the print kiosk.

LOCKING / IDEMPOTENCY DESIGN
============================
SQLite has no row-level `SELECT ... FOR UPDATE`; the database file is the
unit of locking. The guarantee we actually need ("a webhook delivered twice
must never charge or print twice") is provided by three layers:

 1. WAL mode — readers (admin dashboard, status polls) never block on the
    writer that's flipping a job's state.

 2. BEGIN IMMEDIATE for write transactions, via SQLAlchemy's documented
    pysqlite recipe (disable the driver's implicit BEGIN, then emit our own
    in the engine "begin" hook). This takes the write lock up front rather
    than optimistically escalating later and failing.

 3. Compare-and-swap UPDATEs (`atomic_transition`):
        UPDATE jobs SET status=:new WHERE id=:id AND status IN (:allowed)
    and check rowcount == 1. Only the first caller's WHERE clause matches;
    every subsequent duplicate is a no-op. This is the correct pattern for a
    job state machine on any database, not a SQLite workaround.

 4. A UNIQUE constraint on payment_ref is an independent backstop: the DB
    itself refuses a second row claiming the same gateway payment id.
"""

import enum
import os
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, event, update,
    Column, String, Integer, Float, Boolean, DateTime, Text, Enum as SAEnum,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# On Vercel / serverless, writeable storage is restricted to /tmp
DB_PATH = "/tmp/print_kiosk.db" if os.environ.get("VERCEL") else os.environ.get("KIOSK_DB_PATH", "print_kiosk.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)

_local = threading.local()


@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _):
    # Hand BEGIN control to us (see docstring point 2).
    dbapi_conn.isolation_level = None
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


@event.listens_for(engine, "begin")
def _on_begin(conn):
    if getattr(_local, "begin_mode", "DEFERRED") == "IMMEDIATE":
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        _local.begin_mode = "DEFERRED"   # don't leak into the next txn
    else:
        conn.exec_driver_sql("BEGIN")


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class JobStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    # Paid and waiting for the student to physically arrive at the kiosk and
    # key in their PIN. Nothing prints until then — otherwise a job uploaded
    # from a hostel room at midnight lands on an unattended tray.
    AWAITING_RELEASE = "AWAITING_RELEASE"
    # The PIN was entered and the kiosk is showing a full-screen preview.
    # The student must click 'Approve' to trigger the print.
    REVIEWING = "REVIEWING"
    SPOOLING = "SPOOLING"
    PRINTING = "PRINTING"
    PAUSED_ERROR = "PAUSED_ERROR"
    COMPLETED = "COMPLETED"
    ABORTED_REFUNDED = "ABORTED_REFUNDED"
    FAILED = "FAILED"
    FAILED_REBOOT = "FAILED_REBOOT"   # stranded by a crash/restart, recovered at boot
    EXPIRED = "EXPIRED"               # unpaid and swept by the scavenger


TERMINAL_STATES = {
    JobStatus.COMPLETED, JobStatus.ABORTED_REFUNDED,
    JobStatus.FAILED, JobStatus.FAILED_REBOOT, JobStatus.EXPIRED,
}

# States that mean "this job was mid-flight when the process died".
# AWAITING_RELEASE is deliberately EXCLUDED: those jobs are paid but not yet
# printed, and they survive a reboot perfectly well — the student can still
# walk up and key in their PIN afterwards.
STRANDED_STATES = {
    JobStatus.PAID, JobStatus.SPOOLING, JobStatus.PRINTING, JobStatus.PAUSED_ERROR,
}


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kiosk_id = Column(String(32), nullable=False, index=True)

    # --- source file ---
    original_filename = Column(String(512), nullable=False)
    source_path = Column(String(1024), nullable=False)   # full uploaded PDF in /dev/shm
    print_path = Column(String(1024), nullable=False)    # sliced subset actually printed
    source_page_count = Column(Integer, nullable=False, default=0)

    # --- print options ---
    page_range = Column(String(256), nullable=True)      # raw user input, e.g. "1-5, 8"
    pages_selected = Column(Integer, nullable=False, default=0)   # after slicing
    copies = Column(Integer, nullable=False, default=1)
    duplex = Column(Boolean, nullable=False, default=False)
    paper_size = Column(String(16), nullable=False, default="A4")      # A4 | Letter | Legal
    orientation = Column(String(16), nullable=False, default="portrait")
    pages_per_sheet = Column(Integer, nullable=False, default=1)       # 1 | 2 | 4
    # This build drives a monochrome-only printer, so colour is not an option
    # the student can pick. Stored explicitly so the column is already here if
    # a colour unit is ever added to the fleet.
    color_mode = Column(String(16), nullable=False, default="monochrome")

    # --- pricing (always server-computed; client input is never trusted) ---
    price_per_page = Column(Float, nullable=False, default=2.0)
    total_price = Column(Float, nullable=False, default=0.0)

    # --- state ---
    status = Column(SAEnum(JobStatus), nullable=False, default=JobStatus.UPLOADED, index=True)
    error_reason = Column(Text, nullable=True)

    # --- payment ---
    payment_order_id = Column(String(128), nullable=True)
    payment_ref = Column(String(128), nullable=True, unique=True)  # backstop vs replay
    paid_at = Column(DateTime, nullable=True)
    release_pin = Column(String(8), nullable=True)   # 4-digit collection code
    released_at = Column(DateTime, nullable=True)    # when the PIN was keyed in

    # --- printing ---
    cups_job_id = Column(Integer, nullable=True)
    sheets_printed = Column(Integer, nullable=False, default=0)
    sheets_total = Column(Integer, nullable=False, default=0)

    # --- student identity ---
    user_name = Column(String(128), nullable=True)
    user_phone = Column(String(32), nullable=True)

    # --- refund ---
    refund_amount = Column(Float, nullable=True)
    refund_ref = Column(String(128), nullable=True)
    refund_error = Column(Text, nullable=True)   # set if the gateway call failed

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def public(self):
        """Safe projection for the student's browser. Never exposes file
        paths, gateway internals, or the release PIN."""
        return {
            "job_id": self.id,
            "kiosk_id": self.kiosk_id,
            "filename": self.original_filename,
            "user_name": self.user_name or "Student",
            "user_phone": self.user_phone,
            "source_page_count": self.source_page_count,
            "pages_selected": self.pages_selected,
            "page_range": self.page_range,
            "copies": self.copies,
            "duplex": self.duplex,
            "paper_size": self.paper_size,
            "orientation": self.orientation,
            "pages_per_sheet": self.pages_per_sheet,
            "color_mode": self.color_mode,
            "price_per_page": self.price_per_page,
            "total_price": self.total_price,
            "status": self.status.value if isinstance(self.status, JobStatus) else self.status,
            "error_reason": self.error_reason,
            "sheets_printed": self.sheets_printed,
            "sheets_total": self.sheets_total,
            # release_pin intentionally OMITTED — never send PIN over the wire
            "refund_amount": self.refund_amount,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
        }

    def admin(self):
        d = self.public()
        d.update({
            "payment_ref": self.payment_ref,
            "refund_ref": self.refund_ref,
            "refund_error": self.refund_error,
            "cups_job_id": self.cups_job_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        })
        return d


def init_db():
    Base.metadata.create_all(engine)
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(print_jobs)")
        existing = [row[1] for row in cur.fetchall()]
        if "user_name" not in existing:
            cur.execute("ALTER TABLE print_jobs ADD COLUMN user_name VARCHAR(128)")
        if "user_phone" not in existing:
            cur.execute("ALTER TABLE print_jobs ADD COLUMN user_phone VARCHAR(32)")
        conn.commit()
        conn.close()
    except Exception as e:
        print("[init_db] migration note:", e)


def get_session() -> Session:
    """Read session (deferred BEGIN, shared lock only)."""
    return SessionLocal()


def get_write_session() -> Session:
    """Write session — takes SQLite's write lock immediately."""
    _local.begin_mode = "IMMEDIATE"
    return SessionLocal()


def atomic_transition(session: Session, job_id: str,
                      from_statuses: set, to_status: JobStatus, **fields) -> bool:
    """Compare-and-swap the status. True iff THIS call performed the move.
    Caller owns the transaction (commit/rollback)."""
    stmt = (
        update(PrintJob)
        .where(PrintJob.id == job_id)
        .where(PrintJob.status.in_(list(from_statuses)))
        .values(status=to_status, updated_at=datetime.now(timezone.utc), **fields)
    )
    return session.execute(stmt).rowcount == 1
