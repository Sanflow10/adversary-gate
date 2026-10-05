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

from adversary_gate.cli import EXIT_BLOCK, EXIT_INCONCLUSIVE, EXIT_MERGE, EXIT_USAGE, main
from adversary_gate.core.circuit_breaker import CircuitBreaker
from adversary_gate.core.contestation import Contestation
from adversary_gate.core.evidence_log import EvidenceLog
from adversary_gate.core.exitmap import classify_exit, describe
from adversary_gate.core.gate import Gate, GateConfig
from adversary_gate.core.metrics import compute_metrics, compare_models, format_report
from adversary_gate.core.path_policy import PathPolicy
from adversary_gate.core.quarantine import QuarantineStore
from adversary_gate.core.types import (
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
from adversary_gate.sandbox.runner import SandboxResult, build_target
from adversary_gate.verifiers.coverage import UnparseableDiff, covered_diff_ratio, validate_diff
from adversary_gate.verifiers.stability import policy_for
from adversary_gate.verifiers.strength import (
    _is_test_path,
    calculate_mutation_score,
    changed_foreign_source_files,
    changed_lines,
    changed_source_files,
    classify_changes,
    deleted_source_files,
    measure_mutation_score,
)


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
    from adversary_gate.core.types import outcome_of

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
        from adversary_gate.core.exitmap import UNRUNNABLE_CODES

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
            from adversary_gate.core.types import CLASSIFICATION_OUTCOME

            self.assertIn(classification, CLASSIFICATION_OUTCOME)

    def test_no_error_class_maps_to_merge_licence(self):
        from adversary_gate.core.types import CLASSIFICATION_OUTCOME

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

    def test_missing_coverage_evidence_is_inconclusive(self):
        """AG-002: no evidence is not a perfect score."""
        vs = [verdict_with(self.gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(self.gate.decide(vs, 4, None), Decision.INCONCLUSIVE)

    def test_coverage_floor_zero_disables_the_requirement(self):
        """A floor of 0 says the requirement is off, not that it was met."""
        gate = make_gate(config=GateConfig(coverage_floor=0.0))
        vs = [verdict_with(gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(gate.decide(vs, 4, None), Decision.MERGE)

    def test_refuted_outranks_missing_coverage_evidence(self):
        """AG-011: the floor used to be checked *before* ``REFUTED``.

        A patch whose execution proved a regression came back INCONCLUSIVE
        whenever coverage was low -- missing evidence beating evidence we
        watched happen, which is the opposite of the documented precedence.
        """
        refuted = verdict_with(self.gate, FailureClass.REGRESSION)
        self.assertIs(self.gate.decide([refuted], 4, None), Decision.BLOCK)
        self.assertIs(self.gate.decide([refuted], 4, 0.0), Decision.BLOCK)

    def test_full_suite_regression_outranks_missing_coverage_evidence(self):
        vs = [verdict_with(self.gate, FailureClass.CLAIM_DISCARDED)]
        self.assertIs(
            self.gate.decide(
                [vs[0]], 4, None, full_suite_exit_codes=(0, 1)
            ),
            Decision.BLOCK,
        )

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

            with patch("adversary_gate.core.gate.run_test", side_effect=fake):
                v = gate.verify_claim(
                    CriticClaim("test_two.py", "test_alpha"),
                    root / "baseline",
                    root / "patch",
                )

            # test_alpha passes on both sides -> no regression (AG-030), not a fix.
            self.assertIs(v.outcome, Outcome.VERIFIED)
            self.assertIs(v.classification, FailureClass.NO_REGRESSION)
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

            with patch("adversary_gate.core.gate.run_test", side_effect=fake):
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

    def test_file_level_symlink_in_changed_paths_is_caught(self):
        """AG-004: the changed file itself was not in the checked set.

        The policy checked ``root`` and ``unresolved.parents`` but never
        ``unresolved`` -- so a changed path that *is* a symlink produced no
        violation even with ``repo_dir`` supplied.
        """
        with tempfile.TemporaryDirectory() as outside_dir, tempfile.TemporaryDirectory() as repo:
            import os

            outside = Path(outside_dir) / "secret.py"
            outside.write_text("x = 1\n")
            (Path(repo) / "src").mkdir()
            os.symlink(outside, Path(repo) / "src" / "evil.py")

            violations = PathPolicy().violations(["src/evil.py"], Path(repo))
            self.assertTrue(violations, "a file-level symlink must be a violation")
            self.assertIn("symlink", str(violations[0]))

    def test_gate_validates_changed_paths_against_the_patch_dir(self):
        """AG-004: ``verify_claim`` called the policy with no root at all."""
        with tempfile.TemporaryDirectory() as outside_dir, tempfile.TemporaryDirectory() as patch:
            import os

            outside = Path(outside_dir) / "secret.py"
            outside.write_text("x = 1\n")
            (Path(patch) / "src").mkdir()
            os.symlink(outside, Path(patch) / "src" / "evil.py")

            gate = make_gate()
            verdict = gate.verify_claim(
                CriticClaim("t.py", "t"),
                Path(patch),
                Path(patch),
                changed_paths=["src/evil.py"],
            )
            self.assertIs(verdict.outcome, Outcome.UNVERIFIED)
            self.assertIn("symlink", verdict.reason)


class TestCliExitCodes(unittest.TestCase):
    """Exit codes are the CI contract."""

    #: v2.0.2 made coverage an evidence question (AG-002). Tests that are not
    #: *about* coverage still have to clear the floor to reach the branch they
    #: assert on, so they declare a caller-supplied number as untrusted and get
    #: on with their own claim. The default path -- no evidence at all -- is
    #: covered by ``test_no_coverage_evidence_is_inconclusive_not_merge``.
    UNTRUSTED_COVERAGE = ["--coverage-ratio", "1.0", "--coverage-source", "untrusted"]

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
            code = self._invoke(root, list(self.UNTRUSTED_COVERAGE))
            self.assertEqual(code, EXIT_MERGE)

    def test_no_coverage_evidence_is_inconclusive_not_merge(self):
        """AG-002: the default used to be ``--coverage-ratio 1.0``.

        A clean patch with no coverage artefact and no claim at all must not
        merge -- "we did not measure" is not "we measured and it was perfect".
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            code = self._invoke(root, [])
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_caller_supplied_ratio_must_be_declared_untrusted(self):
        """A bare ``--coverage-ratio`` is rejected rather than silently trusted."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            code = self._invoke(root, ["--coverage-ratio", "1.0"])
            self.assertEqual(code, EXIT_USAGE)
            # ...and the same number, declared, is accepted.
            code = self._invoke(
                root, ["--coverage-ratio", "1.0", "--coverage-source", "untrusted"]
            )
            self.assertEqual(code, EXIT_MERGE)

    def test_computed_diff_coverage_drives_the_decision(self):
        """The ratio must be derivable from artefacts the gate reads itself."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            # calc.py line 2 is the only added line in this diff; the report
            # either executed it or did not.
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def add(a, b):\n-    return a + b\n+    return (a + b)\n"
            )
            full = json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})
            partial = json.dumps({"files": {"calc.py": {"executed_lines": [1]}}})

            (root / "full.json").write_text(full)
            code = self._invoke(
                root, ["--diff", str(root / "change.diff"), "--coverage-json", str(root / "full.json")]
            )
            self.assertEqual(code, EXIT_MERGE)

            # Same fixture, a coverage report that misses the added line:
            # measured below the floor, so no merge.
            (root / "partial.json").write_text(partial)
            code = self._invoke(
                root,
                [
                    "--diff", str(root / "change.diff"),
                    "--coverage-json", str(root / "partial.json"),
                    "--coverage-floor", "1.0",
                ],
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_coverage_artefacts_are_hashed_into_the_evidence(self):
        """A ratio the artefact cannot be recomputed from is not evidence."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            (root / "change.diff").write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def add(a, b):\n-    return a + b\n+    return a - b\n"
            )
            (root / "cov.json").write_text(
                json.dumps({"files": {"calc.py": {"executed_lines": [2]}}})
            )
            log_path = Path(directory) / "ev.jsonl"
            self._invoke(
                root,
                [
                    "--diff", str(root / "change.diff"),
                    "--coverage-json", str(root / "cov.json"),
                    "--evidence-log", str(log_path),
                ],
            )
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decision = [r for r in records if r.get("kind") == "decision"][-1]
            self.assertEqual(decision["diff_coverage_source"], "computed")
            self.assertIn("diff_sha256", decision["diff_coverage"])
            self.assertIn("coverage_json_sha256", decision["diff_coverage"])
            # ``full_suite_ran`` separates "the suite passed" from "we never
            # ran it" (AG-010); v2.0.1 wrote ``null`` for both.
            self.assertTrue(decision["full_suite_ran"])

    def test_missing_test_file_exits_inconclusive_not_merge(self):
        """Claim we cannot execute, on a patch with no other damage -> INCONCLUSIVE.

        Split from the BLOCK case below: the original test paired an
        unverifiable claim with ``break_patch=True``, so it asserted
        INCONCLUSIVE while the fixture was also breaking the suite. Which of
        the two answers is correct depends on that second condition, and
        testing both under one name hid it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            code = self._invoke(
                root, ["--test-path", "nao_existe.py", *self.UNTRUSTED_COVERAGE]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_missing_test_file_with_regressed_full_suite_exits_block(self):
        """Unverifiable claim, but the collateral run proves the patch broke tests.

        Direct evidence outranks missing evidence. We could not check the
        claim, yet baseline passed the full suite and patch did not -- that is
        a regression we watched happen, and reporting it as a shrug would
        understate what we know.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = self._invoke(root, ["--test-path", "nao_existe.py"])
            self.assertEqual(code, EXIT_BLOCK)

    def test_protected_path_exits_inconclusive_not_merge(self):
        """Protected path on a patch with no other damage -> INCONCLUSIVE."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=False)
            code = self._invoke(
                root, ["--changed-path", "conftest.py", *self.UNTRUSTED_COVERAGE]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_protected_path_with_regressed_full_suite_exits_block(self):
        """Protected path plus a real collateral regression -> BLOCK."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, break_patch=True)
            code = self._invoke(root, ["--changed-path", "conftest.py"])
            self.assertEqual(code, EXIT_BLOCK)

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
            # NUCLEAR with an exhausted budget cannot merge. Coverage is
            # supplied so that INCONCLUSIVE can only come from the budget.
            code = self._invoke(
                root,
                [
                    "--aggression", "nuclear", "--rounds-used", "1", "--max-rounds", "4",
                    *self.UNTRUSTED_COVERAGE,
                ],
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
        self.assertIs(gate.decide([v], 4, 1.0, suite_strength=0.50), Decision.INCONCLUSIVE)

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
        self.assertIs(gate.decide([v], 4, 1.0, full_suite_passed=False), Decision.BLOCK)



class TestSuiteStrengthIsMeasured(unittest.TestCase):
    """Regression for two gates that existed but could never fire.

    ``suite_strength`` was computed as ``calculate_suite_strength(1.0,
    patch_output)``: coverage hardcoded to 1.0 and ``assertion_count``
    defaulting to 1, so ``has_assertions`` was unconditionally true and the
    score came out 1.0 for every input -- including an empty one. The only
    two tests covering the area asserted ``decide(..., suite_strength=0.50)``
    and ``decide(..., full_suite_passed=False)``, which proved the branches
    were correct while proving nothing about whether a value ever reached
    them. Both were unreachable in production.

    Everything below asserts on the value *produced* by execution.
    """

    BASE = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"
    TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"

    def _tree(self, root: Path, patch_source: str) -> None:
        for side in ("baseline", "patch"):
            (root / side).mkdir(parents=True, exist_ok=True)
            (root / side / "test_sum.py").write_text(self.TEST)
        (root / "baseline" / "calc.py").write_text(self.BASE)
        (root / "patch" / "calc.py").write_text(patch_source)

    def _strength(self, root: Path, **kwargs):
        return measure_mutation_score(
            root / "baseline", root / "patch", "test_sum.py", **kwargs
        )

    def test_score_comes_from_execution_and_is_not_always_one(self):
        """The bug: a suite nobody exercises must not read as perfect."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # patch edits `sub`, which no test touches
            self._tree(root, self.BASE.replace("return a - b", "return a + b"))
            score, detail = self._strength(root, max_mutants=2)
            self.assertTrue(score.is_measured, detail)
            self.assertLess(score.mutation_score, 1.0)
            self.assertLess(score.mutation_score, 0.75)
            self.assertFalse(score.is_strong)
            self.assertEqual(detail["survivors"], ["calc.py:5"])

    def test_change_the_suite_covers_scores_at_or_above_the_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE.replace("return a + b", "return (a + b)"))
            score, detail = self._strength(root, max_mutants=2, confidence=0)
            self.assertTrue(score.is_measured, detail)
            self.assertGreaterEqual(score.mutation_score, 0.75)
            self.assertTrue(score.is_strong)
            self.assertEqual(score.mutants_killed, score.mutants_total)

    def test_one_killed_mutant_is_a_ratio_not_evidence(self):
        """AG-023: the same 1-of-1, at the default 80 % interval, is not strong."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE.replace("return a + b", "return (a + b)"))
            score, detail = self._strength(root, max_mutants=2)
            self.assertEqual(score.mutation_score, 1.0)
            self.assertLess(score.lower, 0.75)
            self.assertFalse(score.is_strong)
            self.assertEqual(detail["interval"], [score.lower, score.upper])
            self.assertEqual(detail["confidence"], 0.80)

    def test_unchanged_source_is_unknown_not_strong(self):
        """No code changed -> nothing to measure. ``is_strong`` must stay False."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE)
            self.assertEqual(changed_source_files(root / "baseline", root / "patch"), [])
            score, detail = self._strength(root)
            self.assertFalse(score.is_measured)
            self.assertFalse(score.is_strong)
            self.assertIn("reason", detail)

    def test_detail_shape_is_identical_on_every_exit_path(self):
        """An artefact whose keys vanish when nothing was measured is unqueryable."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE)
            _, unmeasured = self._strength(root)
            self._tree(root, self.BASE.replace("return a + b", "return (a + b)"))
            _, measured = self._strength(root, max_mutants=2)
            self.assertEqual(set(unmeasured), set(measured))
            for key in ("measured", "reason", "changed_files", "mutants",
                        "survivors", "mutants_counted", "stillborn"):
                self.assertIn(key, unmeasured)
                self.assertIn(key, measured)

    def test_only_lines_the_patch_wrote_are_mutated(self):
        """File granularity would mutate code the patch never touched."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE.replace("return a + b", "return (a + b)"))
            before = (root / "baseline" / "calc.py").read_text()
            after = (root / "patch" / "calc.py").read_text()
            self.assertEqual(changed_lines(before, after), {2})
            score, detail = self._strength(root, max_mutants=5)
            self.assertTrue(score.is_measured, detail)
            for mutant in detail["mutants"]:
                self.assertEqual(mutant["line"], 2)

    def test_evidence_artifact_carries_the_measured_value(self):
        """A strength the log cannot show is as unauditable as no strength."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, self.BASE.replace("return a - b", "return a + b"))
            log_path = root / "evidence.jsonl"
            code = main([
                "--baseline", str(root / "baseline"),
                "--patch", str(root / "patch"),
                "--test-path", "test_sum.py",
                "--test-id", "test_add",
                "--evidence-log", str(log_path),
            ])
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decisions = [r for r in records if r.get("kind") == "decision"]
            self.assertTrue(decisions, records)
            record = decisions[-1]
            self.assertIn("suite_strength", record)
            self.assertIsInstance(record["suite_strength"], float)
            self.assertLess(record["suite_strength"], 1.0)
            self.assertIn("mutation", record)
            self.assertEqual(code, EXIT_INCONCLUSIVE)

    def test_is_test_path_recognizes_all_test_patterns(self):
        """Standard pytest discovery recognizes both test_*.py and *_test.py."""
        self.assertTrue(_is_test_path("test_calc.py"))
        self.assertTrue(_is_test_path("calc_test.py"))
        self.assertTrue(_is_test_path("tests/sub/util.py"))
        self.assertTrue(_is_test_path("conftest.py"))
        self.assertFalse(_is_test_path("src/calc.py"))
        self.assertFalse(_is_test_path("calc.py"))


class TestFullSuiteUsesBothSides(unittest.TestCase):
    """``full_suite_passed`` defaulted to True and was never assigned.

    Checking only the patch side would repeat the collapse ``exitmap.py``
    documents for claim execution: blaming the patch for failures that were
    already in the baseline.
    """

    def _verified(self) -> GateVerdict:
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([0, 0, 0], policy, label="baseline")
        patched, _, _ = gate._resolve_side([0, 0, 0], policy, label="patch")
        return gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(
                base, patched, run_count=3,
                baseline_exit_codes=(0, 0, 0), patch_exit_codes=(0, 0, 0),
            ),
        )

    def _decide(self, codes):
        return make_gate().decide([self._verified()], 4, 1.0, full_suite_exit_codes=codes)

    def test_baseline_ok_patch_broken_is_block(self):
        self.assertIs(self._decide((0, 1)), Decision.BLOCK)

    def test_already_broken_on_both_sides_is_not_attributable(self):
        self.assertIs(self._decide((1, 1)), Decision.INCONCLUSIVE)

    def test_patch_fixing_a_broken_baseline_is_not_a_regression(self):
        self.assertIs(self._decide((1, 0)), Decision.MERGE)

    def test_both_clean_is_merge(self):
        self.assertIs(self._decide((0, 0)), Decision.MERGE)

    def test_unrunnable_full_suite_is_inconclusive(self):
        for codes in ((2, 1), (0, 2), (5, 1)):
            with self.subTest(codes=codes):
                if codes[0] == 0:
                    # patch side unrunnable while baseline passed: the patch
                    # destroyed the suite's ability to run at all -> BLOCK
                    self.assertIs(self._decide(codes), Decision.BLOCK)
                else:
                    self.assertIs(self._decide(codes), Decision.INCONCLUSIVE)

    def test_unmeasured_strength_blocks_merge(self):
        """Unknown is not strong: it must not be able to reach MERGE."""
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        base, _, _ = gate._resolve_side([0, 0, 0], policy, label="baseline")
        patched, _, _ = gate._resolve_side([0, 0, 0], policy, label="patch")
        verdict = gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(
                base, patched, run_count=3,
                baseline_exit_codes=(0, 0, 0), patch_exit_codes=(0, 0, 0),
            ),
        )
        self.assertIs(verdict.outcome, Outcome.VERIFIED)
        self.assertIs(gate.decide([verdict], 4, 1.0), Decision.MERGE)
        self.assertIs(
            gate.decide([verdict], 4, 1.0, suite_strength_unverified=True),
            Decision.INCONCLUSIVE,
        )


class TestFullSuiteHasItsOwnLimits(unittest.TestCase):
    """AG-033: the collateral run lived under the per-test limits.

    ``--timeout 30`` / ``--cpu-seconds 10`` are sized for one test. The whole
    suite ran under them too, so any suite longer than that was killed on both
    sides (exit -9, -9): never a false MERGE -- both sides failing is
    "unverifiable" -- but the check that catches a patch deleting the guard a
    test relied on was gone for every repository of real size. Measured on
    more-itertools (a 50 s suite): three reverted guard-removal fixes came back
    INCONCLUSIVE instead of BLOCK. Here a 1 s per-test limit and a suite with a
    1.5 s test stand in for the same thing at small scale.
    """

    def _tree(self, root: Path) -> None:
        calc_base = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    if a is None:\n        raise ValueError('a')\n    return a - b\n"
        tests = (
            "import time\n\nimport pytest\n\nfrom calc import add, sub\n\n\n"
            "def test_add():\n    assert add(2, 2) == 4\n\n\n"
            "def test_sub_rejects_none():\n    with pytest.raises(ValueError):\n        sub(None, 1)\n\n\n"
            "def test_slow():\n    time.sleep(1.5)\n"
        )
        for side in ("baseline", "patch"):
            (root / side).mkdir(parents=True)
            (root / side / "test_calc.py").write_text(tests)
        (root / "baseline" / "calc.py").write_text(calc_base)
        # The guard is deleted: nothing the claim runs notices, the suite does.
        (root / "patch" / "calc.py").write_text(calc_base.replace("    if a is None:\n        raise ValueError('a')\n", ""))

    def _run(self, *extra):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            out = Path(directory) / "out.json"
            with patch("sys.stdout", new_callable=lambda: open(out, "w")):
                code = main([
                    "--baseline", str(root / "baseline"), "--patch", str(root / "patch"),
                    "--claim", "test_calc.py::test_add", "--max-rounds", "1", "--rounds-used", "1",
                    "--timeout", "1", *TestCliExitCodes.UNTRUSTED_COVERAGE, *extra,
                ])
            return code, json.loads(out.read_text())

    def test_the_suite_outlives_the_per_test_limit_and_blocks(self):
        code, out = self._run()
        self.assertEqual(code, EXIT_BLOCK, out.get("reason"))
        self.assertEqual(out["full_suite_exit_codes"], [0, 1])
        self.assertEqual(out["execution"]["full_suite_timeout_seconds"], 900)

    def test_the_reason_names_the_collateral_regression(self):
        """AG-034: the claim passed, so its own reason ("nothing regressed") contradicted the BLOCK."""
        _, out = self._run()
        self.assertIn("full suite", out["reason"])
        self.assertIn("fails on the patch", out["reason"])

    def test_the_suite_has_its_own_memory_limit(self):
        """512M of address space killed more-itertools' pytest with MemoryError (exit 3, both sides)."""
        _, out = self._run()
        self.assertEqual(out["execution"]["full_suite_memory_bytes"], 4 * 1024**3)
        self.assertEqual(out["execution"]["memory_bytes"], 512 * 1024**2)

    def test_a_runner_that_died_on_both_sides_is_named(self):
        code, out = self._run("--full-suite-command", "exit 3")
        self.assertEqual(code, EXIT_INCONCLUSIVE)
        self.assertIn("could not run on either side", out["reason"])
        self.assertIn("--full-suite-memory", out["reason"])

    def test_a_suite_killed_by_its_own_limit_is_named_not_merged(self):
        code, out = self._run("--full-suite-timeout", "1")
        self.assertEqual(code, EXIT_INCONCLUSIVE)
        self.assertIn("--full-suite-timeout", out["reason"])


class TestDeletionsAreChanges(unittest.TestCase):
    """AG-001: a deleted source file must count as a change.

    ``changed_source_files`` iterated the patch side only, so a file the patch
    removed never appeared -- ``changed_files`` came back empty, the strength
    question was never asked, and the gate could reach MERGE with no
    measurement at all. The docstring always said deletions counted; only the
    code disagreed.
    """

    def _tree(self, root: Path) -> None:
        for side in ("baseline", "patch"):
            (root / side).mkdir(parents=True, exist_ok=True)
            (root / side / "test_sum.py").write_text(
                "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
            )
            (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "baseline" / "dead_weight.py").write_text("def unused():\n    return 1\n")

    def test_deleted_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            changed = changed_source_files(root / "baseline", root / "patch")
            self.assertEqual(changed, ["dead_weight.py"])
            self.assertEqual(
                deleted_source_files(root / "baseline", root / "patch"), ["dead_weight.py"]
            )

    def test_added_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            (root / "patch" / "brand_new.py").write_text("def brand_new():\n    return 2\n")
            self.assertIn(
                "brand_new.py", changed_source_files(root / "baseline", root / "patch")
            )

    def test_identical_sides_report_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            # make both sides genuinely identical: same files, same bytes
            (root / "patch" / "dead_weight.py").write_text("def unused():\n    return 1\n")
            (root / "baseline" / "extra.py").write_text("x = 1\n")
            (root / "patch" / "extra.py").write_text("x = 1\n")
            self.assertEqual(changed_source_files(root / "baseline", root / "patch"), [])

    def test_deletion_only_patch_cannot_reach_merge(self):
        """The observable consequence of the bug: deletion -> MERGE."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--evidence-log", str(log_path),
                ]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decision = [r for r in records if r.get("kind") == "decision"][-1]
            self.assertIn("dead_weight.py", decision["mutation"]["changed_files"])
            self.assertTrue(decision["suite_strength_unverified"])

    def test_mixed_deletion_and_modification_cannot_reach_merge(self):
        """AG-018: a deletion must not ride under the score of a modified file.

        Reconstructed from the third-party audit's artefact on ``5159f5c``:
        ``calc.py`` modified and measured (mutation 1/1, ``suite_strength``
        ``1.0``), ``gone.py`` deleted and never judged, a real diff and a
        coverage artefact supplied. The gate returned ``exit 0`` / ``merge``.

        The score measured ``calc.py`` and nothing else -- a deleted file has
        no lines left to mutate, so it produced no mutant, survived nothing
        and never lowered the single number that authorised the decision. The
        decision, meanwhile, was presented for the change as a whole. That is
        the same shape as AG-001 (deletion dropped from ``changed_files``) and
        AG-012 (C++ outside the engine), which is why the guard lives next to
        theirs rather than in a rule of its own.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                (root / side).mkdir(parents=True, exist_ok=True)
                (root / side / "test_sum.py").write_text(
                    "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
                )
            # Behaviour-preserving edit on the measured side...
            (root / "baseline" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            (root / "patch" / "calc.py").write_text("def add(a, b):\n    return (a + b)\n")
            # ...and a file the patch removes, which the test never imports.
            (root / "baseline" / "gone.py").write_text("def helper():\n    return 1\n")

            diff_path = Path(directory) / "change.diff"
            diff_path.write_text(
                "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
                " def add(a, b):\n-    return a + b\n+    return (a + b)\n"
                "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
                "-def helper():\n-    return 1\n"
            )
            cov_path = Path(directory) / "cov.json"
            cov_path.write_text(
                json.dumps({"files": {"calc.py": {"executed_lines": [1, 2]}}})
            )

            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--diff", str(diff_path),
                    "--coverage-json", str(cov_path),
                    "--evidence-log", str(log_path),
                ]
            )
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decision = [r for r in records if r.get("kind") == "decision"][-1]

            # The measurement itself is unchanged and still says what it covered.
            self.assertEqual(decision["mutation"]["deleted_files"], ["gone.py"])
            self.assertEqual(decision["mutation"]["changed_files"], ["calc.py", "gone.py"])
            self.assertEqual(decision["suite_strength"], 1.0)
            # What changed is that the score may no longer stand for the patch.
            self.assertTrue(decision["suite_strength_unverified"])
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            self.assertEqual(decision["decision"], "inconclusive")


class TestForeignSourceCannotMergeUnmeasured(unittest.TestCase):
    """AG-012: a patch that changes only non-Python source must not merge.

    ``changed_source_files`` walked ``*.py``, so a patch whose entire effect
    was in ``calculator.cpp`` reported ``changed_files: []`` and the strength
    question was never asked. Paired with a Python test that always passes and
    a coverage artefact, that reached MERGE while the only thing that had
    executed was ``assert True`` -- and the artefact recorded the false claim
    ``"no non-test source file changed between baseline and patch"``.

    The fail-closed invariant is not that every patch blocks; it is that the
    gate never reports what it did not observe.
    """

    def _tree(self, root: Path) -> None:
        for side in ("baseline", "patch"):
            (root / side).mkdir(parents=True, exist_ok=True)
            (root / side / "test_sum.py").write_text(
                "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
            )
            (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        # The behaviour under test is changed here, and nowhere in Python.
        (root / "baseline" / "calculator.cpp").write_text(
            "int add(int a, int b) { return a - b; }\n"
        )
        (root / "patch" / "calculator.cpp").write_text(
            "int add(int a, int b) { return a * b; }\n"
        )

    def test_cpp_change_is_reported_as_foreign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"),
                ["calculator.cpp"],
            )
            # ...and it is *not* claimed as muateable Python source.
            self.assertEqual(changed_source_files(root / "baseline", root / "patch"), [])

    def test_non_source_and_test_files_are_not_foreign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            # Neutralise the .cpp the shared tree ships, so this test is about
            # the README and the test file only.
            (root / "patch" / "calculator.cpp").write_text(
                (root / "baseline" / "calculator.cpp").read_text()
            )
            (root / "baseline" / "README.md").write_text("old\n")
            (root / "patch" / "README.md").write_text("new\n")
            (root / "baseline" / "tests").mkdir()
            (root / "patch" / "tests").mkdir()
            (root / "baseline" / "tests" / "test_calc.cpp").write_text("// a\n")
            (root / "patch" / "tests" / "test_calc.cpp").write_text("// b\n")
            # A README is not code, and a test file is never a mutation target.
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"), []
            )

    def test_foreign_only_patch_cannot_reach_merge(self):
        """The observable consequence: before the fix this returned exit 0."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    # Coverage is declared good on purpose: it must be the
                    # *strength* question that blocks, not the coverage floor.
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--evidence-log", str(log_path),
                ]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decision = [r for r in records if r.get("kind") == "decision"][-1]
            self.assertEqual(
                decision["mutation"]["foreign_changed_files"], ["calculator.cpp"]
            )
            self.assertTrue(decision["suite_strength_unverified"])
            self.assertIsNone(decision["suite_strength"])

    def test_mixed_python_and_cpp_patch_cannot_reach_merge(self):
        """The half the first fix missed: Python measures, C++ still does not.

        When Python changed too, ``is_measured`` became true and the foreign
        files were recorded but never acted on, so a mixed patch merged with
        half of its change never executed. Coverage can mask this (the untested
        ``.cpp`` lines drag the ratio down), which is why the case has to be
        pinned with coverage deliberately declared good.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            (root / "patch" / "calc.py").write_text(
                "def add(a, b):\n    return (a + b)\n"
            )
            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--evidence-log", str(log_path),
                ]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            records = [json.loads(line) for line in log_path.read_text().splitlines()]
            decision = [r for r in records if r.get("kind") == "decision"][-1]

            # The Python half really was measured -- it is not a null score.
            self.assertIsNotNone(decision["suite_strength"])
            self.assertEqual(decision["mutation"]["changed_files"], ["calc.py"])
            # ...and that is precisely why the flag has to fire anyway.
            self.assertEqual(
                decision["mutation"]["foreign_changed_files"], ["calculator.cpp"]
            )
            self.assertTrue(decision["suite_strength_unverified"])
            self.assertIn("never judged", decision["mutation"]["reason"])

    def test_reason_never_claims_nothing_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            _score, detail = measure_mutation_score(
                root / "baseline", root / "patch", "test_sum.py"
            )
            self.assertIn("calculator.cpp", detail["reason"])
            self.assertNotIn("no source file changed", detail["reason"])

    def test_python_only_change_still_reports_nothing_changed(self):
        """The honest wording survives for the case it was written for."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            (root / "patch" / "calculator.cpp").write_text(
                (root / "baseline" / "calculator.cpp").read_text()
            )
            _score, detail = measure_mutation_score(
                root / "baseline", root / "patch", "test_sum.py"
            )
            self.assertEqual(detail["foreign_changed_files"], [])
            self.assertEqual(detail["reason"], "no source file changed between baseline and patch")


class TestAg012ResidualPaths(unittest.TestCase):
    """The two ways the AG-012 guard could still be walked around.

    Both were reproduced against v2.1.0 before being fixed, and both are now
    recorded in ``ERRORS_AND_INCONSISTENCIES.md`` -- as AG-013 and AG-014:

    * the guard recognised source through an **allowlist** of languages
      somebody had typed, so ``.sql`` -- and every other suffix outside it --
      was invisible, and a patch changing only ``schema.sql`` wrote the exact
      false statement AG-012 was opened to remove while reaching MERGE;
    * the guard's evaluation lived **inside** the mutation branch, so
      ``--mutation-max 0`` skipped it entirely and a pure-C++ patch merged.
    """

    def _tree(self, root: Path, extra: str = "calculator.cpp") -> None:
        for side in ("baseline", "patch"):
            (root / side).mkdir(parents=True, exist_ok=True)
            (root / side / "test_sum.py").write_text(
                "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
            )
            (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "baseline" / extra).write_text("old\n")
        (root / "patch" / extra).write_text("new\n")

    # ------------------------------------------------------------------
    # A1: allowlist -> denylist
    # ------------------------------------------------------------------
    def test_unlisted_source_suffix_is_still_foreign(self):
        """`.sql` was the suffix that reproduced the original bug."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="schema.sql")
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"),
                ["schema.sql"],
            )
            # The false claim must not be reachable any more.
            detail = classify_changes(root / "baseline", root / "patch")
            self.assertEqual(detail["changed_files"], [])
            self.assertEqual(detail["foreign_changed_files"], ["schema.sql"])

    def test_every_common_source_suffix_is_recognised(self):
        """The allowlist's whole failure mode: forgetting a language."""
        for name in (
            "a.proto", "b.pyi", "c.sol", "d.vue", "e.svelte", "f.scss",
            "g.r", "h.tmpl", "i.jinja", "j.tf", "k.sql", "l.hcl",
        ):
            with self.subTest(suffix=Path(name).suffix):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._tree(root, extra=name)
                    self.assertEqual(
                        changed_foreign_source_files(root / "baseline", root / "patch"),
                        [name],
                        f"{name} must count as source we cannot judge",
                    )

    def test_documents_are_still_not_foreign(self):
        """A denylist only helps if the denylist is right."""
        # evidence.jsonl is there because the Action writes its own log into
        # the tree it is judging, and .jsonl -- unlike .json -- was missing
        # from the list, so the evidence log came back as source nobody had
        # judged and took base-sha from merge to inconclusive.
        for name in ("README.md", "CHANGELOG", "LICENSE", "data.json",
                     "evidence.jsonl", "events.ndjson", "ci.yml"):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    self._tree(root, extra=name)
                    self.assertEqual(
                        changed_foreign_source_files(root / "baseline", root / "patch"),
                        [],
                        f"{name} is not code under test",
                    )

    def test_vcs_internals_and_caches_are_not_source_we_cannot_judge(self):
        """Regression for the defect the AG-013 fix introduced itself.

        Replacing "is this one of the languages we remember" with "is this
        plainly not code" made the question *broader*, and the extra things it
        now sees are mostly not code at all. A scan over a real checkout finds
        ``.git/HEAD``, ``.git/config`` and ``.git/objects/...``; none of those
        have a suffix either table recognises, so all of them became "source
        we cannot judge". The old allowlist had hidden them only by accident.

        The consequence was measured rather than guessed: every gate run whose
        ``--patch`` happened to be a repository reported 26 such paths,
        including ``.git/HEAD``, and forced INCONCLUSIVE -- which is what
        turned ``prepare_evidence.sh`` from exit 0 into exit 2 in CI.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="README.md")
            for rel in (
                ".git/HEAD",
                ".git/config",
                ".git/objects/ab/cdef",
                ".pytest_cache/v/cache/nodeids",
                ".pytest_cache/.gitignore",
                "__pycache__/calc.cpython-312.pyc",
                ".coverage",
            ):
                target = root / "patch" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("bookkeeping\n")
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"),
                [],
                "VCS metadata and tool caches are not patch content",
            )

    def test_a_worktree_or_submodule_git_file_is_not_foreign(self):
        """AG-035: in a git worktree or submodule ``.git`` is a *file*.

        The exclusion only looked at a path's parent directories, so
        ``.git/HEAD`` was skipped but a top-level ``.git`` file -- the
        ``gitdir: ...`` pointer every ``git worktree add`` writes -- was
        "source we cannot judge", and every verify_repo call on a worktree
        (measured: more-itertools, all seven runs) carried it as foreign.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="README.md")
            (root / "patch" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
            (root / "patch" / "vendor").mkdir()
            (root / "patch" / "vendor" / ".git").write_text("gitdir: ../.git/modules/vendor\n")
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"),
                [],
                "a .git pointer file is VCS bookkeeping, like the .git directory",
            )

    def test_the_actions_own_build_output_is_not_foreign(self):
        """The Action runs ``pip install`` in the very tree it is judging.

        That writes ``build/`` and ``src/<name>.egg-info/`` into the patch side
        while the baseline, materialised from git, has neither -- so the two
        trees always differ there. Exactly one file made the difference count:
        ``PKG-INFO`` is suffixless and not on the name list, so it read as
        "source we cannot judge" and took every base-sha run from exit 0 to
        exit 2. Nothing here is excluded by its suffix; it is excluded because
        it is output, not input.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="README.md")
            for rel in (
                "src/adversary_gate.egg-info/PKG-INFO",
                "src/adversary_gate.egg-info/SOURCES.txt",
                "build/lib/gate_fixture/calc.py",
                "dist/index.html",
            ):
                target = root / "patch" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("generated\n")
            self.assertEqual(
                changed_foreign_source_files(root / "baseline", root / "patch"),
                [],
                "setuptools output is not source the patch introduced",
            )

    def test_the_scan_exemption_does_not_extend_to_the_diff(self):
        """Pinning the boundary of the exclusion, in both directions.

        ``NON_SOURCE_DIRS`` exists so a checkout stops looking like a patch,
        so the important question is where the exemption stops. It stops at
        the scan: the diff is the author's own statement of what changed, and
        a diff that names something under ``.git/`` or ``node_modules/`` still
        has to answer for it. Exempting those too would be a place to hide a
        file, which is exactly the AG-013 failure mode under a new name.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="README.md")
            for rel in (
                ".git/hooks/post-checkout",
                "node_modules/vendored.js",
                ".venv/lib/site/module.go",
            ):
                with self.subTest(diff_path=rel):
                    self.assertEqual(
                        changed_foreign_source_files(
                            root / "baseline", root / "patch", [rel]
                        ),
                        [rel],
                        f"a diff naming {rel} must not be dropped by the scan fix",
                    )

    def test_diff_paths_are_unioned_with_the_scan(self):
        """The caller's diff is authoritative about what the patch touched."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            # Not present on either side for the scan to find.
            self.assertEqual(
                changed_foreign_source_files(
                    root / "baseline", root / "patch", ["generated/migration.sql"]
                ),
                ["calculator.cpp", "generated/migration.sql"],
            )

    def test_sql_only_patch_cannot_reach_merge(self):
        """The end-to-end consequence: before, this exited 0."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root, extra="schema.sql")
            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--evidence-log", str(log_path),
                ]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            decision = self._decision(log_path)
            self.assertEqual(
                decision["mutation"]["foreign_changed_files"], ["schema.sql"]
            )
            self.assertTrue(decision["suite_strength_unverified"])

    # ------------------------------------------------------------------
    # A2: --mutation-max 0 must not disable the guard
    # ------------------------------------------------------------------
    def test_mutation_budget_zero_keeps_the_foreign_guard(self):
        """Reproduced: `--mutation-max 0` returned exit 0 for this fixture."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log_path = Path(directory) / "ev.jsonl"
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--mutation-max", "0",
                    "--evidence-log", str(log_path),
                ]
            )
            self.assertEqual(code, EXIT_INCONCLUSIVE)
            decision = self._decision(log_path)
            self.assertEqual(
                decision["mutation"]["foreign_changed_files"], ["calculator.cpp"]
            )
            self.assertTrue(decision["suite_strength_unverified"])
            # The measurement really was disabled -- that part is honoured.
            self.assertFalse(decision["mutation"]["measured"])
            self.assertIsNone(decision["suite_strength"])

    def test_mutation_budget_zero_keeps_the_full_artefact_shape(self):
        """`strength.py` promises "fixed shape on every return path"."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            log_path = Path(directory) / "ev.jsonl"
            main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--mutation-max", "0",
                    "--evidence-log", str(log_path),
                ]
            )
            mutation = self._decision(log_path)["mutation"]
            for key in (
                "measured", "reason", "changed_files", "foreign_changed_files",
                "deleted_files", "mutants", "survivors", "mutants_counted",
                "stillborn",
            ):
                self.assertIn(key, mutation, f"{key} disappeared from the artefact")

    def test_mutation_budget_zero_still_merges_a_clean_python_patch(self):
        """The budget opt-out must keep working; only the guard was at fault."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for side in ("baseline", "patch"):
                (root / side).mkdir(parents=True, exist_ok=True)
                (root / side / "test_sum.py").write_text(
                    "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n"
                )
                (root / side / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--coverage-ratio", "1.0",
                    "--coverage-source", "untrusted",
                    "--mutation-max", "0",
                ]
            )
            self.assertEqual(code, EXIT_MERGE)

    # ------------------------------------------------------------------
    # A3: the diff parser assumed git's `+++ b/` prefix
    # ------------------------------------------------------------------
    GIT_DIFF = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n     return a + b\n+    # note\n"
    )
    PLAIN_DIFF = (
        "--- calc.py\t2026-01-01\n+++ calc.py\t2026-01-01\n@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n     return a + b\n+    # note\n"
    )

    def test_plain_diff_u_is_parsed(self):
        """`diff -u` output used to parse to zero files and measure 1.0."""
        cov = {"files": {"calc.py": {"executed_lines": []}}}
        self.assertEqual(covered_diff_ratio(self.PLAIN_DIFF, cov).ratio, 0.0)
        # ...where git's form gives the identical answer.
        self.assertEqual(covered_diff_ratio(self.GIT_DIFF, cov).ratio, 0.0)

    def test_both_prefixes_agree_on_the_path(self):
        self.assertEqual(
            validate_diff(self.PLAIN_DIFF),
            validate_diff(self.GIT_DIFF),
        )

    def test_unattributable_hunks_are_refused_not_measured_as_perfect(self):
        """A failed parse used to become `changed_lines: 0` -> ratio 1.0."""
        with self.assertRaises(UnparseableDiff):
            validate_diff("@@ -1,2 +1,3 @@\n+orphan line\n")

    def test_empty_diff_is_still_not_a_failed_parse(self):
        self.assertEqual(validate_diff(""), {})
        self.assertEqual(validate_diff("sem hunk aqui\n"), {})

    def test_deletion_only_diff_keeps_its_documented_vacuous_ratio(self):
        cov = {"files": {}}
        diff = "--- a/x\n+++ b/x\n@@ -1,2 +0,0 @@\n-old\n-older\n"
        self.assertEqual(covered_diff_ratio(diff, cov).ratio, 1.0)

    def test_whole_file_deletion_is_parsed_not_refused(self):
        """`+++ /dev/null` is how a deleted file is spelled.

        The old header was ignored, so the hunk had no file to belong to and
        `validate_diff` rejected a perfectly good diff as unparseable -- the
        gate telling you your *input* was bad when it was fine.
        """
        diff = (
            "--- a/legacy.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n"
            "-def a():\n-    pass\n-def b():\n"
        )
        self.assertEqual(validate_diff(diff), {"legacy.py": set()})
        # Same vacuous ratio as any other deletion-only patch: nothing added.
        self.assertEqual(covered_diff_ratio(diff, {"files": {}}).ratio, 1.0)

    def test_file_added_out_of_nothing_still_attributes_to_the_new_path(self):
        """`--- /dev/null` has no old path, so the new one must win."""
        diff = (
            "--- /dev/null\n+++ b/novo.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
        )
        self.assertEqual(validate_diff(diff), {"novo.py": {1, 2}})

    def test_deletion_and_modification_in_one_patch(self):
        """The realistic case: the deleted file must not swallow the other."""
        diff = (
            "--- a/legacy.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-gone()\n"
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,1 +1,2 @@\n def x():\n+    y = 1\n"
        )
        self.assertEqual(
            validate_diff(diff), {"legacy.py": set(), "calc.py": {2}}
        )

    def test_cli_refuses_an_unattributable_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            diff_path = Path(directory) / "bad.diff"
            diff_path.write_text("@@ -1,2 +1,3 @@\n+orphan line\n")
            cov_path = Path(directory) / "cov.json"
            cov_path.write_text(json.dumps({"files": {}}))
            code = main(
                [
                    "--baseline", str(root / "baseline"),
                    "--patch", str(root / "patch"),
                    "--test-path", "test_sum.py",
                    "--test-id", "test_add",
                    "--diff", str(diff_path),
                    "--coverage-json", str(cov_path),
                ]
            )
            self.assertEqual(code, EXIT_USAGE)

    @staticmethod
    def _decision(log_path: Path) -> dict:
        records = [json.loads(line) for line in log_path.read_text().splitlines()]
        return [r for r in records if r.get("kind") == "decision"][-1]


class TestBaselineNotApplicable(unittest.TestCase):
    """AG-009: a guard shadowed the two branches written for these states.

    ``classify`` opened with "baseline must be PASS or FAIL, else UNVERIFIED".
    Every ``StabilityPolicy`` runs more than once (3 / 100 / 5), so that guard
    fired on every claim -- and ``NEW_BUG`` ("test is absent on baseline and
    fails on patch") plus ``"baseline timed out"`` were unreachable, while a
    patch adding a new test was reported as flaky.
    """

    @staticmethod
    def _classify(base, patch) -> GateVerdict:
        gate = make_gate()
        policy = policy_for(BugKind.DETERMINISTIC)
        return gate.classify(
            CriticClaim("t.py", "t1"),
            ExecutionOutcome(
                base,
                patch,
                baseline_failures=0,
                patch_failures=policy.runs if patch is ExecState.FAIL else 0,
                run_count=policy.runs,
                baseline_exit_codes=(),
                patch_exit_codes=(0,) * policy.runs,
            ),
        )

    def test_new_test_that_passes_is_verified(self):
        v = self._classify(ExecState.NOT_APPLICABLE, ExecState.PASS)
        self.assertIs(v.outcome, Outcome.VERIFIED)
        self.assertIs(v.classification, FailureClass.CLAIM_DISCARDED)

    def test_new_test_that_fails_is_a_new_bug(self):
        v = self._classify(ExecState.NOT_APPLICABLE, ExecState.FAIL)
        self.assertIs(v.outcome, Outcome.REFUTED)
        self.assertIs(v.classification, FailureClass.NEW_BUG)

    def test_baseline_timeout_reaches_its_own_branch(self):
        v = self._classify(ExecState.TIMED_OUT, ExecState.PASS)
        self.assertIs(v.outcome, Outcome.UNVERIFIED)
        self.assertIn("baseline timed out", v.reason)

    def test_the_misleading_flaky_reason_is_gone(self):
        v = self._classify(ExecState.NOT_APPLICABLE, ExecState.PASS)
        self.assertNotIn("not consistent across runs", v.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)

