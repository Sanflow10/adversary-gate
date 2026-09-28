"""Regression suite for the three-valued gate.

Every test here corresponds to a failure that was *executed* against the
previous version, not theorized:

* ``test_fail_open_routes_are_closed`` -> the six probes that all returned
  ``should_accept_patch=True``.
* ``test_exit_code_matrix``            -> the matrix where a collection error
  became REGRESSION on one side and an approval on the other.
* ``test_claim_granularity``           -> a claim about a passing test being
  accepted because a sibling test failed.
* ``test_evidence_contains_execution`` -> the evidence log's zero occurrences
  of exit codes / output.
* ``test_cli_exit_codes``              -> confirmed regression returning 0.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = str(Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, SRC)

from cli import EXIT_BLOCK, EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main
from core.circuit_breaker import CircuitBreaker
from core.contestation import Contestation
from core.evidence_log import EvidenceLog
from core.exitmap import classify_exit, describe
from core.gate import Gate
from core.metrics import compute_metrics, compare_models, format_report
from core.path_policy import PathPolicy
from core.quarantine import QuarantineStore
from core.types import (
    AcceptanceCriterion,
    AggressionLevel,
    BugKind,
    CriticClaim,
    Decision,
    ExecutionOutcome,
    ExecState,
    FailureClass,
    GateVerdict,
    Outcome,
)
from sandbox.runner import SandboxResult, build_target
from verifiers.stability import policy_for


def make_gate(**kwargs) -> Gate:
    return Gate([AcceptanceCriterion("AC-1", "Session must reject expired tokens")], **kwargs)


def verdict_for(gate: Gate, classification: FailureClass) -> GateVerdict:
    """Build a verdict whose outcome comes from the table, not from a bool."""
    return GateVerdict(
        CriticClaim("t.py", "t1"),
        classification,
        Outcome.UNVERIFIED,  # replaced below from the authoritative table
        "constructed for test",
    )


def verdict_with(gate: Gate, classification: FailureClass) -> GateVerdict:
    from core.types import outcome_of

    return GateVerdict(
        CriticClaim("t.py", "t1"),
        classification,
        outcome_of(classification),
        f"synthetic {classification.value}",
    )


class TestExitCodeMapping(unittest.TestCase):
    """The measured exit codes must map to states, not to a single bool."""

    def test_each_code_gets_its_own_state(self):
        self.assertIs(classify_exit(0), ExecState.PASS)
        self.assertIs(classify_exit(1), ExecState.FAIL)
        self.assertIs(classify_exit(-1), ExecState.TIMED_OUT)
        for code in (2, 3, 4, 5):
            self.assertIs(
                classify_exit(code), ExecState.UNRUNNABLE, f"exit {code} must be UNRUNNABLE"
            )

    def test_only_exit_one_is_a_failure_signal(self):
        from core.exitmap import UNRUNNABLE_CODES

        # 1 is the sole code that means "the test ran and did not like it".
        self.assertNotIn(1, UNRUNNABLE_CODES)
        self.assertEqual(UNRUNNABLE_CODES, frozenset({2, 3, 4, 5}))

    def test_describe_covers_every_code_we_measured(self):
        for code in (0, 1, -1, 2, 3, 4, 5):
            self.assertNotIn("unexpected", describe(code))


class TestExitMatrix(unittest.TestCase):
    """Replay of the measured matrix, now expecting the safe answer."""

    def _run(self, baseline_codes, patch_codes) -> GateVerdict:
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base_state, _, _ = gate._resolve_side(baseline_codes, policy, label="baseline")
        patch_state, _, _ = gate._resolve_side(patch_codes, policy, label="patch")
        outcome = ExecutionOutcome(
            base_state,
            patch_state,
            baseline_failures=sum(1 for c in baseline_codes if c != 0),
            patch_failures=sum(1 for c in patch_codes if c != 0),
            run_count=policy.runs,
            baseline_exit_codes=tuple(baseline_codes),
            patch_exit_codes=tuple(patch_codes),
        )
        return gate.classify(CriticClaim("t.py", "t1"), outcome)

    def test_real_failure_is_refuted(self):
        v = self._run([0, 0, 0], [1, 1, 1])
        self.assertIs(v.outcome, Outcome.REFUTED)
        self.assertIs(v.classification, FailureClass.REGRESSION)

    def test_collection_error_in_patch_alone_is_not_a_regression(self):
        # Previously: REGRESSION -> blocked a good patch on a harness fault.
        v = self._run([0, 0, 0], [2, 2, 2])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(v.classification, FailureClass.UNRUNNABLE)

    def test_symmetric_harness_fault_is_not_an_approval(self):
        # Previously: INVALID -> accepted=False -> should_accept_patch=True.
        v = self._run([2, 2, 2], [2, 2, 2])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)

    def test_no_tests_collected_is_not_a_regression(self):
        v = self._run([0, 0, 0], [5, 5, 5])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)

    def test_baseline_only_failure_does_not_discard_the_claim(self):
        # Previously: DISCARDED ("patch is clean") from a broken baseline.
        v = self._run([2, 2, 2], [0, 0, 0])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)

    def test_timeout_with_clean_baseline_is_a_hang(self):
        v = self._run([0, 0, 0], [-1, -1, -1])
        self.assertIs(v.outcome, Outcome.REFUTED)
        self.assertIs(v.classification, FailureClass.HANG)

    def test_timeout_on_both_sides_is_inconclusive(self):
        v = self._run([-1, -1, -1], [-1, -1, -1])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)

    def test_unrunnable_contaminates_even_a_failing_patch(self):
        # A patch that both fails AND cannot fully run: we cannot attribute.
        v = self._run([0, 0, 0], [1, 1, 2])
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(v.classification, FailureClass.UNRUNNABLE)


class TestFailOpenRoutesAreClosed(unittest.TestCase):
    """The six probes that all returned True before must not merge now."""

    def setUp(self):
        self.gate = make_gate()

    def _decision(self, verdicts, rounds=4, coverage=1.0):
        return self.gate.decide(verdicts, rounds, coverage)

    def test_p1_protected_path_violation(self):
        gate = make_gate(path_policy=PathPolicy(["tests/critic.py"]))
        v = gate.verify_claim(
            CriticClaim("tests/x.py", "t"), Path("."), Path("."), changed_paths=["conftest.py"]
        )
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_p2_missing_critic_test_file(self):
        v = self.gate.verify_claim(CriticClaim("nao_existe.py", "t"), Path("."), Path("."))
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(self._decision([v]), Decision.INCONCLUSIVE)

    def test_p3_builder_deletes_critic_test(self):
        gate = make_gate(path_policy=PathPolicy(["tests/critic_test.py"]))
        v = gate.verify_claim(
            CriticClaim("tests/x.py", "t"),
            Path("."),
            Path("."),
            changed_paths=["tests/critic_test.py"],
        )
        self.assertIn("Critic test file", v.reason)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_p4_open_circuit_breaker(self):
        breaker = CircuitBreaker(threshold=1)
        gate = make_gate(circuit_breaker=breaker)
        claim = CriticClaim("nao_existe.py", "same")
        gate.verify_claim(claim, Path("."), Path("."))
        v = gate.verify_claim(claim, Path("."), Path("."))
        self.assertIs(v.classification, FailureClass.CIRCUIT_OPEN)
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_p5_flaky_result(self):
        v = self.gate.classify(
            CriticClaim("t.py", "t"),
            ExecutionOutcome(ExecState.PASS, ExecState.FAIL, 0, 2, 3),
        )
        self.assertIs(v.classification, FailureClass.FLAKY)
        self.assertIs(self._decision([v]), Decision.INCONCLUSIVE)

    def test_p6_no_verdicts_at_all(self):
        # "Nothing was checked" must not read as "nothing is wrong".
        self.assertIs(self.gate.decide([], 4, 1.0), Decision.INCONCLUSIVE)
        self.assertFalse(self.gate.should_accept_patch([], 4, 1.0))

    def test_every_failure_class_has_a_declared_outcome(self):
        """A FailureClass missing from the table is a fail-open bug in waiting."""
        for classification in FailureClass:
            from core.types import CLASSIFICATION_OUTCOME

            self.assertIn(classification, CLASSIFICATION_OUTCOME)

    def test_no_error_class_maps_to_merge_licence(self):
        from core.types import CLASSIFICATION_OUTCOME

        for classification in (
            FailureClass.INVALID,
            FailureClass.UNRUNNABLE,
            FailureClass.CIRCUIT_OPEN,
            FailureClass.FLAKY,
            FailureClass.NOT_EXECUTED,
        ):
            self.assertIs(CLASSIFICATION_OUTCOME[classification], Outcome.UNVERIFIED)


class TestDecisionSemantics(unittest.TestCase):
    def setUp(self):
        self.gate = make_gate()

    def test_all_verified_merges(self):
        vs = [verdict_with(self.gate, FailureClass.CLAIM_DISCARDED)] * 3
        self.assertIs(self.gate.decide(vs, 4, 1.0), Decision.MERGE)

    def test_single_refuted_blocks(self):
        vs = [
            verdict_with(self.gate, FailureClass.CLAIM_DISCARDED),
            verdict_with(self.gate, FailureClass.REGRESSION),
        ]
        self.assertIs(self.gate.decide(vs, 4, 1.0), Decision.BLOCK)

    def test_refuted_beats_unverified(self):
        vs = [
            verdict_with(self.gate, FailureClass.UNRUNNABLE),
            verdict_with(self.gate, FailureClass.REGRESSION),
        ]
        self.assertIs(self.gate.decide(vs, 4, 1.0), Decision.BLOCK)

    def test_coverage_floor_blocks_merge(self):
        vs = [verdict_with(self.gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(self.gate.decide(vs, 4, 0.5), Decision.INCONCLUSIVE)

    def test_out_of_range_rounds_is_inconclusive(self):
        vs = [verdict_with(self.gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(self.gate.decide(vs, 99, 1.0), Decision.INCONCLUSIVE)
        self.assertIs(self.gate.decide(vs, -1, 1.0), Decision.INCONCLUSIVE)

    def test_nuclear_requires_full_budget(self):
        gate = make_gate(aggression=AggressionLevel.NUCLEAR)
        vs = [verdict_with(gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(gate.decide(vs, 1, 1.0), Decision.INCONCLUSIVE)
        self.assertIs(gate.decide(vs, 4, 1.0), Decision.MERGE)

    def test_accepted_property_tracks_outcome(self):
        refuted = verdict_with(self.gate, FailureClass.REGRESSION)
        unver = verdict_with(self.gate, FailureClass.UNRUNNABLE)
        self.assertTrue(refuted.accepted)
        self.assertFalse(unver.accepted)
        self.assertFalse(unver.verified)


class TestClaimGranularity(unittest.TestCase):
    """The target must be ``path::id``, not the whole file."""

    def test_target_includes_test_id(self):
        self.assertEqual(build_target("tests/t.py", "test_add"), "tests/t.py::test_add")
        self.assertEqual(build_target("tests/t.py", ""), "tests/t.py")

    def test_sibling_failure_does_not_condemn_another_test(self):
        """Previously: claim about a PASSING test accepted via its sibling."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "def test_alpha():\n    assert 1 == 1\n\n\ndef test_beta():\n    assert 1 == 2\n"
            for sub in ("baseline", "patch"):
                (root / sub).mkdir()
                (root / sub / "test_two.py").write_text(source)

            gate = make_gate()
            calls = []

            def fake(repo_dir, test_path, test_id="", *a, **k):
                calls.append((Path(repo_dir).name, test_path, test_id))
                # Simulate what pytest does with a node id: only that test runs.
                if test_id == "test_alpha":
                    return SandboxResult(0, "", "", False)
                return SandboxResult(1, "", "", False)

            with patch("core.gate.run_test", side_effect=fake):
                v = gate.verify_claim(
                    CriticClaim("test_two.py", "test_alpha"),
                    root / "baseline",
                    root / "patch",
                )

            # test_alpha passes on both sides -> the claim is discarded.
            self.assertIs(v.outcome, Outcome.VERIFIED)
            self.assertIs(v.classification, FailureClass.CLAIM_DISCARDED)
            # And the id really did reach the runner.
            self.assertTrue(all(call[2] == "test_alpha" for call in calls))

    def test_runner_never_receives_a_bare_file_when_id_given(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sub in ("baseline", "patch"):
                (root / sub).mkdir()
                (root / sub / "test_two.py").write_text(
                    "def test_alpha():\n    assert 1 == 1\n\n\ndef test_beta():\n    assert 1 == 2\n"
                )
            gate = make_gate()
            received = []

            def fake(repo_dir, test_path, test_id="", *a, **k):
                received.append(test_id)
                return SandboxResult(1, "", "", False)

            with patch("core.gate.run_test", side_effect=fake):
                gate.verify_claim(
                    CriticClaim("test_two.py", "test_beta"), root / "baseline", root / "patch"
                )
            self.assertEqual(set(received), {"test_beta"})


class TestEvidenceArtefact(unittest.TestCase):
    def test_record_carries_executed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            log = EvidenceLog(path, context={"model": "claude-opus-4", "commit": "abc123"})
            outcome = ExecutionOutcome(
                ExecState.PASS,
                ExecState.FAIL,
                0,
                3,
                3,
                (0, 0, 0),
                (1, 1, 1),
            )
            verdict = GateVerdict(
                CriticClaim("tests/t.py", "test_add", rationale="r"),
                FailureClass.REGRESSION,
                Outcome.REFUTED,
                "test passes on baseline and fails on patch",
                outcome,
                duration_seconds=1.25,
            )
            log.append(verdict)

            record = log.read_all()[0]
            self.assertEqual(record["patch_exit_codes"], [1, 1, 1])
            self.assertEqual(record["baseline_exit_codes"], [0, 0, 0])
            self.assertEqual(record["outcome"], "refuted")
            self.assertEqual(record["ctx_model"], "claude-opus-4")
            self.assertEqual(record["ctx_commit"], "abc123")
            self.assertEqual(record["rationale"], "r")

            # The four fields the old log omitted entirely.
            for field in ("baseline_exit_codes", "patch_exit_codes", "outcome", "reason"):
                self.assertIn(field, record)

    def test_decision_records_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e.jsonl"
            log = EvidenceLog(path)
            log.append(
                GateVerdict(
                    CriticClaim("t.py", "t"), FailureClass.UNRUNNABLE, Outcome.UNVERIFIED, "r"
                )
            )
            log.append_decision("inconclusive", 3, 1.0, {"claims_total": 1, "unverified": 1})
            records = log.read_all()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]["kind"], "decision")
            self.assertEqual(records[1]["decision"], "inconclusive")


