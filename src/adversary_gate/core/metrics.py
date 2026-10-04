"""Metrics that make the thesis countable instead of rhetorical.

The thesis claims three kinds of loss: merged regressions, tasks reported
done that were not, and human rework. None of that is measurable from a
verdict list alone -- you also need to know what was *merged*. So these
functions consume both the verdict records and the decision records.

The one that matters most is ``unverified_merges``: patches that shipped
while the gate could not verify them. That is "parece certo" expressed as a
number. If the product's pitch is "menos autoengano", this is the dashboard
tile that proves it -- and the one to watch go down.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class GateMetrics:
    # --- claim level ---
    claims_total: int = 0
    verified: int = 0
    refuted: int = 0
    unverified: int = 0
    #: AG-030: of the verified claims, how many proved a fix (FAIL -> PASS)
    #: and how many only proved nothing regressed (PASS -> PASS).
    fixed: int = 0
    no_regression: int = 0

    # --- the three losses the thesis names ---
    refuted_patches_blocked: int = 0
    escaped_regressions: int = 0
    unverified_merges: int = 0

    # --- process cost ---
    rework_rounds_total: int = 0
    rework_rounds_max: int = 0
    decision_count: int = 0
    merge_count: int = 0
    block_count: int = 0
    inconclusive_count: int = 0

    # --- by model, for the "troca de modelo" claim ---
    by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def verified_rate(self) -> float:
        return self.verified / self.claims_total if self.claims_total else 0.0

    @property
    def unverified_rate(self) -> float:
        return self.unverified / self.claims_total if self.claims_total else 0.0

    @property
    def refuted_rate(self) -> float:
        return self.refuted / self.claims_total if self.claims_total else 0.0

    @property
    def self_deception_index(self) -> float:
        """Share of shipped patches that were never verified.

        0.0 -> everything that merged had executed evidence behind it.
        1.0 -> everything that merged was a guess.

        On records this gate wrote it is 0.0 by construction (AG-029):
        ``decide()`` never returns MERGE with an unverified claim. It measures
        something only over decisions from a pipeline that *can* merge
        unverified work -- which is why it is not the product's headline.
        """
        return (
            self.unverified_merges / self.merge_count if self.merge_count else 0.0
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.update(
            verified_rate=round(self.verified_rate, 4),
            unverified_rate=round(self.unverified_rate, 4),
            refuted_rate=round(self.refuted_rate, 4),
            self_deception_index=round(self.self_deception_index, 4),
        )
        return data


def _model_of(record: Dict[str, Any]) -> str:
    return str(record.get("ctx_model") or "unknown")


def compute_metrics(records: Iterable[Dict[str, Any]]) -> GateMetrics:
    """Fold evidence-log records into metrics.

    Accepts a flat iterable of records from both ``append`` (verdicts) and
    ``append_decision`` (patch decisions); they are discriminated by the
    ``kind`` field, defaulting to a verdict record.
    """
    metrics = GateMetrics()

    # First pass: claim outcomes, and per-model tallies.
    model_rows: Dict[str, Dict[str, int]] = {}
    for record in records:
        if record.get("kind") == "decision":
            continue
        metrics.claims_total += 1
        outcome = record.get("outcome")
        if outcome == "verified":
            metrics.verified += 1
        elif outcome == "refuted":
            metrics.refuted += 1
        elif outcome == "unverified":
            metrics.unverified += 1
        if record.get("classification") == "fixed":
            metrics.fixed += 1
        elif record.get("classification") == "no_regression":
            metrics.no_regression += 1

        model = _model_of(record)
        row = model_rows.setdefault(
            model, {"claims": 0, "verified": 0, "refuted": 0, "unverified": 0}
        )
        row["claims"] += 1
        if outcome in ("verified", "refuted", "unverified"):
            row[outcome] += 1

    # Second pass: decisions, which is where "and did it ship anyway?" lives.
    for record in records:
        if record.get("kind") != "decision":
            continue
        metrics.decision_count += 1
        decision = record.get("decision")
        rounds = int(record.get("rounds_used") or 0)
        metrics.rework_rounds_total += rounds
        metrics.rework_rounds_max = max(metrics.rework_rounds_max, rounds)

        claims_total = int(record.get("claims_total") or 0)
        refuted = int(record.get("refuted") or 0)
        unverified = int(record.get("unverified") or 0)

        if decision == "merge":
            metrics.merge_count += 1
            # The two loss counters.
            if unverified:
                metrics.unverified_merges += 1
        elif decision == "block":
            metrics.block_count += 1
            if refuted:
                metrics.refuted_patches_blocked += 1
        elif decision == "inconclusive":
            metrics.inconclusive_count += 1

        # A REFUTED claim followed by a merge is an escape: the gate saw the
        # bug and it shipped anyway. `decide()` cannot produce this today;
        # the counter exists so a regression in that invariant is visible.
        if decision == "merge" and refuted:
            metrics.escaped_regressions += 1

    metrics.by_model = model_rows
    return metrics


def compare_models(metrics: GateMetrics) -> Dict[str, Dict[str, Any]]:
    """Per-model rates, for the model-switch question.

    Deliberately reports *verification outcomes on the same harness*, not
    "which model is better" -- the gate runs the same tests either way, so
    what it can honestly say is which model's patches it could verify and
    how often the evidence condemned them.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for model, row in metrics.by_model.items():
        claims = row["claims"] or 1
        out[model] = {
            **row,
            "verified_rate": round(row["verified"] / claims, 4),
            "refuted_rate": round(row["refuted"] / claims, 4),
            "unverified_rate": round(row["unverified"] / claims, 4),
        }
    return out


def format_report(metrics: GateMetrics) -> str:
    """Compact human-readable summary for the CLI."""
    lines = [
        "Evidence summary",
        "----------------",
        f"claims                 {metrics.claims_total}",
        f"  verified             {metrics.verified}  ({metrics.verified_rate:.1%})",
        f"  refuted              {metrics.refuted}  ({metrics.refuted_rate:.1%})",
        f"  unverified           {metrics.unverified}  ({metrics.unverified_rate:.1%})",
        f"    of verified: fixed {metrics.fixed}, no regression {metrics.no_regression}",
        "",
        f"decisions              {metrics.decision_count}",
        f"  merge                {metrics.merge_count}",
        f"  block                {metrics.block_count}",
        f"  inconclusive         {metrics.inconclusive_count}",
        "",
        f"regressions blocked    {metrics.refuted_patches_blocked}",
        f"regressions escaped    {metrics.escaped_regressions}",
        f"unverified merges      {metrics.unverified_merges}",
        f"self-deception index   {metrics.self_deception_index:.1%}",
        f"rework rounds          avg "
        f"{(metrics.rework_rounds_total / metrics.decision_count) if metrics.decision_count else 0:.2f}"
        f"  max {metrics.rework_rounds_max}",
    ]
    comparison = compare_models(metrics)
    if comparison:
        lines += ["", "by model (same harness, same tests)"]
        for model, row in sorted(comparison.items()):
            lines.append(
                f"  {model:<24} claims={row['claims']:<4} "
                f"refuted={row['refuted_rate']:.1%} "
                f"unverified={row['unverified_rate']:.1%}"
            )
    return "\n".join(lines)
