"""Quarantine: fingerprints of claims that were actually proven.

Only REFUTED verdicts are stored. The original keyed off ``verdict.accepted``,
which was a bool derived from the same fail-open path -- now ``outcome`` is
explicit, so an UNVERIFIED claim can never be mistaken for a proven one.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List

from adversary_gate.core.types import CriticClaim, GateVerdict, Outcome


@dataclass(frozen=True)
class QuarantinedClaim:
    fingerprint: str
    test_path: str
    test_id: str
    classification: str
    rationale: str
    source_outcome: str = ""


def fingerprint_claim(claim: CriticClaim, test_source: str = "") -> str:
    material = "\0".join(
        (claim.test_path, claim.test_id, claim.bug_kind.value, test_source)
    )
    return hashlib.sha256(material.encode()).hexdigest()


class QuarantineStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> List[QuarantinedClaim]:
        if not self.path.exists():
            return []
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        return [QuarantinedClaim(**row) for row in rows]

    def add(self, verdict: GateVerdict, test_source: str = "") -> bool:
        # Proven bugs only. UNVERIFIED and VERIFIED are not quarantineable.
        if verdict.outcome is not Outcome.REFUTED:
            return False
        claim = verdict.claim
        item = QuarantinedClaim(
            fingerprint_claim(claim, test_source),
            claim.test_path,
            claim.test_id,
            verdict.classification.value,
            claim.rationale,
            verdict.outcome.value,
        )
        rows = self.load()
        if any(row.fingerprint == item.fingerprint for row in rows):
            return False
        rows.append(item)
        fd, temporary = tempfile.mkstemp(prefix=".quarantine-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump([asdict(row) for row in rows], handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return True
