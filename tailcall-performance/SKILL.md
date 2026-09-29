---
name: tailcall-performance
description: Use before touching code whenever a task involves speed, latency, throughput, memory, CPU, build or startup time — "X got slower", "optimize this", "it feels slow", "speed up", "p99 regressed", "memory keeps growing", "profile this", "is it fast enough", or confirming a benchmark result or "N% faster" claim for a PR — even if the user never says "performance". Also use before concluding code does not need optimizing. Not for readability refactors with no speed or resource goal, or for load-testing production.
---

# Measurement-driven performance investigation

The failure mode this skill exists to prevent: reading code, guessing a
bottleneck, "optimizing" it, running once, and declaring a win. That produces
changes that are unmeasured, often irrelevant, sometimes incorrect, and
occasionally slower. Every claim you make should be backed by numbers you can
point to on disk, gathered the same way before and after.

## Red flags — stop and measure instead

| You are thinking... | Reality |
|---|---|
| "The bottleneck is obvious from the code" | Obvious-looking code is wrong about the hot spot often enough that one profile run (seconds) is cheaper than being wrong. Measure anyway. |
| "One timing before and after is enough" | A single run can be off by 30%+ on a shared machine. Without n≥5 and a spread you cannot tell change from noise. |
| "The user already measured it" | Users bring one favourable run. Reproduce it before repeating the claim. |
| "The tests pass, so it's equivalent" | Tests cover the happy path. Compare old and new on inputs the tests don't cover. |
| "I'll clean this up while I'm here" | Unrelated edits in the same change make the speed-up unattributable. Separate commit. |
| "It's already fast, nothing to report" | "Fast" is a measurement: give n, median, spread and the target. |

## Situations this covers

- **Something got slower or is too slow** — find the cause, fix it, prove it.
- **"Optimize this" / "it feels slow"** — measure against the user's target
  first; often the right answer is "no change needed" with numbers.
- **"Confirm this speedup"** — audit the benchmark itself, re-measure both
  sides, and check the faster code still behaves the same.

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
5. **Audit the benchmark before trusting it** (yours or the user's): does the
   timed region include setup, I/O, sleeps, randomness, JIT/cache warm-up or
   process startup that swamp the thing being compared? Is the input size
   representative — 10 rows can hide an O(n²) that millions expose? Fix or
   report flaws before comparing numbers.
6. Keep raw evidence under a scratch directory (e.g. `perf/` in the work tree,
   untracked, or a temp dir) and tell the user where it is. Always save the
   baseline and after JSON; do not leave evidence only in chat.

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

- If the fix is invasive (new dependency, architecture/data-model change,
  caching with invalidation, concurrency), present a short preflight first —
  what changes, expected gain, risks, how to verify and revert — and let the
  user confirm. Small local fixes can go ahead.
- Make the **narrowest** change that tests it. One change per measurement —
  two changes at once make it impossible to attribute the effect.
- **Preserve correctness and safety.** Run the project's tests after the
  change. Never gain speed by disabling validation, auth, TLS, bounds checks,
  fsync/durability, logging required for audit, or by deleting tests or
  caching results that must be fresh. If a tradeoff (memory for speed,
  staleness for latency) is involved, surface it as a decision for the user.
- **Check behaviour beyond the tests.** Existing tests often cover only the
  happy path, so a faster variant can silently change semantics. Before
  claiming a change (yours or the user's) is safe, compare old vs new on inputs
  the tests don't cover: other types (bytes vs str, None, empty), edge values,
  malformed input, error types/messages, ordering, and large inputs. A quick
  side-by-side script is enough; report any divergence as a correctness finding,
  even when the task was only "is it faster?".
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
**Behaviour outside tests**: <inputs compared old vs new, divergences>
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
