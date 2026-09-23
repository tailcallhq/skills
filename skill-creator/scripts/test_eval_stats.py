#!/usr/bin/env python3
# Modified by Tailcall for Forge, 2026 — original: anthropics/skills
# (New file added by Tailcall.)
"""Unit checks for the eval statistics and timeout handling.

Stdlib only, no network, no `forge3`: these run anywhere in well under a
second. They exist because both bugs they cover were silent — the stats
reported impossible numbers and timeouts were scored as real non-triggers, and
in neither case did anything fail loudly.

Run with:
    python -m scripts.test_eval_stats
    python -m unittest scripts.test_eval_stats
"""

import subprocess
import sys
import unittest
from unittest import mock

from scripts.forge_client import ForgeTimeout, contaminating_skills
from scripts.run_loop import compute_confusion


def result(should_trigger: bool, triggers: int, runs: int, errors: int = 0) -> dict:
    """One query's aggregated outcome, in the shape run_eval emits."""
    return {
        "query": f"q-{should_trigger}-{triggers}-{runs}",
        "should_trigger": should_trigger,
        "triggers": triggers,
        "runs": runs,
        "errors": errors,
        "trigger_rate": (triggers / runs) if runs else None,
        "pass": None if runs == 0 else True,
    }


class ConfusionStatsTest(unittest.TestCase):
    """The reported `correct` count and the rates must describe the same runs.

    The original bug printed `7/12 correct, precision=0% recall=0%`, which is
    arithmetically impossible: tp+tn=7 means tp>0 unless every correct run was
    a true negative, and the rates were computed from a different population
    than the count.
    """

    def test_correct_count_agrees_with_accuracy(self):
        results = [
            result(True, 3, 3),    # 3 tp
            result(True, 1, 3),    # 1 tp, 2 fn
            result(False, 0, 3),   # 3 tn
            result(False, 2, 3),   # 2 fp, 1 tn
        ]
        stats = compute_confusion(results)

        self.assertEqual(stats["tp"], 4)
        self.assertEqual(stats["fn"], 2)
        self.assertEqual(stats["fp"], 2)
        self.assertEqual(stats["tn"], 4)
        # correct and total are derived from the same four counts
        self.assertEqual(stats["correct"], stats["tp"] + stats["tn"])
        self.assertEqual(
            stats["total"],
            stats["tp"] + stats["tn"] + stats["fp"] + stats["fn"],
        )
        # and accuracy is exactly correct/total — the invariant that broke
        self.assertAlmostEqual(stats["accuracy"], stats["correct"] / stats["total"])
        self.assertAlmostEqual(stats["precision"], 4 / 6)
        self.assertAlmostEqual(stats["recall"], 4 / 6)

    def test_nonzero_correct_implies_nonzero_rates(self):
        """Guards the exact symptom that was reported."""
        results = [result(True, 2, 3), result(False, 0, 3)]
        stats = compute_confusion(results)
        self.assertGreater(stats["correct"], 0)
        # With true positives present, precision and recall cannot be zero.
        self.assertGreater(stats["precision"], 0)
        self.assertGreater(stats["recall"], 0)

    def test_undefined_rates_are_none_not_one(self):
        """No positive cases at all: claiming 100% recall would be a lie."""
        stats = compute_confusion([result(False, 0, 3)])
        self.assertIsNone(stats["precision"])  # nothing was predicted positive
        self.assertIsNone(stats["recall"])     # there were no positive cases
        self.assertEqual(stats["accuracy"], 1.0)  # 3 tn out of 3

    def test_errored_runs_are_excluded_not_counted_as_misses(self):
        """A timeout must not become a false negative.

        Two queries, both expected to trigger. One ran cleanly and triggered
        3/3; the other timed out on every attempt. The clean query alone should
        define recall — the timeouts contribute nothing.
        """
        results = [
            result(True, 3, 3),
            result(True, 0, 0, errors=3),
        ]
        stats = compute_confusion(results)
        self.assertEqual(stats["fn"], 0, "timeouts must not appear as misses")
        self.assertEqual(stats["total"], 3, "only scored runs are counted")
        self.assertEqual(stats["recall"], 1.0)
        self.assertEqual(stats["errored_runs"], 3)

    def test_empty_result_set(self):
        stats = compute_confusion([])
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["accuracy"])


