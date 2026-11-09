"""Evidence grounding: decide whether an extracted value can be trusted.

An LLM can return a fluent value with a plausible-looking quote that does not
exist in the document. Every candidate is therefore checked against the parsed
source text:

1. the quote must occur (exactly, or approximately for OCR pages) on the
   cited page - or on some other page, in which case the page is corrected;
2. the normalised value must be recoverable from the quote itself.

Candidates failing either test are kept (a reviewer may still want to see
them) but marked ``grounded=False`` with low confidence, which blocks
auto-verification and raises an UNGROUNDED_VALUE issue.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..ingest import ParsedDocument, TextLine
from ..normalize import (
    canonical_text,
    fuzzy_contains,
    parse_bool,
    parse_date,
    parse_int,
    parse_lines,
    parse_money,
)
from ..schema import FIELD_SPECS, Candidate

UNGROUNDED_CONFIDENCE = 0.3


def best_line(parsed: ParsedDocument, page_no: int, quote: str) -> tuple[int, list[TextLine]] | None:
    """Find the page/lines that best contain ``quote``. Prefers the cited page."""
    pages = sorted(parsed.pages, key=lambda p: (p.number != page_no, p.number))
    q = re.sub(r"\s+", " ", quote).strip().lower()
    for page in pages:
        if not fuzzy_contains(page.text, quote, threshold=0.9 if page.ocr else 0.97):
            continue
        # single line containing the quote
        for ln in page.lines:
            if q and q in re.sub(r"\s+", " ", ln.text).lower():
                return page.number, [ln]
        # quote spanning consecutive lines
        for i in range(len(page.lines) - 1):
            pair = f"{page.lines[i].text} {page.lines[i + 1].text}"
            if q in re.sub(r"\s+", " ", pair).lower():
                return page.number, page.lines[i : i + 2]
        # approximate (OCR): highest similarity line
        scored = sorted(
            page.lines,
            key=lambda ln: SequenceMatcher(None, ln.text.lower(), q).ratio(),
            reverse=True,
        )
        if scored:
            return page.number, [scored[0]]
    return None


def value_supported(path: str, value, quote: str) -> bool:
    """Is ``value`` actually stated in ``quote``?"""
    if value is None:
        return False
    kind = FIELD_SPECS[path].kind if path in FIELD_SPECS else "text"
    if kind == "money":
        amounts = [parse_money(m.group(0)) for m in re.finditer(r"\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:million|mm|m|k)?\b", quote, re.I)]
        return any(a is not None and abs(a - value) <= max(1.0, 0.005 * abs(value)) for a in amounts)
    if kind == "int":
        nums = [parse_int(m.group(0)) for m in re.finditer(r"\d[\d,]*", quote)]
        return value in nums or parse_int(quote) == value
    if kind == "date":
        found = re.finditer(
            r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}",
            quote,
        )
        return any((d := parse_date(m.group(0))) and d.isoformat() == value for m in found)
    if kind == "fein":
        return re.sub(r"\D", "", str(value)) in re.sub(r"\D", "", quote)
    if kind == "lines":
        got = parse_lines(quote) or []
        return set(value).issubset(set(got))
    if kind == "bool":
        return parse_bool(quote.split(":")[-1]) == value or parse_bool(quote) == value
    cq, cv = canonical_text(quote), canonical_text(str(value))
    if cv in cq:
        return True
    return SequenceMatcher(None, cv, cq).find_longest_match(0, len(cv), 0, len(cq)).size >= 0.85 * len(cv)


def ground_candidate(cand: Candidate, parsed: ParsedDocument) -> Candidate:
    quote = cand.raw if cand.evidence.text == "" else cand.evidence.text
    hit = best_line(parsed, cand.evidence.page, quote)
    ev = cand.evidence.model_copy()
    if hit is None:
        ev.grounded = False
        ev.confidence = min(ev.confidence, UNGROUNDED_CONFIDENCE)
        return cand.model_copy(update={"evidence": ev})
    page_no, lines = hit
    page = parsed.page(page_no)
    ev.page = page_no
    ev.text = "\n".join(ln.text for ln in lines)
    ev.locator = lines[0].locator
    boxes = [ln.bbox for ln in lines if ln.bbox]
    ev.bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None
    if page is not None and page.ocr:
        ev.confidence = round(ev.confidence * min(1.0, min(ln.confidence for ln in lines) + 0.04), 3)
    if not value_supported(cand.path, cand.value, quote):
        ev.grounded = False
        ev.confidence = min(ev.confidence, UNGROUNDED_CONFIDENCE)
    return cand.model_copy(update={"evidence": ev})
