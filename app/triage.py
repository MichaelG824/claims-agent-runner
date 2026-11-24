"""Triage one claim: rules first, then the model tiers, then a human.

Pure of I/O apart from model calls and sleeping, so it is easy to test with
scripted models and a no-op sleep.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.domain import Claim, ModelReply
from app.llm import Attempt, CompletionModel, RateLimiter
from app.mock_model import ModelServerError, ModelTimeout, RateLimitError
from app.prompt import build_prompt
from app.rules import Expectation, expect
from app.validate import Outcome, validate_reply


class Status(StrEnum):
    DONE = "done"
    HUMAN_REVIEW = "human_review"


class Source(StrEnum):
    RULE = "rule"
    MODEL = "model"


@dataclass(frozen=True)
class Tier:
    """A model plus the limiter that paces calls to it. Tiers are tried in order."""

    model: CompletionModel
    limiter: RateLimiter


@dataclass(frozen=True)
class Policy:
    attempts_per_tier: int = 3
    # Rate-limit waits don't count as attempts, but are capped so a stuck
    # quota can't spin a worker forever.
    max_rate_limit_waits: int = 20
    backoff_base_s: float = 0.2
    backoff_cap_s: float = 2.0
    timeout_s: float = 2.0


@dataclass(frozen=True)
class TriageResult:
    claim_id: str
    status: Status
    source: Source
    primary_code: str | None
    category: str | None
    action: str | None
    confidence: Decimal | None = None
    rationale: str | None = None
    appeal_draft: str | None = None
    model: str | None = None
    review_reason: str | None = None
    # Sent once per tier attempt; stored so the audit survives prompt changes.
    prompt: str | None = None
    attempts: tuple[Attempt, ...] = field(default=())

    @property
    def cost_usd(self) -> Decimal:
        return sum((a.cost_usd for a in self.attempts), Decimal("0"))


class Triager:
    def __init__(
        self,
        tiers: Sequence[Tier],
        policy: Policy = Policy(),
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        if not tiers:
            raise ValueError("at least one model tier is required")
        self._tiers = tuple(tiers)
        self._policy = policy
        self._sleep = sleep
        self._jitter = jitter

    def triage(self, claim: Claim) -> TriageResult:
        expected = expect(claim)
        if not expected.needs_model:
            return TriageResult(
                claim_id=claim.claim_id,
                status=Status.HUMAN_REVIEW,
                source=Source.RULE,
                primary_code=None,
                category=None,
                action=None,
                review_reason=expected.review_reason,
            )

        assert expected.primary is not None
        prompt = build_prompt(claim, expected.primary)
        attempts: list[Attempt] = []

        for tier in self._tiers:
            reply = self._try_tier(tier, prompt, expected, attempts)
            if reply is not None:
                return self._accepted(claim, expected, tier, reply, prompt, attempts)

        return TriageResult(
            claim_id=claim.claim_id,
            status=Status.HUMAN_REVIEW,
            source=Source.RULE,
            primary_code=expected.primary.code,
            # The lookup is still a useful suggestion for the specialist; status
            # says it was not confirmed and the reason says why.
            category=expected.category,
            action=expected.action,
            review_reason=_summarise_failures(attempts),
            prompt=prompt,
            attempts=tuple(attempts),
        )

    def _try_tier(
        self, tier: Tier, prompt: str, expected: Expectation, attempts: list[Attempt]
    ) -> ModelReply | None:
        name = tier.model.model
        failures = rate_limit_waits = 0

        while failures < self._policy.attempts_per_tier:
            tier.limiter.acquire()
            try:
                response = tier.model.complete(prompt, timeout_s=self._policy.timeout_s)
            except RateLimitError as exc:
                attempts.append(Attempt(name, Outcome.RATE_LIMITED, str(exc)))
                rate_limit_waits += 1
                if rate_limit_waits > self._policy.max_rate_limit_waits:
                    return None
                self._sleep(exc.retry_after)
                continue
            except (ModelTimeout, ModelServerError) as exc:
                outcome = Outcome.TIMEOUT if isinstance(exc, ModelTimeout) else Outcome.SERVER_ERROR
                attempts.append(Attempt(name, outcome, str(exc)))
                failures += 1
                self._backoff(failures)
                continue

            verdict = validate_reply(response.text, expected)
            attempts.append(Attempt.from_response(response, verdict.outcome, verdict.detail))
            if verdict.accepted:
                return verdict.reply
            # Bad output is not a transient fault, so retry without backing off.
            failures += 1
        return None

    def _backoff(self, failures: int) -> None:
        # Exponential backoff with full jitter, so workers don't retry in lockstep.
        ceiling = min(self._policy.backoff_cap_s, self._policy.backoff_base_s * 2 ** (failures - 1))
        self._sleep(ceiling * self._jitter())

    @staticmethod
    def _accepted(
        claim: Claim,
        expected: Expectation,
        tier: Tier,
        reply: ModelReply,
        prompt: str,
        attempts: list[Attempt],
    ) -> TriageResult:
        assert expected.primary is not None
        return TriageResult(
            claim_id=claim.claim_id,
            status=Status.DONE,
            source=Source.MODEL,
            primary_code=expected.primary.code,
            category=reply.category,
            action=reply.action,
            confidence=Decimal(str(reply.confidence)),
            rationale=reply.rationale,
            appeal_draft=reply.appeal_draft,
            model=tier.model.model,
            prompt=prompt,
            attempts=tuple(attempts),
        )


def _summarise_failures(attempts: Sequence[Attempt]) -> str:
    counts: dict[str, int] = {}
    for attempt in attempts:
        counts[attempt.outcome.value] = counts.get(attempt.outcome.value, 0) + 1
    detail = ", ".join(f"{name} x{n}" for name, n in sorted(counts.items()))
    return f"no model reply passed validation ({detail})"
