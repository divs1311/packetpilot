"""Streamlit reviewer workspace and operations dashboard.

    streamlit run streamlit_app.py

Set PACKETPILOT_API_URL to talk to a running FastAPI service; otherwise the
UI runs the service in-process (handy for Streamlit Community Cloud).
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from ..pipeline import InputFile
from ..rules_engine import ISSUE_CATALOG
from ..schema import DOC_TYPE_LABELS, FIELD_SPECS, LINE_LABELS, DocType, Line
from .client import ClientError, HttpClient, LocalClient

STATUS_BADGE = {
    "auto_verified": ("Verified", "green"), "accepted": ("Accepted", "green"), "corrected": ("Corrected", "blue"),
    "extracted": ("Check", "orange"), "low_confidence": ("Low confidence", "orange"), "conflict": ("Conflict", "red"),
    "missing": ("Missing", "gray"), "escalated": ("Escalated", "violet"),
}
SUB_STATUS = {"received": "gray", "processing": "gray", "needs_review": "orange", "awaiting_info": "violet",
              "ready": "green", "failed": "red"}
SEV_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
BENCH_DIR = Path(os.getenv("PACKETPILOT_DEMO_DIR", "benchmark/packages"))


@st.cache_resource
def get_client():
    url = os.getenv("PACKETPILOT_API_URL")
    if url:
        return HttpClient(url, os.getenv("PACKETPILOT_API_KEY"))
    return LocalClient()


def fmt_value(path: str, v) -> str:
    if v is None:
        return "—"
    kind = FIELD_SPECS[path].kind
    if kind == "money":
        return f"${v:,.0f}"
    if kind == "lines":
        return ", ".join(LINE_LABELS.get(Line(x), x) for x in v)
    if kind == "bool":
        return "Yes" if v else "No"
    if kind == "date":
        return date.fromisoformat(v).strftime("%m/%d/%Y")
    return str(v)


def badge(text: str, color: str) -> str:
    return f":{color}-badge[{text}]"


def run_action(fn, *a, success: str | None = None, **kw):
    try:
        out = fn(*a, **kw)
        if success:
            st.session_state["flash"] = ("success", success)
        return out
    except ClientError as e:
        blockers = e.extra.get("blockers")
        msg = e.message + ("\n\n" + "\n".join(f"- {b}" for b in blockers) if blockers else "")
        st.session_state["flash"] = ("error", msg)
        return None


def show_flash():
    if "flash" in st.session_state:
        kind, msg = st.session_state.pop("flash")
        (st.success if kind == "success" else st.error)(msg)


# --------------------------------------------------------------------------- inbox


def page_inbox(client, reviewer: str):
    st.header("Submission inbox")
    show_flash()
    with st.expander("➕ New submission package", expanded=False):
        with st.form("upload", clear_on_submit=True):
            files = st.file_uploader("Documents (PDF, XLSX, CSV, DOCX, EML, images)", accept_multiple_files=True)
            email_text = st.text_area("…or paste an email from the client", height=100)
            as_of = st.date_input("Received date", value=date.today())
            if st.form_submit_button("Upload and process", type="primary"):
                inputs = [InputFile(f.name, f.getvalue(), f.type) for f in files or []]
                if not inputs and not email_text.strip():
                    st.warning("Add at least one document or email.")
                else:
                    with st.spinner("Parsing, extracting and validating…"):
                        sid = run_action(client.create, inputs, reviewer, as_of, email_text)
                    if sid:
                        st.session_state["selected"] = sid
                        st.session_state["flash"] = ("success", "Submission processed — opening review.")
                        st.session_state["goto"] = "Review"
                        st.rerun()
    if BENCH_DIR.exists():
        with st.expander("🧪 Load fictional demo packages from the benchmark"):
            pkgs = sorted(p.name for p in BENCH_DIR.iterdir() if (p / "ground_truth.json").exists())
            choice = st.multiselect("Packages", pkgs, default=pkgs[:3])
            if st.button("Load selected"):
                with st.spinner(f"Processing {len(choice)} package(s)…"):
                    for name in choice:
                        gt = json.loads((BENCH_DIR / name / "ground_truth.json").read_text())
                        files = [InputFile(f, (BENCH_DIR / name / f).read_bytes()) for f in gt["files"]]
                        run_action(client.create, files, reviewer, date.fromisoformat(gt["as_of"]))
                st.rerun()

    status = st.segmented_control("Status", ["all", "needs_review", "awaiting_info", "ready", "failed"], default="all")
    rows = client.list(None if status in (None, "all") else status)
    if not rows:
        st.info("No submissions yet. Upload a package or load demo packages above.")
        return
    df = pd.DataFrame(rows)[["reference", "applicant_name", "status", "documents", "open_blocking_issues",
                             "fields_pending_review", "age_hours", "sla_breached", "id"]]
    df.columns = ["Reference", "Applicant", "Status", "Docs", "Blocking issues", "Fields to review", "Age (h)", "SLA breached", "id"]
    event = st.dataframe(df.drop(columns=["id"]), hide_index=True, width="stretch",
                         on_select="rerun", selection_mode="single-row", key="inbox_table")
    sel = event.selection.rows if event and event.selection else []
    if sel:
        row = df.iloc[sel[0]]
        if st.button(f"Open {row['Reference']} — {row['Applicant']} →", type="primary"):
            st.session_state["selected"] = row["id"]
            st.session_state["goto"] = "Review"
            st.rerun()


# --------------------------------------------------------------------------- review


def evidence_view(client, sid: str, ev: dict, key: str):
    st.caption(f"📄 **{ev['filename']}** · page {ev['page']} · {ev.get('locator') or ''} · `{ev['method']}` · "
               f"confidence {ev['confidence']:.0%}" + ("" if ev.get("grounded", True) else " · ⚠️ **not found in source**"))
    st.code(ev["text"], language=None)
    if ev.get("bbox") and ev["filename"].lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
        if st.toggle("Show on page", key=f"img-{key}"):
            try:
                st.image(client.page_png(sid, ev["document_id"], ev["page"], ev["bbox"]), width="stretch")
            except ClientError as e:
                st.caption(e.message)


def field_panel(client, sid: str, path: str, f: dict, reviewer: str, version: int):
    spec = FIELD_SPECS[path]
    label, color = STATUS_BADGE[f["status"]]
    title = f"{spec.label} — {fmt_value(path, f['value'])}  {badge(label, color)}"
    attention = f["status"] in ("conflict", "low_confidence", "extracted", "missing", "escalated")
    with st.expander(title, expanded=attention and f["status"] != "missing"):
        if f.get("review_note") or f.get("reviewed_by"):
            st.caption(f"Reviewed by {f.get('reviewed_by')}: {f.get('review_note') or ''}")
        cands = f["candidates"]
        if cands:
            for c in cands:
                chosen = c["id"] == f.get("selected_candidate_id")
                cols = st.columns([5, 1])
                with cols[0]:
                    st.markdown(f"{'✅ ' if chosen else ''}**{fmt_value(path, c['value']) if c['value'] is not None else '(unreadable: ' + c['raw'] + ')'}**")
                    evidence_view(client, sid, c["evidence"], f"{path}-{c['id']}")
                with cols[1]:
                    if c["value"] is not None and st.button("Use this", key=f"use-{path}-{c['id']}"):
                        run_action(client.review_field, sid, path, "accept", reviewer, candidate_id=c["id"],
                                   expected_version=version, success=f"{spec.label} accepted")
                        st.rerun()
        else:
            st.caption("Not found in any document.")
        c1, c2, c3 = st.columns(3)
        with c1:
            if f["value"] is not None and f["status"] not in ("accepted", "corrected"):
                if st.button("Accept", key=f"acc-{path}", type="primary"):
                    run_action(client.review_field, sid, path, "accept", reviewer, expected_version=version,
                               note=None, success=f"{spec.label} accepted")
                    st.rerun()
        with st.form(f"fix-{path}"):
            new = st.text_input("Correct / enter value", value="" if f["value"] is None else str(fmt_value(path, f["value"])))
            note = st.text_input("Note (required if no document supports the value, or to escalate)")
            a, b = st.columns(2)
            if a.form_submit_button("Save correction"):
                run_action(client.review_field, sid, path, "correct", reviewer, value=new, note=note or None,
                           expected_version=version, success=f"{spec.label} corrected")
                st.rerun()
            if b.form_submit_button("Escalate"):
                run_action(client.review_field, sid, path, "escalate", reviewer, note=note or None,
                           expected_version=version, success=f"{spec.label} escalated")
                st.rerun()


def page_review(client, reviewer: str):
    subs = client.list()
    if not subs:
        st.info("No submissions yet.")
        return
    ids = [s["id"] for s in subs]
    labels = {s["id"]: f"{s['reference']} · {s['applicant_name'] or 'unknown'} · {s['status']}" for s in subs}
    current = st.session_state.get("selected", ids[0])
    sid = st.selectbox("Submission", ids, index=ids.index(current) if current in ids else 0, format_func=labels.get)
    st.session_state["selected"] = sid
    data = client.get(sid)
    show_flash()
    if not data["state"]:
        st.warning(f"Status: {data['status']}. {data.get('error') or 'Processing…'}")
        if st.button("Refresh"):
            st.rerun()
        return
    state, rd, version = data["state"], data["readiness"], data["version"]

    st.subheader(f"{data['applicant_name'] or 'Unknown applicant'}")
    st.markdown(
        f"{badge(data['status'].replace('_', ' '), SUB_STATUS.get(data['status'], 'gray'))} &nbsp; `{data['reference']}` · "
        f"received {state['as_of']} · extractor `{state['extractor']}` · model `{state['model_version']}` · "
        f"processed in {state.get('processing_ms')} ms"
    )
    m = st.columns(4)
    m[0].metric("Blocking issues", rd["open_blocking_issues"])
    m[1].metric("Fields to review", len(rd["fields_pending_review"]))
    m[2].metric("Bulk-approvable", len(rd["fields_bulk_approvable"]))
    m[3].metric("Ready", "Yes" if rd["ready"] else "No")
    b = st.columns(3)
    if b[0].button(f"✔ Approve {len(rd['fields_bulk_approvable'])} verified fields",
                   disabled=not rd["fields_bulk_approvable"], width="stretch"):
        run_action(client.approve_verified, sid, reviewer, version, success="High-confidence fields approved")
        st.rerun()
    if b[1].button("✉ Request info from client", width="stretch"):
        out = run_action(client.request_info, sid, reviewer, success="Follow-up drafted; status set to awaiting info")
        if out:
            st.session_state["last_draft"] = out
        st.rerun()
    if b[2].button("🚀 Mark ready for market", type="primary", width="stretch"):
        run_action(client.mark_ready, sid, reviewer, version, success="Marked ready")
        st.rerun()
    if rd["blockers"]:
        with st.expander(f"Why it isn't ready ({len(rd['blockers'])})"):
            for bl in rd["blockers"]:
                st.markdown(f"- {bl}")

    t_fields, t_issues, t_sched, t_docs, t_mail, t_audit, t_export = st.tabs(
        ["Fields", f"Issues ({sum(1 for i in state['issues'] if i['status'] == 'open')})", "Schedules",
         f"Documents ({len(state['documents'])})", "Follow-up email", "Audit trail", "Export"])

    with t_fields:
        order = {"conflict": 0, "escalated": 1, "low_confidence": 2, "missing": 3, "extracted": 4, "auto_verified": 5,
                 "corrected": 6, "accepted": 7}
        flt = st.segmented_control("Show", ["Needs attention", "All"], default="Needs attention", key="fieldfilter")
        paths = sorted(state["fields"], key=lambda p: (order[state["fields"][p]["status"]], list(FIELD_SPECS).index(p)))
        shown = 0
        for p in paths:
            f = state["fields"][p]
            if flt == "Needs attention" and f["status"] in ("auto_verified", "accepted", "corrected"):
                continue
            if flt == "Needs attention" and f["status"] == "missing" and not FIELD_SPECS[p].is_required(set(state["fields"]["policy.lines_requested"]["value"] or [])):
                continue
            field_panel(client, sid, p, f, reviewer, version)
            shown += 1
        if not shown:
            st.success("Nothing needs individual attention. Remaining fields are high-confidence, grounded and "
                       "consistent across sources — approve them in bulk above, or switch to **All** to inspect each one.")

    with t_issues:
        issues = sorted(state["issues"], key=lambda i: (i["status"] != "open", ["critical", "warning", "info"].index(i["severity"])))
        show_closed = st.toggle("Show resolved / waived", value=False)
        for i in issues:
            if i["status"] != "open" and not show_closed:
                continue
            title = ISSUE_CATALOG[i["code"]][2]
            head = f"{SEV_ICON[i['severity']]} **{title}** · `{i['code']}` · {i['status']}"
            with st.container(border=True):
                st.markdown(head)
                st.write(i["message"])
                if i.get("resolution_note"):
                    st.caption(f"{i['status'].title()} by {i.get('resolved_by') or 'system'}: {i['resolution_note']}")
                for k, ev in enumerate(i["evidence"][:3]):
                    evidence_view(client, sid, ev, f"iss-{i['id']}-{k}")
                if i["status"] == "open" and i["code"] not in {"FIELD_CONFLICT", "EXTRACTION_DISAGREEMENT", "UNGROUNDED_VALUE",
                                                                "LOW_CONFIDENCE", "UNPARSEABLE_VALUE", "FIELD_ESCALATED"}:
                    with st.form(f"iss-{i['id']}"):
                        note = st.text_input("Note")
                        a, b2 = st.columns(2)
                        if a.form_submit_button("Resolve"):
                            run_action(client.issue_action, sid, i["id"], "resolve", reviewer, note or None, version, success="Issue resolved")
                            st.rerun()
                        if b2.form_submit_button("Waive"):
                            run_action(client.issue_action, sid, i["id"], "waive", reviewer, note or None, version, success="Issue waived")
                            st.rerun()
                elif i["status"] == "open":
                    st.caption(f"Resolve this from the **Fields** tab ({FIELD_SPECS[i['field_path']].label if i.get('field_path') in FIELD_SPECS else i.get('field_path')}).")
                elif st.button("Reopen", key=f"re-{i['id']}"):
                    run_action(client.issue_action, sid, i["id"], "reopen", reviewer, None, version, success="Issue reopened")
                    st.rerun()

    with t_sched:
        for coll, title in (("claims", "Loss history"), ("payroll", "Payroll by class"), ("vehicles", "Vehicles"),
                            ("property", "Statement of values"), ("locations", "Locations on application")):
            recs = [r for r in state["records"] if r["collection"] == coll]
            if not recs:
                continue
            st.markdown(f"#### {title}")
            df = pd.DataFrame([{**r["data"], "source": f"{r['evidence']['filename']} p{r['evidence']['page']}",
                                "row status": r["status"] + (" (statement)" if r["partial"] else "")} for r in recs])
            st.dataframe(df, hide_index=True, width="stretch")
            with st.expander(f"Reject / restore a {title.lower()} row"):
                opts = {r["id"]: f"{r['key']} · {r['evidence']['filename']} ({r['status']})" for r in recs}
                rid = st.selectbox("Row", list(opts), format_func=opts.get, key=f"rsel-{coll}")
                note = st.text_input("Reason", key=f"rnote-{coll}")
                a, b2 = st.columns(2)
                if a.button("Reject row", key=f"rrej-{coll}"):
                    run_action(client.record_action, sid, rid, "reject", reviewer, note or None, version, success="Row rejected")
                    st.rerun()
                if b2.button("Restore row", key=f"rres-{coll}"):
                    run_action(client.record_action, sid, rid, "restore", reviewer, None, version, success="Row restored")
                    st.rerun()

    with t_docs:
        docs = state["documents"]
        st.dataframe(pd.DataFrame([{
            "File": d["filename"], "Type": DOC_TYPE_LABELS.get(DocType(d["doc_type"]), d["doc_type"]),
            "Classifier confidence": f"{d['classification_confidence']:.0%}", "Pages": d["page_count"],
            "OCR pages": ", ".join(map(str, d["ocr_pages"])) or "—", "Valuation date": d["meta"].get("valuation_date", ""),
            "Duplicate": "yes" if d.get("duplicate_of") else "",
        } for d in docs]), hide_index=True, width="stretch")
        names = {d["id"]: d["filename"] for d in docs}
        did = st.selectbox("View document", list(names), format_func=names.get)
        doc = next(d for d in docs if d["id"] == did)
        st.caption("Why this type: " + "; ".join(doc["classification_signals"][:4]))
        pages = client.document_pages(sid, did)
        if pages["pages"]:
            pno = st.number_input("Page", 1, len(pages["pages"]), 1)
            pg = pages["pages"][pno - 1]
            if pages["has_original"] and doc["filename"].lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")):
                st.image(client.page_png(sid, did, int(pno)), width="stretch")
            with st.expander("Extracted text layer", expanded=not pages["has_original"]):
                st.code("\n".join(l["text"] for l in pg["lines"]) or "(no text)", language=None)

    with t_mail:
        draft = st.session_state.get("last_draft") or client.followup(sid)
        st.text_input("Subject", draft["subject"])
        st.text_area("Body", draft["body"], height=360)
        st.caption("Every line in this draft is generated from an open issue — nothing is invented. Edit before sending.")

    with t_audit:
        events = client.audit(sid)
        st.dataframe(pd.DataFrame([{"When": e["at"][:19].replace("T", " "), "Who": e["actor"], "Action": e["action"],
                                    "Target": e["target_id"] or "", "Note": e["note"] or "",
                                    "Change": json.dumps({"before": e["before"], "after": e["after"]}) if e["before"] or e["after"] else "",
                                    "Model": e["model_version"] or ""} for e in events]),
                     hide_index=True, width="stretch")

    with t_export:
        ex = client.export(sid)
        st.download_button("Download submission record (JSON)", json.dumps(ex, indent=2), file_name=f"{ex['reference']}.json",
                           mime="application/json", type="primary")
        st.json(ex, expanded=1)


# --------------------------------------------------------------------------- operations


def page_ops(client):
    st.header("Operations")
    m = client.metrics()
    if not m["total_submissions"]:
        st.info("No submissions yet.")
        return
    c = st.columns(5)
    c[0].metric("Submissions", m["total_submissions"])
    c[1].metric("Outstanding", m["outstanding"])
    c[2].metric("Incomplete packages", m["incomplete_packages"])
    c[3].metric(f"SLA breaches (>{m['sla_hours']:g}h)", len(m["sla_breaches"]))
    c[4].metric("Correction rate", "—" if m["correction_rate"] is None else f"{m['correction_rate']:.1%}",
                help="Share of human-reviewed fields that were corrected rather than accepted — the live extraction-quality signal.")
    c = st.columns(5)
    c[0].metric("Median processing", "—" if m["median_processing_seconds"] is None else f"{m['median_processing_seconds']:.2f}s")
    c[1].metric("Median hours to ready", "—" if m["median_hours_to_ready"] is None else f"{m['median_hours_to_ready']:.1f}")
    c[2].metric("Median review minutes", "—" if m["median_review_minutes"] is None else f"{m['median_review_minutes']:.1f}",
                help="First human action to 'ready'.")
    c[3].metric("First-pass ready", "—" if m["first_pass_ready_rate"] is None else f"{m['first_pass_ready_rate']:.0%}",
                help="Ready submissions that never needed a client follow-up.")
    c[4].metric("Rework rate", "—" if m["rework_rate"] is None else f"{m['rework_rate']:.0%}",
                help="Submissions that needed at least one information request.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Pipeline by status**")
        st.bar_chart(pd.Series(m["by_status"], name="submissions"), horizontal=True)
        st.markdown("**Intake, last 14 days**")
        st.bar_chart(pd.DataFrame(m["intake_last_14_days"]).set_index("date"))
    with right:
        st.markdown("**Open issues by type**")
        if m["open_issues_by_code"]:
            s = pd.Series({ISSUE_CATALOG[k][2]: v for k, v in m["open_issues_by_code"].items()}, name="open")
            st.bar_chart(s.sort_values(), horizontal=True)
        else:
            st.caption("No open issues.")
        if m["corrections_by_method"]:
            st.markdown("**Human verdicts by extraction method**")
            st.dataframe(pd.DataFrame(m["corrections_by_method"]).T, width="stretch")
    if m["sla_breaches"]:
        st.markdown("**SLA breaches**")
        st.dataframe(pd.DataFrame(m["sla_breaches"]), hide_index=True, width="stretch")


# --------------------------------------------------------------------------- main


def main():
    st.set_page_config(page_title="PacketPilot", page_icon="📦", layout="wide")
    client = get_client()
    if "goto" in st.session_state:  # must be applied before the nav widget is created
        st.session_state["nav"] = st.session_state.pop("goto")
    with st.sidebar:
        st.title("📦 PacketPilot")
        st.caption("Submission intake for commercial insurance brokers. The model proposes, rules validate, a human approves.")
        reviewer = st.text_input("Reviewer", value=st.session_state.get("reviewer", "reviewer@brokerage.example"))
        st.session_state["reviewer"] = reviewer
        nav = st.radio("Go to", ["Inbox", "Review", "Operations"], key="nav")
        st.divider()
        st.caption(f"Mode: **{client.mode}** · demo uses fictional data only")
    if nav == "Inbox":
        page_inbox(client, reviewer)
    elif nav == "Review":
        page_review(client, reviewer)
    else:
        page_ops(client)
