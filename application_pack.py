"""Build one combined PDF from the documents a student already has.

Application portals routinely want a single PDF, in the order the programme
lists its requirements. Doing that by hand — converting scans, reordering,
merging — is the tedious part of applying, and it is pure file handling with no
judgement in it, so it belongs here rather than in the agent.

What this deliberately does NOT do:

  * It does not write any document. A motivation letter must be the applicant's
    own words, and a transcript or a reference letter is issued by a third party
    — generating either is forgery, not drafting.
  * It does not submit anything. The declaration that an application is truthful
    and the applicant's own work belongs to the student, so the pack is handed
    back to them and they submit it themselves.

It only ever moves the student's own files around.
"""

from __future__ import annotations

import io
from datetime import date

# Files we can fold into a PDF. Scans and exports in the wild are nearly always
# one of these; anything else is reported rather than silently dropped.
PDF_TYPES = {".pdf"}
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}
SUPPORTED = PDF_TYPES | IMAGE_TYPES

MAX_FILE_BYTES = 20 * 1024 * 1024


def _suffix(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def _image_to_pdf(data: bytes) -> bytes:
    """Render an image as a single A4 page, scaled to fit with a margin."""
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.convert("RGBA").split()[-1])
        image = background
    else:
        image = image.convert("RGB")

    A4 = (1654, 2339)  # A4 at 200 dpi — plenty for a scan, not a huge file
    margin = 60
    box = (A4[0] - margin * 2, A4[1] - margin * 2)
    image.thumbnail(box, Image.LANCZOS)

    page = Image.new("RGB", A4, "white")
    page.paste(image, ((A4[0] - image.width) // 2, (A4[1] - image.height) // 2))

    buffer = io.BytesIO()
    page.save(buffer, format="PDF", resolution=200.0)
    return buffer.getvalue()


def _cover_page(opportunity: str, institution: str, entries: list[dict]) -> bytes:
    """A contents sheet, so a reviewer can see what is in the pack."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 30 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, y, "Application documents")
    y -= 9 * mm

    c.setFont("Helvetica", 11)
    for line in (opportunity, institution):
        if line and line.strip() and line.strip().lower() != "not stated":
            c.drawString(20 * mm, y, line[:88])
            y -= 6 * mm

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.45, 0.47, 0.52)
    c.drawString(20 * mm, y, f"Compiled {date.today().isoformat()}")
    y -= 12 * mm

    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(20 * mm, y, "Contents")
    y -= 7 * mm

    c.setFont("Helvetica", 10)
    for i, entry in enumerate(entries, 1):
        if y < 30 * mm:
            c.showPage()
            y = height - 25 * mm
            c.setFont("Helvetica", 10)
        c.drawString(22 * mm, y, f"{i}.  {entry['label'][:76]}")
        c.setFillColorRGB(0.45, 0.47, 0.52)
        c.setFont("Helvetica", 8)
        c.drawString(28 * mm, y - 4.2 * mm, entry["filename"][:76])
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 10)
        y -= 10 * mm

    y -= 4 * mm
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColorRGB(0.45, 0.47, 0.52)
    c.drawString(20 * mm, y, "Assembled from documents supplied by the applicant. Review before submitting.")

    c.showPage()
    c.save()
    return buffer.getvalue()


def build_pack(
    entries: list[dict],
    opportunity: str = "",
    institution: str = "",
    cover: bool = True,
) -> dict:
    """Merge the student's files into one PDF, in the order given.

    `entries` is [{"label": <required document>, "filename": str, "data": bytes}]
    already ordered the way the programme lists its requirements.

    Returns {"ok", "pdf", "included", "skipped", "pages"}. Unsupported or broken
    files are reported in `skipped` rather than dropped quietly — a student needs
    to know a page is missing before they submit, not after.
    """
    from pypdf import PdfReader, PdfWriter

    if not entries:
        return {"ok": False, "error": "No documents were supplied.", "skipped": []}

    usable, skipped = [], []
    for entry in entries:
        filename = (entry.get("filename") or "").strip()
        data = entry.get("data") or b""
        label = (entry.get("label") or filename or "Document").strip()

        if not data:
            skipped.append({"label": label, "filename": filename, "why": "the file was empty"})
            continue
        if len(data) > MAX_FILE_BYTES:
            skipped.append({"label": label, "filename": filename,
                            "why": f"larger than {MAX_FILE_BYTES // (1024*1024)} MB"})
            continue
        suffix = _suffix(filename)
        if suffix not in SUPPORTED:
            skipped.append({"label": label, "filename": filename,
                            "why": f"{suffix or 'this file type'} cannot be merged — "
                                   "export it as PDF first"})
            continue
        usable.append({"label": label, "filename": filename, "data": data, "suffix": suffix})

    if not usable:
        return {"ok": False, "error": "None of the supplied files could be merged.",
                "skipped": skipped}

    writer = PdfWriter()
    included = []

    if cover:
        try:
            for page in PdfReader(io.BytesIO(_cover_page(opportunity, institution, usable))).pages:
                writer.add_page(page)
        except Exception:
            pass  # a missing cover must never cost the student their pack

    for entry in usable:
        try:
            blob = entry["data"] if entry["suffix"] in PDF_TYPES else _image_to_pdf(entry["data"])
            reader = PdfReader(io.BytesIO(blob))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")  # some scanners set an empty owner password
                except Exception:
                    raise ValueError("the PDF is password-protected")
            for page in reader.pages:
                writer.add_page(page)
            included.append({"label": entry["label"], "filename": entry["filename"],
                             "pages": len(reader.pages)})
        except Exception as exc:
            skipped.append({"label": entry["label"], "filename": entry["filename"],
                            "why": f"could not be read ({exc})"})

    if not included:
        return {"ok": False, "error": "None of the supplied files could be read.",
                "skipped": skipped}

    out = io.BytesIO()
    writer.write(out)
    return {
        "ok": True,
        "pdf": out.getvalue(),
        "included": included,
        "skipped": skipped,
        "pages": len(writer.pages),
    }


def readiness(required_documents: list, supplied_labels: list) -> dict:
    """How much of the required list the student has actually attached."""
    required = [str(d).strip() for d in (required_documents or []) if str(d).strip()]
    supplied = {str(s).strip() for s in (supplied_labels or []) if str(s).strip()}
    missing = [d for d in required if d not in supplied]
    return {
        "required": len(required),
        "attached": len(required) - len(missing),
        "missing": missing,
        "complete": not missing and bool(required),
    }
