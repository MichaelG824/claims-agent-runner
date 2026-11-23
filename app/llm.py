"""Model access: a client-side rate limiter and one-call attempt records."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.mock_model import ModelResponse
from app.validate import Outcome

COST_QUANTUM = Decimal("0.000001")


class CompletionModel(Protocol):
    """Anything shaped like MockModel. Lets tests script exact replies."""

    model: str

    def complete(self, prompt: str, *, timeout_s: float = ...) -> ModelResponse: ...


class RateLimiter:
    """Spaces calls evenly so we stay under a per-second limit.

    Pacing ourselves is cheaper than bouncing off the provider's limit and
    sleeping on retry_after, though we still handle RateLimitError in case
    another process shares the quota.
    """

    def __init__(
        self,
        per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self._interval = 1.0 / per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        # Sleep outside the lock so other threads can reserve later slots.
        if (wait := slot - now) > 0:
            self._sleep(wait)


@dataclass(frozen=True)
class Attempt:
    """One model call and what we made of it; persisted as the audit trail."""

    model: str
    outcome: Outcome
    detail: str = ""
    raw_response: str | None = None
    latency_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")

    @classmethod
    def from_response(cls, response: ModelResponse, outcome: Outcome, detail: str = "") -> Attempt:
        return cls(
            model=response.model,
            outcome=outcome,
            detail=detail,
            raw_response=response.text,
            latency_ms=response.latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            # The mock reports cost as a float; convert via str and quantize so
            # stored cents never pick up binary rounding noise.
            cost_usd=Decimal(str(response.cost_usd)).quantize(COST_QUANTUM),
        )
