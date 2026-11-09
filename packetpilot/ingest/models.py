from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TextLine:
    """One visual line of a page (or one row of a sheet / table).

    ``cells`` holds the line split at large horizontal gaps (PDF/OCR) or at
    cell boundaries (spreadsheets, DOCX tables). ``text`` is the verbatim line
    used as evidence.
    """

    text: str
    cells: list[str]
    bbox: tuple[float, float, float, float] | None = None
    confidence: float = 1.0  # OCR word confidence (0-1); 1.0 for digital text
    locator: str | None = None
    # Horizontal extent of each cell (x0, x1). PDF/OCR: points; sheets/tables:
    # column index (i, i+1). Used to align table rows with header columns.
    cell_spans: list[tuple[float, float]] | None = None


@dataclass
class ParsedPage:
    number: int  # 1-based
    lines: list[TextLine] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    ocr: bool = False
    name: str | None = None  # sheet name for spreadsheets

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass
class Attachment:
    filename: str
    data: bytes
    content_type: str | None = None


@dataclass
class ParsedDocument:
    filename: str
    content_type: str
    pages: list[ParsedPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    kind: str = "unknown"  # pdf | spreadsheet | docx | email | text | image

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def ocr_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.ocr]

    def page(self, number: int) -> ParsedPage | None:
        for p in self.pages:
            if p.number == number:
                return p
        return None
