#!/usr/bin/env python3
# Modified by Tailcall for Forge, 2026 — original: anthropics/skills
# (New file added by Tailcall.)
"""Render the eval report the agent must post in chat.

The eval report is a *deliverable*, not a side artifact. An HTML file written
to /tmp is invisible to a user reading a chat transcript, and a PR that says
"evals passed" without numbers is unreviewable. This script turns the files the
eval loop already produced — `benchmark.json`, per-run `grading.json`, and the
trigger-eval JSON — into a markdown report the agent prints verbatim.

It is a renderer, not a judge: everything here comes from files on disk, so the
agent cannot accidentally report a number it wishes were true.

Usage:
    python -m scripts.eval_report <workspace>/iteration-N \
        [--trigger-results <run_eval-output.json>] \
        [--review-html <path/to/review.html>] \
        [--model MODEL --provider PROVIDER]
"""

import argparse
import json
import sys
from pathlib import Path

# Config directory names that mean "this run had the skill" vs "this is the
# thing we compare against".
BASELINE_CONFIGS = {"without_skill", "old_skill", "baseline"}


def load_benchmark(workspace: Path) -> dict | None:
    """Read benchmark.json, or None when it hasn't been generated."""
    path = workspace / "benchmark.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not read {path}: {exc}", file=sys.stderr)
        return None


def collect_failures(benchmark: dict) -> list[dict]:
    """Every failed assertion, with the evidence the grader recorded.

    Failures are the actionable part of a report: a pass rate says something is
    wrong, the failed assertion and its evidence say what.
    """
    failures = []
    for run in benchmark.get("runs", []):
        for exp in run.get("expectations", []):
            if exp.get("passed") is False:
                failures.append({
                    "eval_id": run.get("eval_id"),
                    "configuration": run.get("configuration"),
                    "run_number": run.get("run_number"),
                    "text": exp.get("text", "(unnamed assertion)"),
                    "evidence": exp.get("evidence", "(no evidence recorded)"),
                })
    return failures