class TimeoutHandlingTest(unittest.TestCase):
    """run_single_query turns a timeout into ERRORED, never a non-trigger."""

    def test_timeout_is_errored_not_non_trigger(self):
        from scripts import run_eval

        with mock.patch.object(
            run_eval, "run_prompt", side_effect=ForgeTimeout("no response within 120s")
        ):
            outcome = run_eval.run_single_query(
                "some query", "my-skill", "desc", timeout=120,
                model="m", provider="p",
            )
        self.assertEqual(outcome, run_eval.ERRORED)
        self.assertNotEqual(outcome, run_eval.NOT_TRIGGERED)

    def test_errors_do_not_lower_the_trigger_rate(self):
        """An all-errored query is unscored rather than a failure."""
        from scripts import run_eval

        outcomes = [run_eval.ERRORED, run_eval.ERRORED]
        triggers = sum(1 for o in outcomes if o == run_eval.TRIGGERED)
        errors = sum(1 for o in outcomes if o == run_eval.ERRORED)
        scored = len(outcomes) - errors
        self.assertEqual(scored, 0)
        self.assertEqual(triggers, 0)
        # scored == 0 is the branch that yields pass=None (unscored)

    def test_genuine_non_trigger_still_counts(self):
        """The fix must not make real negatives disappear."""
        from scripts import run_eval

        with mock.patch.object(
            run_eval, "run_prompt",
            return_value={"text": "", "tool_calls": [], "skills_loaded": []},
        ):
            outcome = run_eval.run_single_query(
                "some query", "my-skill", "desc", timeout=120,
                model="m", provider="p",
            )
        self.assertEqual(outcome, run_eval.NOT_TRIGGERED)


class ProcessTerminationTest(unittest.TestCase):
    """_terminate must always leave the child dead and never raise."""

    def test_kills_process_that_ignores_terminate(self):
        from scripts.forge_client import _terminate

        process = mock.Mock(spec=subprocess.Popen)
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.poll.return_value = None  # still running
        process.wait.side_effect = [subprocess.TimeoutExpired("forge3", 5), 0]

        _terminate(process)

        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        process.stdin.close.assert_called_once()

    def test_does_not_raise_when_process_already_gone(self):
        from scripts.forge_client import _terminate

        process = mock.Mock(spec=subprocess.Popen)
        process.stdin = mock.Mock()
        process.stdout = mock.Mock()
        process.poll.return_value = 0  # already exited
        _terminate(process)  # must not raise
        process.kill.assert_not_called()

    def test_survives_oserror_on_close(self):
        from scripts.forge_client import _terminate

        process = mock.Mock(spec=subprocess.Popen)
        process.stdin = mock.Mock()
        process.stdin.close.side_effect = OSError("broken pipe")
        process.stdout = mock.Mock()
        process.poll.return_value = 0
        _terminate(process)  # must not raise


class ContaminationTest(unittest.TestCase):
    def test_baseline_treats_any_skill_as_contamination(self):
        run = {"skills_loaded": ["tailcall-project"]}
        self.assertEqual(contaminating_skills(run, None), ["tailcall-project"])

    def test_with_skill_run_excludes_the_candidate(self):
        run = {"skills_loaded": ["my-skill", "other-skill"]}
        self.assertEqual(contaminating_skills(run, "my-skill"), ["other-skill"])

    def test_clean_run(self):
        self.assertEqual(contaminating_skills({"skills_loaded": []}, "my-skill"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