class TestMetrics(unittest.TestCase):
    def _log(self, directory: str) -> EvidenceLog:
        log = EvidenceLog(Path(directory) / "e.jsonl", context={"model": "gpt-5"})
        log.append(
            GateVerdict(
                CriticClaim("t.py", "t1"), FailureClass.REGRESSION, Outcome.REFUTED, "r"
            )
        )
        log.append(
            GateVerdict(
                CriticClaim("t.py", "t2"), FailureClass.UNRUNNABLE, Outcome.UNVERIFIED, "r"
            )
        )
        log.append(
            GateVerdict(
                CriticClaim("t.py", "t3"), FailureClass.CLAIM_DISCARDED, Outcome.VERIFIED, "r"
            )
        )
        return log

    def test_claim_tallies(self):
        with tempfile.TemporaryDirectory() as directory:
            log = self._log(directory)
            log.append_decision("merge", 4, 1.0, {"claims_total": 3, "unverified": 1})
            m = compute_metrics(log.read_all())
            self.assertEqual(m.claims_total, 3)
            self.assertEqual(m.verified, 1)
            self.assertEqual(m.refuted, 1)
            self.assertEqual(m.unverified, 1)
            self.assertEqual(m.merge_count, 1)

    def test_unverified_merge_is_counted(self):
        """The 'parece certo' tile: shipped without verification."""
        with tempfile.TemporaryDirectory() as directory:
            log = self._log(directory)
            log.append_decision("merge", 4, 1.0, {"claims_total": 3, "unverified": 1})
            m = compute_metrics(log.read_all())
            self.assertEqual(m.unverified_merges, 1)
            self.assertEqual(m.self_deception_index, 1.0)

    def test_clean_merge_scores_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EvidenceLog(Path(directory) / "e.jsonl")
            log.append(
                GateVerdict(
                    CriticClaim("t.py", "t1"),
                    FailureClass.CLAIM_DISCARDED,
                    Outcome.VERIFIED,
                    "r",
                )
            )
            log.append_decision("merge", 4, 1.0, {"claims_total": 1, "unverified": 0})
            m = compute_metrics(log.read_all())
            self.assertEqual(m.self_deception_index, 0.0)
            self.assertEqual(m.unverified_merges, 0)

    def test_escaped_regression_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EvidenceLog(Path(directory) / "e.jsonl")
            log.append(
                GateVerdict(
                    CriticClaim("t.py", "t1"), FailureClass.REGRESSION, Outcome.REFUTED, "r"
                )
            )
            log.append_decision("merge", 4, 1.0, {"claims_total": 1, "refuted": 1})
            m = compute_metrics(log.read_all())
            self.assertEqual(m.escaped_regressions, 1)

    def test_rework_rounds_tracked(self):
        with tempfile.TemporaryDirectory() as directory:
            log = EvidenceLog(Path(directory) / "e.jsonl")
            log.append_decision("block", 2, 1.0, {})
            log.append_decision("merge", 4, 1.0, {})
            m = compute_metrics(log.read_all())
            self.assertEqual(m.rework_rounds_total, 6)
            self.assertEqual(m.rework_rounds_max, 4)

    def test_model_comparison_groups_by_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "e.jsonl"
            log_a = EvidenceLog(path, context={"model": "claude"})
            log_a.append(
                GateVerdict(
                    CriticClaim("t.py", "t1"), FailureClass.REGRESSION, Outcome.REFUTED, "r"
                )
            )
            log_b = EvidenceLog(path, context={"model": "gpt"})
            log_b.append(
                GateVerdict(
                    CriticClaim("t.py", "t2"),
                    FailureClass.UNRUNNABLE,
                    Outcome.UNVERIFIED,
                    "r",
                )
            )
            m = compute_metrics(EvidenceLog(path).read_all())
            comparison = compare_models(m)
            self.assertIn("claude", comparison)
            self.assertIn("gpt", comparison)
            self.assertEqual(comparison["gpt"]["unverified"], 1)
            self.assertEqual(comparison["claude"]["refuted"], 1)

    def test_report_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            log = self._log(directory)
            log.append_decision("merge", 4, 1.0, {"claims_total": 3, "unverified": 1})
            text = format_report(compute_metrics(log.read_all()))
            self.assertIn("self-deception index", text)
            self.assertIn("unverified merges", text)


