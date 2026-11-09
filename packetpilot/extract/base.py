from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..ingest import ParsedDocument, TextLine
from ..schema import Candidate, DocumentInfo, Evidence, RecordState


def stable_id(*parts: Any, n: int = 12) -> str:
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return h[:n]


@dataclass
class ExtractionResult:
    candidates: list[Candidate] = field(default_factory=list)
    records: list[RecordState] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    meta_evidence: dict[str, Evidence] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def extend(self, other: "ExtractionResult") -> None:
        self.candidates.extend(other.candidates)
        self.records.extend(other.records)
        for k, v in other.meta.items():
            self.meta.setdefault(k, v)
        for k, v in other.meta_evidence.items():
            self.meta_evidence.setdefault(k, v)
        self.notes.extend(other.notes)


class Extractor(Protocol):
    name: str
    version: str

    def extract(self, info: DocumentInfo, parsed: ParsedDocument) -> ExtractionResult: ...


def evidence_from_lines(
    info: DocumentInfo,
    page_no: int,
    lines: list[TextLine],
    method: str,
    confidence: float,
    ocr: bool = False,
) -> Evidence:
    text = "\n".join(ln.text for ln in lines)
    boxes = [ln.bbox for ln in lines if ln.bbox]
    bbox = None
    if boxes:
        bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    if ocr:
        line_conf = min(ln.confidence for ln in lines)
        confidence = confidence * min(1.0, line_conf + 0.04)
        method = f"ocr+{method}"
    return Evidence(
        document_id=info.id,
        filename=info.filename,
        doc_type=info.doc_type,
        page=page_no,
        locator=lines[0].locator if lines else None,
        text=text,
        bbox=bbox,
        method=method,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        grounded=True,
    )
