"""Every injected defect type must raise exactly the expected issue and nothing else."""

from datetime import date

import pytest
from conftest import build_defect_package

from packetpilot.bench.generate import DEFECTS, render_package
from packetpilot.config import Settings
from packetpilot.pipeline import InputFile, process_submission
from packetpilot.rules_engine import vin_problem

DATA = {"contradiction", "missing", "validity", "document"}


def _run(p, tmp_path):
    files = render_package(p, tmp_path / "pkg")
    inputs = [InputFile(f, (tmp_path / "pkg" / f).read_bytes()) for f in files]
    return process_submission(inputs, as_of=p.as_of, settings=Settings(extractor="rules")).state


def _matches(issue, exp):
    return (issue.code == exp["code"]
            and (not exp.get("field_path") or issue.field_path == exp["field_path"])
            and (not exp.get("subject") or (issue.subject or "").upper() == exp["subject"].upper()))


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_each_defect_is_detected_precisely(defect, tmp_path):
    p = build_defect_package(defect)
    state = _run(p, tmp_path)
    produced = [i for i in state.issues if i.status == "open" and i.category in DATA and i.severity != "info"]
    for exp in p.expected:
        assert any(_matches(i, exp) for i in produced), f"{defect}: expected {exp}, got {[i.code for i in produced]}"
    unexpected = [i for i in produced if not any(_matches(i, e) for e in p.expected)]
    assert not unexpected, f"{defect}: unexpected {[(i.code, i.message) for i in unexpected]}"


def test_clean_package_has_no_data_issues(tmp_path):
    p = build_defect_package(None)
    state = _run(p, tmp_path)
    assert [i.code for i in state.issues if i.status == "open" and i.category in DATA and i.severity != "info"] == []
    assert all(f.status in ("auto_verified", "missing") for f in state.fields.values())


def test_vin_check_digit():
    assert vin_problem("1M8GDM9AXKP042788") is None  # canonical valid example
    assert "check-digit" in vin_problem("1M8GDM9A1KP042788")
    assert "17" in vin_problem("1M8GDM9AXKP04278")
    assert "I, O or Q" in vin_problem("1M8GDM9AXKP04278O")


def test_conflict_issue_resolves_when_reviewer_picks_value(tmp_path):
    from packetpilot.pipeline import reevaluate

    p = build_defect_package("revenue_conflict")
    state = _run(p, tmp_path)
    issue = next(i for i in state.issues if i.code == "FIELD_CONFLICT")
    assert issue.blocking
    f = state.fields["financials.annual_revenue"]
    f.status, f.reviewed_by = "accepted", "tester"
    reevaluate(state, Settings())
    issue = next(i for i in state.issues if i.code == "FIELD_CONFLICT")
    assert issue.status == "resolved" and issue.auto_resolved and "tester" == issue.resolved_by


def test_human_waiver_survives_reevaluation(tmp_path):
    from packetpilot.pipeline import reevaluate

    p = build_defect_package("coverage_gap")
    state = _run(p, tmp_path)
    issue = next(i for i in state.issues if i.code == "PRIOR_COVERAGE_GAP")
    issue.status, issue.resolution_note, issue.auto_resolved = "waived", "Client confirmed a planned gap", False
    reevaluate(state, Settings())
    again = next(i for i in state.issues if i.code == "PRIOR_COVERAGE_GAP")
    assert again.status == "waived" and again.resolution_note == "Client confirmed a planned gap"


def test_effective_date_rules_use_submission_date(tmp_path):
    p = build_defect_package(None)
    state = _run(p, tmp_path)
    assert date.fromisoformat(state.value("policy.effective_date")) > state.as_of
