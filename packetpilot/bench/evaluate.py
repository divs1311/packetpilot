"""Run the pipeline over the benchmark and score it against ground truth.

    python -m packetpilot.bench.evaluate --packages benchmark/packages --out benchmark/results

Metrics (all computed, none estimated):

* field accuracy          - correct / fields present in ground truth (conflict fields excluded)
* silent error rate       - wrong values that the system marked ``auto_verified`` (the dangerous case)
* missing-field recall    - injected missing fields/documents/explanations flagged
* contradiction recall    - injected cross-document contradictions flagged
* validity recall         - injected invalid values flagged
* issue precision         - share of raised data-quality issues that correspond to an injected defect
* evidence traceability   - extracted values whose evidence span is found verbatim (or, for OCR,
                            approximately) on the cited page of the cited document
* clean-package pass rate - defect-free packages with no blocking data-quality issue
* defect-package block rate - defective packages that are blocked from "ready" by the right issues
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from ..config import Settings
from ..normalize import canonical_text, fuzzy_contains, values_equal
from ..pipeline import InputFile, process_submission, resolve_extractors
from ..rules_engine import ISSUE_CATALOG
from ..schema import FIELD_SPECS

DATA_CATEGORIES = {"contradiction", "missing", "validity", "document"}


def _same(path: str, pred, gt) -> bool:
    if pred is None or gt is None:
        return pred is None and gt is None
    kind = FIELD_SPECS[path].kind
    if kind == "money":
        return abs(float(pred) - float(gt)) < 1.0
    if kind == "lines":
        return set(pred) == set(gt)
    if kind == "text":
        # same equivalence the product uses (e.g. "Acme, L.L.C." == "Acme LLC" for legal names)
        return values_equal(kind, str(pred), str(gt), path=path) and (
            path == "applicant.legal_name" or canonical_text(str(pred)) == canonical_text(str(gt))
        )
    if kind == "email":
        return str(pred).lower() == str(gt).lower()
    return pred == gt


def group_of(gt: dict) -> str:
    if gt["scanned"]:
        return "scanned (OCR)"
    return {"A": "style A", "B": "style B", "C": "style C (held-out phrasing)"}[gt["style"]]


def evaluate_package(pdir: Path, settings: Settings, llm_client=None) -> dict:
    gt = json.loads((pdir / "ground_truth.json").read_text())
    files = [InputFile(f, (pdir / f).read_bytes()) for f in gt["files"]]
    t0 = time.perf_counter()
    out = process_submission(files, as_of=date.fromisoformat(gt["as_of"]), settings=settings, llm_client=llm_client)
    elapsed = time.perf_counter() - t0
    s = out.state

    # ---- fields
    field_rows = []
    for path in FIELD_SPECS:
        if path in gt["conflict_fields"]:
            continue
        g = gt["fields"].get(path)
        f = s.fields[path]
        pred = f.value
        if g is None and pred is None:
            outcome = "absent_ok"
        elif g is None:
            outcome = "spurious"
        elif pred is None:
            outcome = "missed"
        elif _same(path, pred, g):
            outcome = "correct"
        else:
            outcome = "wrong"
        field_rows.append({"path": path, "gt": g, "pred": pred, "status": f.status, "outcome": outcome,
                           "confidence": f.confidence})
    present = [r for r in field_rows if r["gt"] is not None]
    silent = [r for r in field_rows if r["outcome"] in ("wrong", "spurious") and r["status"] == "auto_verified"]

    # ---- traceability (fields + records)
    trace_total = trace_ok = 0
    untraceable = []

    def check(ev) -> bool:
        parsed = out.parsed.get(ev.document_id)
        if parsed is None:
            return False
        page = parsed.page(ev.page)
        if page is None or not ev.text:
            return False
        return fuzzy_contains(page.text, ev.text, threshold=0.9)

    for path, f in s.fields.items():
        if f.value is None:
            continue
        trace_total += 1
        sel = f.selected
        if sel is not None and sel.evidence.document_id and sel.evidence.page and check(sel.evidence) and sel.evidence.grounded:
            trace_ok += 1
        else:
            untraceable.append(path)
    for r in s.records:
        trace_total += 1
        if check(r.evidence):
            trace_ok += 1
        else:
            untraceable.append(f"{r.collection}:{r.key}")

    # ---- issues
    produced = [i for i in s.issues if i.status == "open" and i.category in DATA_CATEGORIES and i.severity != "info"]
    matched_ids: set[str] = set()
    expected_rows = []
    for e in gt["expected_issues"]:
        hit = None
        for i in produced:
            if i.code != e["code"]:
                continue
            if e.get("field_path") and i.field_path != e["field_path"]:
                continue
            if e.get("subject") and (i.subject or "").upper() != e["subject"].upper():
                continue
            hit = i
            break
        if hit:
            matched_ids.add(hit.id)
        expected_rows.append({**e, "category": ISSUE_CATALOG[e["code"]][1], "detected": hit is not None})
    false_pos = [
        {"code": i.code, "field_path": i.field_path, "subject": i.subject, "message": i.message}
        for i in produced if i.id not in matched_ids
    ]
    blocking_data = [i for i in s.issues if i.blocking and i.category in DATA_CATEGORIES]
    is_clean = not gt["defects"]

    # ---- records
    rec = {}
    claims = {r.key: r for r in s.records_of("claims") if not r.partial}
    gt_claims = {c["claim_number"]: c for c in gt["records"]["claims"]}
    rec["claims"] = (sum(1 for k, c in gt_claims.items() if k in claims and claims[k].data.get("date_of_loss") == c["date_of_loss"]
                         and abs((claims[k].data.get("paid") or 0) - c["paid"]) < 1 and claims[k].data.get("status") == c["status"]),
                     len(gt_claims))
    veh = {r.key for r in s.records_of("vehicles")}
    rec["vehicles"] = (sum(1 for v in gt["records"]["vehicles"] if v["vin"] in veh), len(gt["records"]["vehicles"]))
    pay = {r.data.get("class_code"): r.data.get("payroll") for r in s.records_of("payroll")}
    rec["payroll"] = (sum(1 for r in gt["records"]["payroll"] if r["class_code"] in pay and abs(pay[r["class_code"]] - r["payroll"]) < 1),
                      len(gt["records"]["payroll"]))

    review_touches = sum(1 for f in s.fields.values() if f.value is not None and f.status != "auto_verified") + sum(
        1 for i in s.issues if i.status == "open" and i.category == "review"
    )
    bulk = sum(1 for f in s.fields.values() if f.status == "auto_verified")
    return {
        "package_id": gt["package_id"],
        "group": group_of(gt),
        "defects": gt["defects"],
        "clean": is_clean,
        "seconds": round(elapsed, 3),
        "fields": field_rows,
        "field_present": len(present),
        "field_correct": sum(1 for r in present if r["outcome"] == "correct"),
        "field_missed": sum(1 for r in present if r["outcome"] == "missed"),
        "field_wrong": sum(1 for r in present if r["outcome"] == "wrong"),
        "field_spurious": sum(1 for r in field_rows if r["outcome"] == "spurious"),
        "silent_errors": [r["path"] for r in silent],
        "trace_total": trace_total,
        "trace_ok": trace_ok,
        "untraceable": untraceable,
        "expected": expected_rows,
        "false_positives": false_pos,
        "blocking_data_issues": len(blocking_data),
        "clean_pass": is_clean and not blocking_data,
        "defect_blocked": (not is_clean) and all(e["detected"] for e in expected_rows) and bool(blocking_data),
        "records": rec,
        "review_touches": review_touches,
        "bulk_approvable": bulk,
        "doc_types": {d.filename: d.doc_type for d in s.documents},
    }


def _pct(n: float, d: float) -> float | None:
    return None if d == 0 else round(100.0 * n / d, 1)


def summarise(results: list[dict]) -> dict:
    def block(rs: list[dict]) -> dict:
        exp = [e for r in rs for e in r["expected"]]
        cat = defaultdict(lambda: [0, 0])
        for e in exp:
            cat[e["category"]][1] += 1
            cat[e["category"]][0] += int(e["detected"])
        fp = sum(len(r["false_positives"]) for r in rs)
        tp = sum(int(e["detected"]) for e in exp)
        clean = [r for r in rs if r["clean"]]
        dirty = [r for r in rs if not r["clean"]]
        recs = defaultdict(lambda: [0, 0])
        for r in rs:
            for k, (a, b) in r["records"].items():
                recs[k][0] += a
                recs[k][1] += b
        return {
            "packages": len(rs),
            "field_accuracy_pct": _pct(sum(r["field_correct"] for r in rs), sum(r["field_present"] for r in rs)),
            "fields_scored": sum(r["field_present"] for r in rs),
            "fields_wrong": sum(r["field_wrong"] for r in rs),
            "fields_missed": sum(r["field_missed"] for r in rs),
            "fields_spurious": sum(r["field_spurious"] for r in rs),
            "silent_errors": sum(len(r["silent_errors"]) for r in rs),
            "traceability_pct": _pct(sum(r["trace_ok"] for r in rs), sum(r["trace_total"] for r in rs)),
            "issue_recall_pct": _pct(tp, len(exp)),
            "issues_expected": len(exp),
            "recall_by_category_pct": {k: _pct(a, b) for k, (a, b) in sorted(cat.items())},
            "recall_by_category_counts": {k: f"{a}/{b}" for k, (a, b) in sorted(cat.items())},
            "issue_precision_pct": _pct(tp, tp + fp),
            "false_positives": fp,
            "clean_pass_pct": _pct(sum(r["clean_pass"] for r in clean), len(clean)),
            "clean_packages": len(clean),
            "defect_block_pct": _pct(sum(r["defect_blocked"] for r in dirty), len(dirty)),
            "defect_packages": len(dirty),
            "record_recall_pct": {k: _pct(a, b) for k, (a, b) in recs.items()},
            "median_seconds": round(statistics.median(r["seconds"] for r in rs), 3) if rs else None,
            "mean_review_touches": round(statistics.mean(r["review_touches"] for r in rs), 1) if rs else None,
            "mean_bulk_approvable": round(statistics.mean(r["bulk_approvable"] for r in rs), 1) if rs else None,
        }

    groups = defaultdict(list)
    for r in results:
        groups[r["group"]].append(r)
    in_dist = [r for r in results if r["group"] != "style C (held-out phrasing)"]
    by_code = defaultdict(lambda: [0, 0])
    for r in results:
        for e in r["expected"]:
            by_code[e["code"]][1] += 1
            by_code[e["code"]][0] += int(e["detected"])
    fp_codes = Counter(fp["code"] for r in results for fp in r["false_positives"])
    return {
        "overall": block(results),
        "in_distribution (A, B, scanned)": block(in_dist),
        "by_group": {g: block(rs) for g, rs in sorted(groups.items())},
        "recall_by_issue_code": {k: f"{a}/{b}" for k, (a, b) in sorted(by_code.items())},
        "false_positive_codes": dict(fp_codes.most_common()),
    }


def to_markdown(summary: dict, results: list[dict], meta: dict) -> str:
    def row(name, b):
        rc = b["recall_by_category_pct"]
        return (f"| {name} | {b['packages']} | {b['field_accuracy_pct']}% | {b['silent_errors']} | "
                f"{rc.get('missing', '–')}% | {rc.get('contradiction', '–')}% | {rc.get('validity', '–')}% | "
                f"{b['issue_precision_pct']}% | {b['traceability_pct']}% | {b['clean_pass_pct']}% | {b['defect_block_pct']}% |")

    lines = [
        "# PacketPilot benchmark results", "",
        f"Generated {meta['generated']} · extractor `{meta['extractor']}` · {meta['packages']} packages · "
        f"generator seed {meta.get('seed', 'n/a')}", "",
        "| Slice | Pkgs | Field accuracy | Silent errors | Missing recall | Contradiction recall | Validity recall | Issue precision | Traceability | Clean pass | Defect blocked |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        row("**Overall**", summary["overall"]),
        row("In-distribution (A, B, scanned)", summary["in_distribution (A, B, scanned)"]),
    ]
    for g, b in summary["by_group"].items():
        lines.append(row(g, b))
    o = summary["overall"]
    lines += [
        "", "## Detail", "",
        f"* Fields scored: {o['fields_scored']} (wrong {o['fields_wrong']}, missed {o['fields_missed']}, spurious {o['fields_spurious']})",
        f"* Injected issues: {o['issues_expected']} · false positives: {o['false_positives']}",
        f"* Record recall: " + ", ".join(f"{k} {v}%" for k, v in o["record_recall_pct"].items()),
        f"* Median processing time per package: {o['median_seconds']} s",
        f"* Mean fields needing individual review per package: {o['mean_review_touches']} "
        f"(mean bulk-approvable: {o['mean_bulk_approvable']})",
        "", "### Recall by issue code", "", "| Code | Detected |", "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in summary["recall_by_issue_code"].items()]
    lines += ["", "### False positives by code", "", "| Code | Count |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["false_positive_codes"].items()] or ["| none | 0 |"]
    misses = [(r["package_id"], e["code"], e.get("field_path") or e.get("subject")) for r in results for e in r["expected"] if not e["detected"]]
    lines += ["", "### Missed injected issues", ""]
    lines += [f"* {pid}: {code} ({what})" for pid, code, what in misses] or ["* none"]
    silent = [(r["package_id"], p) for r in results for p in r["silent_errors"]]
    lines += ["", "### Silent errors (wrong value marked auto-verified)", ""]
    lines += [f"* {pid}: {p}" for pid, p in silent] or ["* none"]
    return "\n".join(lines) + "\n"


def run(packages: Path, out: Path, settings: Settings | None = None, llm_client=None) -> dict:
    settings = settings or Settings()
    pdirs = sorted(p for p in packages.iterdir() if (p / "ground_truth.json").exists())
    results = [evaluate_package(p, settings, llm_client) for p in pdirs]
    summary = summarise(results)
    gen = packages / "generator.json"
    mode, extractors = resolve_extractors(settings, llm_client)
    meta = {
        "generated": date.today().isoformat(),
        "extractor": f"{mode} ({'+'.join(e.version for e in extractors)})",
        "packages": len(results),
        "seed": json.loads(gen.read_text()).get("seed") if gen.exists() else "n/a",
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "packages": results}, indent=2, default=str))
    (out / "RESULTS.md").write_text(to_markdown(summary, results, meta))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packages", default="benchmark/packages")
    ap.add_argument("--out", default="benchmark/results")
    ap.add_argument("--extractor", default=None, help="rules | llm | hybrid | auto")
    a = ap.parse_args()
    settings = Settings()
    if a.extractor:
        object.__setattr__(settings, "extractor", a.extractor)
    summary = run(Path(a.packages), Path(a.out), settings)
    print(json.dumps(summary["overall"], indent=2))


if __name__ == "__main__":
    main()
