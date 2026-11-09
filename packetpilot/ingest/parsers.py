"""File parsers: PDF (digital + OCR fallback), spreadsheets, DOCX, email, text, images."""

from __future__ import annotations

import csv
import email
import io
import logging
import mimetypes
import re
from datetime import date, datetime
from email import policy
from html import unescape
from pathlib import Path

from .layout import Fragment, group_lines, rows_to_lines
from .models import Attachment, ParsedDocument, ParsedPage, TextLine

log = logging.getLogger(__name__)

OCR_DPI = 300
_MIN_DIGITAL_CHARS = 25


def guess_kind(filename: str, content_type: str | None = None) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in {".xlsx", ".xlsm", ".csv"}:
        return "spreadsheet"
    if ext == ".docx":
        return "docx"
    if ext in {".eml", ".msg"}:
        return "email"
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        return "image"
    if ext in {".txt", ".md"}:
        return "text"
    if content_type:
        if "pdf" in content_type:
            return "pdf"
        if "message/rfc822" in content_type:
            return "email"
        if content_type.startswith("text/"):
            return "text"
    return "unknown"


def parse_bytes(filename: str, data: bytes, content_type: str | None = None, ocr: bool = True) -> ParsedDocument:
    content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    kind = guess_kind(filename, content_type)
    doc = ParsedDocument(filename=filename, content_type=content_type, kind=kind)
    try:
        if kind == "pdf":
            _parse_pdf(data, doc, ocr)
        elif kind == "spreadsheet":
            if filename.lower().endswith(".csv"):
                _parse_csv(data, doc)
            else:
                _parse_xlsx(data, doc)
        elif kind == "docx":
            _parse_docx(data, doc)
        elif kind == "email":
            _parse_email(data, doc)
        elif kind == "image":
            _parse_image(data, doc, ocr)
        elif kind == "text":
            _parse_text(data.decode("utf-8", errors="replace"), doc)
        else:
            doc.warnings.append(f"Unsupported file type for {filename}; stored but not parsed.")
    except Exception as exc:  # a corrupt file must not take down the submission
        log.exception("parse failed for %s", filename)
        doc.warnings.append(f"Could not parse {filename}: {exc.__class__.__name__}: {exc}")
    return doc


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _parse_pdf(data: bytes, doc: ParsedDocument, ocr: bool) -> None:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for idx, page in enumerate(pdf):
            frags: list[Fragment] = []
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "")
                        if txt.strip():
                            x0, y0, x1, y1 = span["bbox"]
                            frags.append(Fragment(x0, y0, x1, y1, txt))
            n_chars = sum(len(f.text.strip()) for f in frags)
            ppage = ParsedPage(number=idx + 1, width=page.rect.width, height=page.rect.height)
            if n_chars < _MIN_DIGITAL_CHARS:
                if ocr:
                    ppage.lines = _ocr_pdf_page(page, doc)
                    ppage.ocr = True
                else:
                    doc.warnings.append(f"Page {idx + 1} has no text layer and OCR is disabled.")
            else:
                ppage.lines = group_lines(frags)
            for i, ln in enumerate(ppage.lines, 1):
                ln.locator = f"p{idx + 1} line {i}"
            doc.pages.append(ppage)


