from __future__ import annotations

import io
import os
import random
import shutil
from pathlib import Path

import pytest

from packetpilot.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """SQLite per test by default; set TEST_DATABASE_URL to run against PostgreSQL."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        from packetpilot.db import Base, get_engine

        engine = get_engine(url)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
    return Settings(
        database_url=url or f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "uploads",
        extractor="rules",
        process_mode="sync",
    )


def make_pdf(lines: list[tuple[float, float, str]], pages: int = 1) -> bytes:
    """Tiny PDF builder: (x, y_from_top, text) triples on letter pages."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    h = letter[1]
    for _ in range(pages):
        c.setFont("Helvetica", 9)
        for x, y, t in lines:
            c.drawString(x, h - y, t)
        c.showPage()
    c.save()
    return buf.getvalue()


def make_xlsx(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


tesseract_available = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not tesseract_available, reason="tesseract not installed")


def build_defect_package(defect: str | None, style: str = "A", scanned: bool = False):
    """Find a deterministic fictional company to which ``defect`` applies and build it."""
    from packetpilot.bench.generate import DEFECTS, build_package

    for seed in range(500):
        probe = build_package(1, style, scanned, random.Random(seed), set(), forced=[])
        if defect is None or DEFECTS[defect][1](probe):
            return build_package(1, style, scanned, random.Random(seed), set(), forced=[defect] if defect else [])
    raise RuntimeError(f"no applicable company for {defect}")
