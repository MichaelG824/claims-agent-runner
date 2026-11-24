from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain import Claim
from app.prompt import build_prompt
from app.rules import expect, primary_adjustment
from tests.conftest import make_claim, make_raw


def test_money_parses_to_exact_decimal():
    claim = make_claim(codes=[("CO-50", "1123.91")])
    assert claim.adjustments[0].amount == Decimal("1123.91")
    assert isinstance(claim.lines[0].billed, Decimal)


def test_money_rejects_floats():
    raw = make_raw()
    raw["lines"][0]["billed"] = 0.1
    with pytest.raises(ValidationError, match="not float"):
        Claim.model_validate(raw)


def test_money_rejects_fractional_cents():
    raw = make_raw(codes=[("CO-50", "10.001")])
    with pytest.raises(ValidationError):
        Claim.model_validate(raw)


def test_total_billed_sums_lines_exactly():
    raw = make_raw()
    raw["lines"] = [
        {"line_no": 1, "cpt": "99214", "billed": "0.10", "allowed": "0.10"},
        {"line_no": 2, "cpt": "36415", "billed": "0.20", "allowed": "0.20"},
    ]
    # 0.1 + 0.2 != 0.3 in float; Decimal gets it right.
    assert Claim.model_validate(raw).total_billed == Decimal("0.30")


def test_primary_is_largest_known_adjustment():
    claim = make_claim(codes=[("CO-45", "20.00"), ("CO-50", "300.00")])
    assert primary_adjustment(claim).code == "CO-50"


def test_primary_tie_keeps_remittance_order():
    claim = make_claim(codes=[("CO-97", "50.00"), ("CO-50", "50.00")])
    assert primary_adjustment(claim).code == "CO-97"


def test_unknown_codes_are_ignored_for_primary():
    claim = make_claim(codes=[("CO-999", "900.00"), ("PR-1", "10.00")])
    assert primary_adjustment(claim).code == "PR-1"


def test_expectation_from_taxonomy():
    expected = expect(make_claim(codes=[("CO-197", "80.00")]))
    assert (expected.category, expected.action) == ("authorization", "appeal")
    assert expected.needs_model


@pytest.mark.parametrize(
    ("codes", "reason"),
    [([], "no adjustment reason code"), ([("CO-999", "10.00")], "unknown reason code(s): CO-999")],
)
def test_claims_without_known_code_skip_the_model(codes, reason):
    expected = expect(make_claim(codes=codes))
    assert not expected.needs_model
    assert reason in expected.review_reason


def test_prompt_excludes_patient_identity():
    claim = make_claim()
    prompt = build_prompt(claim, claim.adjustments[0])
    for phi in ("Ana Lee", "1980-01-02", "M12345678"):
        assert phi not in prompt


def test_prompt_leads_with_primary_code():
    claim = make_claim(codes=[("CO-45", "20.00"), ("CO-50", "300.00")])
    prompt = build_prompt(claim, primary_adjustment(claim))
    assert prompt.index("CO-50") < prompt.index("CO-45")