class TestQuarantineAndContestation(unittest.TestCase):
    def test_quarantine_only_accepts_refuted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QuarantineStore(Path(directory) / "q.json")
            for classification in (
                FailureClass.REGRESSION,
                FailureClass.UNRUNNABLE,
                FailureClass.FLAKY,
                FailureClass.CLAIM_DISCARDED,
            ):
                gate = make_gate()
                v = verdict_with(gate, classification)
                stored = store.add(v, "src")
                if classification is FailureClass.REGRESSION:
                    self.assertTrue(stored)
                else:
                    self.assertFalse(stored, f"{classification} must not be quarantined")
            self.assertEqual(len(store.load()), 1)

    def test_contestation_rejects_unrelated_counter_test(self):
        claim = CriticClaim("tests/critical_bug.py", "t1")
        original = GateVerdict(
            claim, FailureClass.REGRESSION, Outcome.REFUTED, "real regression"
        )
        contest = Contestation("t1", "tests/unrelated.py", "trivial")
        passing = ExecutionOutcome(ExecState.PASS, ExecState.PASS)
        result = Gate([]).adjudicate_contestation(original, contest, passing)
        self.assertFalse(result.accepted)

    def test_contestation_requires_matching_path_and_pass(self):
        claim = CriticClaim("tests/t.py", "t1")
        original = GateVerdict(claim, FailureClass.REGRESSION, Outcome.REFUTED, "bad")
        ok = Gate([]).adjudicate_contestation(
            original,
            Contestation("t1", "tests/t.py", "counter"),
            ExecutionOutcome(ExecState.PASS, ExecState.PASS),
        )
        self.assertTrue(ok.accepted)

        wrong_id = Gate([]).adjudicate_contestation(
            original,
            Contestation("other", "tests/t.py", "counter"),
            ExecutionOutcome(ExecState.PASS, ExecState.PASS),
        )
        self.assertFalse(wrong_id.accepted)

    def test_contestation_cannot_use_an_unrunnable_run(self):
        """A counter-test that never executed cannot overturn anything."""
        claim = CriticClaim("tests/t.py", "t1")
        original = GateVerdict(claim, FailureClass.REGRESSION, Outcome.REFUTED, "bad")
        result = Gate([]).adjudicate_contestation(
            original,
            Contestation("t1", "tests/t.py", "counter"),
            ExecutionOutcome(ExecState.PASS, ExecState.UNRUNNABLE),
        )
        self.assertFalse(result.accepted)
        self.assertIn("did not execute", result.reason)


