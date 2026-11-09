"""Rebuild reading-order lines (and table cells) from positioned text fragments.

Both PyMuPDF spans and Tesseract words arrive as boxes with text. Grouping by
vertical position and splitting at wide horizontal gaps yields one ``TextLine``
per visual row with ``cells`` that preserve column structure - enough to read
label/value forms and tabular loss runs without a table-detection model.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import TextLine


@dataclass
class Fragment:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float = 1.0

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)


def group_lines(frags: list[Fragment], cell_gap: float = 12.0) -> list[TextLine]:
    frags = [f for f in frags if f.text and f.text.strip()]
    if not frags:
        return []
    frags.sort(key=lambda f: (f.yc, f.x0))
    rows: list[list[Fragment]] = []
    for f in frags:
        if rows:
            row = rows[-1]
            ref = sum(r.yc for r in row) / len(row)
            tol = max(2.0, 0.45 * min(f.h, min(r.h for r in row)))
            if abs(f.yc - ref) <= tol:
                row.append(f)
                continue
        rows.append([f])

    return [row_to_line(row, cell_gap) for row in rows]


def rows_to_lines(rows: list[list[Fragment]], cell_gap: float = 12.0) -> list[TextLine]:
    """Build lines from rows that were grouped upstream (e.g. by the OCR engine,
    which follows skewed baselines better than a fixed y-tolerance)."""
    rows = [[f for f in r if f.text and f.text.strip()] for r in rows]
    rows = [r for r in rows if r]
    rows.sort(key=lambda r: (sum(f.yc for f in r) / len(r), min(f.x0 for f in r)))
    return [row_to_line(r, cell_gap) for r in rows]


def row_to_line(row: list[Fragment], cell_gap: float = 12.0) -> TextLine:
    """Join one visual row of fragments, splitting cells at gaps wider than ``cell_gap``."""
    row = sorted(row, key=lambda f: f.x0)
    cells: list[str] = []
    spans: list[tuple[float, float]] = []
    current = row[0].text.strip()
    cur_x0, cur_x1 = row[0].x0, row[0].x1
    for prev, nxt in zip(row, row[1:]):
        gap = nxt.x0 - prev.x1
        piece = nxt.text.strip()
        if gap > cell_gap:
            cells.append(current)
            spans.append((cur_x0, cur_x1))
            current = piece
            cur_x0, cur_x1 = nxt.x0, nxt.x1
            continue
        cur_x1 = max(cur_x1, nxt.x1)
        if gap > 1.0 or prev.text[-1:].isspace() or nxt.text[:1].isspace():
            current = f"{current} {piece}"
        else:
            current = f"{current}{piece}"
    cells.append(current)
    spans.append((cur_x0, cur_x1))
    keep = [i for i, c in enumerate(cells) if c.strip()]
    cells = [cells[i].strip() for i in keep]
    spans = [spans[i] for i in keep]
    bbox = (min(f.x0 for f in row), min(f.y0 for f in row), max(f.x1 for f in row), max(f.y1 for f in row))
    conf = sum(f.conf for f in row) / len(row)
    return TextLine(text="  ".join(cells), cells=cells, bbox=bbox, confidence=round(conf, 3), cell_spans=spans)
