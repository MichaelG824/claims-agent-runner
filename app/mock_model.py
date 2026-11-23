"""A fake LLM that behaves like a real one under load.

It is slow, has a per-second rate limit, sometimes times out or 500s, and
sometimes returns output you cannot trust: prose around the JSON, truncated
JSON, the wrong schema, or a confident answer with a made-up category.

Two models are available so model routing is a real trade-off:

    mock-small  cheap, fast, less accurate, flakier output
    mock-large  ~10x the price, slower, more accurate

Everything is driven by a seeded RNG so a run is reproducible.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field


class ModelError(Exception):
    """Base class for every failure the model can raise."""


class RateLimitError(ModelError):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:.2f}s")
        self.retry_after = retry_after


class ModelTimeout(ModelError):
    pass


class ModelServerError(ModelError):
    pass


# CARC group+code -> (category, recommended action). The model is asked to
# produce one of these categories; anything else is a hallucination.
DENIAL_TAXONOMY: dict[str, tuple[str, str]] = {
    "CO-16": ("missing_information", "correct_and_resubmit"),
    "CO-50": ("medical_necessity", "appeal"),
    "CO-97": ("bundling", "review_coding"),
    "CO-29": ("timely_filing", "write_off"),
    "CO-197": ("authorization", "appeal"),
    "CO-45": ("contractual_adjustment", "no_action"),
    "CO-18": ("duplicate", "no_action"),
    "CO-22": ("coordination_of_benefits", "bill_secondary"),
    "PR-1": ("patient_responsibility", "bill_patient"),
}

VALID_CATEGORIES = sorted({c for c, _ in DENIAL_TAXONOMY.values()})
VALID_ACTIONS = sorted({a for _, a in DENIAL_TAXONOMY.values()} | {"human_review"})

PRICING_PER_1K_TOKENS = {
    "mock-small": {"input": 0.0005, "output": 0.0015},
    "mock-large": {"input": 0.005, "output": 0.015},
}


@dataclass(frozen=True)
class ModelProfile:
    latency_ms: tuple[int, int]
    slow_tail_rate: float  # chance a call takes far longer than usual
    timeout_rate: float
    server_error_rate: float
    malformed_rate: float  # not parseable as JSON
    wrong_schema_rate: float  # valid JSON, wrong shape
    hallucination_rate: float  # valid shape, invented category
    misclassify_rate: float  # valid category, but the wrong one
    rate_limit_per_sec: int


PROFILES = {
    "mock-small": ModelProfile(
        latency_ms=(40, 250),
        slow_tail_rate=0.02,
        timeout_rate=0.02,
        server_error_rate=0.04,
        malformed_rate=0.08,
        wrong_schema_rate=0.06,
        hallucination_rate=0.05,
        misclassify_rate=0.10,
        rate_limit_per_sec=20,
    ),
    "mock-large": ModelProfile(
        latency_ms=(300, 1200),
        slow_tail_rate=0.03,
        timeout_rate=0.02,
        server_error_rate=0.03,
        malformed_rate=0.02,
        wrong_schema_rate=0.02,
        hallucination_rate=0.01,
        misclassify_rate=0.02,
        rate_limit_per_sec=5,
    ),
}

RELIABLE = ModelProfile(
    latency_ms=(0, 0),
    slow_tail_rate=0,
    timeout_rate=0,
    server_error_rate=0,
    malformed_rate=0,
    wrong_schema_rate=0,
    hallucination_rate=0,
    misclassify_rate=0,
    rate_limit_per_sec=10_000,
)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int

    @property
    def cost_usd(self) -> float:
        price = PRICING_PER_1K_TOKENS[self.model]
        return (
            self.input_tokens / 1000 * price["input"]
            + self.output_tokens / 1000 * price["output"]
        )


@dataclass
class ModelStats:
    calls: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    server_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_outcome: dict[str, int] = field(default_factory=dict)


_CODE_RE = re.compile(r"\b(CO|PR|OA|PI)-\d{1,3}\b")


class MockModel:
    """Thread-safe fake model. Call `complete(prompt)`.

    The prompt should contain the claim's denial code(s), e.g. "CO-50".
    The reply is meant to be JSON shaped like:

        {"category": str, "action": str, "confidence": float,
         "rationale": str, "appeal_draft": str | null}
    """

    def __init__(
        self,
        model: str = "mock-small",
        *,
        seed: int | None = None,
        profile: ModelProfile | None = None,
        sleep: bool = True,
    ):
        if model not in PROFILES:
            raise ValueError(f"unknown model {model!r}; choose from {sorted(PROFILES)}")
        self.model = model
        self.profile = profile or PROFILES[model]
        self.sleep = sleep
        self.stats = ModelStats()
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._recent_calls: deque[float] = deque()

    @classmethod
    def reliable(cls, model: str = "mock-small") -> "MockModel":
        """A model that never fails and never sleeps. Handy for unit tests."""
        return cls(model, seed=0, profile=RELIABLE, sleep=False)

    def complete(self, prompt: str, *, timeout_s: float = 2.0) -> ModelResponse:
        with self._lock:
            self._check_rate_limit()
            self.stats.calls += 1
            roll = self._rng.random
            p = self.profile
            lo, hi = p.latency_ms
            latency_ms = self._rng.randint(lo, hi)
            if roll() < p.slow_tail_rate:
                latency_ms = int(timeout_s * 1000 * self._rng.uniform(1.2, 3.0))
            timed_out = latency_ms > timeout_s * 1000 or roll() < p.timeout_rate
            server_error = roll() < p.server_error_rate
            outcome_roll = roll()
            pick = self._rng.choice
            confidence = round(self._rng.uniform(0.55, 0.99), 2)

        if timed_out:
            self._wait(timeout_s * 1000)
            with self._lock:
                self.stats.timeouts += 1
            raise ModelTimeout(f"no response within {timeout_s}s")

        self._wait(latency_ms)

        if server_error:
            with self._lock:
                self.stats.server_errors += 1
            raise ModelServerError("upstream returned HTTP 500")

        text, outcome = self._generate(prompt, outcome_roll, confidence, pick)
        response = ModelResponse(
            text=text,
            model=self.model,
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            latency_ms=latency_ms,
        )
        with self._lock:
            self.stats.input_tokens += response.input_tokens
            self.stats.output_tokens += response.output_tokens
            self.stats.cost_usd += response.cost_usd
            self.stats.by_outcome[outcome] = self.stats.by_outcome.get(outcome, 0) + 1
        return response

    # internals

    def _check_rate_limit(self) -> None:
        now = time.monotonic()
        while self._recent_calls and now - self._recent_calls[0] >= 1.0:
            self._recent_calls.popleft()
        if len(self._recent_calls) >= self.profile.rate_limit_per_sec:
            self.stats.rate_limited += 1
            retry_after = 1.0 - (now - self._recent_calls[0])
            raise RateLimitError(retry_after=max(retry_after, 0.01))
        self._recent_calls.append(now)

    def _wait(self, ms: float) -> None:
        if self.sleep and ms > 0:
            time.sleep(ms / 1000)

    def _generate(self, prompt, outcome_roll, confidence, pick) -> tuple[str, str]:
        p = self.profile
        codes = [m.group(0) for m in _CODE_RE.finditer(prompt)]
        known = [c for c in codes if c in DENIAL_TAXONOMY]

        if known:
            category, action = DENIAL_TAXONOMY[known[0]]
        else:
            category, action = "missing_information", "human_review"
            confidence = min(confidence, 0.6)

        thresholds = [
            ("malformed", p.malformed_rate),
            ("wrong_schema", p.wrong_schema_rate),
            ("hallucination", p.hallucination_rate),
            ("misclassified", p.misclassify_rate),
        ]
        outcome = "ok"
        acc = 0.0
        for name, rate in thresholds:
            acc += rate
            if outcome_roll < acc:
                outcome = name
                break

        if outcome == "misclassified":
            category = pick([c for c in VALID_CATEGORIES if c != category])
        if outcome == "hallucination":
            category = pick(["clinical_review_pending", "payer_error", "experimental_service"])

        body = {
            "category": category,
            "action": action,
            "confidence": confidence,
            "rationale": f"Denial code(s) {', '.join(codes) or 'none found'} indicate {category}.",
            "appeal_draft": (
                f"To whom it may concern: we request reconsideration of this {category} denial..."
                if action == "appeal"
                else None
            ),
        }

        if outcome == "wrong_schema":
            variant = pick(["missing_field", "string_confidence", "nested"])
            if variant == "missing_field":
                body.pop("action")
            elif variant == "string_confidence":
                body["confidence"] = "high"
            else:
                body = {"result": body}

        text = json.dumps(body)
        if outcome == "malformed":
            variant = pick(["prose", "truncated", "fenced"])
            if variant == "prose":
                text = f"Sure! Here is my analysis of the claim:\n{text}\nLet me know if you need more."
            elif variant == "truncated":
                text = text[: len(text) // 2]
            else:
                text = f"```json\n{text}\n```"
        return text, outcome
