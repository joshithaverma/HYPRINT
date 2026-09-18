"""
main.py — Campus Print Kiosk backend.

Run locally:   uvicorn main:app --host 127.0.0.1 --port 8000
Production:    via deploy/kiosk-backend.service (systemd, Restart=always)

The server binds to 127.0.0.1 ONLY. Public reachability comes exclusively
from the Cloudflare named tunnel, which lets us expose student routes to the
internet while /admin and the debug routes stay physically unreachable from
outside the machine (enforced again in code by require_local()).
"""

import asyncio
import base64
import io
import os
import random
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import qrcode
from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Request, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

import config
import cups_manager
import payments
import pdf_tools
from database import (
    PrintJob, JobStatus, STRANDED_STATES, TERMINAL_STATES,
    atomic_transition, get_session, get_write_session, init_db,
)

os.makedirs(config.SHM_DIR, exist_ok=True)

app = FastAPI(title="Campus Print Kiosk", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root_page():
    """Serve the student-facing print page at the root URL."""
    return FileResponse("static/index.html")


@app.get("/select-location")
def select_location_redirect():
    """Legacy route — redirect to root since there's only one printer."""
    return FileResponse("static/index.html")


# ===========================================================================
# Access control
# ===========================================================================
def require_local(request: Request):
    """
    Gate for /admin and debug routes.

    Defense in depth: uvicorn is already bound to 127.0.0.1 so nothing but
    this machine can connect directly, but the Cloudflare tunnel DOES reach
    127.0.0.1:8000 — so without this check, tunnelled requests would sail
    straight into the admin panel. Tunnelled requests arrive with the
    tunnel's own source address and/or X-Forwarded-For set; a genuinely
    local browser tab has neither.
    """
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404, detail="Not found.")
    # A forwarded header means it came through a proxy/tunnel, not the
    # kiosk's own browser — reject even though the socket looks local.
    if request.headers.get("x-forwarded-for") or request.headers.get("cf-connecting-ip"):
        raise HTTPException(status_code=404, detail="Not found.")
    if config.ADMIN_TOKEN:
        supplied = request.headers.get("x-admin-token") or request.query_params.get("token")
        if not supplied or not secrets.compare_digest(supplied, config.ADMIN_TOKEN):
            raise HTTPException(status_code=401, detail="Admin token required.")
    return True


# ===========================================================================
# WebSocket manager
# ===========================================================================
class ConnectionManager:
    def __init__(self):
        self._per_job: dict[str, list[WebSocket]] = {}
        self._kiosk: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def join_job(self, job_id: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._per_job.setdefault(job_id, []).append(ws)

    async def leave_job(self, job_id: str, ws: WebSocket):
        async with self._lock:
            lst = self._per_job.get(job_id, [])
            if ws in lst:
                lst.remove(ws)
            if not lst:
                self._per_job.pop(job_id, None)

    async def join_kiosk(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._kiosk.append(ws)

    async def leave_kiosk(self, ws: WebSocket):
        async with self._lock:
            if ws in self._kiosk:
                self._kiosk.remove(ws)

    async def _send_all(self, targets: list[WebSocket], payload: dict):
        dead = []
        for ws in list(targets):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in targets:
                        targets.remove(ws)

    async def to_job(self, job_id: str, payload: dict):
        async with self._lock:
            targets = list(self._per_job.get(job_id, []))
        await self._send_all(targets, payload)

    async def to_kiosk(self, payload: dict):
        async with self._lock:
            targets = list(self._kiosk)
        await self._send_all(targets, payload)


manager = ConnectionManager()


async def broadcast_job(job_id: str, message: str):
    """Push the current DB truth for one job to its phone AND the kiosk screen."""
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job:
            return
        payload = job.public()
    payload["message"] = message
    await manager.to_job(job_id, payload)
    await manager.to_kiosk({"event": "job_update", **payload, "message": message})


# ===========================================================================
# Lifecycle: boot recovery + background tasks
# ===========================================================================
@app.on_event("startup")
async def startup():
    init_db()
    recovered = recover_stranded_jobs()
    if recovered:
        print(f"[boot] recovered {recovered} stranded job(s) -> FAILED_REBOOT")
    asyncio.create_task(scavenger_loop())


def recover_stranded_jobs() -> int:
    """
    A crash/power-cut mid-print leaves rows in PAID/SPOOLING/PRINTING forever,
    which would show as phantom active jobs on the kiosk screen and block the
    queue. At boot, every such row is moved to FAILED_REBOOT.

    These are jobs the student PAID for and may not have received, so they're
    surfaced in the admin dashboard as needing a manual refund decision rather
    than being silently auto-refunded (we can't know from here how many sheets
    physically came out before the power died).
    """
    count = 0
    with get_write_session() as s:
        stranded = s.query(PrintJob).filter(PrintJob.status.in_(list(STRANDED_STATES))).all()
        for job in stranded:
            job.status = JobStatus.FAILED_REBOOT
            job.error_reason = "Kiosk restarted while this job was in progress."
            _purge_files(job)
            count += 1
        s.commit()
    return count


async def scavenger_loop():
    """Every 5 minutes: delete RAM-disk files for jobs that were never paid
    for, and mark them EXPIRED. Without this, /dev/shm fills with abandoned
    uploads and eventually exhausts RAM."""
    while True:
        try:
            await asyncio.sleep(config.SCAVENGER_INTERVAL_SECONDS)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.ABANDONED_JOB_TTL_SECONDS)
            purged = 0
            with get_write_session() as s:
                stale = s.query(PrintJob).filter(
                    PrintJob.status.in_([JobStatus.UPLOADED, JobStatus.PENDING_PAYMENT,
                                         JobStatus.PREFLIGHT_BLOCKED]),
                    PrintJob.created_at < cutoff,
                ).all()
                for job in stale:
                    _purge_files(job)
                    job.status = JobStatus.EXPIRED
                    job.error_reason = "Expired before payment."
                    purged += 1
                s.commit()

            # Also sweep any orphan files on disk with no DB row at all
            # (e.g. a crash between writing the file and inserting the row).
            _sweep_orphan_files()

            # --- paid but never collected -> destroy file, refund money ---
            uncollected_cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=config.UNCOLLECTED_TTL_SECONDS)
            with get_session() as s:
                stale_paid = s.query(PrintJob).filter(
                    PrintJob.status == JobStatus.AWAITING_RELEASE,
                    PrintJob.paid_at < uncollected_cutoff,
                ).all()
                ids = [j.id for j in stale_paid]
            for jid in ids:
                # Nothing printed, so the whole amount goes back.
                await fail_and_refund(jid, "Not collected in time.", sheets_done=0,
                                      from_states={JobStatus.AWAITING_RELEASE})

            if purged:
                await manager.to_kiosk({"event": "jobs_expired", "count": purged})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[scavenger] error: {e}")


