"""The LLM path is exercised with a scripted client: the point is to prove the
plumbing and, above all, that hallucinated or unsupported values are caught."""

from conftest import make_pdf

from packetpilot.config import Settings
from packetpilot.extract.llm import LLMExtractor, response_schema
from packetpilot.ingest import parse_bytes
from packetpilot.pipeline import InputFile, process_submission
from packetpilot.schema import DocumentInfo

APP = make_pdf([
    (50, 60, "Commercial Insurance Application"),
    (50, 100, "Name of Business Entity: Foxhill Bakehouse LLC"),
    (50, 117, "Gross Annual Receipts: $1,450,000"),
    (50, 134, "Persons Employed (FT): 14"),
    (50, 151, "Coverage Start: 2027-02-01"),
])


class ScriptedLLM:
    model_version = "fake:scripted-1"

    def __init__(self, payload=None, fail=False):
        self.payload = payload or {}
        self.fail = fail
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        assert "Document text" in user and schema["type"] == "object"
        if self.fail:
            raise TimeoutError("upstream timeout")
        return self.payload


def _payload(**overrides):
    fields = [
        {"path": "applicant.legal_name", "value": "Foxhill Bakehouse LLC", "page": 1, "quote": "Name of Business Entity: Foxhill Bakehouse LLC"},
        {"path": "financials.annual_revenue", "value": "$1,450,000", "page": 1, "quote": "Gross Annual Receipts: $1,450,000"},
        {"path": "workforce.full_time_employees", "value": "14", "page": 1, "quote": "Persons Employed (FT): 14"},
        {"path": "policy.effective_date", "value": "2027-02-01", "page": 1, "quote": "Coverage Start: 2027-02-01"},
    ]
    fields += overrides.get("extra", [])
    return {"fields": fields, "claims": [], "vehicles": [], "payroll": [], "locations": [], "property": []}


def _extract(payload):
    parsed = parse_bytes("app.pdf", APP)
    info = DocumentInfo(id="d1", filename="app.pdf", sha256="x", doc_type="application")
    return LLMExtractor(ScriptedLLM(payload)).extract(info, parsed)


def test_schema_is_strict_mode_compatible():
    s = response_schema()

    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        if node.get("type") == "array":
            walk(node["items"])

    walk(s)


def test_grounded_values_are_trusted():
    res = _extract(_payload())
    by = {c.path: c for c in res.candidates}
    assert by["financials.annual_revenue"].value == 1_450_000
    assert all(c.evidence.grounded for c in res.candidates)
    assert by["workforce.full_time_employees"].evidence.bbox is not None  # located on the page


def test_hallucinated_quote_is_flagged():
    res = _extract(_payload(extra=[{"path": "applicant.fein", "value": "12-3456789", "page": 1, "quote": "FEIN: 12-3456789"}]))
    fein = next(c for c in res.candidates if c.path == "applicant.fein")
    assert fein.evidence.grounded is False and fein.evidence.confidence <= 0.3


def test_real_quote_but_unsupported_value_is_flagged():
    res = _extract(_payload(extra=[{"path": "workforce.part_time_employees", "value": "6", "page": 1,
                                    "quote": "Persons Employed (FT): 14"}]))
    pt = next(c for c in res.candidates if c.path == "workforce.part_time_employees")
    assert pt.evidence.grounded is False


def test_wrong_page_is_corrected():
    p = _payload()
    p["fields"][0]["page"] = 7
    res = _extract(p)
    assert next(c for c in res.candidates if c.path == "applicant.legal_name").evidence.page == 1


def test_pipeline_llm_mode_blocks_ungrounded_value():
    llm = ScriptedLLM(_payload(extra=[{"path": "applicant.fein", "value": "98-7654321", "page": 1, "quote": "EIN 98-7654321"}]))
    out = process_submission([InputFile("app.pdf", APP)], settings=Settings(extractor="llm"), llm_client=llm)
    s = out.state
    assert s.extractor == "llm" and s.model_version == "fake:scripted-1"
    assert s.fields["applicant.fein"].status == "low_confidence"
    issue = next(i for i in s.issues if i.code == "UNGROUNDED_VALUE")
    assert issue.field_path == "applicant.fein" and issue.severity == "critical"
    # grounded LLM values from a held-out layout are usable
    assert s.fields["financials.annual_revenue"].value == 1_450_000


def test_hybrid_agreement_and_llm_failure_fallback():
    out = process_submission([InputFile("app.pdf", APP)], settings=Settings(extractor="hybrid"),
                             llm_client=ScriptedLLM(fail=True))
    s = out.state
    assert s.extractor == "hybrid"
    assert any("LLM extraction failed" in n for n in out.notes)
    # rules alone cannot read this layout's revenue label: reported missing, never guessed
    assert s.fields["financials.annual_revenue"].value is None
    assert any(i.code == "MISSING_REQUIRED_FIELD" and i.field_path == "financials.annual_revenue" for i in s.issues)


def test_no_key_means_rules_only():
    out = process_submission([InputFile("app.pdf", APP)], settings=Settings(extractor="auto", openai_api_key=None, gemini_api_key=None))
    assert out.state.extractor == "rules"
