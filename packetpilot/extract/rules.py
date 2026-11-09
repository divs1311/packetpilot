"""Deterministic extractor.

Reads label/value forms, tables and a handful of free-text statements using
explicit, auditable rules. It needs no API key, is reproducible run-to-run and
- importantly - *fails closed*: when it cannot find a field it reports nothing,
which the rule engine turns into a "missing" issue rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ingest import ParsedDocument, ParsedPage, TextLine
from ..normalize import (
    clean_text,
    normalize_field,
    parse_date,
    parse_int,
    parse_lines,
    parse_money,
)
from ..schema import FIELD_SPECS, Candidate, DocType, DocumentInfo, RecordState
from .base import ExtractionResult, evidence_from_lines, stable_id

VERSION = "rules-v1"

# --------------------------------------------------------------------------- #
# Label dictionary
# --------------------------------------------------------------------------- #

LABELS: dict[str, list[str]] = {
    "applicant.legal_name": [
        r"applicant name", r"applicant'?s? legal name", r"legal name( of applicant| of business)?",
        r"(first )?named insured", r"insured name", r"name of applicant",
    ],
    "applicant.dba": [r"dba", r"d/?b/?a", r"doing business as", r"trade name"],
    "applicant.fein": [
        r"fein", r"fein number", r"fein #", r"federal employer identification number", r"federal tax id( number)?",
        r"ein", r"tax id( number)?", r"fein/ssn",
    ],
    "applicant.entity_type": [
        r"entity type", r"business entity( type)?", r"legal entity( type)?", r"form of business", r"type of entity",
        r"organization type",
    ],
    "applicant.mailing_address": [r"mailing address", r"business address", r"address of applicant", r"mailing address of applicant"],
    "applicant.years_in_business": [r"years in business", r"years in operation", r"number of years in business"],
    "applicant.naics": [r"naics( code)?", r"naics code/sic"],
    "contact.name": [r"contact name", r"primary contact", r"contact person"],
    "contact.email": [r"contact email", r"e-?mail( address)?"],
    "contact.phone": [r"contact phone", r"(business )?phone( number)?", r"telephone"],
    "operations.description": [
        r"description of operations", r"nature of business", r"description of business", r"business description",
        r"operations description",
    ],
    "financials.annual_revenue": [
        r"(total |gross )?annual revenue", r"annual gross sales", r"(annual )?gross sales", r"total revenue",
        r"annual sales", r"revenue", r"gross revenue", r"net sales", r"total sales",
    ],
    "financials.total_payroll": [
        r"(total |estimated )?annual payroll", r"total payroll", r"payroll", r"total annual remuneration",
    ],
    "workforce.full_time_employees": [
        r"(number of |# of |no\.? of )?full[- ]time employees", r"ft employees", r"employees,? full[- ]time",
    ],
    "workforce.part_time_employees": [
        r"(number of |# of |no\.? of )?part[- ]time employees", r"pt employees", r"employees,? part[- ]time",
    ],
    "policy.effective_date": [
        r"(proposed |requested |policy )?effective date", r"proposed eff\.? date",
    ],
    "policy.lines_requested": [
        r"coverages? requested", r"lines of (business|coverage)( requested)?", r"lines requested",
    ],
    "prior.carrier": [r"(prior|current|expiring) (carrier|insurer)", r"current insurance carrier"],
    "prior.expiration_date": [
        r"(current |prior |expiring )?(policy )?expiration( date)?", r"current policy expires",
    ],
    "loss_history.prior_losses_disclosed": [
        r"any (losses|claims) in (the )?(past|last) (5|five) years", r"(prior )?losses in (the )?(past|last) (5|five) years",
        r"prior losses",
    ],
    "auto.vehicle_count": [r"(number|#|no\.?) of (vehicles|power units)", r"vehicle count", r"total vehicles"],
    "gl.subcontractors_used": [
        r"do you (use|hire) subcontractors", r"(uses|use of) subcontractors", r"subcontractors used",
        r"any work subcontracted",
    ],
    "gl.subcontractor_explanation": [
        r"if yes,? explain", r"if yes,? describe( subcontracted work)?", r"subcontractor explanation",
        r"describe subcontracted work",
    ],
    "gl.subcontracted_cost": [
        r"(annual )?(cost of )?subcontracted (work|cost)( cost)?", r"subcontractor costs?",
        r"annual cost of subcontracted work",
    ],
}

LOCATION_LABELS = [r"location( #?\d+)?( address)?", r"premises( address)?( #?\d+)?", r"location address( #?\d+)?"]

FIELDS_BY_DOC: dict[str, set[str] | None] = {
    DocType.APPLICATION.value: None,  # all fields
    DocType.GL_SUPPLEMENT.value: {
        "applicant.legal_name", "applicant.fein", "operations.description", "financials.annual_revenue",
        "gl.subcontractors_used", "gl.subcontractor_explanation", "gl.subcontracted_cost",
    },
    DocType.FINANCIAL_STATEMENT.value: {"financials.annual_revenue", "applicant.fein"},
}

_COMPILED = {path: [re.compile(rf"^(?:{p})$") for p in pats] for path, pats in LABELS.items()}
_LOC_COMPILED = [re.compile(rf"^(?:{p})$") for p in LOCATION_LABELS]
_FIN_TOTAL = re.compile(r"^(total revenues?|net sales|total sales|gross revenue|total income|gross sales|total net revenue)$")
_EMPTY_VALUE = re.compile(r"^[\s_\-—–.]*$|^(n/?a|none provided|tbd|unknown)$", re.IGNORECASE)


def norm_label(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"^\s*(\d+[.)]|[a-z][.)]|q\d+[.:)]?)\s+", "", s)
    s = s.strip(" :*-–—\t?.,;'‘’`_|")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" :*?")


def match_label(text: str, allowed: set[str] | None = None) -> str | None:
    lab = norm_label(text)
    if not lab or len(lab) > 80:
        return None
    for path, pats in _COMPILED.items():
        if allowed is not None and path not in allowed:
            continue
        if any(p.match(lab) for p in pats):
            return path
    if allowed is None and any(p.match(lab) for p in _LOC_COMPILED):
        return "_location"
    return None


def _is_label_like(text: str) -> bool:
    head = text.split(":", 1)[0]
    return match_label(head) is not None


def _line_is_label(line: TextLine) -> bool:
    first = (line.cells or [line.text])[0]
    return _is_label_like(first) or _is_label_like(line.text)


def _is_header(text: str) -> bool:
    t = text.strip()
    letters = [c for c in t if c.isalpha()]
    return bool(
        re.match(r"^(section|part)\b", t, re.IGNORECASE)
        or (len(letters) > 3 and all(c.isupper() for c in letters))
    )


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #


class RulesExtractor:
    name = "rules"
    version = VERSION

    def extract(self, info: DocumentInfo, parsed: ParsedDocument) -> ExtractionResult:
        dtype = info.doc_type
        out = ExtractionResult()
        if dtype in (DocType.APPLICATION.value, DocType.GL_SUPPLEMENT.value):
            for page in parsed.pages:
                out.extend(self._form(info, page, FIELDS_BY_DOC.get(dtype)))
        elif dtype == DocType.FINANCIAL_STATEMENT.value:
            for page in parsed.pages:
                out.extend(self._form(info, page, FIELDS_BY_DOC[dtype], first_number=True))
        elif dtype == DocType.LOSS_RUN.value:
            for page in parsed.pages:
                out.extend(self._loss_run(info, page))
        elif dtype == DocType.PAYROLL_SCHEDULE.value:
            for page in parsed.pages:
                out.extend(self._table(info, page, PAYROLL_COLUMNS, "payroll", _payroll_row, min_hits=2))
        elif dtype == DocType.VEHICLE_SCHEDULE.value:
            for page in parsed.pages:
                out.extend(self._table(info, page, VEHICLE_COLUMNS, "vehicles", _vehicle_row, min_hits=3))
        elif dtype == DocType.PROPERTY_SOV.value:
            for page in parsed.pages:
                out.extend(self._table(info, page, SOV_COLUMNS, "property", _sov_row, min_hits=2))
        elif dtype == DocType.EMAIL.value:
            for page in parsed.pages:
                out.extend(self._free_text(info, page))
        return out

    # ------------------------------------------------------------------ forms
    def _form(self, info: DocumentInfo, page: ParsedPage, allowed: set[str] | None, first_number: bool = False) -> ExtractionResult:
        out = ExtractionResult()
        lines = page.lines
        seen: set[tuple[str, int]] = set()

        def emit(path: str, raw: str, ev_lines: list[TextLine], conf: float, label: str = "") -> None:
            raw = clean_text(raw) or ""
            if _EMPTY_VALUE.match(raw):
                return
            if first_number and path == "financials.annual_revenue":
                # Statements contain section headings ("Revenue") and sub-lines;
                # only a total line is a statement of annual revenue.
                if not _FIN_TOTAL.match(norm_label(label)):
                    return
                nums = re.findall(r"\$?\s?\(?\d[\d,]*(?:\.\d+)?\)?", raw)
                if nums:
                    raw = nums[0]
            ev = evidence_from_lines(info, page.number, ev_lines, "rules", conf, ocr=page.ocr)
            if path == "_location":
                rec = _location_record(info, raw, ev)
                if rec:
                    out.records.append(rec)
                return
            key = (path, id(ev_lines[0]))
            if key in seen:
                return
            seen.add(key)
            value = normalize_field(path, raw)
            out.candidates.append(
                Candidate(id=stable_id(info.id, path, page.number, ev.locator, raw), path=path, value=value, raw=raw, evidence=ev)
            )

        for li, line in enumerate(lines):
            cells = line.cells or [line.text]
            j = 0
            while j < len(cells):
                cell = cells[j]
                path = None
                inline_val = None
                if ":" in cell:
                    lab, _, rest = cell.partition(":")
                    path = match_label(lab, allowed)
                    if path:
                        inline_val = rest.strip()
                if path is None:
                    path = match_label(cell, allowed)
                if path is None:
                    j += 1
                    continue
                label = cell.partition(":")[0] if inline_val is not None else cell
                if inline_val:
                    tail = _value_cells(cells[j + 1 :]) if (_multi_cell_kind(path) or page.ocr) else []
                    emit(path, " ".join([inline_val, *tail]), [line], 0.96, label)
                    j += 1 + len(tail)
                    continue
                following = _value_cells(cells[j + 1 :])
                if following:
                    # OCR cell boundaries are unreliable: read the rest of the row.
                    take = following if (_multi_cell_kind(path) or page.ocr) else following[:1]
                    emit(path, " ".join(take), [line], 0.95, label)
                    j += 1 + len(take)
                    continue
                # label alone on its line -> value may sit on the next line
                if len(cells) == 1 and li + 1 < len(lines) and not first_number:
                    nxt = lines[li + 1]
                    if (
                        not _line_is_label(nxt)
                        and not _is_header(nxt.text)
                        and _vertically_adjacent(line, nxt)
                    ):
                        emit(path, nxt.text, [line, nxt], 0.85, label)
                j += 1
        return out

    # ------------------------------------------------------------------ tables
    def _table(
        self,
        info: DocumentInfo,
        page: ParsedPage,
        columns: dict[str, list[str]],
        collection: str,
        row_fn: Callable[[dict[str, str]], tuple[str, dict] | None],
        min_hits: int,
    ) -> ExtractionResult:
        out = ExtractionResult()
        header_idx, header_cols = _find_header(page.lines, columns, min_hits)
        if header_idx is None:
            out.notes.append(f"{info.filename} p{page.number}: no {collection} table header found")
            return out
        header = page.lines[header_idx]
        for line in page.lines[header_idx + 1 :]:
            mapped = _map_row(header, header_cols, line)
            first = (line.cells[0] if line.cells else "").strip().lower()
            if not first and line.cells:
                first = next((c.strip().lower() for c in line.cells if c.strip()), "")
            if first.startswith("total"):
                _table_totals(out, info, page, line, mapped, collection)
                continue
            built = row_fn(mapped)
            if not built:
                continue
            key, data = built
            ev = evidence_from_lines(info, page.number, [line], "rules:table", 0.94, ocr=page.ocr)
            out.records.append(
                RecordState(
                    id=stable_id(info.id, collection, page.number, line.locator, key),
                    collection=collection, key=key, data=data, evidence=ev,
                )
            )
        return out

    # ------------------------------------------------------------------ loss runs
    def _loss_run(self, info: DocumentInfo, page: ParsedPage) -> ExtractionResult:
        out = self._table(info, page, CLAIM_COLUMNS, "claims", _claim_row, min_hits=3)
        out.notes = [n for n in out.notes if "no claims table" not in n]
        for line in page.lines:
            text = line.text
            low = text.lower()
            for cell in line.cells or [text]:
                lab, sep, val = cell.partition(":")
                if not sep:
                    continue
                labn = norm_label(lab)
                if re.fullmatch(r"valuation date|valued as of|as of( date)?|evaluation date|report date", labn):
                    d = parse_date(val)
                    if d and "valuation_date" not in out.meta:
                        out.meta["valuation_date"] = d.isoformat()
                        out.meta_evidence["valuation_date"] = evidence_from_lines(info, page.number, [line], "rules", 0.95, page.ocr)
                elif re.fullmatch(r"carrier|insurance company|insurer|issuing carrier", labn) and val.strip():
                    out.meta.setdefault("carrier", clean_text(val))
                elif re.fullmatch(r"policy (period|term)", labn) and val.strip():
                    out.meta.setdefault("policy_period", clean_text(val))
            if re.search(r"no (losses|claims) (reported|on file)|no loss activity", low):
                out.meta["no_losses_reported"] = True
                out.meta_evidence["no_losses_reported"] = evidence_from_lines(info, page.number, [line], "rules", 0.95, page.ocr)
        return out

    # ------------------------------------------------------------------ free text
    def _free_text(self, info: DocumentInfo, page: ParsedPage) -> ExtractionResult:
        out = ExtractionResult()
        lines = [ln for ln in page.lines if not re.match(r"^(From|To|Cc|Date|Subject):", ln.text)]
        joined, offsets = "", []
        for ln in lines:
            start = len(joined)
            joined += ln.text + " "
            offsets.append((start, len(joined), ln))

        def lines_for(a: int, b: int) -> list[TextLine]:
            return [ln for s, e, ln in offsets if s < b and e > a] or [lines[0]]

        def emit(path: str, raw: str, a: int, b: int, conf: float = 0.72) -> None:
            value = normalize_field(path, raw)
            if value is None:
                return
            ev_lines = lines_for(a, b)
            ev = evidence_from_lines(info, page.number, ev_lines, "rules:text", conf, page.ocr)
            out.candidates.append(
                Candidate(id=stable_id(info.id, path, ev.locator, raw, a), path=path, value=value, raw=raw, evidence=ev)
            )

        money = r"(\$\s?\d[\d,]*(?:\.\d+)?\s*(?:million|mm|m|k)?\b)"
        qual = r"(?:about|approximately|roughly|around|just over|just under|nearly)?\s*"
        for m in re.finditer(rf"\b(?:annual |total |gross )?(?:revenue|sales|gross receipts)\b[^.$\d]{{0,45}}?{qual}{money}", joined, re.I):
            emit("financials.annual_revenue", m.group(1), m.start(), m.end())
        for m in re.finditer(rf"\b(?:annual |total )?payroll\b[^.$\d]{{0,45}}?{qual}{money}", joined, re.I):
            emit("financials.total_payroll", m.group(1), m.start(), m.end())
        num = r"(\d{1,4}|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
        for kind, path in (("full", "workforce.full_time_employees"), ("part", "workforce.part_time_employees")):
            for m in re.finditer(rf"\b{num}\s+{kind}[- ]time\s+(?:employees|staff|workers|people)", joined, re.I):
                emit(path, m.group(1), m.start(), m.end())
            for m in re.finditer(rf"\b{kind}[- ]time\s+(?:employees|headcount|staff)\b[^.\d]{{0,25}}?\b(?:is|are|of|at|now|to|=|:)\s*{num}\b", joined, re.I):
                emit(path, m.group(1), m.start(), m.end())
        date_pat = r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})"
        for m in re.finditer(rf"\beffective(?: date)?(?: of| on| is| as of|:)?\s+{date_pat}", joined, re.I):
            emit("policy.effective_date", m.group(1), m.start(), m.end())
        for m in re.finditer(r"\b(?:FEIN|EIN|federal tax id)\b[^\d]{0,15}(\d{2}-?\d{7})\b", joined, re.I):
            emit("applicant.fein", m.group(1), m.start(), m.end())

        # Claim status statements, one sentence at a time.
        for a, b in _sentences(joined):
            sentence = joined[a:b]
            claims = re.findall(r"\bclaim\s*(?:#|no\.?|number)?\s*:?\s*([A-Z]{1,5}-?\d[\w-]{3,})", sentence, re.I)
            claims = list(dict.fromkeys(claims))
            status = re.findall(r"\b(closed|open|reopened|re-opened|settled|pending|still open)\b", sentence, re.I)
            if len(claims) == 1 and status:
                st = _claim_status(status[-1])
                ev = evidence_from_lines(info, page.number, lines_for(a, b), "rules:text", 0.72, page.ocr)
                key = _claim_key(claims[0])
                out.records.append(
                    RecordState(
                        id=stable_id(info.id, "claims", key, a), collection="claims", key=key,
                        data={"claim_number": key, "status": st}, evidence=ev, partial=True,
                    )
                )
        return out


# --------------------------------------------------------------------------- #
# Table helpers
# --------------------------------------------------------------------------- #

CLAIM_COLUMNS = {
    "claim_number": [r"claim( no\.?| number| #| id)?", r"claim#"],
    "date_of_loss": [r"date of loss", r"loss date", r"dol", r"accident date", r"date of accident"],
    "line": [r"line( of business)?", r"coverage", r"lob", r"policy type"],
    "description": [r"(loss |claim )?description", r"cause( of loss)?", r"loss type"],
    "paid": [r"(total |amount )?paid( loss)?", r"paid to date"],
    "reserve": [r"(outstanding |open )?reserves?", r"outstanding"],
    "incurred": [r"(total )?incurred"],
    "status": [r"(claim )?status", r"open/closed"],
}
PAYROLL_COLUMNS = {
    "state": [r"state", r"st"],
    "class_code": [r"(wc )?class( code)?", r"code", r"class #"],
    "description": [r"(class )?description", r"classification", r"job description", r"duties"],
    "employees": [r"(# |no\.? |number )?(of )?employees", r"headcount", r"# emp", r"full[- ]time employees"],
    "part_time": [r"part[- ]time( employees)?"],
    "payroll": [r"(estimated |annual |est\.? annual )*(payroll|remuneration)", r"annual payroll"],
}
VEHICLE_COLUMNS = {
    "year": [r"(model )?year", r"yr"],
    "make": [r"make"],
    "model": [r"model"],
    "vin": [r"vin", r"vehicle identification number", r"serial( number)?"],
    "garaging_zip": [r"garag(e|ing)( zip| zip code| location)?", r"zip( code)?"],
    "cost_new": [r"cost new", r"stated (amount|value)", r"value"],
    "use": [r"use", r"usage", r"radius"],
}
SOV_COLUMNS = {
    "address": [r"(location |street )?address", r"location", r"premises"],
    "city": [r"city"],
    "state": [r"state", r"st"],
    "zip": [r"zip( code)?"],
    "building_value": [r"building( value| limit)?"],
    "contents_value": [r"contents( value| limit)?", r"bpp", r"business personal property"],
    "year_built": [r"year built", r"yr built"],
    "construction": [r"construction( type)?"],
    "occupancy": [r"occupancy"],
}

_COL_CACHE: dict[int, dict[str, list[re.Pattern]]] = {}


def _col_patterns(columns: dict[str, list[str]]) -> dict[str, list[re.Pattern]]:
    key = id(columns)
    if key not in _COL_CACHE:
        _COL_CACHE[key] = {k: [re.compile(rf"^(?:{p})$") for p in v] for k, v in columns.items()}
    return _COL_CACHE[key]


def _match_column(text: str, columns: dict[str, list[str]]) -> str | None:
    lab = norm_label(text).replace("#", " #").strip()
    lab = re.sub(r"\s+", " ", lab)
    for col, pats in _col_patterns(columns).items():
        if any(p.match(lab) or p.match(norm_label(text)) for p in pats):
            return col
    return None


def _find_header(lines: list[TextLine], columns, min_hits: int) -> tuple[int | None, list[tuple[str | None, tuple[float, float]]]]:
    for i, line in enumerate(lines[:60]):
        cells = line.cells or []
        spans = line.cell_spans or [(float(k), float(k + 1)) for k in range(len(cells))]
        mapped: list[tuple[str | None, tuple[float, float]]] = []
        for k, c in enumerate(cells):
            name = _match_column(c, columns) if c.strip() else None
            if name is None and " " in c.strip():
                split = _split_header_cell(c, spans[k], columns)
                if split:
                    mapped.extend(split)
                    continue
            mapped.append((name, spans[k]))
        hits = {m for m, _ in mapped if m}
        if len(hits) >= min_hits:
            return i, mapped
    return None, []


def _split_header_cell(text: str, span: tuple[float, float], columns) -> list | None:
    """Adjacent headers printed close together ('Loss Date Coverage Cause') arrive
    as one cell. Split at word boundaries so that every part is a known column,
    dividing the horizontal span in proportion to character position."""
    t = text.strip()
    words = t.split()

    def solve(i: int) -> list[tuple[str, int, int]] | None:
        if i == len(words):
            return []
        for j in range(len(words), i, -1):  # prefer the longest matching phrase
            name = _match_column(" ".join(words[i:j]), columns)
            if name:
                rest = solve(j)
                if rest is not None:
                    return [(name, i, j), *rest]
        return None

    parts = solve(0)
    if not parts or len(parts) < 2 or len({p[0] for p in parts}) != len(parts):
        return None
    x0, x1 = span
    out, pos = [], 0
    for name, i, j in parts:
        start = t.index(words[i], pos)
        end = start + len(" ".join(words[i:j]))
        pos = end
        out.append((name, (x0 + (x1 - x0) * start / len(t), x0 + (x1 - x0) * end / len(t))))
    return out


def _map_row(header: TextLine, header_cols, line: TextLine) -> dict[str, str]:
    # Unrecognised header columns keep their own band so values beneath them
    # are dropped rather than spilling into a neighbouring known column.
    cols = list(header_cols)
    if not any(name for name, _ in cols):
        return {}
    centers = [(s[0] + s[1]) / 2 for _, s in cols]
    bands = []
    for k, (name, _) in enumerate(cols):
        lo = -1e9 if k == 0 else (centers[k - 1] + centers[k]) / 2
        hi = 1e9 if k == len(cols) - 1 else (centers[k] + centers[k + 1]) / 2
        bands.append((name, lo, hi))
    spans = line.cell_spans or [(float(k), float(k + 1)) for k in range(len(line.cells))]
    out: dict[str, str] = {}
    for cell, (x0, x1) in zip(line.cells, spans):
        if not cell.strip():
            continue
        best, best_ov = None, -1.0
        for name, lo, hi in bands:
            ov = min(x1, hi) - max(x0, lo)
            if ov > best_ov:
                best, best_ov = name, ov
        if best:
            out[best] = f"{out[best]} {cell}".strip() if best in out else cell.strip()
    return out


def _claim_key(raw: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", raw.upper())


def _claim_status(raw: str) -> str:
    s = raw.lower()
    if "reopen" in s or "re-open" in s:
        return "Reopened"
    if "closed" in s or "settled" in s:
        return "Closed"
    if "open" in s or "pending" in s:
        return "Open"
    return raw.strip().title()


def _claim_row(m: dict[str, str]) -> tuple[str, dict] | None:
    num = m.get("claim_number", "").strip()
    dol = parse_date(m.get("date_of_loss"))
    if not num or not dol or not re.search(r"\d", num):
        return None
    key = _claim_key(num)
    lines = parse_lines(m.get("line") or "")
    data = {
        "claim_number": key,
        "date_of_loss": dol.isoformat(),
        "line": lines[0] if lines else clean_text(m.get("line")),
        "description": clean_text(m.get("description")),
        "paid": parse_money(m.get("paid")) if m.get("paid") else None,
        "reserve": parse_money(m.get("reserve")) if m.get("reserve") else None,
        "incurred": parse_money(m.get("incurred")) if m.get("incurred") else None,
        "status": _claim_status(m["status"]) if m.get("status") else None,
    }
    return key, data


def _payroll_row(m: dict[str, str]) -> tuple[str, dict] | None:
    code = (m.get("class_code") or "").strip()
    payroll = parse_money(m.get("payroll")) if m.get("payroll") else None
    if not re.fullmatch(r"\d{3,4}", code) or payroll is None:
        return None
    state = (m.get("state") or "").strip().upper() or None
    data = {
        "state": state,
        "class_code": code,
        "description": clean_text(m.get("description")),
        "employees": parse_int(m.get("employees")) if m.get("employees") else None,
        "part_time": parse_int(m.get("part_time")) if m.get("part_time") else None,
        "payroll": payroll,
    }
    return f"{state or 'XX'}-{code}", data


def _vehicle_row(m: dict[str, str]) -> tuple[str, dict] | None:
    vin = re.sub(r"\s", "", (m.get("vin") or "")).upper()
    year = parse_int(m.get("year")) if m.get("year") else None
    if not vin and not (year and m.get("make")):
        return None
    if year is not None and not (1950 <= year <= 2100):
        return None
    data = {
        "year": year,
        "make": clean_text(m.get("make")),
        "model": clean_text(m.get("model")),
        "vin": vin or None,
        "garaging_zip": (re.search(r"\d{5}", m.get("garaging_zip") or "") or [None])[0],
        "cost_new": parse_money(m.get("cost_new")) if m.get("cost_new") else None,
        "use": clean_text(m.get("use")),
    }
    return vin or f"{year}-{data['make']}-{data['model']}", data


def _sov_row(m: dict[str, str]) -> tuple[str, dict] | None:
    addr = clean_text(m.get("address"))
    if not addr or not re.match(r"^\d", addr):
        return None
    data = {
        "address": addr,
        "city": clean_text(m.get("city")),
        "state": (m.get("state") or "").strip().upper() or None,
        "zip": (re.search(r"\d{5}", m.get("zip") or "") or [None])[0],
        "building_value": parse_money(m.get("building_value")) if m.get("building_value") else None,
        "contents_value": parse_money(m.get("contents_value")) if m.get("contents_value") else None,
        "year_built": parse_int(m.get("year_built")) if m.get("year_built") else None,
        "construction": clean_text(m.get("construction")),
    }
    return address_key(addr), data


def _table_totals(out: ExtractionResult, info, page, line, mapped, collection) -> None:
    ev = evidence_from_lines(info, page.number, [line], "rules:table", 0.94, page.ocr)
    if collection == "payroll":
        if mapped.get("payroll"):
            out.meta["stated_total_payroll"] = parse_money(mapped["payroll"])
            out.meta_evidence["stated_total_payroll"] = ev
        if mapped.get("employees"):
            out.meta["stated_total_employees"] = parse_int(mapped["employees"])
            out.meta_evidence["stated_total_employees"] = ev
    elif collection == "claims":
        for k in ("paid", "reserve", "incurred"):
            if mapped.get(k):
                out.meta[f"stated_total_{k}"] = parse_money(mapped[k])
                out.meta_evidence[f"stated_total_{k}"] = ev


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #

_STREET_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd", "lane": "ln",
    "suite": "ste", "highway": "hwy", "parkway": "pkwy", "court": "ct", "place": "pl", "circle": "cir",
    "north": "n", "south": "s", "east": "e", "west": "w", "terrace": "ter", "way": "way",
}


def address_key(addr: str) -> str:
    street = addr.split(",")[0].lower()
    street = re.sub(r"[^\w\s]", " ", street)
    words = [_STREET_ABBR.get(w, w) for w in street.split()]
    return " ".join(words)


_ADDR_TAIL = re.compile(r",\s*([^,]+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")


def _location_record(info: DocumentInfo, raw: str, ev) -> RecordState | None:
    if not re.match(r"^\d", raw):
        return None
    m = _ADDR_TAIL.search(raw)
    data = {
        "address": raw.split(",")[0].strip(),
        "city": m.group(1).strip() if m else None,
        "state": m.group(2) if m else None,
        "zip": m.group(3) if m else None,
        "full": raw,
    }
    key = address_key(raw)
    return RecordState(
        id=stable_id(info.id, "locations", key, ev.locator), collection="locations", key=key, data=data, evidence=ev
    )


_ABBREV = {"no", "inc", "llc", "co", "mr", "ms", "mrs", "st", "dr", "corp", "ltd", "approx", "vs", "e.g", "i.e"}


def _sentences(text: str) -> list[tuple[int, int]]:
    """Split into sentence spans without breaking on 'Claim No. 123' style abbreviations."""
    spans, start = [], 0
    for m in re.finditer(r"[.!?]+\s+", text):
        prev_word = re.findall(r"([A-Za-z.]+)$", text[start : m.start()])
        if prev_word and prev_word[0].lower().strip(".") in _ABBREV:
            continue
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


_NOISE_CELL = re.compile(r"^[\W_]+$")


def _value_cells(cells: list[str]) -> list[str]:
    out = []
    for c in cells:
        if _NOISE_CELL.match(c.strip() or "_"):
            continue  # stray punctuation / form rules picked up as a cell
        if _is_label_like(c):
            break
        out.append(c)
    return out


def _multi_cell_kind(path: str) -> bool:
    if path == "_location":
        return False
    return FIELD_SPECS[path].kind in ("lines", "entity", "bool")


def _vertically_adjacent(a: TextLine, b: TextLine) -> bool:
    if not a.bbox or not b.bbox:
        return True
    h = max(a.bbox[3] - a.bbox[1], 1.0)
    return 0 <= b.bbox[1] - a.bbox[3] <= 1.6 * h
