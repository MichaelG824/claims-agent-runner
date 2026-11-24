"""Typed shapes for remittance input and model output.

Parsing happens once, at the edge. Everything past this module works with
validated objects, so money is always a Decimal and dates are always dates.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _money_from_text(value: Any) -> Any:
    # Floats cannot represent most cent amounts exactly, so refuse them outright
    # rather than silently rounding. Remittances carry money as strings.
    if isinstance(value, float):
        raise ValueError("money must be a string or Decimal, not float")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(_money_from_text),
    Field(max_digits=12, decimal_places=2),
]


class Line(BaseModel):
    model_config = ConfigDict(frozen=True)

    line_no: int = Field(ge=1)
    cpt: str = Field(min_length=1)
    billed: Money
    allowed: Money


class Adjustment(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    amount: Money


class Patient(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    dob: date
    member_id: str


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim_id: str = Field(min_length=1)
    payer: str
    patient: Patient
    service_date: date
    lines: tuple[Line, ...] = Field(min_length=1)
    adjustments: tuple[Adjustment, ...]
    clinical_note: str = ""

    @property
    def total_billed(self) -> Decimal:
        return sum((line.billed for line in self.lines), Decimal("0.00"))


class ModelReply(BaseModel):
    """The JSON shape we ask the model for.

    Strict mode rejects coercions such as confidence="high" or a missing field,
    which the mock produces on purpose. Unknown keys (e.g. a "result" wrapper)
    are rejected too.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    category: str
    action: str
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    appeal_draft: str | None
