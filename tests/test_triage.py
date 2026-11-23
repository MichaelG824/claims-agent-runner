from dataclasses import replace
from decimal import Decimal

import pytest

from app.llm import RateLimiter
from app.mock_model import PROFILES, MockModel, ModelServerError, ModelTimeout, RateLimitError
from app.rules import expect
from app.triage import Policy, Source, Status, Tier, Triager
from app.validate import Outcome
from tests.conftest import ScriptedModel, make_claim, reply_json

GOOD_APPEAL = reply_json("medical_necessity", "appeal")
WRONG = reply_json("duplicate", "appeal")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def free_limiter() -> RateLimiter:
    return RateLimiter(1_000_000, sleep=lambda _: None)


DEFAULT_POLICY = Policy()


def make_triager(*models, policy=DEFAULT_POLICY, sleeps=None) -> Triager:
    record = sleeps.append if sleeps is not None else (lambda _: None)
    return Triager(
        [Tier(m, free_limiter()) for m in models],
        policy,
        sleep=record,
        jitter=lambda: 1.0,
    )


def test_happy_path_uses_one_small_call():
    small = ScriptedModel([GOOD_APPEAL])
    result = make_triager(small).triage(make_claim())
    assert result.status is Status.DONE
    assert (result.category, result.action, result.model) == ("medical_necessity", "appeal", "mock-small")
    assert result.confidence == Decimal("0.9")
    assert [a.outcome for a in result.attempts] == [Outcome.OK]


def test_transient_errors_are_retried_with_backoff():
    sleeps: list[float] = []
    small = ScriptedModel([ModelTimeout("slow"), ModelServerError("500"), GOOD_APPEAL])
    result = make_triager(small, sleeps=sleeps).triage(make_claim())
    assert result.status is Status.DONE
    assert [a.outcome for a in result.attempts] == [Outcome.TIMEOUT, Outcome.SERVER_ERROR, Outcome.OK]
    assert sleeps == [0.2, 0.4]


def test_rate_limit_waits_retry_after_and_does_not_use_an_attempt():
    sleeps: list[float] = []
    small = ScriptedModel([RateLimitError(0.37)] * 5 + [GOOD_APPEAL])
    result = make_triager(small, policy=Policy(attempts_per_tier=1), sleeps=sleeps).triage(make_claim())
    assert result.status is Status.DONE
    assert sleeps == [0.37] * 5


def test_misclassification_escalates_to_large_model():
    small = ScriptedModel([WRONG])
    large = ScriptedModel([GOOD_APPEAL], model="mock-large")
    result = make_triager(small, large).triage(make_claim())
    assert result.status is Status.DONE
    assert result.model == "mock-large"
    assert [a.outcome for a in result.attempts] == [Outcome.MISMATCH] * 3 + [Outcome.OK]


def test_exhausted_tiers_go_to_human_review_with_reason_and_suggestion():
    small = ScriptedModel([WRONG])
    large = ScriptedModel(["not json"], model="mock-large")
    result = make_triager(small, large).triage(make_claim())
    assert result.status is Status.HUMAN_REVIEW
    assert result.source is Source.RULE
    # The CARC lookup is offered as a suggestion, never as a confirmed answer.
    assert (result.category, result.action) == ("medical_necessity", "appeal")
    assert result.rationale is None
    assert "malformed x3" in result.review_reason and "mismatch x3" in result.review_reason
    assert len(result.attempts) == 6


def test_no_reason_code_never_calls_the_model():
    small = ScriptedModel([GOOD_APPEAL])
    result = make_triager(small).triage(make_claim(codes=[]))
    assert result.status is Status.HUMAN_REVIEW
    assert small.prompts == []
    assert result.attempts == ()


def test_cost_is_summed_as_decimal():
    small = ScriptedModel([WRONG, GOOD_APPEAL])
    result = make_triager(small).triage(make_claim())
    assert result.cost_usd == sum((a.cost_usd for a in result.attempts), Decimal("0"))
    assert result.cost_usd > 0


def test_rate_limiter_spaces_calls_evenly():
    clock = FakeClock()
    limiter = RateLimiter(4, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        limiter.acquire()
    assert clock.sleeps == pytest.approx([0.25, 0.25, 0.25, 0.25])


@pytest.mark.parametrize("seed", range(5))
def test_never_accepts_a_wrong_answer_from_the_real_mock(seed):
    """Run the unmodified failure profile hard and check every accepted answer."""
    # Only the rate limit is lifted so the test is fast; failure rates are untouched.
    profile = replace(PROFILES["mock-small"], rate_limit_per_sec=1_000_000, latency_ms=(0, 0))
    model = MockModel("mock-small", seed=seed, profile=profile, sleep=False)
    triager = make_triager(model)
    codes = ["CO-16", "CO-50", "CO-97", "CO-29", "CO-197", "CO-45", "CO-18", "CO-22", "PR-1"]
    for i in range(200):
        claim = make_claim(f"CLM-{i}", codes=[(codes[i % len(codes)], "10.00")])
        result = triager.triage(claim)
        expected = expect(claim)
        if result.status is Status.DONE:
            assert (result.category, result.action) == (expected.category, expected.action)
            assert (result.appeal_draft is not None) == (result.action == "appeal")
        else:
            assert result.review_reason
