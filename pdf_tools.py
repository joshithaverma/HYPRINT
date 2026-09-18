"""
pdf_tools.py — page-range parsing, validation, and slicing.

Kept separate from main.py because this is the logic that decides what a
student is charged for, so it needs to be readable and independently testable.
"""

from pypdf import PdfReader, PdfWriter, Transformation


class PdfError(Exception):
    """Raised with a student-readable message."""


def parse_page_range(spec: str | None, page_count: int) -> list[int]:
    """
    Parse '1-5, 8, 11-15' into a sorted, de-duplicated list of 1-based page
    numbers. Empty/None means "all pages".

    Raises PdfError with a friendly message on anything malformed or out of
    bounds — the student sees this text, so it must not leak internals.
    """
    if page_count < 1:
        raise PdfError("This document has no pages.")

    if not spec or not spec.strip():
        return list(range(1, page_count + 1))

    pages: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            bits = chunk.split("-")
            if len(bits) != 2:
                raise PdfError(f"'{chunk}' isn't a valid page range.")
            start_s, end_s = bits[0].strip(), bits[1].strip()
            if not start_s.isdigit() or not end_s.isdigit():
                raise PdfError(f"'{chunk}' isn't a valid page range.")
            start, end = int(start_s), int(end_s)
            if start > end:
                raise PdfError(f"'{chunk}' is backwards — start page is after the end page.")
            if start < 1 or end > page_count:
                raise PdfError(f"'{chunk}' is outside this document (it has {page_count} pages).")
            pages.update(range(start, end + 1))
        else:
            if not chunk.isdigit():
                raise PdfError(f"'{chunk}' isn't a valid page number.")
            n = int(chunk)
            if n < 1 or n > page_count:
                raise PdfError(f"Page {n} doesn't exist (document has {page_count} pages).")
            pages.add(n)

    if not pages:
        raise PdfError("No pages selected.")
    return sorted(pages)


def inspect_pdf(path: str) -> int:
    """Validate and return the page count. Rejects encrypted/corrupt files."""
    try:
        reader = PdfReader(path)
    except Exception:
        raise PdfError("This file couldn't be read — it may be corrupted.")

    if reader.is_encrypted:
        # Some PDFs are "encrypted" with an empty owner password and open fine;
        # try that before giving up, since students hit this a lot with
        # bank statements and exported docs.
        try:
            if reader.decrypt("") == 0:
                raise PdfError("This PDF is password protected. Please remove the password and re-upload.")
        except PdfError:
            raise
        except Exception:
            raise PdfError("This PDF is password protected. Please remove the password and re-upload.")

    try:
        count = len(reader.pages)
    except Exception:
        raise PdfError("This PDF couldn't be read — it may be corrupted.")

    if count < 1:
        raise PdfError("This PDF has no pages.")
    return count


def slice_pdf(src_path: str, dest_path: str, pages: list[int], margin_scale: float = 0.94) -> int:
    """Write a new PDF at dest_path containing only `pages` (1-based), 
    scaled down and centered to create an unprintable hardware margin.
    Returns the number of pages written."""
    try:
        reader = PdfReader(src_path)
        writer = PdfWriter()
        for p in pages:
            page = reader.pages[p - 1]
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            tx = (w * (1 - margin_scale)) / 2
            ty = (h * (1 - margin_scale)) / 2
            
            transform = Transformation().scale(margin_scale, margin_scale).translate(tx, ty)
            page.add_transformation(transform)
            writer.add_page(page)
        with open(dest_path, "wb") as fh:
            writer.write(fh)
    except Exception:
        raise PdfError("Couldn't prepare the selected pages for printing.")
    return len(pages)
