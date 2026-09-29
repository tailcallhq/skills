# Profilers by stack

Check availability with `command -v <tool>` before relying on one. Ask before
installing anything system-wide; `pip install --user`/project dev-deps are
lighter but still a change to mention.

| Stack | CPU | Memory | Built-in / zero-install fallback |
|---|---|---|---|
| Python | `py-spy record -o prof.svg -- cmd`, `python -m cProfile -o out.prof` + `pstats` | `tracemalloc`, `memray` | `cProfile` (stdlib), `time.perf_counter` around phases |
| Node/TS | `node --cpu-prof` (writes `.cpuprofile`), `0x`, `clinic flame` | `node --heap-prof`, `--inspect` heap snapshots | `console.time`, `performance.now()`, `process.memoryUsage()` |
| Rust | `cargo flamegraph`, `samply record`, `perf record -g` | `heaptrack`, `dhat` crate | `std::time::Instant`, criterion benches, build `--release` |
| Go | `go test -cpuprofile`, `net/http/pprof`, `go tool pprof` | `-memprofile`, pprof heap | `testing.B` with `-count=10` + `benchstat` |
| JVM | async-profiler, JFR (`-XX:+FlightRecorder`) | JFR, heap dumps | JMH |
| Any process | `perf stat`/`perf record` (Linux), `sample`/Instruments (macOS), ETW/WPR (Windows) | `/usr/bin/time -v` (Linux, peak RSS), `/usr/bin/time -l` (macOS) | `hyperfine`, `scripts/bench.py` |
| Web frontend | browser devtools performance panel (external; hand off if no browser driver) | devtools memory | Lighthouse CLI if installed |

Tips
- Profile the optimized build; debug builds distort hot spots.
- Sampling profilers are safe for long runs; tracing/instrumenting profilers
  inflate short calls — confirm hot spots with a timer before acting.
- Startup regressions: measure import/module load (`python -X importtime`,
  `node --cpu-prof` on the entrypoint, `cargo build --timings` for build time).
- Regression between two commits with no obvious cause: `git bisect run`
  using a bench threshold script, within the agreed budget.
- Memory growth: take measurements at several input sizes/durations; a leak
  grows with time or iterations, a high-water mark does not.
