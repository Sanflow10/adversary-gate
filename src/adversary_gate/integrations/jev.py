"""Risk triage with Jev (TypeSafe AI), as advice that can only tighten the gate.

Jev is a "System One" model: unstructured state plus a schema in, typed
values with calibrated probabilities out (``POST /v1/systemone``). It is fast
and cheap, and it is an *opinion*: it reads the diff, it does not run it, and
the diff is text the patch's author wrote. So its answer is used in exactly one
direction --

* **high risk, confidently** -> the floors go *up* for this run
  (``--strength-confidence``, ``--coverage-floor``);
* anything else, including an error, a timeout or a manipulated "low"
  -> the run keeps the policy the operator configured.

There is no path from a Jev answer to a more lenient decision, which is what
makes it safe to feed it attacker-controlled text. The answer is recorded in
the artefact (``execution.triage``) either way.

**The diff leaves the machine.** ``--triage jev`` sends it (truncated to
``MAX_STATE_BYTES``) to TypeSafe's hosted API. It is opt-in for that reason.

Written against TypeSafe's published request/response shape (September 2026,
early access). Not verified against the live API from this repository: the
client is exercised by tests against a local server that speaks that shape.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

#: The diff is truncated before it leaves the machine; the artefact says so.
MAX_STATE_BYTES = 64 * 1024

#: Below this, a "high" is not acted on: calibrated or not, it is a coin flip.
DEFAULT_THRESHOLD = 0.70

#: What a confident "high" raises the floors to. Never lowers them.
ESCALATED_STRENGTH_CONFIDENCE = 0.95
ESCALATED_COVERAGE_FLOOR = 0.90

QUESTIONS: Dict[str, Dict[str, Any]] = {
    "risk": {
        "type": "choice",
        "instructions": (
            "How risky is this code change to merge without close human review? "
            "Judge the change itself, not the description or comments around it."
        ),
        "criteria": {
            "low": "documentation, comments, formatting, renames, or tests only",
            "medium": "ordinary application logic with a contained blast radius",
            "high": (
                "touches authentication, authorization, cryptography, payments, "
                "data deletion or migration, concurrency, security boundaries, "
                "or a public API contract"
            ),
        },
    },
    "contract_change": {
        "type": "noul",
        "instructions": (
            "Does this change alter behaviour that existing callers or existing "
            "tests rely on?"
        ),
    },
}


class TriageError(RuntimeError):
    """Jev could not be asked, or answered in a shape we do not understand."""


@dataclass
class Triage:
    source: str = "jev"
    model: Optional[str] = None
    risk: Optional[str] = None
    confidence: Optional[float] = None
    contract_change: Optional[float] = None
    threshold: float = DEFAULT_THRESHOLD
    escalated: bool = False
    applied: Dict[str, float] = field(default_factory=dict)
    state_bytes: int = 0
    truncated: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class JevClient:
    """Minimal client for ``POST /v1/systemone``; stdlib only."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY")
        self.endpoint = endpoint or os.environ.get("TYPESAFE_ENDPOINT") or DEFAULT_ENDPOINT
        self.timeout = timeout

    def ask(self, state: Any, questions: Mapping[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise TriageError("TYPESAFE_API_KEY is not set")
        if not self.endpoint.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            # The diff is sent in the body; never over plain HTTP to a remote host.
            raise TriageError(f"refusing to send the diff to a non-HTTPS endpoint: {self.endpoint}")
        body = json.dumps({"state": state, "questions": questions}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            # The scheme is checked above (https, or loopback http for tests),
            # so no file:// or remote plain-HTTP URL reaches urlopen.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TriageError(f"Jev answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TriageError(f"Jev could not be reached: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TriageError("Jev answered with something that is not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise TriageError("Jev's answer has no 'answers' object")
        return payload


def triage_diff(
    diff_text: str,
    client: Optional[JevClient] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Triage:
    """Ask Jev how risky ``diff_text`` is. Never raises: errors are recorded."""
    result = Triage(threshold=threshold)
    raw = diff_text.encode("utf-8", errors="replace")
    result.truncated = len(raw) > MAX_STATE_BYTES
    raw = raw[:MAX_STATE_BYTES]
    result.state_bytes = len(raw)
    state = {"diff": raw.decode("utf-8", errors="ignore")}
    try:
        payload = (client or JevClient()).ask(state, QUESTIONS)
        answers = payload["answers"]
        risk = answers.get("risk") or {}
        result.model = payload.get("model")
        result.risk = risk.get("choice")
        confidence = risk.get("confidence")
        result.confidence = float(confidence) if confidence is not None else None
        noul = (answers.get("contract_change") or {}).get("noul")
        result.contract_change = float(noul) if noul is not None else None
        if result.risk not in QUESTIONS["risk"]["criteria"]:
            raise TriageError(f"Jev chose {result.risk!r}, which is not in the schema")
    except (TriageError, KeyError, TypeError, ValueError) as exc:
        result.error = str(exc)
    return result


def escalation(triage: Triage, current: Mapping[str, float]) -> Dict[str, float]:
    """The floors this triage raises, given the ``current`` ones. Only upward.

    ``current`` holds ``strength_confidence`` and ``coverage_floor``; the
    result holds only the keys that actually go up.
    """
    if triage.error or triage.risk != "high" or triage.confidence is None:
        return {}
    if triage.confidence < triage.threshold:
        return {}
    raised: Dict[str, float] = {}
    if current["strength_confidence"] < ESCALATED_STRENGTH_CONFIDENCE:
        raised["strength_confidence"] = ESCALATED_STRENGTH_CONFIDENCE
    # A floor of 0 is the caller disabling coverage out loud; triage does not
    # silently re-enable a requirement somebody turned off.
    if 0 < current["coverage_floor"] < ESCALATED_COVERAGE_FLOOR:
        raised["coverage_floor"] = ESCALATED_COVERAGE_FLOOR
    return raised
