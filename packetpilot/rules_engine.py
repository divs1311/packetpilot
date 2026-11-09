"""Deterministic validation rules.

The LLM (or rules extractor) proposes values; this module decides what is
wrong with the submission. Every rule is plain Python, unit-tested, and emits
``Issue`` objects with a stable fingerprint so that a reviewer's resolution
survives re-evaluation - and a *changed* condition (e.g. a new conflicting
value) re-opens as a new issue.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Callable, Iterable

from .config import Settings
from .extract.base import stable_id
from .extract.rules import address_key
from .normalize import FEIN_RE, values_equal
from .reconcile import cluster_candidates
from .schema import (
    DOC_TYPE_LABELS,
    FIELD_SPECS,
    LINE_LABELS,
    REQUIRED_DOCUMENTS,
    REVIEWED_STATUSES,
    DocType,
    Evidence,
    Issue,
    Line,
    RecordState,
    SubmissionState,
    utcnow,
)

# code -> (severity, category, human title)
ISSUE_CATALOG: dict[str, tuple[str, str, str]] = {
    "MISSING_REQUIRED_FIELD": ("critical", "missing", "Required field missing"),
    "MISSING_DOCUMENT": ("critical", "missing", "Required document missing"),
    "INCOMPLETE_EXPLANATION": ("critical", "missing", "Explanation missing"),
    "FIELD_CONFLICT": ("critical", "contradiction", "Sources disagree"),
    "CLAIM_STATUS_CONFLICT": ("critical", "contradiction", "Claim status disagrees across documents"),
    "PAYROLL_SCHEDULE_MISMATCH": ("critical", "contradiction", "Payroll schedule does not match stated payroll"),
    "EMPLOYEE_COUNT_MISMATCH": ("critical", "contradiction", "Employee schedule does not match stated headcount"),
    "VEHICLE_COUNT_MISMATCH": ("critical", "contradiction", "Vehicle schedule does not match stated vehicle count"),
    "LOSS_DISCLOSURE_CONFLICT": ("critical", "contradiction", "Application denies losses that the loss run shows"),
    "LOCATION_NOT_IN_APPLICATION": ("warning", "contradiction", "Scheduled location not on application"),
    "CLAIM_NOT_IN_LOSS_RUN": ("warning", "contradiction", "Claim mentioned but not on any loss run"),
    "INVALID_FEIN": ("critical", "validity", "FEIN format invalid"),
    "INVALID_VIN": ("critical", "validity", "VIN invalid"),
    "DUPLICATE_VIN": ("critical", "validity", "Duplicate VIN"),
    "DUPLICATE_CLAIM": ("warning", "validity", "Possible duplicate claim"),
    "INVALID_LOSS_DATE": ("critical", "validity", "Loss date after report valuation date"),
    "CLAIM_RESERVE_INCONSISTENT": ("warning", "validity", "Claim status and reserve inconsistent"),
    "STALE_LOSS_RUN": ("warning", "validity", "Loss run too old"),
    "LOSS_RUN_UNDATED": ("warning", "validity", "Loss run has no valuation date"),
    "EFFECTIVE_DATE_PAST": ("warning", "validity", "Effective date already passed"),
    "PRIOR_COVERAGE_GAP": ("warning", "validity", "Gap between prior expiration and new effective date"),
    "SCHEDULE_TOTAL_ARITHMETIC": ("critical", "validity", "Schedule total row does not add up"),
    "PAYROLL_EXCEEDS_REVENUE": ("warning", "validity", "Payroll exceeds revenue"),
    "LOW_CONFIDENCE": ("warning", "review", "Low-confidence extraction"),
    "UNGROUNDED_VALUE": ("critical", "review", "Value not found in cited source"),
    "UNPARSEABLE_VALUE": ("warning", "review", "Value present but not understood"),
    "EXTRACTION_DISAGREEMENT": ("warning", "review", "Extractors disagree within one document"),
    "FIELD_ESCALATED": ("critical", "review", "Escalated by reviewer"),
    "UNCLASSIFIED_DOCUMENT": ("warning", "document", "Document type not recognised"),
    "DOCUMENT_UNREADABLE": ("warning", "document", "No text could be read"),
    "DUPLICATE_DOCUMENT": ("info", "document", "Duplicate upload"),
}


def _fmt_money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.0f}"


def _fmt(path: str, v) -> str:
    kind = FIELD_SPECS[path].kind if path in FIELD_SPECS else "text"
    if v is None:
        return "—"
    if kind == "money":
        return _fmt_money(v)
    if kind == "lines":
        return ", ".join(LINE_LABELS.get(Line(x), x) for x in v)
    if kind == "bool":
        return "Yes" if v else "No"
    return str(v)


class _Ctx:
    def __init__(self, state: SubmissionState, settings: Settings):
        self.s = state
        self.settings = settings
        self.issues: list[Issue] = []

    def add(
        self,
        code: str,
        message: str,
        *,
        field_path: str | None = None,
        subject: str | None = None,
        evidence: Iterable[Evidence] = (),
        fingerprint: str = "",
        severity: str | None = None,
        status: str = "open",
        note: str | None = None,
        resolved_by: str | None = None,
        details: dict | None = None,
    ) -> None:
        sev, cat, _ = ISSUE_CATALOG[code]
        self.issues.append(
            Issue(
                id=stable_id(code, field_path, subject, fingerprint),
                code=code,
                severity=severity or sev,
                category=cat,
                field_path=field_path,
                subject=subject,
                message=message,
                evidence=list(evidence),
                status=status,
                auto_resolved=status != "open",
                resolution_note=note,
                resolved_by=resolved_by,
                resolved_at=utcnow() if status != "open" else None,
                details=details or {},
            )
        )

    def field_ev(self, path: str) -> list[Evidence]:
        f = self.s.fields.get(path)
        if not f:
            return []
        sel = f.selected
        return [sel.evidence] if sel else []

    def as_of(self) -> date:
        return self.s.as_of

    def date_value(self, path: str) -> date | None:
        v = self.s.value(path)
        return date.fromisoformat(v) if v else None


# --------------------------------------------------------------------------- #
# Field-level rules
# --------------------------------------------------------------------------- #


def rule_required_fields(ctx: _Ctx) -> None:
    lines = ctx.s.lines()
    for spec in FIELD_SPECS.values():
        f = ctx.s.fields.get(spec.path)
        if spec.is_required(lines) and (f is None or f.value is None) and (f is None or f.status != "escalated"):
            why = "all submissions" if "ALL" in spec.required_for else ", ".join(
                LINE_LABELS[Line(x)] for x in spec.required_for if x in lines
            )
            found = f is not None and f.candidates
            ctx.add(
                "MISSING_REQUIRED_FIELD",
                f"{spec.label} is required for {why} but "
                + ("its value could not be read from the source (see evidence)." if found else "was not found in any document."),
                field_path=spec.path,
                evidence=[c.evidence for c in f.candidates[:2]] if found else [],
                details={"label": spec.label},
            )


def rule_conflicts(ctx: _Ctx) -> None:
    for path, f in ctx.s.fields.items():
        clusters = cluster_candidates(path, f.candidates, ctx.settings.money_tolerance)
        if len(clusters) < 2:
            continue
        spec = FIELD_SPECS[path]
        evidence = [min(cl.members, key=lambda c: -c.evidence.confidence).evidence for cl in clusters]
        values = sorted(_fmt(path, cl.value) for cl in clusters)
        docs = {c.evidence.document_id for cl in clusters for c in cl.members}
        fingerprint = "|".join(values)
        reviewed = f.status in REVIEWED_STATUSES
        status = "resolved" if reviewed else "open"
        note = f"Reviewer set value to {_fmt(path, f.value)}" if reviewed else None
        if len(docs) == 1:
            ctx.add(
                "EXTRACTION_DISAGREEMENT",
                f"{spec.label}: extraction methods read different values from the same document ({'; '.join(values)}).",
                field_path=path, evidence=evidence, fingerprint=fingerprint, status=status, note=note,
                resolved_by=f.reviewed_by if reviewed else None,
            )
            continue
        parts = []
        for cl in clusters:
            srcs = sorted({f"{c.evidence.filename} p{c.evidence.page}" for c in cl.members})
            parts.append(f"{_fmt(path, cl.value)} ({', '.join(srcs)})")
        ctx.add(
            "FIELD_CONFLICT",
            f"{spec.label} differs between documents: " + " vs ".join(parts) + ".",
            field_path=path, evidence=evidence, fingerprint=fingerprint,
            severity="critical" if spec.critical else "warning",
            status=status, note=note, resolved_by=f.reviewed_by if reviewed else None,
            details={"label": spec.label, "values": [
                {"value": _fmt(path, cl.value), "sources": sorted({c.evidence.doc_type for c in cl.members})} for cl in clusters
            ]},
        )


def rule_trust(ctx: _Ctx) -> None:
    for path, f in ctx.s.fields.items():
        spec = FIELD_SPECS[path]
        if f.status == "escalated":
            ctx.add("FIELD_ESCALATED", f"{spec.label} escalated by {f.reviewed_by}: {f.review_note or 'no note'}",
                    field_path=path, evidence=ctx.field_ev(path), fingerprint=str(f.reviewed_at))
            continue
        if f.status != "low_confidence":
            continue
        if f.value is None and f.candidates:
            ctx.add("UNPARSEABLE_VALUE",
                    f"{spec.label}: found text '{f.candidates[0].raw}' but could not interpret it as {spec.kind}.",
                    field_path=path, evidence=[f.candidates[0].evidence])
            continue
        sel = f.selected
        if sel is not None and not sel.evidence.grounded:
            ctx.add("UNGROUNDED_VALUE",
                    f"{spec.label}: proposed value {_fmt(path, f.value)} could not be verified in the cited source text.",
                    field_path=path, evidence=[sel.evidence], severity="critical" if spec.critical else "warning",
                    fingerprint=str(f.value))
        else:
            ctx.add("LOW_CONFIDENCE",
                    f"{spec.label}: extracted {_fmt(path, f.value)} with confidence {f.confidence:.0%}; verify against source.",
                    field_path=path, evidence=ctx.field_ev(path), fingerprint=str(f.value))


def rule_fein(ctx: _Ctx) -> None:
    v = ctx.s.value("applicant.fein")
    if v and not FEIN_RE.match(v):
        ctx.add("INVALID_FEIN", f"FEIN '{v}' is not a valid 9-digit employer identification number (XX-XXXXXXX).",
                field_path="applicant.fein", evidence=ctx.field_ev("applicant.fein"), fingerprint=v, details={"fein": v})


def rule_dates(ctx: _Ctx) -> None:
    eff = ctx.date_value("policy.effective_date")
    if eff and eff < ctx.as_of():
        ctx.add("EFFECTIVE_DATE_PAST",
                f"Proposed effective date {eff:%m/%d/%Y} is before the submission date {ctx.as_of():%m/%d/%Y}.",
                field_path="policy.effective_date", evidence=ctx.field_ev("policy.effective_date"), fingerprint=eff.isoformat(),
                details={"effective": f"{eff:%m/%d/%Y}"})
    exp = ctx.date_value("prior.expiration_date")
    if eff and exp and exp < eff:
        gap = (eff - exp).days
        ctx.add("PRIOR_COVERAGE_GAP",
                f"Prior policy expires {exp:%m/%d/%Y} but new coverage starts {eff:%m/%d/%Y} — a {gap}-day gap in coverage.",
                field_path="prior.expiration_date",
                evidence=ctx.field_ev("prior.expiration_date") + ctx.field_ev("policy.effective_date"),
                fingerprint=f"{exp}|{eff}", details={"expiration": f"{exp:%m/%d/%Y}", "effective": f"{eff:%m/%d/%Y}", "days": gap})


def rule_explanations(ctx: _Ctx) -> None:
    if ctx.s.value("gl.subcontractors_used") is True and not ctx.s.value("gl.subcontractor_explanation"):
        ctx.add("INCOMPLETE_EXPLANATION",
                "Applicant answered 'Yes' to using subcontractors but did not describe the subcontracted work.",
                field_path="gl.subcontractor_explanation", evidence=ctx.field_ev("gl.subcontractors_used"))


def rule_payroll_vs_revenue(ctx: _Ctx) -> None:
    pay, rev = ctx.s.value("financials.total_payroll"), ctx.s.value("financials.annual_revenue")
    if pay and rev and pay > rev:
        ctx.add("PAYROLL_EXCEEDS_REVENUE",
                f"Stated payroll {_fmt_money(pay)} exceeds stated annual revenue {_fmt_money(rev)}.",
                field_path="financials.total_payroll",
                evidence=ctx.field_ev("financials.total_payroll") + ctx.field_ev("financials.annual_revenue"),
                fingerprint=f"{pay}|{rev}", details={"payroll": _fmt_money(pay), "revenue": _fmt_money(rev)})


# --------------------------------------------------------------------------- #
# Document rules
# --------------------------------------------------------------------------- #


def rule_documents(ctx: _Ctx) -> None:
    lines = ctx.s.lines()
    needed: list[DocType] = list(REQUIRED_DOCUMENTS["ALL"])
    for ln in sorted(lines):
        needed.extend(REQUIRED_DOCUMENTS.get(ln, ()))
    for dtype in dict.fromkeys(needed):
        if not ctx.s.docs_of(dtype):
            reason = "every submission" if dtype in REQUIRED_DOCUMENTS["ALL"] else next(
                LINE_LABELS[Line(ln)] for ln in sorted(lines) if dtype in REQUIRED_DOCUMENTS.get(ln, ())
            )
            ctx.add("MISSING_DOCUMENT", f"No {DOC_TYPE_LABELS[dtype].lower()} was provided (required for {reason}).",
                    subject=dtype.value, details={"document": DOC_TYPE_LABELS[dtype]})
    for d in ctx.s.documents:
        if d.duplicate_of:
            ctx.add("DUPLICATE_DOCUMENT", f"{d.filename} is an exact duplicate of another uploaded file and was ignored.",
                    subject=d.id)
            continue
        if d.doc_type == DocType.OTHER.value:
            ctx.add("UNCLASSIFIED_DOCUMENT", f"{d.filename} could not be classified; confirm what it is or remove it.",
                    subject=d.id)
        if d.page_count == 0 or d.meta.get("_empty_text"):
            ctx.add("DOCUMENT_UNREADABLE", f"No text could be read from {d.filename}. {' '.join(d.parse_warnings)}".strip(),
                    subject=d.id)


# --------------------------------------------------------------------------- #
# Claims / loss history
# --------------------------------------------------------------------------- #


def rule_claims(ctx: _Ctx) -> None:
    claims = ctx.s.records_of("claims")
    full = [r for r in claims if not r.partial]
    docs = {d.id: d for d in ctx.s.documents}
    by_key: dict[str, list[RecordState]] = defaultdict(list)
    for r in claims:
        by_key[r.key].append(r)

    # duplicates within one document (same claim number twice)
    per_doc: dict[tuple[str, str], list[RecordState]] = defaultdict(list)
    for r in full:
        per_doc[(r.evidence.document_id, r.key)].append(r)
    for (doc_id, key), rows in per_doc.items():
        if len(rows) > 1:
            ctx.add("DUPLICATE_CLAIM", f"Claim {key} appears {len(rows)} times in {rows[0].evidence.filename}.",
                    subject=key, evidence=[r.evidence for r in rows], fingerprint=str(len(rows)), details={"claims": [key]})
    # same loss, different claim numbers
    sig: dict[tuple, list[RecordState]] = defaultdict(list)
    for r in full:
        if r.data.get("date_of_loss") and r.data.get("paid"):
            sig[(r.data["date_of_loss"], r.data.get("paid"), (r.data.get("description") or "").lower())].append(r)
    for rows in sig.values():
        keys = sorted({r.key for r in rows})
        if len(keys) > 1:
            ctx.add("DUPLICATE_CLAIM",
                    f"Claims {', '.join(keys)} share the same loss date, amount and description — possible duplicate.",
                    subject="/".join(keys), evidence=[r.evidence for r in rows], details={"claims": keys})

    loss_run_keys = {r.key for r in full if r.evidence.doc_type == DocType.LOSS_RUN.value}
    for key, rows in by_key.items():
        statuses = {r.data.get("status") for r in rows if r.data.get("status")}
        if len(statuses) > 1:
            detail = "; ".join(f"{r.data.get('status')} in {r.evidence.filename}" for r in rows if r.data.get("status"))
            ctx.add("CLAIM_STATUS_CONFLICT", f"Claim {key} status differs: {detail}.", subject=key,
                    evidence=[r.evidence for r in rows], fingerprint="|".join(sorted(statuses)),
                    details={"claim": key, "statuses": [{"status": r.data.get("status"), "source": r.evidence.doc_type}
                                                        for r in rows if r.data.get("status")]})
        if ctx.s.docs_of(DocType.LOSS_RUN) and key not in loss_run_keys and all(r.partial for r in rows):
            ctx.add("CLAIM_NOT_IN_LOSS_RUN",
                    f"Claim {key} is referenced in {rows[0].evidence.filename} but does not appear on any loss run.",
                    subject=key, evidence=[r.evidence for r in rows], details={"claim": key})

    for r in full:
        dol = date.fromisoformat(r.data["date_of_loss"]) if r.data.get("date_of_loss") else None
        doc = docs.get(r.evidence.document_id)
        val = doc.meta.get("valuation_date") if doc else None
        limit = date.fromisoformat(val) if val else ctx.as_of()
        if dol and dol > min(limit, ctx.as_of()):
            ctx.add("INVALID_LOSS_DATE",
                    f"Claim {r.key} has date of loss {dol:%m/%d/%Y}, after the report valuation/submission date {min(limit, ctx.as_of()):%m/%d/%Y}.",
                    subject=r.key, evidence=[r.evidence], fingerprint=str(dol), details={"claim": r.key, "date": f"{dol:%m/%d/%Y}"})
        status, reserve = r.data.get("status"), r.data.get("reserve")
        if status in ("Open", "Reopened") and reserve is not None and reserve == 0:
            ctx.add("CLAIM_RESERVE_INCONSISTENT", f"Claim {r.key} is {status.lower()} but carries a $0 reserve.",
                    subject=r.key, evidence=[r.evidence], fingerprint=f"{status}|{reserve}", details={"claim": r.key})
        elif status == "Closed" and reserve:
            ctx.add("CLAIM_RESERVE_INCONSISTENT",
                    f"Claim {r.key} is closed but still shows an outstanding reserve of {_fmt_money(reserve)}.",
                    subject=r.key, evidence=[r.evidence], fingerprint=f"{status}|{reserve}", details={"claim": r.key})

    disclosed = ctx.s.value("loss_history.prior_losses_disclosed")
    if disclosed is False:
        horizon = ctx.as_of() - timedelta(days=5 * 365 + 1)
        recent = [r for r in full if r.data.get("date_of_loss") and date.fromisoformat(r.data["date_of_loss"]) >= horizon]
        if recent:
            ctx.add("LOSS_DISCLOSURE_CONFLICT",
                    f"Application answers 'No' to prior losses, but loss runs show {len(recent)} claim(s) in the past five years.",
                    field_path="loss_history.prior_losses_disclosed",
                    evidence=ctx.field_ev("loss_history.prior_losses_disclosed") + [r.evidence for r in recent[:3]],
                    fingerprint=str(len(recent)), details={"count": len(recent)})

    for d in ctx.s.docs_of(DocType.LOSS_RUN):
        val = d.meta.get("valuation_date")
        ev = [d.meta_evidence["valuation_date"]] if "valuation_date" in d.meta_evidence else []
        if not val:
            ctx.add("LOSS_RUN_UNDATED", f"{d.filename} has no valuation date, so its currency cannot be confirmed.", subject=d.id,
                    details={"carrier": d.meta.get("carrier")})
            continue
        age = (ctx.as_of() - date.fromisoformat(val)).days
        if age > ctx.settings.loss_run_max_age_days:
            ctx.add("STALE_LOSS_RUN",
                    f"{d.filename} is valued {date.fromisoformat(val):%m/%d/%Y} — {age} days old (limit {ctx.settings.loss_run_max_age_days}). Request a current loss run.",
                    subject=d.id, evidence=ev, fingerprint=val,
                    details={"carrier": d.meta.get("carrier"), "valued": f"{date.fromisoformat(val):%m/%d/%Y}"})


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #


def rule_payroll(ctx: _Ctx) -> None:
    rows = [r for r in ctx.s.records_of("payroll") if not r.partial]
    if not rows:
        return
    tol = ctx.settings.money_tolerance
    total = round(sum(r.data.get("payroll") or 0 for r in rows), 2)
    row_ev = [r.evidence for r in rows]
    stated = ctx.s.value("financials.total_payroll")
    if stated is not None and not values_equal("money", total, stated, tol):
        ctx.add("PAYROLL_SCHEDULE_MISMATCH",
                f"Payroll schedule rows total {_fmt_money(total)}, but stated total payroll is {_fmt_money(stated)} "
                f"(difference {_fmt_money(abs(total - stated))}).",
                field_path="financials.total_payroll", evidence=ctx.field_ev("financials.total_payroll") + row_ev[:3],
                fingerprint=f"{total}|{stated}", details={"schedule": _fmt_money(total), "stated": _fmt_money(stated)})
    for d in ctx.s.docs_of(DocType.PAYROLL_SCHEDULE):
        doc_rows = [r for r in rows if r.evidence.document_id == d.id]
        st = d.meta.get("stated_total_payroll")
        if st is not None and doc_rows:
            s = round(sum(r.data.get("payroll") or 0 for r in doc_rows), 2)
            if abs(s - st) > 1.0:
                ctx.add("SCHEDULE_TOTAL_ARITHMETIC",
                        f"{d.filename}: rows add up to {_fmt_money(s)} but the total row says {_fmt_money(st)}.",
                        subject=f"{d.id}:payroll", evidence=[d.meta_evidence["stated_total_payroll"]], fingerprint=f"{s}|{st}",
                        details={"what": "payroll"})
        ste = d.meta.get("stated_total_employees")
        if ste is not None and doc_rows and all(r.data.get("employees") is not None for r in doc_rows):
            s = sum(r.data["employees"] + (r.data.get("part_time") or 0) for r in doc_rows)
            if s != ste:
                ctx.add("SCHEDULE_TOTAL_ARITHMETIC",
                        f"{d.filename}: employee rows add up to {s} but the total row says {ste}.",
                        subject=f"{d.id}:employees", evidence=[d.meta_evidence["stated_total_employees"]], fingerprint=f"{s}|{ste}",
                        details={"what": "employee"})
    if all(r.data.get("employees") is not None for r in rows):
        sched = sum(r.data["employees"] + (r.data.get("part_time") or 0) for r in rows)
        ft = ctx.s.value("workforce.full_time_employees")
        pt_raw = ctx.s.value("workforce.part_time_employees")
        pt = pt_raw or 0
        # If part-time headcount is unknown, a schedule larger than the full-time
        # count may simply include part-timers: not a contradiction we can assert.
        ambiguous = pt_raw is None and ft is not None and sched > ft
        if ft is not None and sched != ft + pt and not ambiguous:
            ctx.add("EMPLOYEE_COUNT_MISMATCH",
                    f"Payroll schedule lists {sched} employees; application states {ft} full-time + {pt} part-time = {ft + pt}.",
                    field_path="workforce.full_time_employees",
                    evidence=ctx.field_ev("workforce.full_time_employees") + row_ev[:3], fingerprint=f"{sched}|{ft}|{pt}",
                    details={"schedule": sched, "stated": ft + pt})


_VIN_MAP = {**{str(i): i for i in range(10)}, **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))),
            "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def vin_problem(vin: str) -> str | None:
    if len(vin) != 17:
        return f"has {len(vin)} characters (expected 17)"
    if re.search(r"[IOQ]", vin):
        return "contains I, O or Q, which are not used in VINs"
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        return "contains invalid characters"
    total = sum(_VIN_MAP[c] * w for c, w in zip(vin, _VIN_WEIGHTS))
    check = total % 11
    expected = "X" if check == 10 else str(check)
    if vin[8] != expected:
        return f"fails the check-digit test (position 9 is '{vin[8]}', expected '{expected}')"
    return None


def rule_vehicles(ctx: _Ctx) -> None:
    rows = [r for r in ctx.s.records_of("vehicles") if not r.partial]
    stated = ctx.s.value("auto.vehicle_count")
    if rows and stated is not None and len(rows) != stated:
        ctx.add("VEHICLE_COUNT_MISMATCH",
                f"Vehicle schedule lists {len(rows)} vehicles; application states {stated}.",
                field_path="auto.vehicle_count", evidence=ctx.field_ev("auto.vehicle_count") + [r.evidence for r in rows[:2]],
                fingerprint=f"{len(rows)}|{stated}", details={"schedule": len(rows), "stated": stated})
    seen: dict[str, list[RecordState]] = defaultdict(list)
    for r in rows:
        vin = r.data.get("vin")
        if not vin:
            continue
        seen[vin].append(r)
        problem = vin_problem(vin)
        if problem:
            ctx.add("INVALID_VIN", f"VIN {vin} ({r.data.get('year') or ''} {r.data.get('make') or ''}) {problem}.",
                    subject=vin, evidence=[r.evidence],
                    details={"vin": vin, "vehicle": f"{r.data.get('year') or ''} {r.data.get('make') or ''} {r.data.get('model') or ''}".strip()})
    for vin, rs in seen.items():
        if len(rs) > 1:
            ctx.add("DUPLICATE_VIN", f"VIN {vin} is listed {len(rs)} times on the vehicle schedule.", subject=vin,
                    evidence=[r.evidence for r in rs], fingerprint=str(len(rs)), details={"vin": vin})


def rule_locations(ctx: _Ctx) -> None:
    prop = [r for r in ctx.s.records_of("property") if not r.partial]
    if not prop:
        return
    known = {r.key for r in ctx.s.records_of("locations")}
    mailing = ctx.s.value("applicant.mailing_address")
    if mailing:
        known.add(address_key(mailing))
    if not known:
        return
    for r in prop:
        if r.key not in known:
            ctx.add("LOCATION_NOT_IN_APPLICATION",
                    f"{r.data.get('address')} appears on the statement of values but not on the application.",
                    subject=r.key, evidence=[r.evidence], details={"address": r.data.get("address")})


RULES: list[Callable[[_Ctx], None]] = [
    rule_required_fields, rule_conflicts, rule_trust, rule_fein, rule_dates, rule_explanations,
    rule_payroll_vs_revenue, rule_documents, rule_claims, rule_payroll, rule_vehicles, rule_locations,
]


def evaluate(state: SubmissionState, settings: Settings) -> list[Issue]:
    ctx = _Ctx(state, settings)
    for rule in RULES:
        rule(ctx)
    # de-duplicate identical fingerprints (e.g. two rules flagging the same subject)
    unique: dict[str, Issue] = {}
    for i in ctx.issues:
        unique.setdefault(i.id, i)
    return list(unique.values())


def apply_issues(state: SubmissionState, fresh: list[Issue]) -> None:
    """Merge freshly computed issues into the state, preserving human decisions."""
    old = {i.id: i for i in state.issues}
    merged: list[Issue] = []
    fresh_ids = set()
    for new in fresh:
        fresh_ids.add(new.id)
        prev = old.get(new.id)
        if prev is None:
            merged.append(new)
        elif prev.status in ("waived", "resolved") and not prev.auto_resolved:
            # a human closed it: keep their decision, refresh text/evidence
            merged.append(new.model_copy(update={
                "status": prev.status, "auto_resolved": False, "resolution_note": prev.resolution_note,
                "resolved_by": prev.resolved_by, "resolved_at": prev.resolved_at, "first_seen_at": prev.first_seen_at,
            }))
        else:
            merged.append(new.model_copy(update={"first_seen_at": prev.first_seen_at}))
    for prev in state.issues:
        if prev.id in fresh_ids:
            continue
        if prev.status == "open":
            prev = prev.model_copy(update={
                "status": "resolved", "auto_resolved": True, "resolved_at": utcnow(),
                "resolution_note": "Condition no longer present after data changed.",
            })
        merged.append(prev)
    state.issues = merged