class TestPathPolicy(unittest.TestCase):
    def test_symlink_escape_is_caught(self):
        with tempfile.TemporaryDirectory() as outside_dir, tempfile.TemporaryDirectory() as repo:
            import os

            outside = Path(outside_dir) / "secret.py"
            outside.write_text("def test_x():\n    assert True\n")
            link = Path(repo) / "link_test.py"
            os.symlink(outside, link)
            gate = make_gate()
            error = gate.validate_claim(CriticClaim("link_test.py", "t"), Path(repo))
            self.assertIsNotNone(error)
            self.assertIn("symlink", error)

    def test_symlinked_parent_directory_is_caught(self):
        with tempfile.TemporaryDirectory() as outside_dir, tempfile.TemporaryDirectory() as repo:
            import os

            outside = Path(outside_dir) / "tests"
            outside.mkdir()
            (outside / "t.py").write_text("def test_x():\n    assert True\n")
            os.symlink(outside, Path(repo) / "tests")
            gate = make_gate()
            error = gate.validate_claim(CriticClaim("tests/t.py", "t"), Path(repo))
            self.assertIsNotNone(error)
            self.assertIn("symlink", error)

    def test_violations_declare_they_block(self):
        policy = PathPolicy()
        violations = policy.violations(["conftest.py"])
        self.assertEqual(len(violations), 1)
        self.assertTrue(policy.blocks_merge)
        self.assertIn("denylist", str(violations[0]))

    def test_critic_test_file_is_protected(self):
        policy = PathPolicy(critic_test_paths=["tests/critic_test.py"])
        violations = policy.violations(["tests/critic_test.py"])
        self.assertTrue(any("Critic test file" in str(v) for v in violations))


