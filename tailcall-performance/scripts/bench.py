#!/usr/bin/env python3
"""Repeat a command, record raw wall-time samples and summary stats.

Usage: bench.py [--runs N] [--warmup W] [--timeout S] [--cwd DIR] [--out F.json] -- cmd args...
Portable (stdlib only). Peak RSS of children is reported where `resource` exists
(Linux/macOS); it is the max across all runs, not per run.
"""
import argparse, json, os, statistics, subprocess, sys, time


def child_maxrss_kb():
    try:
        import resource
    except ImportError:
        return None
    v = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return v // 1024 if sys.platform == "darwin" else v  # macOS reports bytes


def summarize(samples):
    s = sorted(samples)
    n = len(s)
    mean = statistics.fmean(s)
    sd = statistics.stdev(s) if n > 1 else 0.0
    q = lambda p: s[min(n - 1, int(round(p * (n - 1))))]
    return {"n": n, "median": statistics.median(s), "mean": mean, "stdev": sd,
            "min": s[0], "max": s[-1], "p90": q(0.9),
            "cv": (sd / mean) if mean else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("command required after --")
    samples, failures = [], 0
    for i in range(a.warmup + a.runs):
        t0 = time.perf_counter()
        try:
            r = subprocess.run(cmd, cwd=a.cwd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=a.timeout)
            ok = r.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
        dt = time.perf_counter() - t0
        if not ok:
            failures += 1
        if i >= a.warmup:
            samples.append(dt)
    res = {"label": a.label, "cmd": cmd, "cwd": a.cwd or os.getcwd(),
           "warmup": a.warmup, "failures": failures, "samples_s": samples,
           "stats_s": summarize(samples), "peak_child_rss_kb": child_maxrss_kb()}
    st = res["stats_s"]
    print(f"{a.label or ' '.join(cmd)}: n={st['n']} median={st['median']:.4f}s "
          f"mean={st['mean']:.4f}±{st['stdev']:.4f}s min={st['min']:.4f} max={st['max']:.4f} "
          f"cv={st['cv']:.1%} failures={failures} peakRSS={res['peak_child_rss_kb']}kB")
    if st["cv"] and st["cv"] > 0.05:
        print("warning: CV > 5% — noisy; add runs or reduce background load before comparing")
    if failures:
        print("warning: some runs failed; timings may be meaningless")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        json.dump(res, open(a.out, "w"), indent=2)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
