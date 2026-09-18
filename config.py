"""
config.py — every tunable in one place, read from the environment.

Nothing secret is hardcoded. The systemd unit (deploy/kiosk-backend.service)
is where you set these on the real machine.
"""
import os

# --- Identity -------------------------------------------------------------
KIOSK_ID = os.environ.get("KIOSK_ID", "0004")

# --- Pricing --------------------------------------------------------------
PRICE_PER_PAGE = float(os.environ.get("KIOSK_PRICE_PER_PAGE", "2.0"))
MAX_COPIES = int(os.environ.get("KIOSK_MAX_COPIES", "20"))

# --- File ingestion -------------------------------------------------------
MAX_UPLOAD_BYTES = int(os.environ.get("KIOSK_MAX_UPLOAD_MB", "25")) * 1024 * 1024
# /dev/shm is a RAM-backed tmpfs on Linux: nothing ever touches the physical
# disk, so student documents cannot be recovered from the SSD afterwards.
if os.environ.get("VERCEL"):
    SHM_DIR = "/tmp/kiosk"
    SIMULATE = True
else:
    SHM_DIR = "/dev/shm/kiosk" if os.path.isdir("/dev/shm") else os.path.join(os.getcwd(), "_tmp_kiosk_storage")
    SIMULATE = os.environ.get("KIOSK_SIMULATE", "0") == "1"

PRINTER_NAME = os.environ.get("KIOSK_PRINTER_NAME", "Brother DCP-L2520D series")

# --- Watchdog / lifecycle timings ----------------------------------------
WATCHDOG_POLL_SECONDS = 1.0        # how often we poll CUPS during a print
FREEZE_TIMEOUT_SECONDS = int(os.environ.get("KIOSK_FREEZE_TIMEOUT", "180"))
SCAVENGER_INTERVAL_SECONDS = 300   # sweep /dev/shm every 5 min
ABANDONED_JOB_TTL_SECONDS = 600    # purge unpaid jobs older than 10 min
# Paid but never collected at the kiosk. Long, because a student may pay in
# the morning and collect after class. On expiry the file is destroyed and
# the money is refunded automatically.
UNCOLLECTED_TTL_SECONDS = int(os.environ.get("KIOSK_UNCOLLECTED_TTL", str(12 * 3600)))

# --- Payment gateway ------------------------------------------------------
# Set to True while testing without a live payment gateway.
PAYMENT_DISABLED = os.environ.get("KIOSK_PAYMENT_DISABLED", "1") == "1"
WEBHOOK_SECRET = os.environ.get("KIOSK_WEBHOOK_SECRET", "")
GATEWAY_API_BASE = os.environ.get("KIOSK_GATEWAY_API_BASE", "https://api.razorpay.com")
GATEWAY_KEY_ID = os.environ.get("KIOSK_GATEWAY_KEY_ID", "")
GATEWAY_KEY_SECRET = os.environ.get("KIOSK_GATEWAY_KEY_SECRET", "")
UPI_VPA = os.environ.get("KIOSK_UPI_VPA", "campus-kiosk@upi")
# Reject webhooks whose signed timestamp is older than this (replay defense).
WEBHOOK_MAX_AGE_SECONDS = 300

# --- Public URL (what the kiosk QR code encodes) -------------------------
PUBLIC_BASE_URL = os.environ.get("KIOSK_PUBLIC_BASE_URL", "https://heath-ping-richards-plains.trycloudflare.com")

# --- Admin ----------------------------------------------------------------
# Admin routes are localhost-only by default (see require_local in main.py).
# This token adds a second factor in case you ever proxy them.
ADMIN_TOKEN = os.environ.get("KIOSK_ADMIN_TOKEN", "")

# --- CORS -----------------------------------------------------------------
# Once the tunnel is live, set this to your real domain. Wildcard is only
# acceptable while developing on a closed LAN.
ALLOWED_ORIGINS = [o for o in os.environ.get("KIOSK_ALLOWED_ORIGINS", "*").split(",") if o]
