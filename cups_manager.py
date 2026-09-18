"""
cups_manager.py — all printer I/O lives here, so nothing else touches pycups.

Two backends selected by config.SIMULATE:

  SIMULATE=True  — an in-memory fake printer. Lets you run and demo the whole
                   system (including jams, freezes, refunds) on any machine
                   with no hardware and no CUPS. Fault injection endpoints in
                   main.py drive it.

  SIMULATE=False — real pycups against the local CUPS daemon. Needs
                   `sudo apt install libcups2-dev` before `pip install pycups`,
                   and the printer already added to CUPS.

The rest of the app is identical in both modes.
"""

import itertools
import threading
import time

import config

# printer-state-reasons that must block a student from paying (pre-flight).
BLOCKING_PREFIXES = (
    "media-empty", "media-jam", "media-needed",
    "marker-supply-empty", "cover-open", "door-open",
    "offline-report", "toner-empty", "output-area-full",
    "printer-unreachable", "user-intervention-needed",
    "printer-out-of-memory", "printer-paused",
)

# job-state-reasons that mean the job has stalled mid-print.
JAM_PREFIXES = (
    "media-empty", "media-jam", "media-needed",
    "marker-supply-empty", "cover-open", "door-open",
    "offline-report", "toner-empty", "output-area-full",
    "printer-unreachable", "user-intervention-needed",
    "printer-out-of-memory", "printer-paused",
)

_job_counter = itertools.count(1000)


def _matches(reason: str, prefixes) -> bool:
    return any(reason.startswith(p) for p in prefixes)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