def collect_contamination(workspace: Path) -> list[dict]:
    """Runs that loaded a skill they shouldn't have.

    Written by the run harness as `contamination.json` next to a run's
    grading.json. Absent files simply mean the check wasn't recorded, which is
    itself worth saying out loud rather than silently reporting "clean".
    """
    findings = []
    for path in sorted(workspace.rglob("contamination.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        skills = data.get("contaminating_skills") or []
        if skills:
            findings.append({
                "run": str(path.parent.relative_to(workspace)),
                "skills": skills,
            })
    return findings


def per_eval_table(benchmark: dict) -> list[str]:
    """Per-eval with-skill vs baseline pass rates and their delta."""
    # Group pass rates by (eval_id, configuration).
    grouped: dict[int, dict[str, list[float]]] = {}
    for run in benchmark.get("runs", []):
        eval_id = run.get("eval_id")
        config = run.get("configuration", "?")
        rate = run.get("result", {}).get("pass_rate", 0.0)
        grouped.setdefault(eval_id, {}).setdefault(config, []).append(rate)

    if not grouped:
        return ["_No per-eval results found in benchmark.json._"]

    configs = [c for c in benchmark.get("run_summary", {}) if c != "delta"]
    primary = next((c for c in configs if c not in BASELINE_CONFIGS), None)
    baseline = next((c for c in configs if c in BASELINE_CONFIGS), None)

    lines = [
        f"| Eval | {primary or 'with_skill'} | {baseline or 'baseline'} | Delta |",
        "|---|---|---|---|",
    ]
    for eval_id in sorted(grouped, key=lambda x: (x is None, x)):
        by_config = grouped[eval_id]

        def mean(config: str | None) -> float | None:
            values = by_config.get(config or "", [])
            return sum(values) / len(values) if values else None

        a, b = mean(primary), mean(baseline)
        fmt = lambda v: "—" if v is None else f"{v*100:.0f}%"
        delta = "—" if a is None or b is None else f"{(a - b)*100:+.0f}pp"
        lines.append(f"| {eval_id} | {fmt(a)} | {fmt(b)} | {delta} |")
    return lines


def trigger_section(trigger_path: Path | None) -> list[str]:
    """Trigger-eval accuracy, with the caveat that makes it interpretable."""
    if trigger_path is None:
        return [
            "_No trigger eval provided._ Note that a trigger eval measures only "
            "whether the description gets the skill loaded — it says nothing "
            "about whether the skill helps once loaded."
        ]
    try:
        data = json.loads(trigger_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [f"_Could not read trigger results ({exc})._"]

    summary = data.get("summary", {})
    passed = summary.get("passed", 0)
    total = summary.get("total", 0)
    errored = summary.get("errored_runs", 0)
    unscored = summary.get("unscored", 0)

    lines = [
        f"- **Trigger accuracy**: {passed}/{total} queries "
        f"({(passed/total*100) if total else 0:.0f}%)",
    ]
    if errored or unscored:
        lines.append(
            f"- **Errored/timed-out runs**: {errored} "
            f"({unscored} quer{'y' if unscored == 1 else 'ies'} unscored — "
            "excluded, not counted as failures)"
        )
    lines.append(
        "- Trigger accuracy is **not** a measure of skill quality: it only "
        "says the description gets the skill loaded."
    )
    return lines


def render_report(
    workspace: Path,
    trigger_path: Path | None = None,
    review_html: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> str:
    """Build the full markdown report."""
    benchmark = load_benchmark(workspace)
    lines: list[str] = ["## Eval report", ""]

    meta = (benchmark or {}).get("metadata", {})
    skill_name = meta.get("skill_name") or workspace.parent.name
    lines.append(f"**Skill**: `{skill_name}`  ")
    lines.append(
        f"**Model/provider**: `{model or meta.get('executor_model', '?')}` / "
        f"`{provider or '?'}`  "
    )
    lines.append(f"**Workspace**: `{workspace}`  ")
    if review_html:
        lines.append(f"**Viewer**: {review_html}")
    lines.append("")

    if benchmark is None:
        lines += [
            "> **No `benchmark.json` found — the behavioural eval has not been "
            "run.** A skill must not be committed, pushed or opened as a PR "
            "until it has been evaluated with-skill against a baseline and the "
            "user has approved the result.",
            "",
        ]
        lines += ["### Trigger eval", ""] + trigger_section(trigger_path)
        return "\n".join(lines)

    # Headline comparison
    run_summary = benchmark.get("run_summary", {})
    configs = [c for c in run_summary if c != "delta"]
    lines += ["### Overall", "", "| Configuration | Pass rate | Time | Tokens |", "|---|---|---|---|"]
    for config in configs:
        stats = run_summary[config]
        pr = stats.get("pass_rate", {})
        tm = stats.get("time_seconds", {})
        tk = stats.get("tokens", {})
        lines.append(
            f"| {config} | {pr.get('mean', 0)*100:.0f}% ± {pr.get('stddev', 0)*100:.0f}% "
            f"| {tm.get('mean', 0):.1f}s | {tk.get('mean', 0):.0f} |"
        )
    delta = run_summary.get("delta", {})
    lines += ["", f"**Delta (pass rate)**: {delta.get('pass_rate', '—')}", ""]

    # Per-eval breakdown
    lines += ["### Per-eval", ""] + per_eval_table(benchmark) + [""]

    # Trigger eval
    lines += ["### Trigger eval", ""] + trigger_section(trigger_path) + [""]

    # Failed assertions
    failures = collect_failures(benchmark)
    lines += ["### Failed assertions", ""]
    if not failures:
        lines.append("_None._")
    else:
        for f in failures:
            lines.append(
                f"- **eval {f['eval_id']}** ({f['configuration']}, run "
                f"{f['run_number']}): {f['text']}"
            )
            lines.append(f"  - Evidence: {f['evidence']}")
    lines.append("")

    # Contamination
    contamination = collect_contamination(workspace)
    lines += ["### Contamination check", ""]
    if contamination:
        lines.append(
            "⚠️ **Contaminated runs** — user-global skills were loaded during "
            "these runs, so the comparison is not \"skill vs no skill\":"
        )
        for c in contamination:
            lines.append(f"- `{c['run']}`: loaded {', '.join(c['skills'])}")
        lines.append("")
        lines.append(
            "Forge has no way to hide user-global skills from a run, so this "
            "cannot currently be prevented — only detected. Read the delta "
            "with this in mind."
        )
    else:
        lines.append(
            "No contamination recorded. Note this is only meaningful if the "
            "harness wrote `contamination.json` for each run; absent files mean "
            "the check did not run, not that the runs were clean."
        )
    lines.append("")

    # The gate
    lines += [
        "### Review gate",
        "",
        "This report needs your sign-off before anything is committed, pushed "
        "or opened as a PR. Does the delta look right, and do the failed "
        "assertions reflect real problems?",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render the in-chat eval report from eval artifacts"
    )
    parser.add_argument("workspace", type=Path, help="Path to <workspace>/iteration-N")
    parser.add_argument("--trigger-results", type=Path, default=None,
                        help="JSON output from run_eval.py")
    parser.add_argument("--review-html", default=None,
                        help="Path or URL of the generated review viewer")
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    args = parser.parse_args()

    if not args.workspace.exists():
        print(f"Workspace not found: {args.workspace}", file=sys.stderr)
        sys.exit(1)

    print(render_report(
        args.workspace,
        trigger_path=args.trigger_results,
        review_html=args.review_html,
        model=args.model,
        provider=args.provider,
    ))


if __name__ == "__main__":
    main()
