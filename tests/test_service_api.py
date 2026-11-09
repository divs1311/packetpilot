import pytest
from conftest import build_defect_package

from packetpilot.bench.generate import render_package
from packetpilot.pipeline import InputFile
from packetpilot.service import NotReady, PacketPilotService, ServiceError, VersionConflict


def _files(p, tmp_path):
    names = render_package(p, tmp_path / "pkg")
    return [InputFile(n, (tmp_path / "pkg" / n).read_bytes()) for n in names]


@pytest.fixture
def svc(settings):
    return PacketPilotService(settings)


def test_full_review_flow_with_conflict(svc, tmp_path):
    p = build_defect_package("revenue_conflict")
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    state = svc.process(sid)
    assert state.status == "needs_review"
    f = state.fields["financials.annual_revenue"]
    assert f.status == "conflict" and len({c.value for c in f.candidates}) == 2

    with pytest.raises(NotReady) as exc:
        svc.mark_ready(sid, "dana")
    assert any("Sources disagree" in b for b in exc.value.extra["blockers"])

    # bulk approval never touches the conflicting field
    out = svc.approve_verified(sid, "dana")
    assert "financials.annual_revenue" not in out["approved"]

    # accepting a conflicting field without picking a source or explaining is refused
    with pytest.raises(ServiceError):
        svc.review_field(sid, "financials.annual_revenue", "accept", "dana")
    app_cand = next(c for c in f.candidates if c.evidence.doc_type == "application")
    res = svc.review_field(sid, "financials.annual_revenue", "accept", "dana", candidate_id=app_cand.id)
    assert res["field"]["status"] == "accepted"

    st = svc.state(sid)
    conflict = next(i for i in st.issues if i.code == "FIELD_CONFLICT")
    assert conflict.status == "resolved" and conflict.resolved_by == "dana"
    remaining = [p for p, fl in st.fields.items() if fl.value is not None and fl.status not in ("accepted", "corrected")]
    for path in remaining:
        svc.review_field(sid, path, "accept", "dana")
    assert svc.mark_ready(sid, "dana")["status"] == "ready"

    actions = [e["action"] for e in svc.audit(sid)]
    assert actions[0] == "submission_created" and "processed" in actions and actions[-1] == "marked_ready"
    exported = svc.export(sid)
    assert exported["ready"] and exported["fields"]["financials.annual_revenue"]["evidence"]["document"].endswith(".pdf")


def test_corrections_need_support_or_note(svc, tmp_path):
    p = build_defect_package("missing_fein")
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    svc.process(sid)
    with pytest.raises(ServiceError, match="note is required"):
        svc.review_field(sid, "applicant.fein", "correct", "dana", value="123456789")
    with pytest.raises(ServiceError, match="Could not interpret"):
        svc.review_field(sid, "applicant.fein", "correct", "dana", value="", note="x")
    out = svc.review_field(sid, "applicant.fein", "correct", "dana", value="123456789", note="Confirmed with client 11/09")
    assert out["field"]["value"] == "12-3456789" and out["field"]["status"] == "corrected"
    st = svc.state(sid)
    missing = next(i for i in st.issues if i.code == "MISSING_REQUIRED_FIELD" and i.field_path == "applicant.fein")
    assert missing.status == "resolved" and missing.auto_resolved


def test_issue_actions_rules(svc, tmp_path):
    p = build_defect_package("coverage_gap")
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    st = svc.process(sid)
    gap = next(i for i in st.issues if i.code == "PRIOR_COVERAGE_GAP")
    with pytest.raises(ServiceError, match="note is required"):
        svc.issue_action(sid, gap.id, "waive", "dana")
    out = svc.issue_action(sid, gap.id, "waive", "dana", note="Client confirmed seasonal shutdown")
    assert out["issue"]["status"] == "waived"
    # waivers survive re-processing of the same files
    svc.process(sid)
    assert next(i for i in svc.state(sid).issues if i.id == gap.id).status == "waived"


def test_field_driven_issues_cannot_be_waived_directly(svc, tmp_path):
    p = build_defect_package("employee_conflict")
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    st = svc.process(sid)
    conflict = next(i for i in st.issues if i.code == "FIELD_CONFLICT")
    with pytest.raises(ServiceError, match="reviewing the field"):
        svc.issue_action(sid, conflict.id, "waive", "dana", note="whatever")


def test_optimistic_locking_and_reopen_on_edit(svc, tmp_path):
    p = build_defect_package(None)
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    svc.process(sid)
    v = svc.get(sid)["version"]
    svc.approve_verified(sid, "dana", expected_version=v)
    with pytest.raises(VersionConflict):
        svc.approve_verified(sid, "lee", expected_version=v)
    svc.mark_ready(sid, "dana")
    assert svc.get(sid)["status"] == "ready"
    svc.review_field(sid, "contact.name", "correct", "lee", value="Someone Else", note="New contact per email")
    assert svc.get(sid)["status"] == "needs_review"
    assert "reopened" in [e["action"] for e in svc.audit(sid)]