class _Simulator:
    def __init__(self):
        self.lock = threading.Lock()
        self.printer_reasons: list[str] = []
        self.jobs: dict[int, dict] = {}

    def set_printer_fault(self, reason):
        with self.lock:
            self.printer_reasons = [reason] if reason else []

    def set_job_fault(self, job_id, reason):
        with self.lock:
            j = self.jobs.get(job_id)
            if j:
                j["reasons"] = [reason] if reason else []

    def start(self, sheets_total) -> int:
        jid = next(_job_counter)
        with self.lock:
            self.jobs[jid] = {"state": "processing", "reasons": [],
                              "sheets_total": sheets_total, "sheets_done": 0,
                              "started": time.time()}
        return jid

    def tick(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j["state"] in ("completed", "canceled"):
                return
            if j["reasons"]:
                return                      # stalled
            if j["sheets_done"] < j["sheets_total"]:
                j["sheets_done"] += 1
            if j["sheets_done"] >= j["sheets_total"]:
                j["state"] = "completed"

    def get(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def cancel(self, jid):
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid]["state"] = "canceled"

    def cancel_all(self) -> int:
        with self.lock:
            n = 0
            for j in self.jobs.values():
                if j["state"] == "processing":
                    j["state"] = "canceled"
                    n += 1
            return n


_sim = _Simulator()


def simulator() -> _Simulator:
    return _sim


# ---------------------------------------------------------------------------
# Windows spooler job tracking
# Maps sim_id -> Windows spooler job ID (or None if not tracked)
# ---------------------------------------------------------------------------
_windows_print_jobs: dict[int, int | None] = {}  # sim_id → Win32 job id
_win_job_meta: dict[int, dict] = {}             # sim_id → {sheets_total, start_time, win_job_id}
_wpj_lock = threading.Lock()


def _snapshot_spooler_jobs(printer_name: str) -> set:
    """Return the set of current Windows spooler job IDs for the printer."""
    try:
        import win32print
        h = win32print.OpenPrinter(printer_name)
        try:
            jobs = win32print.EnumJobs(h, 0, -1, 1)
            return {j["JobId"] for j in jobs}
        finally:
            win32print.ClosePrinter(h)
    except Exception:
        return set()


def _find_new_spooler_job(jobs_before: set, printer_name: str, retries: int = 8) -> int | None:
    """
    Poll the spooler up to `retries` times (0.5s apart) to find a job
    that wasn't present before we called SumatraPDF.
    Returns the Windows job ID or None if we couldn’t identify it.
    """
    for _ in range(retries):
        time.sleep(0.5)
        jobs_now = _snapshot_spooler_jobs(printer_name)
        new = jobs_now - jobs_before
        if new:
            return next(iter(new))
    # Job may have already completed (very short doc)
    return None


def _get_win_job_info(win_job_id: int, printer_name: str) -> dict:
    """
    Query a specific Windows spooler job.
    Returns {found, pages_printed, total_pages, status_bits, status_text}.
    """
    try:
        import win32print
        h = win32print.OpenPrinter(printer_name)
        try:
            jobs = win32print.EnumJobs(h, 0, -1, 2)
        finally:
            win32print.ClosePrinter(h)
        for j in jobs:
            if j.get("JobId") == win_job_id:
                return {
                    "found": True,
                    "pages_printed": j.get("PagesPrinted", 0) or 0,
                    "total_pages": j.get("TotalPages", 0) or 0,
                    "status_bits": j.get("Status", 0) or 0,
                    "status_text": (j.get("pStatus") or "").lower(),
                }
        return {"found": False}   # job gone → completed
    except Exception:
        return {"found": False}


# ---------------------------------------------------------------------------
# Real CUPS
# ---------------------------------------------------------------------------
_conn = None
_conn_lock = threading.Lock()


def _cups():
    global _conn
    with _conn_lock:
        if _conn is None:
            import cups                       # imported lazily so dev boxes work
            _conn = cups.Connection()
        return _conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
import os

def _print_windows_native(file_path: str, printer_name: str, title: str, opts: dict):
    """
    Spools PDF directly to the physical Windows printer (Brother DCP-L2520D series).
    Uses the embedded silent SumatraPDF engine with support for duplex, copies,
    paper size, and orientation.

    CREATE_NO_WINDOW is critical — without it, SumatraPDF may open a hidden
    dialog box waiting for input (e.g. "printer not ready") when run from a
    background service/thread, blocking indefinitely.
    """
    import subprocess

    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Print file not found: {abs_path}")

    # Verify printer is reachable; fall back to default if the configured
    # name is not found (e.g. driver was renamed).
    try:
        import win32print
        hprinter = win32print.OpenPrinter(printer_name)
        win32print.ClosePrinter(hprinter)
        print(f"[print] Printer '{printer_name}' opened OK")
    except Exception as e:
        print(f"[print] Warning: could not open '{printer_name}': {e}")
        try:
            import win32print
            printer_name = win32print.GetDefaultPrinter()
            print(f"[print] Falling back to default printer: '{printer_name}'")
        except Exception:
            pass

    # --- Build SumatraPDF -print-settings string ---
    # Brother DCP-L2520D is a monochrome GDI laser printer with 32MB RAM.
    # 1. "monochrome" is MANDATORY so SumatraPDF does NOT generate 25MB+ 24-bit RGB raster data.
    # 2. "fit" scales the document cleanly to printable margins.
    # 3. DO NOT include "portrait" (SumatraPDF's "portrait" flag forces a 90° content rotation).
    # 4. DO NOT include "paper=A4" (printer driver default is already A4; overriding creates geometry conflict).
    settings_parts = ["fit", "monochrome"]

    copies = int(opts.get("copies", 1) or 1)
    if copies > 1:
        settings_parts.append(f"{copies}x")

    if opts.get("duplex"):
        settings_parts.append("duplex")
    else:
        settings_parts.append("simplex")

    if opts.get("orientation") == "landscape":
        settings_parts.append("landscape")

    print_settings = ",".join(settings_parts)

    # For simplex prints, use native Windows GDI directly with contrast & darkness boost.
    # For duplex prints, use SumatraPDF with duplex print settings.
    if not opts.get("duplex"):
        print(f"[print] Using native Windows GDI with toner darkness boost for '{printer_name}'")
        return _print_windows_gdi(abs_path, printer_name, title, opts=opts)

    sumatra_exe = os.path.abspath("bin/SumatraPDF.exe")
    if os.path.exists(sumatra_exe):
        cmd = [
            sumatra_exe,
            "-silent",
            "-print-to", printer_name,
            "-print-settings", print_settings,
            "-exit-on-print",
            abs_path,
        ]
        print(f"[print] CMD: {' '.join(cmd)}")

        creation_flags = 0
        if os.name == 'nt':
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=creation_flags,
            )
            if res.stdout:
                for line in res.stdout.strip().splitlines():
                    print(f"[sumatra] {line}")
            if res.stderr:
                for line in res.stderr.strip().splitlines():
                    print(f"[sumatra:err] {line}")

            if res.returncode == 0:
                print(f"[print] SumatraPDF finished OK (code 0) — job spooled to '{printer_name}'")
                return True
            else:
                print(f"[print] SumatraPDF returned {res.returncode}, falling back to native GDI")
        except Exception as e:
            print(f"[print] SumatraPDF execution failed ({e}), falling back to native GDI")

    # Native GDI fallback (handles any PDF directly using Windows GDI print processor)
    return _print_windows_gdi(abs_path, printer_name, title, opts=opts)


