"""Circuit breaker for claims that keep failing to execute.

Changed from the original: it records :class:`Outcome` instead of a bool.
The old version counted ``accepted=False`` as a strike, which meant an
UNVERIFIED claim (harness down, test missing) counted the same as a
disproven one -- and once the circuit opened, the returned verdict had
``accepted=False``, which flowed straight into ``should_accept_patch -> True``.

So: opening a circuit now yields ``UNVERIFIED``, and the breaker only resets
on ``VERIFIED`` or ``REFUTED`` -- i.e. on claims that actually executed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Optional

from core.types import Outcome


@dataclass
class CircuitBreaker:
    threshold: int = 3
    _counts: Dict[str, int] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be at least 1")

    def is_open(self, test_id: str) -> bool:
        with self._lock:
            return self._counts.get(test_id, 0) >= self.threshold

    def record(self, test_id: str, outcome: Outcome) -> None:
        with self._lock:
            if outcome is Outcome.UNVERIFIED:
                # Could not run: that is a strike against the harness.
                self._counts[test_id] = self._counts.get(test_id, 0) + 1
            else:
                # Executed one way or the other; the claim is back in business.
                self._counts.pop(test_id, None)

    def reset(self, test_id: str) -> None:
        with self._lock:
            self._counts.pop(test_id, None)

    def open_circuits(self) -> Dict[str, int]:
        with self._lock:
            return {k: v for k, v in self._counts.items() if v >= self.threshold}

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)
