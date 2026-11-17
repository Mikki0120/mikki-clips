"""
Selection eval on the labelled fixtures in tests/fixtures/eval/*.json: score (scoring.combine) + quality bar
(scoring.passes_bar) + greedy non-overlap (scoring.select_clips), as discover() does with the shipped defaults.

  python tools/eval_selection.py            # shipped defaults (DEFAULT_BAR)
  python tools/eval_selection.py --before   # v2 behaviour: score >= 50 only

Prints per fixture: candidates, selected, precision (post-worthy among selected), recall of big moments,
recall of all post-worthy moments, and the selected/missed windows.
"""

import argparse
import json
import sys
from pathlib import Path

from streamclipper.scoring import DEFAULT_BAR, NO_BAR, combine, passes_bar, select_clips

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval"


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def evaluate(doc: dict, bar: dict) -> dict:
    cands = []
    for c in doc["candidates"]:
        c = dict(c, score=combine(c["comp"], c["signals"]))
        ok, why = passes_bar(c, bar)
        cands.append(dict(c, ok=ok, why=why))
    picked = select_clips([c for c in cands if c["ok"]], 0, 0)
    good = [c for c in picked if c["label"]]
    big = [c for c in cands if c["big"]]
    worthy = [c for c in cands if c["label"]]
    return {
        "candidates": len(cands),
        "selected": len(picked),
        "precision": len(good) / len(picked) if picked else 1.0,
        "big_recall": sum(1 for c in big if c in picked) / len(big) if big else 1.0,
        "recall": len(good) / len(worthy) if worthy else 1.0,
        "picked": picked,
        "missed_big": [c for c in big if c not in picked],
        "rows": cands,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--before", action="store_true", help="v2 behaviour (score >= 50, no quality bar)")
    ap.add_argument("--verbose", action="store_true", help="also list every rejected candidate with the reason")
    args = ap.parse_args(argv)
    bar = NO_BAR if args.before else DEFAULT_BAR
    print(f"bar: {'before (score >= 50 only)' if args.before else 'shipped DEFAULT_BAR'}")
    worst = (1.0, 1.0)
    for f in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        r = evaluate(doc, bar)
        worst = (min(worst[0], r["precision"]), min(worst[1], r["big_recall"]))
        print(
            f"{f.stem:<26} candidates {r['candidates']:>3}  selected {r['selected']:>3}  "
            f"precision {r['precision']:.2f}  big recall {r['big_recall']:.2f}  post-worthy recall {r['recall']:.2f}"
        )
        for c in sorted(r["picked"], key=lambda c: c["start"]):
            mark = "ok " if c["label"] else "BAD"
            print(f"    {mark} {_mmss(c['start'])} score {c['score']:>3}  {c['why']:<28} {c['reason']}")
        for c in r["missed_big"]:
            print(f"    MISSED BIG {_mmss(c['start'])} score {c['score']}: {c['why']} ({c['reason']})")
        if args.verbose:
            for c in r["rows"]:
                if not c["ok"]:
                    print(f"    rejected {_mmss(c['start'])} score {c['score']:>3} label {c['label']!s:<5} {c['why']}")
    print(f"worst precision {worst[0]:.2f}, worst big-moment recall {worst[1]:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