def _print_windows_gdi(abs_path: str, printer_name: str, title: str, opts: dict = None) -> bool:
    """
    Renders PDF pages directly to the Windows printer DC via PyMuPDF + GDI.
    Uses 300 DPI grayscale (csGRAY / mode L) to stay well under the Brother
    laser printer's 32MB buffer limit. Supports copies, duplex DEVMODE, and landscape/portrait orientation.
    """
    print(f"[print] Rendering via native Windows GDI to '{printer_name}'")
    opts = opts or {}
    copies = max(1, int(opts.get("copies", 1) or 1))
    user_orient = opts.get("orientation", "portrait")
    is_duplex = bool(opts.get("duplex", False))

    import pymupdf
    import win32ui
    import win32gui
    import win32print
    import win32con
    from PIL import Image, ImageWin, ImageEnhance

    hprinter = None
    hdc = None
    try:
        try:
            hprinter = win32print.OpenPrinter(printer_name)
            devmode = win32print.GetPrinter(hprinter, 2).get('pDevMode')
            if devmode:
                devmode.Duplex = win32con.DMDUP_VERTICAL if is_duplex else win32con.DMDUP_SIMPLEX
                devmode.Orientation = win32con.DMORIENT_LANDSCAPE if user_orient == "landscape" else win32con.DMORIENT_PORTRAIT
                devmode.PaperSize = win32con.DMPAPER_A4
                hdc_handle = win32gui.CreateDC("WINSPOOL", printer_name, devmode)
                hdc = win32ui.CreateDCFromHandle(hdc_handle)
        except Exception as dm_err:
            print(f"[print] DevMode initialization failed ({dm_err}), using default DC")
            hdc = None
        finally:
            if hprinter:
                try:
                    win32print.ClosePrinter(hprinter)
                except Exception:
                    pass

        if not hdc:
            hdc = win32ui.CreateDC()
            hdc.CreatePrinterDC(printer_name)

        pw = hdc.GetDeviceCaps(win32con.HORZRES)
        ph = hdc.GetDeviceCaps(win32con.VERTRES)
        doc = pymupdf.open(abs_path)
        contrast_enhancer = 1.4
        dark_table = [min(255, int((i / 255.0) ** 1.35 * 255)) for i in range(256)]

        hdc.StartDoc(title)
        try:
            for copy_idx in range(copies):
                for page in doc:
                    hdc.StartPage()
                    # 300 DPI grayscale (csGRAY) = 1 byte per pixel, crisp and lightweight
                    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
                    img = Image.frombytes("L", [pix.width, pix.height], pix.samples)

                    # --- Darkness & Contrast Boost for laser toner ---
                    img = ImageEnhance.Contrast(img).enhance(contrast_enhancer)
                    img = img.point(dark_table)

                    # --- Auto-Orientation (Landscape vs Portrait) Handling ---
                    img_w, img_h = img.size
                    if user_orient == "landscape" or (user_orient != "portrait" and img_w > img_h):
                        # Brother feeds A4 short-edge first (pw < ph).
                        # Rotating 90° maps wide landscape content along the 297mm height of the A4 paper.
                        if pw < ph:
                            img = img.transpose(Image.Transpose.ROTATE_90)
                            img_w, img_h = img.size
                    elif user_orient == "portrait":
                        # User specifically requested portrait.
                        # If page is portrait, it stays portrait. If page is landscape, it fits horizontally.
                        pass

                    dib = ImageWin.Dib(img)
                    scale = min(pw / img_w, ph / img_h)
                    dest_w = int(img_w * scale)
                    dest_h = int(img_h * scale)
                    offset_x = (pw - dest_w) // 2
                    offset_y = (ph - dest_h) // 2
                    dib.draw(hdc.GetHandleOutput(),
                             (offset_x, offset_y, offset_x + dest_w, offset_y + dest_h))
                    hdc.EndPage()
        finally:
            hdc.EndDoc()
    finally:
        if hdc:
            del hdc
    print(f"[print] Native GDI render complete for '{printer_name}' ({copies} copies, orient={user_orient})")
    return True



