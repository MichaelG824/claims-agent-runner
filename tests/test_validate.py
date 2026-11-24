import json

import pytest

from app.rules import expect
from app.validate import Outcome, extract_json, validate_reply
from tests.conftest import make_claim, reply_json

APPEAL = expect(make_claim(codes=[("CO-50", "100.00")]))
NO_ACTION = expect(make_claim(codes=[("CO-45", "100.00")]))
GOOD = reply_json("medical_necessity", "appeal")


@pytest.mark.parametrize(
    "text",
    [
        GOOD,
        f"```json\n{GOOD}\n```",
        f"Sure! Here is my analysis of the claim:\n{GOOD}\nLet me know if you need more.",
    ],
    ids=["plain", "fenced", "prose"],
)
def test_recoverable_wrappers_are_accepted(text):
    verdict = validate_reply(text, APPEAL)
    assert verdict.outcome is Outcome.OK
    assert verdict.reply.category == "medical_necessity"


def test_truncated_reply_is_malformed():
    verdict = validate_reply(GOOD[: len(GOOD) // 2], APPEAL)
    assert verdict.outcome is Outcome.MALFORMED
    assert verdict.reply is None


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.pop("action"),
        lambda body: body.update(confidence="high"),
        lambda body: body.update(confidence=1.5),
        lambda body: body.update(extra="field"),
    ],
    ids=["missing_field", "string_confidence", "confidence_out_of_range", "unknown_key"],
)
def test_wrong_schema(mutate):
    body = json.loads(GOOD)
    mutate(body)
    assert validate_reply(json.dumps(body), APPEAL).outcome is Outcome.WRONG_SCHEMA


def test_nested_result_wrapper_is_wrong_schema():
    assert validate_reply(json.dumps({"result": json.loads(GOOD)}), APPEAL).outcome is Outcome.WRONG_SCHEMA


def test_invented_category_is_hallucinated():
    verdict = validate_reply(reply_json("experimental_service", "appeal"), APPEAL)
    assert verdict.outcome is Outcome.HALLUCINATED


def test_valid_but_wrong_category_is_caught_by_cross_check():
    # The silent failure: real category, valid JSON, wrong answer.
    verdict = validate_reply(reply_json("duplicate", "appeal"), APPEAL)
    assert verdict.outcome is Outcome.MISMATCH
    assert "CO-50" in verdict.detail


def test_appeal_without_draft_is_rejected():
    verdict = validate_reply(reply_json("medical_necessity", "appeal", draft="  "), APPEAL)
    assert verdict.outcome is Outcome.BAD_APPEAL_DRAFT


def test_draft_on_non_appeal_is_rejected():
    verdict = validate_reply(reply_json("contractual_adjustment", "no_action", draft="Dear payer"), NO_ACTION)
    assert verdict.outcome is Outcome.BAD_APPEAL_DRAFT
