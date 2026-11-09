"""Document type classification.

Transparent keyword scoring: every decision lists the signals that produced it
so a reviewer can see *why* a file was treated as, say, a loss run. Low-scoring
or ambiguous documents are classified ``other`` and raise an issue instead of
being silently forced into a type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ingest import ParsedDocument
from .schema import DocType

# (pattern, weight). Patterns are matched against lower-cased text of the first
# two pages (titles carry most of the signal).
_SIGNALS: dict[DocType, list[tuple[str, float]]] = {
    DocType.APPLICATION: [
        (r"commercial (insurance )?application", 6),
        (r"applicant information", 3),
        (r"named insured|applicant name|legal name", 1.5),
        (r"proposed effective date|requested effective date", 2),
        (r"coverages? requested|lines of (business|coverage)", 2),
        (r"description of operations|nature of business", 1),
        (r"prior (carrier|insurance)|current carrier", 1),
    ],
    DocType.GL_SUPPLEMENT: [
        (r"general liability supplement|supplemental application|gl supplement|liability supplement", 7),
        (r"subcontract", 2),
        (r"products? (sold|manufactured)|completed operations", 1.5),
    ],
    DocType.LOSS_RUN: [
        (r"loss run|loss history report|claims? history|loss experience", 6),
        (r"date of loss|loss date", 2.5),
        (r"valuation date|valued as of|as of date", 2),
        (r"claim (no|number|#)", 2),
        (r"\breserve|outstanding", 1),
        (r"\bpaid\b|incurred", 1),
    ],
    DocType.PAYROLL_SCHEDULE: [
        (r"payroll schedule|employee schedule|payroll by class|remuneration", 6),
        (r"class code|classification", 2.5),
        (r"payroll", 1.5),
        (r"employees|headcount", 1),
    ],
    DocType.VEHICLE_SCHEDULE: [
        (r"vehicle schedule|schedule of (autos|vehicles)|auto schedule|fleet schedule", 6),
        (r"\bvin\b", 3),
        (r"\bmake\b", 1),
        (r"\bmodel\b", 1),
        (r"garag", 1.5),
    ],
    DocType.PROPERTY_SOV: [
        (r"statement of values|schedule of locations|property schedule|\bsov\b", 6),
        (r"building (value|limit)", 2.5),
        (r"contents|business personal property|\bbpp\b", 1.5),
        (r"year built|construction", 1.5),
        (r"\btiv\b|total insured value", 1.5),
    ],
    DocType.FINANCIAL_STATEMENT: [
        (r"profit (and|&) loss|income statement|statement of operations|financial statement", 6),
        (r"balance sheet", 3),
        (r"net income|gross profit|cost of goods sold|operating expenses", 2),
        (r"total revenue|net sales|total income", 1.5),
    ],
}

_FILENAME_HINTS: list[tuple[str, DocType]] = [
    (r"loss[_\- ]?run|losses|claims", DocType.LOSS_RUN),
    (r"payroll|employee", DocType.PAYROLL_SCHEDULE),
    (r"vehicle|auto[_\- ]?sched|fleet", DocType.VEHICLE_SCHEDULE),
    (r"\bsov\b|statement[_\- ]of[_\- ]values|property", DocType.PROPERTY_SOV),
    (r"financial|p&l|pnl|profit|income", DocType.FINANCIAL_STATEMENT),
    (r"supplement|supp\b", DocType.GL_SUPPLEMENT),
    (r"applic|acord[_\- ]?125|\bapp\b", DocType.APPLICATION),
]

MIN_SCORE = 4.0


@dataclass
class Classification:
    doc_type: DocType
    confidence: float
    signals: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


def classify(doc: ParsedDocument) -> Classification:
    if doc.kind == "email":
        return Classification(DocType.EMAIL, 0.99, ["file is an email message"])

    text = "\n".join(p.text for p in doc.pages[:2]).lower()
    fname = doc.filename.lower()
    scores: dict[DocType, float] = {}
    hits: dict[DocType, list[str]] = {}
    for dtype, signals in _SIGNALS.items():
        total = 0.0
        for pattern, weight in signals:
            if re.search(pattern, text):
                total += weight
                hits.setdefault(dtype, []).append(f"text matches '{pattern}' (+{weight:g})")
        scores[dtype] = total
    for pattern, dtype in _FILENAME_HINTS:
        if re.search(pattern, fname):
            scores[dtype] = scores.get(dtype, 0) + 2.0
            hits.setdefault(dtype, []).append(f"filename matches '{pattern}' (+2)")
            break

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    plain_scores = {k.value: round(v, 2) for k, v in scores.items()}
    if best_score < MIN_SCORE:
        return Classification(DocType.OTHER, 0.0, [f"best score {best_score:g} below threshold {MIN_SCORE:g}"], plain_scores)
    margin = (best_score - second) / best_score
    confidence = round(min(0.99, 0.55 + 0.45 * margin + min(best_score, 12) / 60), 3)
    return Classification(best, confidence, hits.get(best, []), plain_scores)