def printer_state_reasons() -> list[str]:
    if config.SIMULATE:
        return list(_sim.printer_reasons)
    if os.name == 'nt':
        reasons = []
        hprinter = None
        target_printer = config.PRINTER_NAME
        try:
            import win32print
            # 1. Attempt to connect to the printer handle
            try:
                hprinter = win32print.OpenPrinter(target_printer)
            except Exception:
                # Fallback to default printer if configured name is not found
                try:
                    target_printer = win32print.GetDefaultPrinter()
                    hprinter = win32print.OpenPrinter(target_printer)
                except Exception:
                    return ["printer-unreachable", "offline-report"]

            info = win32print.GetPrinter(hprinter, 2)
            attributes = info.get("Attributes", 0)
            status = info.get("Status", 0)

            # Check if printer is set to Work Offline (0x400 = PRINTER_ATTRIBUTE_WORK_OFFLINE)
            if attributes & 0x00000400:
                reasons.append("offline-report")

            # Check printer status flags
            if status & 0x00000008:  # PRINTER_STATUS_PAPER_JAM
                reasons.append("media-jam")
            if status & 0x00000010:  # PRINTER_STATUS_PAPER_OUT
                reasons.append("media-empty")
            if status & 0x00000040:  # PRINTER_STATUS_PAPER_PROBLEM
                reasons.append("media-needed")
            if status & 0x00400000:  # PRINTER_STATUS_DOOR_OPEN
                reasons.append("door-open")
            if status & 0x00040000:  # PRINTER_STATUS_NO_TONER
                reasons.append("toner-empty")
            if status & 0x00020000:  # PRINTER_STATUS_TONER_LOW
                reasons.append("marker-supply-low")
            if status & 0x00000800:  # PRINTER_STATUS_OUTPUT_BIN_FULL
                reasons.append("output-area-full")
            if status & 0x00000080:  # PRINTER_STATUS_OFFLINE
                reasons.append("offline-report")
            if status & 0x00001000:  # PRINTER_STATUS_NOT_AVAILABLE
                reasons.append("offline-report")
            if status & 0x00000002:  # PRINTER_STATUS_ERROR
                reasons.append("offline-report")
            if status & 0x00000001:  # PRINTER_STATUS_PAUSED
                reasons.append("printer-paused")
            if status & 0x00100000:  # PRINTER_STATUS_USER_INTERVENTION
                reasons.append("user-intervention-needed")
            if status & 0x00200000:  # PRINTER_STATUS_OUT_OF_MEMORY
                reasons.append("printer-out-of-memory")

            # 2. Check active print jobs in Windows spooler for hardware-level stalls
            try:
                jobs = win32print.EnumJobs(hprinter, 0, -1, 2)
                for job in jobs:
                    j_status = job.get("Status", 0)
                    if j_status & 0x00000040:  # JOB_STATUS_PAPEROUT
                        reasons.append("media-empty")
                    if j_status & 0x00000002:  # JOB_STATUS_ERROR
                        reasons.append("offline-report")
                    if j_status & 0x00000020:  # JOB_STATUS_OFFLINE
                        reasons.append("offline-report")
                    if j_status & 0x00000001:  # JOB_STATUS_PAUSED
                        reasons.append("printer-paused")
                    if j_status & 0x00000400:  # JOB_STATUS_USER_INTERVENTION
                        reasons.append("user-intervention-needed")
                    if j_status & 0x00000200:  # JOB_STATUS_BLOCKED_DEVQ
                        reasons.append("media-jam")

                    # Check textual status string provided by printer driver
                    p_status = (job.get("pStatus") or "").lower()
                    if "jam" in p_status:
                        reasons.append("media-jam")
                    elif "paper" in p_status and ("out" in p_status or "empty" in p_status or "load" in p_status):
                        reasons.append("media-empty")
                    elif "door" in p_status or "cover" in p_status:
                        reasons.append("door-open")
                    elif "toner" in p_status:
                        reasons.append("toner-empty")
                    elif "offline" in p_status:
                        reasons.append("offline-report")
                    elif "error" in p_status:
                        reasons.append("offline-report")
            except Exception:
                pass

        except Exception:
            return ["printer-unreachable", "offline-report"]
        finally:
            if hprinter:
                try:
                    win32print.ClosePrinter(hprinter)
                except Exception:
                    pass

        # 3. WMI check for deep bidirectional USB driver state
        if not reasons:
            try:
                import win32com.client
                wmi = win32com.client.GetObject("winmgmts:")
                wmi_printers = wmi.ExecQuery(f"Select * from Win32_Printer Where Name = '{target_printer}'")
                for wp in wmi_printers:
                    if getattr(wp, "WorkOffline", False):
                        reasons.append("offline-report")
                    err_state = getattr(wp, "DetectedErrorState", 0) or 0
                    if err_state in (3, 4):
                        reasons.append("media-empty")
                    elif err_state == 8:
                        reasons.append("media-jam")
                    elif err_state == 7:
                        reasons.append("door-open")
                    elif err_state in (5, 6):
                        reasons.append("toner-empty")
                    elif err_state in (9, 10):
                        reasons.append("offline-report")
                    elif err_state == 11:
                        reasons.append("output-area-full")

                    ext_status = getattr(wp, "ExtendedPrinterStatus", 0) or 0
                    if ext_status in (7, 8, 9, 11):
                        reasons.append("offline-report")
            except Exception:
                pass

        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                deduped.append(r)

        return deduped or list(_sim.printer_reasons)

    try:
        attrs = _cups().getPrinterAttributes(config.PRINTER_NAME)
        reasons = attrs.get("printer-state-reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        return [r for r in reasons if r != "none"]
    except Exception:
        return []


def blocking_fault(reasons: list[str]) -> str | None:
    """Returns the first reason that should block payment, else None."""
    for r in reasons:
        if _matches(r, BLOCKING_PREFIXES):
            return r
    return None


def submit(file_path: str, title: str, opts: dict, sheets_total: int) -> int:
    """
    Hand the sliced PDF to CUPS or Windows Spooler. `opts` carries the student's choices.
    Returns a job id used by job_status() and cancel().
    """
    if config.SIMULATE:
        return _sim.start(sheets_total)

    if os.name == 'nt':
        # Snapshot spooler BEFORE sending so we can identify our new job.
        jobs_before = _snapshot_spooler_jobs(config.PRINTER_NAME)

        # Send to physical printer via SumatraPDF with native GDI fallback.
        _print_windows_native(file_path, config.PRINTER_NAME, title, opts)

        # Use simulator id as unique handle
        sim_id = _sim.start(sheets_total)

        win_job_id = _find_new_spooler_job(jobs_before, config.PRINTER_NAME)
        with _wpj_lock:
            _windows_print_jobs[sim_id] = win_job_id
            _win_job_meta[sim_id] = {
                "sheets_total": sheets_total,
                "start_time": time.time(),
                "win_job_id": win_job_id,
            }

        if win_job_id:
            print(f"[print] Tracking Windows spooler job #{win_job_id} (sim={sim_id}, sheets={sheets_total})")
        else:
            print(f"[print] Spooler job transferred to printer RAM (sim={sim_id}, sheets={sheets_total})")

        return sim_id

    cups_opts = {
        "copies": str(opts.get("copies", 1)),
        "media": opts.get("paper_size", "A4"),
        "print-color-mode": "monochrome",
        "ColorModel": "Gray",
        "sides": "two-sided-long-edge" if opts.get("duplex") else "one-sided",
        "orientation-requested": "4" if opts.get("orientation") == "landscape" else "3",
    }

    nup = int(opts.get("pages_per_sheet", 1) or 1)
    if nup > 1:
        cups_opts["number-up"] = str(nup)

    return _cups().printFile(config.PRINTER_NAME, file_path, title, cups_opts)


def job_status(cups_job_id: int) -> dict:
    """Normalized: {state, reasons, sheets_done, sheets_total}."""

    if config.SIMULATE:
        _sim.tick(cups_job_id)
        j = _sim.get(cups_job_id)
        if not j:
            return {"state": "completed", "reasons": [], "sheets_done": 0, "sheets_total": 0}
        return {"state": j["state"], "reasons": [],
                "sheets_done": j["sheets_done"], "sheets_total": j["sheets_total"]}

    if os.name == 'nt':
        with _wpj_lock:
            win_job_id = _windows_print_jobs.get(cups_job_id)
            meta = _win_job_meta.get(cups_job_id)

        reasons = []

        # Check Windows spooler for hardware fault bits while job is visible
        if win_job_id and win_job_id != -1:
            info = _get_win_job_info(win_job_id, config.PRINTER_NAME)
            if not info["found"]:
                with _wpj_lock:
                    _windows_print_jobs[cups_job_id] = -1  # Spooled to printer RAM
            else:
                status = info["status_bits"]
                p_status = info["status_text"]
                if status & 0x00000008 or "jam" in p_status:
                    reasons.append("media-jam")
                if status & 0x00000040 or ("paper" in p_status and ("out" in p_status or "empty" in p_status)):
                    reasons.append("media-empty")
                if status & 0x00000080:
                    reasons.append("offline-report")

        # Physical feed progress model:
        # Brother DCP-L2520D warmup & paper grab takes ~8.5s, then ~2.5s per sheet, + 3.0s landing in tray.
        if meta:
            real_total = meta["sheets_total"]
            elapsed = time.time() - meta["start_time"]
            WARMUP_S = 8.5
            PER_PAGE_S = 2.5
            total_physical_time = WARMUP_S + max(0, real_total - 1) * PER_PAGE_S + 3.0

            if elapsed < WARMUP_S:
                sheets_done = 0
                state = "processing"
            elif elapsed >= total_physical_time:
                sheets_done = real_total
                state = "completed"
            else:
                sheets_done = min(real_total, 1 + int((elapsed - WARMUP_S) / PER_PAGE_S))
                state = "processing"

            return {
                "state": state,
                "reasons": reasons,
                "sheets_done": sheets_done,
                "sheets_total": real_total,
            }

        return {"state": "completed", "reasons": [], "sheets_done": 0, "sheets_total": 0}


    # --- Linux/macOS — real CUPS ---
    attrs = _cups().getJobAttributes(cups_job_id)
    state_map = {3: "processing", 4: "processing", 5: "processing",
                 6: "stopped", 7: "canceled", 8: "canceled", 9: "completed"}
    reasons = attrs.get("job-state-reasons", [])
    if isinstance(reasons, str):
        reasons = [reasons]
    return {
        "state": state_map.get(attrs.get("job-state", 9), "processing"),
        "reasons": [r for r in reasons if r != "none"],
        "sheets_done": attrs.get("job-media-sheets-completed", 0) or 0,
        "sheets_total": attrs.get("job-media-sheets", 0) or 0,
    }


def is_jam(reasons: list[str]) -> str | None:
    for r in reasons:
        if _matches(r, JAM_PREFIXES):
            return r
    return None


def cancel(cups_job_id: int):
    if config.SIMULATE:
        _sim.cancel(cups_job_id)
        return
    if os.name == 'nt':
        with _wpj_lock:
            win_job_id = _windows_print_jobs.get(cups_job_id)
        if win_job_id and win_job_id != -1:
            try:
                import win32print
                h = win32print.OpenPrinter(config.PRINTER_NAME)
                try:
                    win32print.SetJob(h, win_job_id, 0, None, win32print.JOB_CONTROL_DELETE)
                finally:
                    win32print.ClosePrinter(h)
            except Exception:
                pass
        return
    _cups().cancelJob(cups_job_id)


def purge_queue() -> int:
    """Admin override: clear every job off the printer. Returns count."""
    if config.SIMULATE:
        return _sim.cancel_all()
    if os.name == 'nt':
        cleared = 0
        try:
            import win32print
            h = win32print.OpenPrinter(config.PRINTER_NAME)
            try:
                jobs = win32print.EnumJobs(h, 0, -1, 1)
                for j in jobs:
                    try:
                        win32print.SetJob(h, j["JobId"], 0, None, win32print.JOB_CONTROL_DELETE)
                        cleared += 1
                    except Exception:
                        pass
            finally:
                win32print.ClosePrinter(h)
        except Exception:
            pass
        return cleared
    conn = _cups()
    jobs = conn.getJobs()
    for jid in jobs:
        try:
            conn.cancelJob(jid)
        except Exception:
            pass
    return len(jobs)

