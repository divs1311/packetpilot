"""Command line interface.

    packetpilot process app.pdf loss_run.pdf payroll.xlsx [--as-of 2026-11-02] [--json out.json]
    packetpilot generate --out benchmark/packages --n 48 --seed 7
    packetpilot evaluate --packages benchmark/packages --out benchmark/results
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def _process(a) -> int:
    from .pipeline import InputFile, process_submission
    from .readiness import assess, draft_followup
    from .schema import FIELD_SPECS

    files = [InputFile(Path(p).name, Path(p).read_bytes()) for p in a.files]
    out = process_submission(files, as_of=date.fromisoformat(a.as_of) if a.as_of else None)
    s = out.state
    print(f"{s.reference}  extractor={s.extractor}  {s.processing_ms} ms")
    for d in s.documents:
        print(f"  [{d.doc_type:<19}] {d.filename} ({d.page_count} page(s){', OCR' if d.ocr_pages else ''})")
    print("\nFields")
    for path, f in s.fields.items():
        if f.value is None and not FIELD_SPECS[path].is_required(s.lines()):
            continue
        src = f.selected.evidence if f.selected else None
        where = f"{src.filename} p{src.page}" if src else ""
        print(f"  {FIELD_SPECS[path].label:<32} {str(f.value)[:48]:<48} {f.status:<14} {where}")
    print("\nIssues")
    for i in s.issues:
        if i.status == "open":
            print(f"  {i.severity.upper():<8} {i.code:<28} {i.message}")
    r = assess(s)
    print(f"\nReady for market: {'yes' if r.ready else 'no'} ({len(r.blockers)} blocker(s))")
    if a.followup:
        d = draft_followup(s)
        print(f"\nSubject: {d['subject']}\n\n{d['body']}")
    if a.json:
        Path(a.json).write_text(json.dumps(s.model_dump(mode="json"), indent=2))
        print(f"\nWrote {a.json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="packetpilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process", help="Process a submission package from local files")
    p.add_argument("files", nargs="+")
    p.add_argument("--as-of")
    p.add_argument("--json")
    p.add_argument("--followup", action="store_true", help="Print the client follow-up email draft")
    g = sub.add_parser("generate", help="Generate the synthetic benchmark")
    g.add_argument("--out", default="benchmark/packages")
    g.add_argument("--n", type=int, default=48)
    g.add_argument("--seed", type=int, default=7)
    e = sub.add_parser("evaluate", help="Score the pipeline against the benchmark")
    e.add_argument("--packages", default="benchmark/packages")
    e.add_argument("--out", default="benchmark/results")
    a = ap.parse_args(argv)
    if a.cmd == "process":
        return _process(a)
    if a.cmd == "generate":
        from .bench.generate import generate

        m = generate(Path(a.out), a.n, a.seed)
        print(f"Generated {len(m)} packages in {a.out}")
        return 0
    from .bench.evaluate import run

    summary = run(Path(a.packages), Path(a.out))
    print(json.dumps(summary["overall"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
