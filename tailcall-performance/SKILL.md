---
name: tailcall-performance
description: Investigate an observed performance problem by measurement — latency, throughput, memory, CPU, build or startup time that got worse or is too slow. Use whenever the user reports "this got slower", "p99 regressed", "memory keeps growing", "startup takes forever", "is this benchmark real?", or asks to profile/speed up a specific workload, even if they never say "performance". Covers baselines, repeat runs with variance, profiling with the stack's own tools, one-hypothesis-at-a-time fixes and honest before/after reporting. Not for stylistic "make this faster" cleanups with no measured symptom, or for load-testing production.
---

# Measurement-driven performance investigation

The failure mode this skill exists to prevent: reading code, guessing a
bottleneck, "optimizing" it, running once, and declaring a win. That produces
changes that are unmeasured, often irrelevant, sometimes incorrect, and
occasionally slower. Every claim you make should be backed by numbers you can
point to on disk, gathered the same way before and after.

## 0. Scope and budget first

- **Pin the symptom.** What is slow, for whom, measured how? Turn it into one
  *workload* (a command, request, test, or input that reproduces it) and one
  *primary metric* (wall time, p50/p95 latency, req/s, peak RSS, CPU time,
  allocations). If the user can't name a symptom, ask — or measure first and
  be willing to conclude nothing needs changing.
- **Set a budget** before running anything: a rough cap on wall time and
  iterations (e.g. "≤10 min of benchmark time, ≤3 hypotheses"). Say it. Stop
  and report when you hit it rather than silently expanding.
- **Stay local.** Only run against local processes or environments the user
  has put in scope. Never generate load against production, shared staging or
  third-party APIs without explicit authorization — a benchmark against a live
  service is a load test on someone else's system.

## 1. Baseline — repeatable, with metadata

1. Record the environment: `python3 <skill-dir>/scripts/env_meta.py` (OS, CPU
   count, tool versions, git commit + dirty state, load average). Measurements
   without this are not comparable later.
2. Build the way the user ships (release/optimized builds; `--release`,
   `NODE_ENV=production`, no debugger). Debug-build timings are a common
   false lead.
3. Run the workload **repeatedly** — warm-up runs discarded, then ≥5 (ideally
   10+) measured runs. Use `scripts/bench.py` if no stack-native harness exists:

   ```bash
   python3 <skill-dir>/scripts/bench.py --runs 10 --warmup 2 \
     --out perf/baseline.json -- <command...>
   ```

   It records every raw sample, median, mean, stdev, min/max, coefficient of
   variation and peak RSS (where the OS reports it). Prefer the stack's own
   benchmark tool when present (`hyperfine`, `cargo bench`/criterion,
   `pytest-benchmark`, `go test -bench -count`, `vitest bench`) — and still keep
   raw output.
4. Check noise: if CV is above ~5%, the machine or workload is noisy. Increase
   runs, close background load, pin inputs, or report the noise — do not
   compare medians that differ by less than the spread.
5. Keep raw evidence under a scratch directory (e.g. `perf/` in the work tree,
   untracked, or a temp dir) and tell the user where it is.

## 2. Profile before hypothesizing

Find where time/memory actually goes using whatever is installed — check with
`command -v` first; do not install system profilers without asking.
`references/profilers.md` lists per-stack options (py-spy/cProfile, perf/
flamegraph/samply, `node --cpu-prof`, pprof, heaptrack/tracemalloc, …) and
cheap fallbacks (timing instrumentation, `/usr/bin/time -v`, bisecting inputs).

If no profiler is available, say so and use coarser evidence (timers around
phases, git bisect across commits, scaling the input size). Profiles, flame
graphs or screenshots are evidence to interpret, not a replacement for the
before/after measurement. For long traces you may delegate interpretation to
a sub-agent, giving it the raw file and asking for top frames with numbers.

## 3. One hypothesis at a time

Write the hypothesis down: "X accounts for ~N% of time because Y; changing Z
should reduce the metric by roughly M." Then:

- Make the **narrowest** change that tests it. One change per measurement —
  two changes at once make it impossible to attribute the effect.
- **Preserve correctness and safety.** Run the project's tests after the
  change. Never gain speed by disabling validation, auth, TLS, bounds checks,
  fsync/durability, logging required for audit, or by deleting tests or
  caching results that must be fresh. If a tradeoff (memory for speed,
  staleness for latency) is involved, surface it as a decision for the user.
- Re-measure with the *same* command, runs, and environment. If the change
  doesn't move the metric beyond noise, revert it — however elegant it is.

Experiment matrices (several variants × inputs) are worth scripting; only use
parallel sub-agents/workflows for them when the matrix is genuinely large and
the user opted in — parallel runs on one machine also contend for CPU and
corrupt each other's timings.

## 4. Report honestly

Use this shape:

```markdown
## Performance report
**Workload / metric**: <command>, <metric>  ·  **Env**: <env_meta summary>
**Budget used**: <time, runs>

| Variant | n | median | mean ± sd | min–max | CV | peak RSS |
|---|---|---|---|---|---|---|
| baseline (<commit>) | 10 | … | … | … | … | … |
| change A | 10 | … | … | … | … | … |

**Finding**: <what the profile showed, with numbers>
**Change**: <diff summary> — tests: <command, result>
**Effect**: <median delta and whether it exceeds run-to-run spread>
**Tradeoffs / risks**: …
**Not established**: <what the data does not prove>
**Raw evidence**: <paths>
```

Language discipline: say "correlated with" or "consistent with" unless you
changed one thing and measured the effect. Don't extrapolate a laptop
microbenchmark to production. `scripts/compare.py baseline.json after.json`
prints the delta and flags when distributions overlap.

## Valid "no change" outcomes

These are successes, not failures — report them plainly:

- **No regression**: the measured difference is within noise. Show the
  distributions and stop; do not invent an optimization to justify the effort.
- **No bottleneck worth fixing**: the workload is already dominated by
  something external (network, disk, a dependency) or meets the target.
- **Blocked**: can't reproduce, no representative input, required tool/auth
  missing. Name the blocker and what would unblock it.
