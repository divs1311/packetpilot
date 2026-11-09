"""Canonical submission schema.

Two layers live here:

* ``FIELD_SPECS`` - the catalogue of scalar business fields PacketPilot extracts,
  with their data kind, criticality and which coverage lines require them.
* Pydantic domain models (``Evidence``, ``Candidate``, ``FieldState``,
  ``RecordState``, ``Issue``, ``DocumentInfo``, ``SubmissionState``) - the
  aggregate the pipeline produces, the reviewer edits and the database stores.

Every value that reaches a reviewer carries an ``Evidence`` object pointing at
the source document, page, verbatim text span and (where available) the
bounding box on the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


class Line(str, Enum):
    GL = "GL"  # General liability
    AUTO = "AUTO"  # Commercial auto
    WC = "WC"  # Workers compensation
    PROPERTY = "PROPERTY"  # Commercial property


LINE_LABELS = {
    Line.GL: "General Liability",
    Line.AUTO: "Commercial Auto",
    Line.WC: "Workers Compensation",
    Line.PROPERTY: "Commercial Property",
}


class DocType(str, Enum):
    APPLICATION = "application"
    GL_SUPPLEMENT = "gl_supplement"
    LOSS_RUN = "loss_run"
    PAYROLL_SCHEDULE = "payroll_schedule"
    VEHICLE_SCHEDULE = "vehicle_schedule"
    PROPERTY_SOV = "property_sov"
    FINANCIAL_STATEMENT = "financial_statement"
    EMAIL = "email"
    OTHER = "other"


DOC_TYPE_LABELS = {
    DocType.APPLICATION: "Commercial application",
    DocType.GL_SUPPLEMENT: "General liability supplement",
    DocType.LOSS_RUN: "Loss run",
    DocType.PAYROLL_SCHEDULE: "Payroll / employee schedule",
    DocType.VEHICLE_SCHEDULE: "Vehicle schedule",
    DocType.PROPERTY_SOV: "Property statement of values",
    DocType.FINANCIAL_STATEMENT: "Financial statement",
    DocType.EMAIL: "Email / correspondence",
    DocType.OTHER: "Unclassified",
}

FieldKind = Literal["text", "money", "int", "date", "fein", "entity", "lines", "bool", "email", "phone", "naics"]


@dataclass(frozen=True)
class FieldSpec:
    path: str
    label: str
    kind: FieldKind
    critical: bool = False
    # Lines of business that make this field required; "ALL" = always required.
    required_for: tuple[str, ...] = ()
    # Preferred sources when values disagree (first wins). Used only to choose a
    # *proposed* value - a conflict still blocks readiness until a human decides.
    source_priority: tuple[DocType, ...] = (DocType.APPLICATION,)

    def is_required(self, lines: set[str]) -> bool:
        if "ALL" in self.required_for:
            return True
        return bool(lines.intersection(self.required_for))


_APP_FIRST = (DocType.APPLICATION, DocType.GL_SUPPLEMENT, DocType.FINANCIAL_STATEMENT, DocType.EMAIL)

FIELD_SPECS: dict[str, FieldSpec] = {
    s.path: s
    for s in [
        FieldSpec("applicant.legal_name", "Legal name", "text", True, ("ALL",), _APP_FIRST),
        FieldSpec("applicant.dba", "DBA / trade name", "text"),
        FieldSpec("applicant.fein", "FEIN", "fein", True, ("ALL",), _APP_FIRST),
        FieldSpec("applicant.entity_type", "Entity type", "entity", False, ("ALL",)),
        FieldSpec("applicant.mailing_address", "Mailing address", "text", False, ("ALL",)),
        FieldSpec("applicant.years_in_business", "Years in business", "int"),
        FieldSpec("applicant.naics", "NAICS code", "naics"),
        FieldSpec("contact.name", "Contact name", "text"),
        FieldSpec("contact.email", "Contact email", "email"),
        FieldSpec("contact.phone", "Contact phone", "phone"),
        FieldSpec("operations.description", "Description of operations", "text", True, ("ALL",),
                  (DocType.APPLICATION, DocType.GL_SUPPLEMENT)),
        FieldSpec("financials.annual_revenue", "Annual revenue", "money", True, ("ALL",),
                  (DocType.APPLICATION, DocType.FINANCIAL_STATEMENT, DocType.GL_SUPPLEMENT, DocType.EMAIL)),
        FieldSpec("financials.total_payroll", "Total annual payroll", "money", True, (Line.WC.value,),
                  (DocType.APPLICATION, DocType.FINANCIAL_STATEMENT, DocType.EMAIL)),
        FieldSpec("workforce.full_time_employees", "Full-time employees", "int", True, ("ALL",),
                  (DocType.APPLICATION, DocType.EMAIL)),
        FieldSpec("workforce.part_time_employees", "Part-time employees", "int", False, (),
                  (DocType.APPLICATION, DocType.EMAIL)),
        FieldSpec("policy.effective_date", "Proposed effective date", "date", True, ("ALL",),
                  (DocType.APPLICATION, DocType.EMAIL)),
        FieldSpec("policy.lines_requested", "Lines of coverage requested", "lines", True, ("ALL",)),
        FieldSpec("prior.carrier", "Prior / current carrier", "text"),
        FieldSpec("prior.expiration_date", "Prior policy expiration", "date"),
        FieldSpec("loss_history.prior_losses_disclosed", "Losses disclosed (past 5 yrs)", "bool", False, ("ALL",)),
        FieldSpec("auto.vehicle_count", "Number of vehicles", "int", True, (Line.AUTO.value,)),
        FieldSpec("gl.subcontractors_used", "Uses subcontractors", "bool", False, (Line.GL.value,),
                  (DocType.GL_SUPPLEMENT, DocType.APPLICATION)),
        FieldSpec("gl.subcontractor_explanation", "Subcontractor explanation", "text", False, (),
                  (DocType.GL_SUPPLEMENT, DocType.APPLICATION)),
        FieldSpec("gl.subcontracted_cost", "Annual subcontracted cost", "money", False, (),
                  (DocType.GL_SUPPLEMENT, DocType.APPLICATION)),
    ]
}

COLLECTIONS = ("claims", "vehicles", "payroll", "locations", "property")

# Documents each line of business needs before a submission can go to market.
REQUIRED_DOCUMENTS: dict[str, tuple[DocType, ...]] = {
    "ALL": (DocType.APPLICATION, DocType.LOSS_RUN),
    Line.WC.value: (DocType.PAYROLL_SCHEDULE,),
    Line.AUTO.value: (DocType.VEHICLE_SCHEDULE,),
    Line.PROPERTY.value: (DocType.PROPERTY_SOV,),
}


# --------------------------------------------------------------------------- #
# Domain models
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Evidence(BaseModel):
    document_id: str
    filename: str
    doc_type: str
    page: int = Field(ge=1, description="1-based page (sheet index for spreadsheets)")
    locator: str | None = Field(None, description="Human readable locator e.g. 'line 14' or 'Payroll!B7'")
    text: str = Field(description="Verbatim source text the value was read from")
    bbox: list[float] | None = Field(None, description="x0,y0,x1,y1 in PDF points, if known")
    method: str = "rules"
    confidence: float = Field(ge=0.0, le=1.0)
    grounded: bool = Field(True, description="Span verified to exist verbatim in the parsed source")


class Candidate(BaseModel):
    id: str
    path: str
    value: Any
    raw: str
    evidence: Evidence


FieldStatus = Literal[
    "missing",  # nothing extracted
    "extracted",  # extracted, needs a human look
    "low_confidence",  # extracted but below confidence threshold / ungrounded
    "conflict",  # sources disagree
    "auto_verified",  # high confidence, grounded, consistent - eligible for bulk approval
    "accepted",  # human accepted
    "corrected",  # human corrected
    "escalated",  # human escalated to senior / account executive
]

REVIEWED_STATUSES = {"accepted", "corrected"}


class FieldState(BaseModel):
    path: str
    value: Any = None
    status: FieldStatus = "missing"
    confidence: float = 0.0
    selected_candidate_id: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    original_value: Any = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None

    @property
    def selected(self) -> Candidate | None:
        for c in self.candidates:
            if c.id == self.selected_candidate_id:
                return c
        return None


RecordStatus = Literal["extracted", "accepted", "corrected", "rejected"]


class RecordState(BaseModel):
    id: str
    collection: Literal["claims", "vehicles", "payroll", "locations", "property"]
    key: str
    data: dict[str, Any]
    evidence: Evidence
    status: RecordStatus = "extracted"
    # Partial records (e.g. an email stating only a claim's status) do not count
    # as a full schedule row but still participate in reconciliation.
    partial: bool = False


Severity = Literal["critical", "warning", "info"]
IssueStatus = Literal["open", "resolved", "waived"]


class Issue(BaseModel):
    id: str
    code: str
    severity: Severity
    category: Literal["contradiction", "missing", "validity", "review", "document"]
    field_path: str | None = None
    subject: str | None = Field(None, description="Stable subject key (claim number, VIN, doc type ...)")
    message: str
    evidence: list[Evidence] = Field(default_factory=list)
    status: IssueStatus = "open"
    auto_resolved: bool = False
    resolution_note: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    details: dict[str, Any] = Field(default_factory=dict, description="Structured facts for client-facing wording")

    @property
    def blocking(self) -> bool:
        return self.status == "open" and self.severity in ("critical", "warning")


class DocumentInfo(BaseModel):
    id: str
    filename: str
    sha256: str
    content_type: str | None = None
    size: int = 0
    doc_type: str = DocType.OTHER.value
    classification_confidence: float = 0.0
    classification_signals: list[str] = Field(default_factory=list)
    page_count: int = 0
    ocr_pages: list[int] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    meta_evidence: dict[str, Evidence] = Field(default_factory=dict)
    duplicate_of: str | None = None


SubmissionStatus = Literal[
    "received", "processing", "needs_review", "awaiting_info", "ready", "failed"
]


class SubmissionState(BaseModel):
    id: str
    reference: str
    as_of: date
    status: SubmissionStatus = "received"
    documents: list[DocumentInfo] = Field(default_factory=list)
    fields: dict[str, FieldState] = Field(default_factory=dict)
    records: list[RecordState] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    extractor: str = "rules"
    model_version: str = "rules-v1"
    processing_ms: int | None = None
    version: int = 0

    # convenience ---------------------------------------------------------
    def value(self, path: str) -> Any:
        f = self.fields.get(path)
        return None if f is None else f.value

    def lines(self) -> set[str]:
        v = self.value("policy.lines_requested") or []
        return set(v)

    def docs_of(self, doc_type: DocType | str) -> list[DocumentInfo]:
        t = doc_type.value if isinstance(doc_type, DocType) else doc_type
        return [d for d in self.documents if d.doc_type == t and not d.duplicate_of]

    def records_of(self, collection: str, include_rejected: bool = False) -> list[RecordState]:
        return [
            r for r in self.records
            if r.collection == collection and (include_rejected or r.status != "rejected")
        ]

    def open_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.status == "open"]
