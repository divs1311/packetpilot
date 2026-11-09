"""Cross-document reconciliation.

Groups every candidate value for a field into clusters of equivalent values
(using kind-aware comparison: money tolerance, org-name suffixes, etc.),
proposes a value from the most authoritative source, and assigns a review
status. A field with more than one cluster is a *conflict*: a value is still
proposed for convenience, but the record cannot be marked ready until a human
picks or corrects it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .normalize import values_equal
from .schema import FIELD_SPECS, REVIEWED_STATUSES, Candidate, DocType, FieldState, SubmissionState


@dataclass
class Cluster:
    value: object
    members: list[Candidate]

    @property
    def documents(self) -> set[str]:
        return {c.evidence.document_id for c in self.members}


def cluster_candidates(path: str, candidates: list[Candidate], tolerance: float) -> list[Cluster]:
    spec = FIELD_SPECS[path]
    clusters: list[Cluster] = []
    for c in candidates:
        if c.value is None:
            continue
        for cl in clusters:
            if values_equal(spec.kind, cl.value, c.value, tolerance, path):
                cl.members.append(c)
                break
        else:
            clusters.append(Cluster(c.value, [c]))
    return clusters


def _priority(path: str, cand: Candidate) -> int:
    order = FIELD_SPECS[path].source_priority
    try:
        return list(order).index(DocType(cand.evidence.doc_type))
    except ValueError:
        return len(order)


def _rank(path: str, c: Candidate) -> tuple:
    return (_priority(path, c), not c.evidence.grounded, -c.evidence.confidence)


def reconcile_field(path: str, candidates: list[Candidate], settings: Settings, previous: FieldState | None = None) -> FieldState:
    # Human decisions survive re-processing.
    if previous is not None and previous.status in REVIEWED_STATUSES | {"escalated"}:
        return previous.model_copy(update={"candidates": candidates})

    clusters = cluster_candidates(path, candidates, settings.money_tolerance)
    if not clusters:
        status = "low_confidence" if candidates else "missing"
        return FieldState(path=path, status=status, candidates=candidates)

    best_cluster = min(clusters, key=lambda cl: (min(_rank(path, c) for c in cl.members), -len(cl.members)))
    chosen = min(best_cluster.members, key=lambda c: _rank(path, c))
    confidence = chosen.evidence.confidence
    if len(best_cluster.documents) > 1:  # independent corroboration
        confidence = min(0.99, confidence + 0.03)

    # OCR-only values always get human eyes unless a digital source agrees.
    ocr_only = all(c.evidence.method.startswith("ocr") for c in best_cluster.members)
    if len(clusters) > 1:
        status = "conflict"
    elif not chosen.evidence.grounded or confidence < settings.low_confidence_threshold:
        status = "low_confidence"
    elif confidence >= settings.auto_verify_threshold and not ocr_only:
        status = "auto_verified"
    else:
        status = "extracted"
    return FieldState(
        path=path,
        value=chosen.value,
        status=status,
        confidence=round(confidence, 3),
        selected_candidate_id=chosen.id,
        candidates=candidates,
        original_value=chosen.value,
    )


def reconcile(state: SubmissionState, candidates: list[Candidate], settings: Settings) -> None:
    excluded = {d.id for d in state.documents if d.duplicate_of}
    by_path: dict[str, list[Candidate]] = {p: [] for p in FIELD_SPECS}
    for c in candidates:
        if c.path in by_path and c.evidence.document_id not in excluded:
            by_path[c.path].append(c)
    state.fields = {
        path: reconcile_field(path, cands, settings, state.fields.get(path)) for path, cands in by_path.items()
    }
