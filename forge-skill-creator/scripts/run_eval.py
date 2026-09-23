#!/usr/bin/env python3
# Modified by Tailcall for Forge, 2026 — original: anthropics/skills
"""Run trigger evaluation for a skill description.

Tests whether a skill's description causes the agent to trigger (consult the
skill) for a set of queries. Outputs results as JSON.

Each query is run through the Forge runner (`forge3`) over its JSON-RPC stdio
protocol — see `scripts/forge_client.py`. The candidate description is staged
as a real skill in a throwaway project's `.forge/skills/<name>/SKILL.md`, the
same place Forge loads project skills from, so the description is evaluated
exactly as a user would experience it.
"""

import argparse
import json
import shutil
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from scripts.forge_client import ForgeError, ForgeTimeout, called_skill, run_prompt
from scripts.utils import parse_skill_md

# Outcome of a single run. A timeout is deliberately its own outcome rather
# than a `False` trigger: the agent never got to decide, so folding it into
# "did not trigger" would blame the description for a slow machine and send
# the improver off rewriting a description that was fine.
TRIGGERED = "triggered"
NOT_TRIGGERED = "not_triggered"
ERRORED = "errored"


def write_candidate_skill(project_root: Path, skill_name: str, description: str) -> Path:
    """Stage the candidate description as a project skill.

    Only the frontmatter matters for triggering: the agent decides whether to
    call `skill_view` from the name + description alone, so the body is a stub.
    """
    skill_dir = project_root / ".forge" / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    # A YAML block scalar keeps quotes and colons in the description safe.
    indented_desc = "\n  ".join(description.split("\n"))
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\n"
        f"name: {skill_name}\n"
        f"description: |\n"
        f"  {indented_desc}\n"
        f"---\n\n"
        f"# {skill_name}\n\n"
        f"This skill handles: {description}\n"
    )
    return skill_dir


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    model: str | None = None,
    provider: str | None = None,
    binary: str | None = None,
) -> str:
    """Run a single query and return its outcome.

    Returns one of `TRIGGERED`, `NOT_TRIGGERED` or `ERRORED`. Each query gets
    its own temp project so parallel runs cannot see each other's staged skill.
    Triggering means the agent called `skill_view` (or searched) for this skill
    name — announced before the tool runs, so the answer is known as soon as
    the decision is made.

    Timeouts and runner errors both come back as `ERRORED` and are excluded
    from the trigger rate rather than counted against the description.
    """
    project_root = Path(tempfile.mkdtemp(prefix=f"forge-eval-{skill_name}-"))
    try:
        write_candidate_skill(project_root, skill_name, skill_description)
        result = run_prompt(
            query,
            cwd=str(project_root),
            timeout=timeout,
            model=model,
            provider=provider,
            binary=binary,
            raise_on_timeout=True,
        )
        return TRIGGERED if called_skill(result, skill_name) else NOT_TRIGGERED
    except ForgeTimeout:
        print(
            f"Warning: timed out after {timeout}s (counted as an error, not a "
            f"non-trigger): {query[:60]}",
            file=sys.stderr,
        )
        return ERRORED
    except ForgeError as exc:
        print(f"Warning: forge3 error for query: {exc}", file=sys.stderr)
        return ERRORED
    finally:
        shutil.rmtree(project_root, ignore_errors=True)


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
    provider: str | None = None,
    binary: str | None = None,
) -> dict:
    """Run the full eval set and return results."""
    results = []

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_info = {}
        for item in eval_set:
            for run_idx in range(runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"],
                    skill_name,
                    description,
                    timeout,
                    model,
                    provider,
                    binary,
                )
                future_to_info[future] = (item, run_idx)

        query_outcomes: dict[str, list[str]] = {}
        query_items: dict[str, dict] = {}
        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_outcomes:
                query_outcomes[query] = []
            try:
                query_outcomes[query].append(future.result())
            except Exception as e:
                print(f"Warning: query failed: {e}", file=sys.stderr)
                query_outcomes[query].append(ERRORED)

    for query, outcomes in query_outcomes.items():
        item = query_items[query]
        triggers = sum(1 for o in outcomes if o == TRIGGERED)
        errors = sum(1 for o in outcomes if o == ERRORED)
        # Scored runs are the ones where the agent actually got to decide.
        scored = len(outcomes) - errors
        should_trigger = item["should_trigger"]

        if scored == 0:
            # Every run errored, so there is no evidence either way. Marking
            # this as a failure would be indistinguishable from a genuinely
            # bad description; surface it as unscored instead.
            trigger_rate = None
            did_pass = None
        else:
            trigger_rate = triggers / scored
            did_pass = (
                trigger_rate >= trigger_threshold
                if should_trigger
                else trigger_rate < trigger_threshold
            )

        results.append({
            "query": query,
            "should_trigger": should_trigger,
            "trigger_rate": trigger_rate,
            "triggers": triggers,
            "runs": scored,
            "errors": errors,
            "attempts": len(outcomes),
            "pass": did_pass,
        })

    passed = sum(1 for r in results if r["pass"] is True)
    failed = sum(1 for r in results if r["pass"] is False)
    unscored = sum(1 for r in results if r["pass"] is None)
    total_errors = sum(r["errors"] for r in results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "unscored": unscored,
            "errored_runs": total_errors,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Run trigger evaluation for a skill description")
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override description to test")
    parser.add_argument("--num-workers", type=int, default=10, help="Number of parallel workers")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout per query in seconds")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--model", required=True, help="forge3 model id — use the model powering the calling session")
    parser.add_argument("--provider", required=True, help="Provider hosting the model — use the provider of the calling session")
    parser.add_argument("--binary", default=None, help="Path to the forge3 binary (default: forge3 on PATH)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description

    if args.verbose:
        print(f"Evaluating: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
        provider=args.provider,
        binary=args.binary,
    )

    if args.verbose:
        summary = output["summary"]
        line = f"Results: {summary['passed']}/{summary['total']} passed"
        if summary["errored_runs"]:
            line += (
                f" ({summary['errored_runs']} run(s) errored or timed out, "
                f"{summary['unscored']} quer(ies) unscored)"
            )
        print(line, file=sys.stderr)
        for r in output["results"]:
            status = {True: "PASS", False: "FAIL", None: "ERR "}[r["pass"]]
            rate_str = f"{r['triggers']}/{r['runs']}" if r["runs"] else "no scored runs"
            err_str = f" errors={r['errors']}" if r["errors"] else ""
            print(f"  [{status}] rate={rate_str}{err_str} expected={r['should_trigger']}: {r['query'][:70]}", file=sys.stderr)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