class TestCliExitCodes(unittest.TestCase):
    """Exit codes are the CI contract."""

    def _tree(self, root: Path, *, break_patch: bool) -> None:
        for sub in ("baseline", "patch"):
            (root / sub).mkdir(parents=True, exist_ok=True)
            (root / sub / "test_sum.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
        (root / "baseline" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "patch" / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n" if break_patch else "def add(a, b):\n    return a + b\n"
        )

    def _invoke(self, root: Path, extra) -> int:
        return main(
            [
                "--baseline",
                str(root / "baseline"),
                "--patch",
                str(root / "patch"),
                "--test-path",
                "test_sum.py",
                "--test-id",
                "test_add",
                "--max-rounds",
                "1",
                "--rounds-used",
                "1",
                *extra,
            ]
        )

    def test_confirmed_regression_exits_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = self._invoke(root, [])
            self.assertEqual(code, EXIT_BLOCK)

    def test_clean_patch_exits_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            code = self._invoke(root, [])
            self.assertEqual(code, EXIT_MERGE)

    def test_missing_test_file_exits_inconclusive_not_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = self._invoke(root, ["--test-path", "nao_existe.py"])
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_protected_path_exits_inconclusive_not_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = self._invoke(root, ["--changed-path", "conftest.py"])
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_usage_error_is_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = main(
                ["--baseline", str(root / "baseline"), "--patch", str(root / "patch")]
            )
            self.assertEqual(code, EXIT_USAGE)

    def test_aggression_and_rounds_are_actually_read(self):
        """Both flags were parsed and ignored before."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            # NUCLEAR with an exhausted budget cannot merge.
            code = self._invoke(
                root, ["--aggression", "nuclear", "--rounds-used", "1", "--max-rounds", "4"]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_evidence_log_written_by_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            log_path = Path(directory) / "ev.jsonl"
            self._invoke(
                root, ["--evidence-log", str(log_path), "--model", "claude", "--commit", "deadbeef"]
            )
            records = json.loads(log_path.read_text().splitlines()[0])
            self.assertEqual(records["ctx_model"], "claude")
            decision = json.loads(log_path.read_text().splitlines()[1])
            self.assertEqual(decision["kind"], "decision")
            self.assertIn(decision["decision"], ("merge", "block", "inconclusive"))


class TestEndToEndRegression(unittest.TestCase):
    """The scenario that produced the wrong answer before."""

    def test_rlimit_style_fault_never_approves(self):
        """Both sides die identically -> INCONCLUSIVE, not MERGE."""
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        codes = [3, 3, 3]
        base, _, _ = gate._resolve_side(codes, policy, label="baseline")
        patch_state, _, _ = gate._resolve_side(codes, policy, label="patch")
        self.assertIs(base, ExecState.UNRUNNABLE)
        self.assertIs(patch_state, ExecState.UNRUNNABLE)
        v = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(base, patch_state, run_count=3, baseline_exit_codes=tuple(codes), patch_exit_codes=tuple(codes)),
        )
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_real_regression_still_blocks(self):
        """The fix must not neuter the gate: a genuine bug still blocks."""
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([0, 0, 0], policy, label="baseline")
        patch_state, _, _ = gate._resolve_side([1, 1, 1], policy, label="patch")
        v = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(
                base, patch_state, run_count=3, baseline_exit_codes=(0, 0, 0), patch_exit_codes=(1, 1, 1)
            ),
        )
        self.assertIs(v.outcome, Outcome.REFUTED)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.BLOCK)

    def test_incomplete_fix_without_criterion_is_unverified(self):
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([1, 1, 1], policy, label="baseline")
        patch_state, _, _ = gate._resolve_side([1, 1, 1], policy, label="patch")
        v = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(
                base, patch_state, run_count=3, baseline_exit_codes=(1, 1, 1), patch_exit_codes=(1, 1, 1)
            ),
        )
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_incomplete_fix_with_criterion_refutes(self):
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([1, 1, 1], policy, label="baseline")
        patch_state, _, _ = gate._resolve_side([1, 1, 1], policy, label="patch")
        v = gate.classify(
            CriticClaim("t.py", "t1", cited_criterion_id="AC-1"),
            ExecutionOutcome(
                base, patch_state, run_count=3, baseline_exit_codes=(1, 1, 1), patch_exit_codes=(1, 1, 1)
            ),
        )
        self.assertIs(v.outcome, Outcome.REFUTED)
        self.assertIs(v.classification, FailureClass.INCOMPLETE_FIX)

    def test_weak_suite_strength_returns_inconclusive(self):
        """Weak suite_strength < 0.75 floor yields INCONCLUSIVE instead of MERGE."""
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([0, 0, 0], policy, label="baseline")
        patch_state, _, _ = gate._resolve_side([0, 0, 0], policy, label="patch")
        v = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(base, patch_state, run_count=3, baseline_exit_codes=(0, 0, 0), patch_exit_codes=(0, 0, 0)),
        )
        v.suite_strength = 0.50
        self.assertIs(gate.decide([v], 4, 1.0), Decision.INCONCLUSIVE)

    def test_failed_full_suite_returns_block(self):
        """Full suite regression collateral failure blocks merge."""
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([0, 0, 0], policy, label="baseline")
        patch_state, _, _ = gate._resolve_side([0, 0, 0], policy, label="patch")
        v = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(base, patch_state, run_count=3, baseline_exit_codes=(0, 0, 0), patch_exit_codes=(0, 0, 0)),
        )
        v.suite_strength = 1.0
        self.assertIs(gate.decide([v], 4, 1.0, full_suite_passed=False), Decision.BLOCK)


if __name__ == "__main__":
    unittest.main(verbosity=2)