def test_request_info_and_records(svc, tmp_path):
    p = build_defect_package("duplicate_claim")
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    st = svc.process(sid)
    dup = next(i for i in st.issues if i.code == "DUPLICATE_CLAIM")
    draft = svc.request_info(sid, "dana")
    assert draft["items"] >= 1 and dup.subject in draft["body"] and ".pdf" not in draft["body"]
    assert svc.get(sid)["status"] == "awaiting_info"
    rows = [r for r in svc.state(sid).records if r.collection == "claims" and r.key == dup.subject]
    svc.record_action(sid, rows[1].id, "reject", "dana", note="Duplicate line on carrier report")
    assert next(i for i in svc.state(sid).issues if i.id == dup.id).status == "resolved"
    m = svc.metrics()
    assert m["total_submissions"] == 1 and m["by_status"]["awaiting_info"] == 1 and m["rework_rate"] == 1.0


def test_evidence_page_image(svc, tmp_path):
    p = build_defect_package(None)
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    st = svc.process(sid)
    ev = st.fields["applicant.fein"].selected.evidence
    png = svc.page_image(sid, ev.document_id, ev.page, ev.bbox)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pages = svc.document_pages(sid, ev.document_id)
    assert pages["has_original"] and any("FEIN" in ln["text"] for ln in pages["pages"][0]["lines"])


# ---------------------------------------------------------------------------- HTTP


@pytest.fixture
def client(settings, monkeypatch):
    from fastapi.testclient import TestClient

    import packetpilot.api.main as api

    monkeypatch.setattr(api, "_service", PacketPilotService(settings))
    monkeypatch.setenv("PACKETPILOT_PROCESS_MODE", "sync")
    monkeypatch.delenv("REDIS_URL", raising=False)
    return TestClient(api.app)


def test_api_end_to_end(client, tmp_path):
    p = build_defect_package("vehicle_count_mismatch")
    files = [("files", (f.filename, f.data)) for f in _files(p, tmp_path)]
    r = client.post("/api/submissions", files=files, data={"as_of": p.as_of.isoformat()}, headers={"X-Reviewer": "dana"})
    assert r.status_code == 202 and r.json()["dispatch"] == "processed"
    sid = r.json()["id"]
    body = client.get(f"/api/submissions/{sid}").json()
    assert body["status"] == "needs_review"
    assert any(i["code"] == "VEHICLE_COUNT_MISMATCH" for i in body["state"]["issues"])

    r = client.post(f"/api/submissions/{sid}/mark-ready", headers={"X-Reviewer": "dana"})
    assert r.status_code == 409 and r.json()["blockers"]

    r = client.post(f"/api/submissions/{sid}/fields/applicant.fein", json={"action": "bogus"})
    assert r.status_code == 422
    r = client.post(f"/api/submissions/{sid}/fields/not.a.field", json={"action": "accept"})
    assert r.status_code == 404
    r = client.post(f"/api/submissions/{sid}/fields/approve-verified", headers={"X-Reviewer": "dana"})
    assert r.status_code == 200 and r.json()["approved"]
    assert client.get(f"/api/submissions/{sid}/audit").json()[-1]["actor"] == "dana"
    assert client.get("/api/metrics").json()["total_submissions"] == 1
    assert client.get("/api/submissions/nope").status_code == 404
    assert client.get("/health").json()["status"] == "ok"


def test_api_key_enforced(client, monkeypatch):
    monkeypatch.setenv("PACKETPILOT_API_KEY", "s3cret")
    assert client.get("/api/submissions").status_code == 401
    assert client.get("/api/submissions", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_api_pasted_email_only(client):
    r = client.post("/api/submissions", data={"email_text": "Our revenue last year was $900,000."})
    assert r.status_code == 202
    st = client.get(f"/api/submissions/{r.json()['id']}").json()["state"]
    assert st["documents"][0]["doc_type"] == "email"
    assert st["fields"]["financials.annual_revenue"]["value"] == 900000


def test_filesystem_storage_backend(settings, tmp_path):
    from dataclasses import replace

    svc = PacketPilotService(replace(settings, storage_backend="fs"))
    p = build_defect_package(None)
    sid = svc.create_submission(_files(p, tmp_path), actor="dana", as_of=p.as_of)
    assert any((tmp_path / "uploads" / sid).iterdir())
    st = svc.process(sid)
    ev = st.fields["applicant.legal_name"].selected.evidence
    assert svc.original_file(sid, ev.document_id)[1][:4] == b"%PDF"
