"""Load fictional benchmark packages into the database for a demo.

    python scripts/seed_demo.py --n 12

Every package is processed by the real pipeline. To give the operations
dashboard something to show, a few clean packages are then reviewed by a
scripted reviewer named "demo-script" (bulk approval + mark ready) and one
defective package gets a client information request. These actions are
recorded in the audit log under that name, so they are never mistaken for
human review.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from packetpilot.pipeline import InputFile
from packetpilot.service import PacketPilotService, ServiceError


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packages", default="benchmark/packages")
    ap.add_argument("--n", type=int, default=12)
    a = ap.parse_args()
    svc = PacketPilotService()
    root = Path(a.packages)
    pdirs = sorted(p for p in root.iterdir() if (p / "ground_truth.json").exists())[: a.n]
    readied = requested = 0
    for p in pdirs:
        gt = json.loads((p / "ground_truth.json").read_text())
        files = [InputFile(f, (p / f).read_bytes()) for f in gt["files"]]
        sid = svc.create_submission(files, actor="demo-script", as_of=date.fromisoformat(gt["as_of"]), source="demo")
        state = svc.process(sid)
        open_blocking = [i for i in state.issues if i.blocking]
        if not gt["defects"] and readied < 3:
            svc.approve_verified(sid, "demo-script")
            for path, f in svc.state(sid).fields.items():
                if f.value is not None and f.status not in ("accepted", "corrected"):
                    svc.review_field(sid, path, "accept", "demo-script", note="Checked against source page")
            try:
                svc.mark_ready(sid, "demo-script")
                readied += 1
            except ServiceError:
                pass
        elif gt["defects"] and open_blocking and requested < 2:
            try:
                svc.request_info(sid, "demo-script")
                requested += 1
            except ServiceError:
                pass
        print(f"{p.name}: {state.reference} {len(state.documents)} docs, {len(open_blocking)} blocking issues")
    print(f"Seeded {len(pdirs)} submissions ({readied} marked ready, {requested} awaiting info).")


if __name__ == "__main__":
    main()
