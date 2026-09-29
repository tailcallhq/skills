#!/usr/bin/env python3
"""Compare two bench.py outputs. Reports median delta, a bootstrap 95% CI of the
median difference, and whether the ranges overlap. Stdlib only.

Usage: compare.py baseline.json candidate.json
"""
import json, random, statistics, sys


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    a, b = (json.load(open(p)) for p in sys.argv[1:])
    xa, xb = a["samples_s"], b["samples_s"]
    ma, mb = statistics.median(xa), statistics.median(xb)
    rng = random.Random(0)
    diffs = sorted(statistics.median(rng.choices(xb, k=len(xb))) -
                   statistics.median(rng.choices(xa, k=len(xa))) for _ in range(2000))
    lo, hi = diffs[50], diffs[1949]
    overlap = min(xa) <= max(xb) and min(xb) <= max(xa)
    pct = (mb - ma) / ma * 100 if ma else float("nan")
    print(f"baseline median {ma:.4f}s (n={len(xa)})  candidate median {mb:.4f}s (n={len(xb)})")
    print(f"delta {mb - ma:+.4f}s ({pct:+.1f}%)  bootstrap 95% CI of median diff [{lo:+.4f}, {hi:+.4f}]s")
    if lo <= 0 <= hi:
        verdict = "NOT distinguishable from noise (CI contains 0)"
    else:
        verdict = "faster" if hi < 0 else "slower"
        verdict = f"candidate {verdict} beyond resampling noise"
    print(f"ranges overlap: {overlap}  →  {verdict}")
    if len(xa) < 5 or len(xb) < 5:
        print("warning: fewer than 5 samples per side; treat as indicative only")


if __name__ == "__main__":
    main()
