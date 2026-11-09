"""End-to-end processing of one submission package.

parse -> classify -> extract (rules / LLM / hybrid) -> ground -> reconcile
-> validate -> status. Pure function of its inputs: no database access, so it
is used identically by the API worker, the CLI and the benchmark.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import date

from .classify import classify
from .config import Settings, get_settings
from .extract import LLMExtractor, RulesExtractor, client_from_settings, stable_id
from .extract.base import ExtractionResult
from .extract.llm import JsonLLM
from .ingest import ParsedDocument, parse_bytes
from .reconcile import reconcile
from .rules_engine import apply_issues, evaluate
from .schema import DocumentInfo, RecordState, SubmissionState

log = logging.getLogger(__name__)


@dataclass
class InputFile:
    filename: str
    data: bytes
    content_type: str | None = None


@dataclass
class PipelineOutput:
    state: SubmissionState
    parsed: dict[str, ParsedDocument]  # document_id -> parsed document
    notes: list[str]


def make_reference(submission_id: str, when: date) -> str:
    return f"PP-{when:%y%m%d}-{submission_id[:4].upper()}"


def resolve_extractors(settings: Settings, llm_client: JsonLLM | None) -> tuple[str, list]:
    mode = settings.extractor
    client = llm_client or (client_from_settings(settings) if mode in ("auto", "llm", "hybrid") else None)
    if mode == "auto":
        mode = "hybrid" if client else "rules"
    if mode in ("llm", "hybrid") and client is None:
        log.warning("Extractor '%s' requested but no LLM API key configured; using rules only.", mode)
        mode = "rules"
    rules = RulesExtractor()
    if mode == "rules":
        return mode, [rules]
    llm = LLMExtractor(client)
    return mode, ([llm] if mode == "llm" else [rules, llm])


def _expand(files: list[InputFile], ocr: bool) -> list[tuple[InputFile, ParsedDocument, str | None]]:
    """Parse files; e-mail attachments become documents of their own."""
    out: list[tuple[InputFile, ParsedDocument, str | None]] = []
    queue: list[tuple[InputFile, str | None]] = [(f, None) for f in files]
    while queue:
        f, parent = queue.pop(0)
        parsed = parse_bytes(f.filename, f.data, f.content_type, ocr=ocr)
        out.append((f, parsed, parent))
        for att in parsed.attachments:
            queue.append((InputFile(att.filename, att.data, att.content_type), f.filename))
    return out


def process_submission(
    files: list[InputFile],
    *,
    submission_id: str | None = None,
    reference: str | None = None,
    as_of: date | None = None,
    settings: Settings | None = None,
    llm_client: JsonLLM | None = None,
    previous: SubmissionState | None = None,
) -> PipelineOutput:
    settings = settings or get_settings()
    t0 = time.perf_counter()
    submission_id = submission_id or (previous.id if previous else uuid.uuid4().hex)
    as_of = as_of or (previous.as_of if previous else date.today())
    state = SubmissionState(
        id=submission_id,
        reference=reference or (previous.reference if previous else make_reference(submission_id, as_of)),
        as_of=as_of,
        status="processing",
        fields=previous.fields if previous else {},
        issues=previous.issues if previous else [],
        version=previous.version if previous else 0,
    )
    mode, extractors = resolve_extractors(settings, llm_client)
    state.extractor = mode
    state.model_version = "+".join(e.version for e in extractors)

    parsed_by_id: dict[str, ParsedDocument] = {}
    notes: list[str] = []
    seen_hashes: dict[str, str] = {}
    results: list[tuple[str, ExtractionResult]] = []

    for f, parsed, parent in _expand(files, settings.ocr_enabled):
        sha = hashlib.sha256(f.data).hexdigest()
        doc_id = stable_id(submission_id, sha, f.filename)
        info = DocumentInfo(
            id=doc_id,
            filename=f.filename,
            sha256=sha,
            content_type=parsed.content_type,
            size=len(f.data),
            page_count=len(parsed.pages),
            ocr_pages=parsed.ocr_pages,
            parse_warnings=list(parsed.warnings),
        )
        if parent:
            info.meta["attached_to"] = parent
        if sha in seen_hashes:
            info.duplicate_of = seen_hashes[sha]
        else:
            seen_hashes[sha] = doc_id
        cls = classify(parsed)
        info.doc_type = cls.doc_type.value
        info.classification_confidence = cls.confidence
        info.classification_signals = cls.signals
        if parsed.pages and not parsed.text.strip():
            info.meta["_empty_text"] = True
        parsed_by_id[doc_id] = parsed
        state.documents.append(info)
        if info.duplicate_of:
            continue
        for ex in extractors:
            res = ex.extract(info, parsed)
            notes.extend(res.notes)
            results.append((ex.name, res))
            for k, v in res.meta.items():
                info.meta.setdefault(k, v)
            for k, v in res.meta_evidence.items():
                info.meta_evidence.setdefault(k, v)

    # Records: when both extractors read the same table, keep the deterministic
    # rows (reproducible) and use LLM rows only where rules found nothing.
    rules_keys = {
        (r.evidence.document_id, r.collection) for name, res in results if name == "rules" for r in res.records
    }
    records: list[RecordState] = []
    for name, res in results:
        for r in res.records:
            if name != "rules" and (r.evidence.document_id, r.collection) in rules_keys:
                continue
            records.append(r)
    if previous:  # keep reviewer decisions on rows that still exist
        prev = {r.id: r for r in previous.records}
        records = [
            r.model_copy(update={"status": prev[r.id].status, "data": prev[r.id].data})
            if r.id in prev and prev[r.id].status != "extracted" else r
            for r in records
        ]
    state.records = records

    candidates = [c for _, res in results for c in res.candidates]
    reconcile(state, candidates, settings)
    apply_issues(state, evaluate(state, settings))
    state.status = "needs_review"
    state.processing_ms = int((time.perf_counter() - t0) * 1000)
    return PipelineOutput(state=state, parsed=parsed_by_id, notes=notes)


def reevaluate(state: SubmissionState, settings: Settings | None = None) -> SubmissionState:
    """Re-run validation after a human edit (no re-parsing)."""
    settings = settings or get_settings()
    apply_issues(state, evaluate(state, settings))
    return state
