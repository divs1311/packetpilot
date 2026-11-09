"""Readiness gate and follow-up drafting.

A submission is *ready* only when:
  * no critical or warning issue is open (each must be resolved or waived with a note);
  * every field that has a value has been accepted or corrected by a human
    (high-confidence fields can be approved in bulk, but a person still clicks);
  * no field is escalated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .rules_engine import ISSUE_CATALOG
from .schema import DOC_TYPE_LABELS, FIELD_SPECS, REVIEWED_STATUSES, DocType, Issue, SubmissionState


@dataclass
class Readiness:
    ready: bool
    blockers: list[str] = field(default_factory=list)
    open_blocking_issues: int = 0
    fields_pending_review: list[str] = field(default_factory=list)
    fields_bulk_approvable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "blockers": self.blockers,
            "open_blocking_issues": self.open_blocking_issues,
            "fields_pending_review": self.fields_pending_review,
            "fields_bulk_approvable": self.fields_bulk_approvable,
        }


def assess(state: SubmissionState) -> Readiness:
    blocking: list[Issue] = [i for i in state.issues if i.blocking]
    pending = [p for p, f in state.fields.items() if f.value is not None and f.status not in REVIEWED_STATUSES]
    bulk = [p for p in pending if state.fields[p].status == "auto_verified"]
    blockers = [f"[{i.severity}] {ISSUE_CATALOG[i.code][2]}: {i.message}" for i in blocking]
    if pending:
        blockers.append(
            f"{len(pending)} extracted field(s) not yet approved by a reviewer"
            + (f" ({len(bulk)} can be bulk-approved)" if bulk else "")
        )
    return Readiness(
        ready=not blockers,
        blockers=blockers,
        open_blocking_issues=len(blocking),
        fields_pending_review=pending,
        fields_bulk_approvable=bulk,
    )


_SOURCE_PHRASE = {
    "application": "the application", "gl_supplement": "the liability supplement", "email": "your email",
    "financial_statement": "the financial statement", "loss_run": "the loss run", "payroll_schedule": "the payroll schedule",
    "vehicle_schedule": "the vehicle schedule", "property_sov": "the statement of values", "other": "another document",
}


def _sources(srcs: list[str]) -> str:
    names = [_SOURCE_PHRASE.get(s, s) for s in srcs]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def client_question(issue: Issue) -> str | None:
    """Client-facing wording for one open issue (no internal file names or jargon)."""
    d = issue.details or {}
    c = issue.code
    if c == "FIELD_CONFLICT" and d.get("values"):
        parts = [f"{v['value']} in {_sources(v['sources'])}" for v in d["values"]]
        return f"We received different figures for {d['label'].lower()}: " + "; ".join(parts) + ". Which is correct?"
    if c == "CLAIM_STATUS_CONFLICT" and d.get("statuses"):
        parts = [f"{s['status'].lower()} in {_SOURCE_PHRASE.get(s['source'], s['source'])}" for s in d["statuses"]]
        return f"Claim {d['claim']} is shown as " + " but ".join(parts) + ". Could you confirm its current status, or send an updated loss run?"
    if c == "CLAIM_NOT_IN_LOSS_RUN":
        return f"You mentioned claim {d.get('claim')}, which does not appear on the loss run we have. Could you send the loss run that includes it?"
    if c == "DUPLICATE_CLAIM":
        return f"The loss run appears to list the same claim more than once ({', '.join(d.get('claims', []))}). Could you ask the carrier for a corrected loss run, or confirm these are separate claims?"
    if c == "INVALID_LOSS_DATE":
        return f"Claim {d.get('claim')} shows a date of loss ({d.get('date')}) after the date of the report. Could you confirm the correct date?"
    if c == "CLAIM_RESERVE_INCONSISTENT":
        return f"Could you confirm whether claim {d.get('claim')} is still open, and if so the current reserve?"
    if c == "LOSS_DISCLOSURE_CONFLICT":
        return (f"The application answers 'No' to losses in the past five years, but the loss run shows {d.get('count')} claim(s). "
                "Could you confirm, and add a short description of each claim?")
    if c == "PAYROLL_SCHEDULE_MISMATCH":
        return f"The payroll schedule adds up to {d.get('schedule')}, but the application states {d.get('stated')}. Which annual payroll figure is correct?"
    if c == "EMPLOYEE_COUNT_MISMATCH":
        return f"The payroll schedule lists {d.get('schedule')} employees, but the application states {d.get('stated')}. How many employees do you have?"
    if c == "SCHEDULE_TOTAL_ARITHMETIC":
        return f"The total row on the payroll schedule does not match the individual {d.get('what', 'payroll')} rows. Could you send a corrected schedule?"
    if c == "VEHICLE_COUNT_MISMATCH":
        return f"The vehicle schedule lists {d.get('schedule')} vehicles, but the application says {d.get('stated')}. How many vehicles should we insure?"
    if c == "INVALID_VIN":
        return f"The VIN {d.get('vin')} ({d.get('vehicle')}) appears to contain a typo. Could you double-check it against the registration?"
    if c == "DUPLICATE_VIN":
        return f"VIN {d.get('vin')} is listed more than once on the vehicle schedule. Could you confirm the VINs for each vehicle?"
    if c == "LOCATION_NOT_IN_APPLICATION":
        return f"{d.get('address')} is on the statement of values but not on the application. Should it be included as an insured location?"
    if c == "INVALID_FEIN":
        return f"The FEIN provided ({d.get('fein')}) looks incomplete. Could you confirm your 9-digit FEIN?"
    if c == "EFFECTIVE_DATE_PAST":
        return f"The requested effective date ({d.get('effective')}) has already passed. What effective date would you like?"
    if c == "PRIOR_COVERAGE_GAP":
        return (f"Your current policy expires {d.get('expiration')}, but coverage is requested from {d.get('effective')}. "
                "Was there, or will there be, a gap in coverage?")
    if c == "PAYROLL_EXCEEDS_REVENUE":
        return f"Annual payroll ({d.get('payroll')}) is higher than annual revenue ({d.get('revenue')}). Could you confirm both figures?"
    return None


def draft_followup(state: SubmissionState, broker_name: str = "the PacketPilot team") -> dict:
    """Draft a client follow-up email from open data-quality issues.

    Deterministic on purpose: every request in it is generated from an open
    issue, so it cannot invent questions or promise anything.
    """
    open_issues = [i for i in state.issues if i.status == "open" and i.category in ("missing", "contradiction", "validity")]
    docs, info, clarify = [], [], []
    for i in open_issues:
        if i.code == "MISSING_DOCUMENT":
            docs.append(DOC_TYPE_LABELS[DocType(i.subject)])
        elif i.code == "STALE_LOSS_RUN":
            carrier = (i.details or {}).get("carrier")
            docs.append(f"A current loss run{' from ' + carrier if carrier else ''} (the one we have is valued {(i.details or {}).get('valued', 'over 90 days ago')})")
        elif i.code == "LOSS_RUN_UNDATED":
            docs.append("A loss run that shows its valuation date")
        elif i.code == "MISSING_REQUIRED_FIELD":
            info.append(FIELD_SPECS[i.field_path].label if i.field_path in FIELD_SPECS else str(i.field_path))
        elif i.code == "INCOMPLETE_EXPLANATION":
            info.append("A short description of the work you subcontract (trades involved, annual cost, and whether "
                        "subcontractors carry their own insurance)")
        else:
            clarify.append(client_question(i) or i.message)
    name = state.value("contact.name")
    first = name.split()[0] if name else "there"
    applicant = state.value("applicant.legal_name") or "your business"
    if not (docs or info or clarify):
        body = (f"Hi {first},\n\nThank you - we have everything we need for {applicant}'s submission and will be in touch "
                f"once we hear back from the insurance markets.\n\nBest regards,\n{broker_name}")
        return {"subject": f"{applicant} - submission complete ({state.reference})", "body": body, "items": 0}
    parts = [f"Hi {first},", "", f"Thank you for sending the information for {applicant}. Before we can take the submission "
             "to insurance markets, we need a few more items:"]
    n = 1
    for title, items in (("Documents we still need:", docs), ("Information we still need:", info), ("Please help us clarify:", clarify)):
        if not items:
            continue
        parts += ["", title]
        for d in dict.fromkeys(items):
            parts.append(f"  {n}. {d}")
            n += 1
    parts += ["", "Replying to this email with the documents or answers is perfect. Thank you!", "", "Best regards,", broker_name]
    return {"subject": f"{applicant} - a few items to complete your insurance submission ({state.reference})",
            "body": "\n".join(parts), "items": n - 1}
