"""Prompt construction.

The prompt deliberately omits the patient block (name, date of birth, member
ID): the model does not need identity to classify a denial or draft an appeal.
"""

from __future__ import annotations

from app.domain import Adjustment, Claim
from app.mock_model import VALID_ACTIONS, VALID_CATEGORIES


def build_prompt(claim: Claim, primary: Adjustment) -> str:
    # The primary code goes first: models (including the mock) tend to anchor
    # on the first code they see. Taxonomy codes are never listed verbatim,
    # because any CARC-looking string in the prompt can be read as the denial.
    others = [a for a in claim.adjustments if a is not primary]
    lines = "\n".join(
        f"  line {line.line_no}: CPT {line.cpt}, billed {line.billed}, allowed {line.allowed}"
        for line in claim.lines
    )
    other_codes = ", ".join(f"{a.code} ({a.amount})" for a in others) or "none"

    return (
        "You triage denied medical claims for a hospital billing team.\n"
        f"Primary denial: {primary.code}, amount {primary.amount}\n"
        f"Other adjustments: {other_codes}\n"
        f"Payer: {claim.payer}\n"
        f"Service date: {claim.service_date.isoformat()}\n"
        f"Service lines:\n{lines}\n"
        f"Clinical note: {claim.clinical_note}\n\n"
        f"Valid categories: {', '.join(VALID_CATEGORIES)}\n"
        f"Valid actions: {', '.join(VALID_ACTIONS)}\n"
        "Reply with only a JSON object with keys category, action, confidence (0-1), "
        "rationale, and appeal_draft (a letter when action is appeal, otherwise null)."
    )