def _purge_files(job: PrintJob):
    for p in {job.source_path, job.print_path}:
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _sweep_orphan_files():
    cutoff = time.time() - config.ABANDONED_JOB_TTL_SECONDS
    try:
        names = os.listdir(config.SHM_DIR)
    except OSError:
        return
    with get_session() as s:
        for name in names:
            path = os.path.join(config.SHM_DIR, name)
            try:
                if os.path.getmtime(path) > cutoff:
                    continue
            except OSError:
                continue
            stem = name.split(".")[0].split("_")[0]
            if not s.get(PrintJob, stem):
                try:
                    os.remove(path)
                except OSError:
                    pass


# ===========================================================================
# Pre-flight
# ===========================================================================
@app.get("/api/preflight")
def preflight():
    try:
        reasons = cups_manager.printer_state_reasons()
    except Exception as e:
        return {"printer_ok": False, "reasons": [], "blocking_reason": "printer-unreachable",
                "detail": str(e), "kiosk_id": config.KIOSK_ID}
    fault = cups_manager.blocking_fault(reasons)

    if fault is None:
        # The printer just recovered. Any job that was parked in
        # PREFLIGHT_BLOCKED at upload time can now proceed to payment —
        # without this, a student who uploaded during a paper jam would have
        # to re-upload from scratch once staff refilled the tray.
        with get_write_session() as s:
            unblocked = s.query(PrintJob).filter(
                PrintJob.kiosk_id == config.KIOSK_ID,
                PrintJob.status == JobStatus.PREFLIGHT_BLOCKED,
            ).update(
                {"status": JobStatus.PENDING_PAYMENT, "error_reason": None},
                synchronize_session=False,
            )
            s.commit()
        if unblocked:
            print(f"[preflight] printer recovered, unblocked {unblocked} job(s)")

    return {
        "printer_ok": fault is None,
        "reasons": reasons,
        "blocking_reason": fault,
        "kiosk_id": config.KIOSK_ID,
        "price_per_page": config.PRICE_PER_PAGE,
        "max_copies": config.MAX_COPIES,
        "max_upload_mb": config.MAX_UPLOAD_BYTES // (1024 * 1024),
        "payment_disabled": config.PAYMENT_DISABLED,
    }