def _ocr_pdf_page(page, doc: ParsedDocument) -> list[TextLine]:
    import pymupdf
    from PIL import Image

    pix = page.get_pixmap(matrix=pymupdf.Matrix(OCR_DPI / 72, OCR_DPI / 72), colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return _ocr_image(img, scale=72 / OCR_DPI, doc=doc)


def _ocr_image(img, scale: float, doc: ParsedDocument) -> list[TextLine]:
    try:
        import pytesseract
    except ImportError:  # pragma: no cover
        doc.warnings.append("pytesseract not installed; OCR skipped.")
        return []
    from PIL import ImageFilter

    # 3x3 median removes scanner speckle without eroding 9-10pt glyphs at 300 dpi.
    img = img.filter(ImageFilter.MedianFilter(3))
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 4")
    except Exception as exc:  # tesseract binary missing etc.
        doc.warnings.append(f"OCR failed: {exc}")
        return []
    # Group words by Tesseract's own line ids: they follow skewed baselines,
    # which a fixed y-tolerance would split into two rows.
    groups: dict[tuple, list[Fragment]] = {}
    for i, word in enumerate(data["text"]):
        conf = float(data["conf"][i])
        if not word.strip() or conf < 0:
            continue
        x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append(Fragment(x * scale, y * scale, (x + w) * scale, (y + h) * scale, word, conf / 100.0))
    return rows_to_lines(list(groups.values()), cell_gap=14.0)


def _parse_image(data: bytes, doc: ParsedDocument, ocr: bool) -> None:
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("L")
    dpi = img.info.get("dpi", (OCR_DPI, OCR_DPI))[0] or OCR_DPI
    page = ParsedPage(number=1, width=img.width * 72 / dpi, height=img.height * 72 / dpi, ocr=True)
    if ocr:
        page.lines = _ocr_image(img, scale=72 / dpi, doc=doc)
    for i, ln in enumerate(page.lines, 1):
        ln.locator = f"p1 line {i}"
    doc.pages.append(page)


# --------------------------------------------------------------------------- #
# Spreadsheets
# --------------------------------------------------------------------------- #


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.2f}"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return re.sub(r"\s+", " ", str(v)).strip()


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _rows_to_page(number: int, name: str, rows: list[list]) -> ParsedPage:
    page = ParsedPage(number=number, name=name)
    for r_idx, row in enumerate(rows, 1):
        cells = [_cell_str(v) for v in row]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        first = next(i for i, c in enumerate(cells) if c)
        page.lines.append(
            TextLine(
                text=" | ".join(c for c in cells if c),
                cells=cells,
                locator=f"{name}!{_col_letter(first)}{r_idx}",
                cell_spans=[(float(i), float(i + 1)) for i in range(len(cells))],
            )
        )
    return page


def _parse_xlsx(data: bytes, doc: ParsedDocument) -> None:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        for i, ws in enumerate(wb.worksheets, 1):
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            doc.pages.append(_rows_to_page(i, ws.title, rows))
            if any(isinstance(v, str) and v.startswith("=") for r in rows for v in r):
                doc.warnings.append(f"Sheet {ws.title} contains formulas without cached values.")
    finally:
        wb.close()


def _parse_csv(data: bytes, doc: ParsedDocument) -> None:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    doc.pages.append(_rows_to_page(1, Path(doc.filename).stem, rows))


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #


def _parse_docx(data: bytes, doc: ParsedDocument) -> None:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    page = ParsedPage(number=1)
    n = 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            txt = Paragraph(child, d).text.strip()
            if txt:
                n += 1
                page.lines.append(TextLine(text=txt, cells=[txt], locator=f"paragraph {n}"))
        elif tag == "tbl":
            table = Table(child, d)
            for r_i, row in enumerate(table.rows, 1):
                cells = []
                for c in row.cells:
                    t = c.text.strip()
                    if not cells or cells[-1] != t:  # merged cells repeat their text
                        cells.append(t)
                if any(cells):
                    n += 1
                    page.lines.append(
                        TextLine(
                            text=" | ".join(c for c in cells if c),
                            cells=cells,
                            locator=f"table row {r_i}",
                            cell_spans=[(float(i), float(i + 1)) for i in range(len(cells))],
                        )
                    )
    doc.pages.append(page)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return unescape(html)


def _parse_email(data: bytes, doc: ParsedDocument) -> None:
    msg = email.message_from_bytes(data, policy=policy.default)
    header_lines = []
    for h in ("From", "To", "Cc", "Date", "Subject"):
        if msg.get(h):
            header_lines.append(f"{h}: {msg.get(h)}")
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    _parse_text("\n".join(header_lines) + "\n\n" + body, doc)
    for part in msg.iter_attachments():
        fname = part.get_filename()
        if not fname:
            continue
        payload = part.get_payload(decode=True) or b""
        doc.attachments.append(Attachment(fname, payload, part.get_content_type()))


def _parse_text(text: str, doc: ParsedDocument) -> None:
    page = ParsedPage(number=1)
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(">"):  # skip quoted replies
            continue
        n += 1
        page.lines.append(TextLine(text=line, cells=[line], locator=f"line {n}"))
    doc.pages.append(page)
