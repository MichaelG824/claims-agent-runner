"""Deterministic classification from the CARC code.

The taxonomy is a lookup table, so the correct category and action are known
before the model is called. The model's job is to explain and draft; this
module is the source of truth its answer is checked against.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain import Adjustment, Claim
from app.mock_model import DENIAL_TAXONOMY


@dataclass(frozen=True)
class Expectation:
    """What the claim should be classified as, or why it can't be."""

    primary: Adjustment | None
    category: str | None
    action: str | None
    # Set when no model call should be made; the claim goes straight to review.
    review_reason: str | None = None

    @property
    def needs_model(self) -> bool:
        return self.review_reason is None


def primary_adjustment(claim: Claim) -> Adjustment | None:
    """The known adjustment carrying the most money; ties keep remittance order.

    Assumption: the largest adjustment is the denial worth working. Secondary
    adjustments are still stored and shown in the prompt.
    """
    known = [a for a in claim.adjustments if a.code in DENIAL_TAXONOMY]
    if not known:
        return None
    # max() returns the first maximal element, so ties resolve to the earlier code.
    return max(known, key=lambda a: a.amount)


def expect(claim: Claim) -> Expectation:
    if not claim.adjustments:
        return Expectation(None, None, None, review_reason="no adjustment reason code on remittance")

    primary = primary_adjustment(claim)
    if primary is None:
        codes = ", ".join(a.code for a in claim.adjustments)
        return Expectation(None, None, None, review_reason=f"unknown reason code(s): {codes}")

    category, action = DENIAL_TAXONOMY[primary.code]
    return Expectation(primary, category, action)
