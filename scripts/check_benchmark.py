"""CI gate: regenerate the benchmark, evaluate, and fail on regression.

Thresholds apply to the in-distribution slice (styles A, B and scanned). The
held-out style C slice is reported but not gated: it exists to show how the
deterministic extractor degrades, and that it degrades *safely* - so the
zero-silent-error gate covers every slice.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from packetpilot.bench.evaluate import run
from packetpilot.bench.generate import generate

GATES = {
    "field_accuracy_pct": 99.0,
    "issue_recall_pct": 95.0,
    "issue_precision_pct": 95.0,
    "traceability_pct": 100.0,
}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        pk, out = Path(tmp) / "packages", Path(tmp) / "results"
        generate(pk, 48, 7)
        summary = run(pk, out)
    ind = summary["in_distribution (A, B, scanned)"]
    failures = [f"{k}: {ind[k]} < {v}" for k, v in GATES.items() if (ind[k] or 0) < v]
    if summary["overall"]["silent_errors"]:
        failures.append(f"silent errors: {summary['overall']['silent_errors']} (must be 0)")
    for k in GATES:
        print(f"{k:22s} {ind[k]}")
    print(f"{'silent_errors':22s} {summary['overall']['silent_errors']}")
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("benchmark gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
