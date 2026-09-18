"""
Direct SumatraPDF print test — run this from the KIOSKKK directory.
It creates a minimal PDF and tries to print it, logging every step.
"""
import os
import subprocess
import sys
import time

PRINTER_NAME = "Brother DCP-L2520D series"
SUMATRA = os.path.abspath("bin/SumatraPDF.exe")
TEST_PDF = os.path.abspath("_tmp_kiosk_storage/test_direct_print.pdf")

def create_test_pdf():
    """Create a tiny valid PDF without any external library."""
    # Minimal single-page PDF (pure text)
    pdf = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]
/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 44>>
stream
BT /F1 24 Tf 200 700 Td (KIOSK TEST PRINT) Tj ET
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
0000000360 00000 n
trailer<</Size 6/Root 1 0 R>>
startxref
441
%%EOF"""
    os.makedirs(os.path.dirname(TEST_PDF), exist_ok=True)
    with open(TEST_PDF, "wb") as f:
        f.write(pdf)
    print(f"[OK] Test PDF created: {TEST_PDF}")

def check_printer():
    try:
        import win32print
        printers = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
        print(f"[INFO] Installed printers: {printers}")
        if PRINTER_NAME in printers:
            print(f"[OK] '{PRINTER_NAME}' is installed")
            h = win32print.OpenPrinter(PRINTER_NAME)
            info = win32print.GetPrinter(h, 2)
            win32print.ClosePrinter(h)
            print(f"[INFO] Printer status bits: {info.get('Status', 0):#010x}")
            print(f"[INFO] Printer attributes: {info.get('Attributes', 0):#010x}")
        else:
            print(f"[ERROR] '{PRINTER_NAME}' NOT found in printer list!")
            print(f"[HINT] Available printers: {printers}")
    except Exception as e:
        print(f"[ERROR] win32print check failed: {e}")

def test_sumatra():
    if not os.path.exists(SUMATRA):
        print(f"[ERROR] SumatraPDF not found at: {SUMATRA}")
        return False
    print(f"[OK] SumatraPDF found: {SUMATRA} ({os.path.getsize(SUMATRA)//1024}KB)")
    
    cmd = [
        SUMATRA,
        "-silent",
        "-print-to", PRINTER_NAME,
        "-print-settings", "fit,simplex,paper=A4,portrait",
        "-exit-on-print",
        TEST_PDF
    ]
    print(f"\n[CMD] {' '.join(cmd)}\n")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            # On Windows, CREATE_NO_WINDOW prevents GUI dialogs from blocking
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        print(f"[RESULT] Return code: {result.returncode}")
        if result.stdout:
            print(f"[STDOUT] {result.stdout}")
        if result.stderr:
            print(f"[STDERR] {result.stderr}")
        
        if result.returncode == 0:
            print("[OK] SumatraPDF returned 0 — job sent to spooler")
            return True
        else:
            print(f"[ERROR] SumatraPDF returned non-zero: {result.returncode}")
            return False
    except subprocess.TimeoutExpired:
        print("[ERROR] SumatraPDF timed out after 30s — it may be waiting for input/dialog")
        return False
    except Exception as e:
        print(f"[ERROR] Subprocess failed: {e}")
        return False

def check_spooler_after():
    """Check if a job appeared in the Windows spooler."""
    time.sleep(2)
    try:
        import win32print
        h = win32print.OpenPrinter(PRINTER_NAME)
        jobs = win32print.EnumJobs(h, 0, -1, 2)
        win32print.ClosePrinter(h)
        if jobs:
            print(f"[OK] {len(jobs)} job(s) in spooler:")
            for j in jobs:
                print(f"     - '{j.get('pDocument')}' status={j.get('Status', 0):#x} pStatus='{j.get('pStatus', '')}'")
        else:
            print("[INFO] No jobs currently in spooler (may have already processed)")
    except Exception as e:
        print(f"[ERROR] Spooler check failed: {e}")

if __name__ == "__main__":
    print("=" * 60)
    print("KIOSK DIRECT PRINT TEST")
    print("=" * 60)
    print()

    print("--- Step 1: Check printer ---")
    check_printer()

    print("\n--- Step 2: Create test PDF ---")
    create_test_pdf()

    print("\n--- Step 3: Send to printer via SumatraPDF ---")
    ok = test_sumatra()

    print("\n--- Step 4: Check spooler ---")
    check_spooler_after()

    print("\n" + "=" * 60)
    if ok:
        print("Test complete. Check your Brother printer for output!")
    else:
        print("Test FAILED. See errors above.")
    print("=" * 60)