# ===========================================================================
# Upload + slicing + pricing
# ===========================================================================
@app.post("/api/submit-job")
async def submit_job(
    request: Request,
    file: UploadFile = File(...),
    kiosk_id: str = Form(config.KIOSK_ID),
    user_name: str = Form("Student"),
    user_phone: str = Form(""),
    copies: int = Form(1),
    duplex: bool = Form(False),
    page_range: str = Form(""),
    paper_size: str = Form("A4"),
    orientation: str = Form("portrait"),
    pages_per_sheet: int = Form(1),
):
    # --- cheap rejects first, before touching RAM ---
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Limit is {config.MAX_UPLOAD_BYTES // (1024*1024)}MB.")

    if not (1 <= copies <= config.MAX_COPIES):
        raise HTTPException(400, f"Copies must be between 1 and {config.MAX_COPIES}.")
    if paper_size not in ("A4", "Letter", "Legal"):
        raise HTTPException(400, "Unsupported paper size.")
    if orientation not in ("portrait", "landscape"):
        raise HTTPException(400, "Unsupported orientation.")
    if pages_per_sheet not in (1, 2, 4, 6):
        raise HTTPException(400, "Pages per sheet must be 1, 2, 4 or 6.")

    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted.")

    clean_name = (user_name or "").strip() or "Student"
    raw_digits = "".join(ch for ch in (user_phone or "") if ch.isdigit())
    if len(raw_digits) > 10 and raw_digits.startswith("91"):
        raw_digits = raw_digits[2:]
    clean_phone = raw_digits[-10:] if len(raw_digits) >= 10 else raw_digits

    job_id = str(uuid.uuid4())
    src = os.path.join(config.SHM_DIR, f"{job_id}.pdf")

    # --- stream into RAM disk, enforcing the cap for real ---
    # The Content-Length header can lie, so we count actual bytes and abort
    # mid-stream the moment the cap is crossed.
    written = 0
    try:
        with open(src, "wb") as fh:
            while chunk := await file.read(1024 * 256):
                written += len(chunk)
                if written > config.MAX_UPLOAD_BYTES:
                    fh.close()
                    os.remove(src)
                    raise HTTPException(413, f"File too large. Limit is {config.MAX_UPLOAD_BYTES // (1024*1024)}MB.")
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception:
        if os.path.exists(src):
            os.remove(src)
        raise HTTPException(500, "Upload failed. Please try again.")

    if written == 0:
        os.remove(src)
        raise HTTPException(400, "That file is empty.")

    # --- validate + slice ---
    try:
        source_pages = pdf_tools.inspect_pdf(src)
        selected = pdf_tools.parse_page_range(page_range, source_pages)
        print_path = os.path.join(config.SHM_DIR, f"{job_id}_print.pdf")
        pdf_tools.slice_pdf(src, print_path, selected)
    except pdf_tools.PdfError as e:
        if os.path.exists(src):
            os.remove(src)
        raise HTTPException(422, str(e))

    pages_selected = len(selected)
    # Server-side pricing. The client sends preferences, never a price.
    # Charging is per SIDE of content, so N-up (2/4 pages per sheet) reduces
    # paper used but not the page count the student is billed for.
    total_price = round(pages_selected * copies * config.PRICE_PER_PAGE, 2)

    # Physical sheets consumed: N-up packs several pages onto one side first,
    # then duplex halves the sheets by using both sides. Order matters.
    sides_per_copy = -(-pages_selected // pages_per_sheet)     # ceil division
    sheets_per_copy = -(-sides_per_copy // 2) if duplex else sides_per_copy
    sheets_total = sheets_per_copy * copies

    # --- hardware gate ---
    try:
        fault = cups_manager.blocking_fault(cups_manager.printer_state_reasons())
    except Exception:
        fault = "printer-unreachable"

    with get_write_session() as s:
        job = PrintJob(
            id=job_id,
            kiosk_id=kiosk_id or config.KIOSK_ID,
            user_name=clean_name,
            user_phone=clean_phone,
            original_filename=filename,
            source_path=src,
            print_path=print_path,
            source_page_count=source_pages,
            page_range=page_range or None,
            pages_selected=pages_selected,
            copies=copies,
            duplex=duplex,
            paper_size=paper_size,
            orientation=orientation,
            pages_per_sheet=pages_per_sheet,
            color_mode="monochrome",
            price_per_page=config.PRICE_PER_PAGE,
            total_price=total_price,
            sheets_total=sheets_total,
            status=JobStatus.PREFLIGHT_BLOCKED if fault else (
                JobStatus.AWAITING_RELEASE if config.PAYMENT_DISABLED else JobStatus.PENDING_PAYMENT
            ),
            release_pin=f"{random.randint(1000, 9999):04d}" if (config.PAYMENT_DISABLED and not fault) else None,
            paid_at=datetime.now(timezone.utc) if (config.PAYMENT_DISABLED and not fault) else None,
            payment_ref=f"test_bypass_{uuid.uuid4().hex[:12]}" if (config.PAYMENT_DISABLED and not fault) else None,
            error_reason=fault,
        )
        s.add(job)
        s.commit()
        result = job.public()
        # PIN is sent ONCE to the submitter's browser at upload time — never again.
        # It is NOT included in queue broadcasts or any other response.
        if job.release_pin:
            result["release_pin"] = job.release_pin   # one-time delivery only
        result["payment_disabled"] = config.PAYMENT_DISABLED

    if config.PAYMENT_DISABLED and not fault:
        await broadcast_job(job_id, f"Testing mode active. {clean_name}, your print is ready for release.")
    await manager.to_kiosk({"event": "new_pending_job", **result})
    result["printer_ok"] = fault is None
    return result


# ===========================================================================
# Payment
# ===========================================================================
@app.post("/api/payment/create/{job_id}")
def create_payment(job_id: str):
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        if job.status == JobStatus.PREFLIGHT_BLOCKED:
            raise HTTPException(409, "Printer has a hardware fault — payment is blocked.")
        if job.status != JobStatus.PENDING_PAYMENT:
            raise HTTPException(409, f"This job is not awaiting payment (status: {job.status.value}).")
        amount = job.total_price

    # Re-check hardware at the last moment — it may have failed since upload.
    try:
        fault = cups_manager.blocking_fault(cups_manager.printer_state_reasons())
    except Exception:
        fault = "printer-unreachable"
    if fault:
        with get_write_session() as s:
            atomic_transition(s, job_id, {JobStatus.PENDING_PAYMENT},
                              JobStatus.PREFLIGHT_BLOCKED, error_reason=fault)
            s.commit()
        raise HTTPException(409, f"Printer fault detected ({fault}). Payment blocked.")

    order_id = f"order_{uuid.uuid4().hex[:16]}"
    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        job.payment_order_id = order_id
        s.commit()

    upi_uri = (f"upi://pay?pa={config.UPI_VPA}&pn=CampusPrintKiosk"
               f"&am={amount:.2f}&cu=INR&tn=Print-{job_id[:8]}")
    buf = io.BytesIO()
    qrcode.make(upi_uri).save(buf, format="PNG")

    return {
        "job_id": job_id,
        "order_id": order_id,
        "amount": amount,
        "currency": "INR",
        "upi_uri": upi_uri,
        "qr_code_base64": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
    }


@app.post("/api/payment/webhook")
async def payment_webhook(request: Request):
    """
    Gateway → kiosk payment confirmation.

    Publicly reachable, so it is signature-gated (see payments.verify_webhook).
    Idempotent by construction: the PENDING_PAYMENT→PAID compare-and-swap can
    only succeed once, so duplicate deliveries never double-print.
    """
    raw = await request.body()
    try:
        payments.verify_webhook(
            raw,
            request.headers.get("x-webhook-signature"),
            request.headers.get("x-webhook-timestamp"),
        )
    except payments.WebhookError as e:
        # Deliberately vague to the caller; detail goes to the local log only.
        print(f"[webhook] rejected: {e}")
        raise HTTPException(401, "Invalid signature.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Malformed JSON.")

    job_id = body.get("job_id")
    payment_ref = body.get("payment_ref")
    if not job_id or not payment_ref:
        raise HTTPException(400, "job_id and payment_ref are required.")
    if body.get("status", "captured") != "captured":
        return {"ok": True, "ignored": True}

    pin = f"{random.randint(0, 9999):04d}"

    try:
        with get_write_session() as s:
            moved = atomic_transition(
                s, job_id, {JobStatus.PENDING_PAYMENT}, JobStatus.AWAITING_RELEASE,
                payment_ref=payment_ref,
                paid_at=datetime.now(timezone.utc),
                release_pin=pin,
            )
            s.commit()
    except Exception as e:
        # UNIQUE violation on payment_ref = this payment was already applied.
        if "UNIQUE" in str(e).upper():
            return {"ok": True, "already_processed": True}
        raise

    if not moved:
        return {"ok": True, "already_processed": True}

    # NOTE: we do NOT print here. The job waits for the student to key their
    # PIN into the kiosk (POST /api/release), so pages never emerge onto an
    # unattended tray.
    await broadcast_job(job_id, "Payment received. Enter your code at the kiosk to print.")
    return {"ok": True, "awaiting_release": True}


# ===========================================================================
# Release (the bridge between the public website and this physical kiosk)
# ===========================================================================
_release_attempts: dict[str, list[float]] = {}


@app.post("/api/release")
async def release_by_pin(request: Request, _: bool = Depends(require_local)):
    """
    Student keys their 4-digit PIN into the kiosk touchscreen; that releases
    the paid job to the printer.

    Localhost-only, so a PIN cannot be brute-forced from the internet — an
    attacker would have to be standing at the kiosk. Even so, we rate-limit:
    4 digits is only 10,000 combinations, which a fast script on the kiosk's
    own touchscreen could otherwise walk through.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Malformed JSON.")
    pin = str(body.get("pin", "")).strip()
    if not (pin.isdigit() and len(pin) == 4):
        raise HTTPException(400, "Enter the 4-digit code from your phone.")

    # --- throttle: max 10 attempts per 60s window, per kiosk (bypassed in test mode) ---
    window = _release_attempts.setdefault(config.KIOSK_ID, [])
    if not config.PAYMENT_DISABLED:
        now = time.time()
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= 10:
            raise HTTPException(429, "Too many attempts. Please wait a minute.")
        window.append(now)

    with get_session() as s:
        query = s.query(PrintJob).filter(PrintJob.status.in_([JobStatus.AWAITING_RELEASE, JobStatus.REVIEWING]))
        if config.PAYMENT_DISABLED and pin in ("0000", "1234", "4044"):
            # Test-mode magic PINs grab the oldest paid job (FIFO)
            job = query.order_by(PrintJob.paid_at.asc()).first()
        else:
            job = query.filter(PrintJob.release_pin == pin).order_by(PrintJob.paid_at.asc()).first()

        if not job:
            raise HTTPException(404, "No job found for that PIN at this kiosk.")
        job_id = job.id

    # Hardware must still be healthy at the moment of release.
    try:
        fault = cups_manager.blocking_fault(cups_manager.printer_state_reasons())
    except Exception:
        fault = "printer-unreachable"
    if fault:
        raise HTTPException(409, f"Printer fault ({fault.replace('-', ' ')}). Please tell staff.")

    with get_write_session() as s:
        moved = atomic_transition(
            s, job_id, {JobStatus.AWAITING_RELEASE, JobStatus.REVIEWING}, JobStatus.REVIEWING,
            released_at=datetime.now(timezone.utc),
        )
        s.commit()
    if not moved:
        # Someone else released it a fraction of a second ago.
        raise HTTPException(409, "That job has already been released.")

    # Clear the throttle on success (window is always defined above)
    if not config.PAYMENT_DISABLED:
        window.clear()

    await broadcast_job(job_id, "Review your document on the kiosk screen.")
    # Return job info but NEVER the PIN
    with get_session() as s:
        job_row = s.get(PrintJob, job_id)
        job_public = job_row.public() if job_row else {}
    return {"ok": True, "job": job_public}


@app.post("/api/release/approve")
async def release_approve(request: Request, _: bool = Depends(require_local)):
    """The student has reviewed the document on the kiosk and clicked Approve."""
    body = await request.json()
    job_id = body.get("job_id")
    
    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        if not job or job.status not in {JobStatus.REVIEWING, JobStatus.AWAITING_RELEASE}:
            raise HTTPException(404, "Job is not awaiting approval.")
            
        moved = atomic_transition(
            s, job_id, {JobStatus.REVIEWING, JobStatus.AWAITING_RELEASE}, JobStatus.PAID
        )
        s.commit()
        
    if moved:
        asyncio.create_task(run_print_job(job_id))
    return {"ok": True}


@app.post("/api/release/cancel")
async def release_cancel(request: Request, _: bool = Depends(require_local)):
    """The student rejected the preview. The job goes back to AWAITING_RELEASE."""
    body = await request.json()
    job_id = body.get("job_id")
    
    with get_write_session() as s:
        moved = atomic_transition(
            s, job_id, {JobStatus.REVIEWING}, JobStatus.AWAITING_RELEASE
        )
        s.commit()
        
    if moved:
        await broadcast_job(job_id, "Preview cancelled. You can enter your code again to print.")
    return {"ok": True}


@app.get("/api/kiosk/job/{job_id}/pdf")
async def kiosk_get_pdf(job_id: str, _: bool = Depends(require_local)):
    """Serve the PDF specifically for the kiosk preview."""
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job or job.status not in {JobStatus.REVIEWING, JobStatus.AWAITING_RELEASE, JobStatus.PRINTING, JobStatus.PAID, JobStatus.SPOOLING}:
            raise HTTPException(404, "Not available")
        if not os.path.exists(job.print_path):
            raise HTTPException(404, "File missing")
    return FileResponse(job.print_path, media_type="application/pdf")


# ===========================================================================
# Print dispatch + watchdog
# ===========================================================================
async def run_print_job(job_id: str):
    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        if not job or job.status != JobStatus.PAID:
            s.rollback()
            return
        job.status = JobStatus.SPOOLING
        s.commit()
        path, sheets_total = job.print_path, job.sheets_total
        title = f"kiosk-{job_id[:8]}"
        opts = {
            "copies": job.copies,
            "duplex": job.duplex,
            "paper_size": job.paper_size,
            "orientation": job.orientation,
            "pages_per_sheet": job.pages_per_sheet,
        }

    await broadcast_job(job_id, "Released — spooling to printer…")

    try:
        cups_job_id = await asyncio.to_thread(
            cups_manager.submit, path, title, opts, sheets_total
        )
    except Exception as e:
        await fail_and_refund(job_id, f"Could not reach the printer: {e}")
        return

    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        job.cups_job_id = cups_job_id
        job.status = JobStatus.PRINTING
        s.commit()
    await broadcast_job(job_id, "Printing started.")

    await watchdog(job_id, cups_job_id, sheets_total)


async def watchdog(job_id: str, cups_job_id: int, sheets_total: int):
    """
    Polls CUPS once a second until the job leaves the queue.

    On Windows, the spooler transfers data to the printer's RAM buffer in
    milliseconds, then the print job disappears from the queue.  Physical
    printing still takes time (Brother DCP-L2520D: ~8.5s first page,
    ~2.3s/page after that).  We enforce a minimum elapsed time before
    declaring "completed" to stop premature file purges.

    Two independent failure timers:
      * jam detected  -> PAUSED_ERROR, student is told to clear it
      * no progress for FREEZE_TIMEOUT_SECONDS (180s default) -> purge + refund
    """
    import os as _os
    print_start = time.time()
    # Brother DCP-L2520D: 8.5s warmup + 2.3s per additional sheet.
    # Add a 5s safety buffer on top.
    if _os.name == 'nt':
        min_print_seconds = max(20, 8.5 + max(0, sheets_total - 1) * 2.5 + 5)
        print(f"[watchdog] Windows min print time: {min_print_seconds:.0f}s for {sheets_total} sheet(s)")
    else:
        min_print_seconds = 0

    last_progress_at = time.time()
    last_sheets = -1

    while True:
        await asyncio.sleep(config.WATCHDOG_POLL_SECONDS)

        try:
            info = await asyncio.to_thread(cups_manager.job_status, cups_job_id)
        except Exception as e:
            await fail_and_refund(job_id, f"Lost contact with the printer: {e}")
            return

        sheets_done = info.get("sheets_done", 0) or 0
        state = info.get("state")
        jam = cups_manager.is_jam(info.get("reasons") or [])

        if sheets_done != last_sheets:
            last_sheets = sheets_done
            last_progress_at = time.time()

        with get_write_session() as s:
            job = s.get(PrintJob, job_id)
            if not job or job.status in TERMINAL_STATES:
                s.rollback()
                return
            job.sheets_printed = sheets_done
            current = job.status
            s.commit()

        # --- terminal outcomes ---
        if state in ("completed", "canceled"):
            # On Windows: the spooler/sim may declare completion before the
            # printer has physically finished.  Wait out the minimum time.
            if _os.name == 'nt':
                elapsed = time.time() - print_start
                remaining = min_print_seconds - elapsed
                if remaining > 0:
                    print(f"[watchdog] Holding {remaining:.1f}s more for physical print to finish…")
                    await broadcast_job(job_id, "Printing… please wait.")
                    await asyncio.sleep(remaining)

            with get_write_session() as s:
                atomic_transition(s, job_id, {JobStatus.PRINTING, JobStatus.PAUSED_ERROR},
                                  JobStatus.COMPLETED, sheets_printed=sheets_total,
                                  error_reason=None)
                s.commit()
            await broadcast_job(job_id, "Print complete — please collect your document.")
            _purge_job_files(job_id)
            return

        # --- freeze timeout (covers jams AND silent wedges) ---
        stalled_for = time.time() - last_progress_at
        if stalled_for >= config.FREEZE_TIMEOUT_SECONDS:
            try:
                await asyncio.to_thread(cups_manager.cancel, cups_job_id)
            except Exception:
                pass
            await fail_and_refund(
                job_id,
                f"Printer stopped responding for {int(stalled_for)}s.",
                sheets_done=sheets_done,
            )
            return

        # --- jam state transitions ---
        if jam:
            if current != JobStatus.PAUSED_ERROR:
                with get_write_session() as s:
                    atomic_transition(s, job_id, {JobStatus.PRINTING},
                                      JobStatus.PAUSED_ERROR, error_reason=jam)
                    s.commit()
                await broadcast_job(job_id, f"⚠️ {jam.replace('-', ' ')} — please clear the printer.")
            else:
                remaining = max(0, int(config.FREEZE_TIMEOUT_SECONDS - stalled_for))
                await broadcast_job(job_id, f"⚠️ Still stopped. Auto-refund in {remaining}s.")
            continue

        if current == JobStatus.PAUSED_ERROR:
            with get_write_session() as s:
                atomic_transition(s, job_id, {JobStatus.PAUSED_ERROR},
                                  JobStatus.PRINTING, error_reason=None)
                s.commit()
            await broadcast_job(job_id, "✅ Printer resumed — continuing your job.")

        if sheets_done > 0:
            await broadcast_job(job_id, f"Printing… {sheets_done} of {sheets_total} sheet(s) done.")
        else:
            await broadcast_job(job_id, "Printing… please wait.")


async def fail_and_refund(job_id: str, reason: str, sheets_done: int | None = None,
                          from_states: set | None = None):
    """
    Refund the UNPRINTED portion and close the job out.

    Ordering matters: we call the gateway BEFORE writing ABORTED_REFUNDED, so
    a gateway failure is recorded as refund_error on the row instead of being
    silently swallowed behind a status that claims the money went back.
    """
    allowed = from_states or {
        JobStatus.PAID, JobStatus.SPOOLING, JobStatus.PRINTING, JobStatus.PAUSED_ERROR,
    }
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job or job.status in TERMINAL_STATES:
            return
        printed = sheets_done if sheets_done is not None else job.sheets_printed
        sheets_total = max(1, job.sheets_total)
        unprinted_fraction = max(0.0, (sheets_total - printed) / sheets_total)
        refund_amount = round(job.total_price * unprinted_fraction, 2)
        payment_ref = job.payment_ref

    refund_ref, refund_error = None, None
    if refund_amount > 0 and payment_ref:
        refund_ref, refund_error = await asyncio.to_thread(
            payments.refund_payment, payment_ref, refund_amount
        )

    with get_write_session() as s:
        atomic_transition(
            s, job_id,
            allowed,
            JobStatus.ABORTED_REFUNDED if refund_error is None else JobStatus.FAILED,
            error_reason=reason,
            refund_amount=refund_amount,
            refund_ref=refund_ref,
            refund_error=refund_error,
        )
        s.commit()

    if refund_error:
        msg = f"❌ {reason} Refund of ₹{refund_amount:.2f} could NOT be completed automatically — please see kiosk staff."
        print(f"[refund] FAILED for job {job_id}: {refund_error}")
    else:
        msg = f"❌ {reason} ₹{refund_amount:.2f} has been refunded to your UPI account."

    await broadcast_job(job_id, msg)
    _purge_job_files(job_id)


def _purge_job_files(job_id: str):
    # On Windows, do NOT delete files immediately upon job completion.
    # The Windows print spooler and USB driver may still be streaming data
    # to the Brother printer buffer. The background scavenger cleans them up
    # safely after their TTL (5 minutes).
    if os.name == 'nt':
        return

    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        if job:
            _purge_files(job)
        s.commit()


# ===========================================================================
# Status + WebSockets
# ===========================================================================
@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return job.public()


@app.websocket("/ws/print/{job_id}")
async def ws_job(ws: WebSocket, job_id: str):
    await manager.join_job(job_id, ws)
    await broadcast_job(job_id, "Connected.")
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.leave_job(job_id, ws)


@app.websocket("/ws/kiosk")
async def ws_kiosk(ws: WebSocket):
    """Kiosk-screen firehose. Only reachable from the machine itself."""
    client = ws.client.host if ws.client else None
    if client not in ("127.0.0.1", "::1"):
        await ws.close(code=1008)
        return
    await manager.join_kiosk(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.leave_kiosk(ws)


# ===========================================================================
# Admin (localhost only)
# ===========================================================================
@app.get("/api/admin/stats")
def admin_stats(_: bool = Depends(require_local)):
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    with get_session() as s:
        def revenue_since(ts):
            total = s.query(func.coalesce(func.sum(PrintJob.total_price), 0.0)).filter(
                PrintJob.status == JobStatus.COMPLETED, PrintJob.paid_at >= ts
            ).scalar() or 0.0
            refunded = s.query(func.coalesce(func.sum(PrintJob.refund_amount), 0.0)).filter(
                PrintJob.refund_amount.isnot(None), PrintJob.paid_at >= ts
            ).scalar() or 0.0
            return round(total - refunded, 2)

        status_counts = dict(
            s.query(PrintJob.status, func.count(PrintJob.id)).group_by(PrintJob.status).all()
        )
        recent = (s.query(PrintJob).order_by(PrintJob.created_at.desc()).limit(50).all())

        sheets_today = s.query(func.coalesce(func.sum(PrintJob.sheets_printed), 0)).filter(
            PrintJob.paid_at >= day_ago
        ).scalar() or 0

        return {
            "kiosk_id": config.KIOSK_ID,
            "revenue_today": revenue_since(day_ago),
            "revenue_week": revenue_since(week_ago),
            "sheets_today": int(sheets_today),
            "status_counts": {
                (k.value if hasattr(k, "value") else str(k)): v for k, v in status_counts.items()
            },
            "needs_attention": [
                j.admin() for j in recent
                if j.status in (JobStatus.FAILED, JobStatus.FAILED_REBOOT)
                or j.refund_error is not None
            ],
            "recent_jobs": [j.admin() for j in recent],
        }


@app.post("/api/admin/override/clear-queue")
async def admin_clear_queue(_: bool = Depends(require_local)):
    """Force-clear the CUPS queue without opening a terminal.

    Also closes out any job rows still marked active, so the kiosk screen
    doesn't keep showing jobs that no longer exist on the printer.
    """
    cleared = await asyncio.to_thread(cups_manager.purge_queue)
    with get_write_session() as s:
        active = s.query(PrintJob).filter(
            PrintJob.status.in_([JobStatus.SPOOLING, JobStatus.PRINTING, JobStatus.PAUSED_ERROR])
        ).all()
        for job in active:
            job.status = JobStatus.FAILED
            job.error_reason = "Queue cleared manually by staff."
            _purge_files(job)
        s.commit()
        affected = len(active)

    await manager.to_kiosk({"event": "queue_cleared", "cups_jobs": cleared, "rows": affected})
    return {"ok": True, "cups_jobs_cancelled": cleared, "job_rows_closed": affected}


@app.post("/api/admin/override/clear-history")
async def admin_clear_history(_: bool = Depends(require_local)):
    """Wipe all job history and temporary files from the admin dashboard."""
    with get_write_session() as s:
        s.query(PrintJob).delete()
        s.commit()

    if os.path.exists(config.SHM_DIR):
        for fname in os.listdir(config.SHM_DIR):
            fpath = os.path.join(config.SHM_DIR, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass

    await manager.to_kiosk({"event": "queue_cleared"})
    return {"ok": True, "message": "All print history and storage cleared."}


@app.post("/api/admin/override/refund/{job_id}")
async def admin_manual_refund(job_id: str, _: bool = Depends(require_local)):
    """Retry a refund that the gateway rejected, or refund a FAILED_REBOOT job."""
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        if not job.payment_ref:
            raise HTTPException(400, "This job was never paid for.")
        amount = job.refund_amount or job.total_price

    refund_ref, error = await asyncio.to_thread(payments.refund_payment, job.payment_ref, amount)
    with get_write_session() as s:
        job = s.get(PrintJob, job_id)
        job.refund_ref = refund_ref
        job.refund_error = error
        if error is None:
            job.status = JobStatus.ABORTED_REFUNDED
            job.refund_amount = amount
        s.commit()

    if error:
        raise HTTPException(502, f"Refund failed: {error}")
    return {"ok": True, "refund_ref": refund_ref, "amount": amount}


# ===========================================================================
# Debug (simulation mode + localhost only)
# ===========================================================================
@app.post("/api/debug/printer-fault")
def debug_printer_fault(reason: str | None = None, _: bool = Depends(require_local)):
    if not config.SIMULATE:
        raise HTTPException(403, "Only available in simulation mode.")
    cups_manager.simulator().set_printer_fault(reason)
    return {"ok": True, "reason": reason}


@app.post("/api/debug/job-fault/{job_id}")
def debug_job_fault(job_id: str, reason: str | None = "media-jam-error",
                    _: bool = Depends(require_local)):
    if not config.SIMULATE:
        raise HTTPException(403, "Only available in simulation mode.")
    with get_session() as s:
        job = s.get(PrintJob, job_id)
        if not job or job.cups_job_id is None:
            raise HTTPException(404, "Job not dispatched to the printer yet.")
        cups_manager.simulator().set_job_fault(job.cups_job_id, reason)
    return {"ok": True, "reason": reason}


@app.post("/api/debug/simulate-payment/{job_id}")
async def debug_simulate_payment(job_id: str, _: bool = Depends(require_local)):
    """Bypass the gateway in simulation mode so the full flow is testable
    without real money. Still routes through hold-for-release, so the PIN
    step gets exercised too. Localhost + SIMULATE only."""
    if not config.SIMULATE:
        raise HTTPException(403, "Only available in simulation mode.")
    pin = f"{random.randint(0, 9999):04d}"
    with get_write_session() as s:
        moved = atomic_transition(
            s, job_id, {JobStatus.PENDING_PAYMENT}, JobStatus.AWAITING_RELEASE,
            payment_ref=f"sim_{uuid.uuid4().hex[:12]}",
            paid_at=datetime.now(timezone.utc), release_pin=pin,
        )
        s.commit()
    if not moved:
        raise HTTPException(409, "Job is not awaiting payment.")
    await broadcast_job(job_id, "Payment received. Enter your code at the kiosk to print.")
    return {"ok": True, "release_pin": pin}


# ===========================================================================
# Pages
# ===========================================================================
@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/select-location")
def select_location():
    return FileResponse("static/index.html")


@app.get("/api/locations")
def list_locations():
    return {
        "current_kiosk_id": config.KIOSK_ID,
        "locations": [
            {
                "kiosk_id": config.KIOSK_ID,
                "name": "Central Library - Ground Floor",
                "campus_area": "Academic Block A, Near Reading Hall",
                "type": "KIOSK",
                "is_online": True,
                "schedule": "Open 24/7",
                "is_open": True,
                "latitude": 13.0827,
                "longitude": 80.2707,
                "distance_default_meters": 45,
                "printer_model": config.PRINTER_NAME,
                "paper_status": "Paper ready (98%)",
                "pricing": {
                    "bw_simplex": config.PRICE_PER_PAGE,
                    "bw_duplex": round(config.PRICE_PER_PAGE * 1.5, 2),
                    "color_simplex": 8.0,
                    "color_duplex": 14.0,
                    "blank_sheet": 1.0,
                },
                "capabilities": {
                    "black_and_white": True,
                    "color": False,
                    "duplex": True,
                    "blank_sheets": True,
                },
            },
            {
                "kiosk_id": "0002",
                "name": "Engineering Block B - North Lobby",
                "campus_area": "Opposite Computer Labs, 1st Floor",
                "type": "KIOSK",
                "is_online": True,
                "schedule": "Open 07:00 AM - 11:00 PM",
                "is_open": True,
                "latitude": 13.0838,
                "longitude": 80.2721,
                "distance_default_meters": 130,
                "printer_model": "HP LaserJet Pro MFP",
                "paper_status": "Paper ready (85%)",
                "pricing": {
                    "bw_simplex": 2.0,
                    "bw_duplex": 3.0,
                    "color_simplex": 7.0,
                    "color_duplex": 12.0,
                    "blank_sheet": 1.0,
                },
                "capabilities": {
                    "black_and_white": True,
                    "color": True,
                    "duplex": True,
                    "blank_sheets": True,
                },
            },
            {
                "kiosk_id": "0003",
                "name": "Student Activity Center (SAC)",
                "campus_area": "Food Court & Student Lounge Entry",
                "type": "KIOSK",
                "is_online": True,
                "schedule": "Open 24/7",
                "is_open": True,
                "latitude": 13.0812,
                "longitude": 80.2690,
                "distance_default_meters": 260,
                "printer_model": "Brother_DCP_T420",
                "paper_status": "Paper ready (92%)",
                "pricing": {
                    "bw_simplex": 2.0,
                    "bw_duplex": 3.0,
                    "color_simplex": 8.0,
                    "color_duplex": 14.0,
                    "blank_sheet": 1.0,
                },
                "capabilities": {
                    "black_and_white": True,
                    "color": False,
                    "duplex": True,
                    "blank_sheets": True,
                },
            },
            {
                "kiosk_id": "SHOP01",
                "name": "Campus Stationery & Reprographics",
                "campus_area": "Commercial Complex, Shop #4",
                "type": "SHOP",
                "is_online": True,
                "schedule": "Open 08:30 AM - 08:30 PM",
                "is_open": True,
                "latitude": 13.0845,
                "longitude": 80.2685,
                "distance_default_meters": 350,
                "printer_model": "Commercial Canon ImageRunner",
                "paper_status": "Full Services",
                "pricing": {
                    "bw_simplex": 2.0,
                    "bw_duplex": 3.0,
                    "color_simplex": 6.0,
                    "color_duplex": 10.0,
                    "blank_sheet": 1.0,
                },
                "capabilities": {
                    "black_and_white": True,
                    "color": True,
                    "duplex": True,
                    "blank_sheets": True,
                    "spiral_binding": True,
                    "lamination": True,
                },
            },
        ],
    }


@app.get("/admin")
def admin_page(_: bool = Depends(require_local)):
    return FileResponse("static/admin.html")


@app.get("/kiosk")
def kiosk_page(_: bool = Depends(require_local)):
    """The physical kiosk touchscreen. Localhost-only — never published
    through the tunnel, because it shows the live queue."""
    return FileResponse("static/kiosk.html")



@app.get("/api/kiosk/queue")
def kiosk_queue(_: bool = Depends(require_local)):
    """
    Live queue for the kiosk display: jobs waiting on a PIN, plus anything
    currently on the printer.

    Ordering: paid_at ASC (first paid = front of queue). Jobs in REVIEWING /
    SPOOLING / PRINTING / PAUSED_ERROR go first since they are already active,
    then AWAITING_RELEASE sorted by payment timestamp.
    """
    # Active (already being processed) states come before waiting jobs.
    active_states = [JobStatus.REVIEWING, JobStatus.SPOOLING,
                     JobStatus.PRINTING, JobStatus.PAUSED_ERROR]
    waiting_states = [JobStatus.AWAITING_RELEASE]
    all_watch = active_states + waiting_states

    with get_session() as s:
        # Order: active jobs first (by paid_at), then waiting (by paid_at)
        # SQLite doesn't support CASE in order_by natively, so we fetch both
        # groups separately and merge.
        active_jobs = (
            s.query(PrintJob)
            .filter(PrintJob.status.in_(active_states))
            .order_by(PrintJob.paid_at.asc())
            .all()
        )
        waiting_jobs = (
            s.query(PrintJob)
            .filter(PrintJob.status.in_(waiting_states))
            .order_by(PrintJob.paid_at.asc())
            .all()
        )
        jobs = active_jobs + waiting_jobs

        out = []
        for i, j in enumerate(jobs):
            d = j.public()
            # PIN is NEVER sent in queue responses — it stays server-side only
            d.pop("release_pin", None)
            d["queue_position"] = i + 1
            d["is_active"] = j.status in active_states
            d["filename_masked"] = _mask_filename(j.original_filename)
            d["user_name"] = j.user_name or "Student"
            phone = j.user_phone or ""
            if len(phone) >= 10:
                d["user_phone_masked"] = f"+91 {phone[:2]}****{phone[-4:]}"
            elif phone:
                d["user_phone_masked"] = phone
            else:
                d["user_phone_masked"] = ""
            d.pop("filename", None)
            out.append(d)
    return out


def _mask_filename(name: str) -> str:
    """'Semester_Assignment.pdf' -> 'Se…nt.pdf'. Enough for the owner to
    recognise their own job, not enough to expose document titles to the
    whole queue."""
    stem, _, ext = name.rpartition(".")
    stem = stem or name
    if len(stem) <= 4:
        return name
    return f"{stem[:2]}…{stem[-2:]}" + (f".{ext}" if ext else "")


@app.get("/api/kiosk-qr")
def kiosk_qr():
    url = f"{config.PUBLIC_BASE_URL}/?kiosk_id={config.KIOSK_ID}"
    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return {"url": url,
            "qr_code_base64": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "kiosk_id": config.KIOSK_ID, "simulate": config.SIMULATE})
