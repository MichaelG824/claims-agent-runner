from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.db import DEFAULT_DATABASE_URL
from app.domain import Claim
from app.mock_model import ModelResponse
from app.store import create_schema


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A throwaway Postgres schema per test, so tests never see each other's rows."""
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'create schema "{schema}"'))
    scoped = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    create_schema(scoped)
    try:
        yield scoped
    finally:
        scoped.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'drop schema "{schema}" cascade'))
        admin.dispose()


def make_raw(claim_id: str = "CLM-1", codes: list[tuple[str, str]] | None = None) -> dict:
    codes = [("CO-50", "100.00")] if codes is None else codes
    return {
        "claim_id": claim_id,
        "payer": "Aetna",
        "patient": {"name": "Ana Lee", "dob": "1980-01-02", "member_id": "M12345678"},
        "service_date": "2026-10-01",
        "lines": [{"line_no": 1, "cpt": "99214", "billed": "250.00", "allowed": "100.00"}],
        "adjustments": [{"code": c, "amount": a} for c, a in codes],
        "clinical_note": "Patient presented with symptoms; see chart for details.",
    }


def make_claim(claim_id: str = "CLM-1", codes: list[tuple[str, str]] | None = None) -> Claim:
    return Claim.model_validate(make_raw(claim_id, codes))


def reply_json(category: str, action: str, *, draft: str | None = None, confidence: float = 0.9) -> str:
    if draft is None and action == "appeal":
        draft = "Please reconsider."
    return json.dumps(
        {
            "category": category,
            "action": action,
            "confidence": confidence,
            "rationale": "because",
            "appeal_draft": draft,
        }
    )


class ScriptedModel:
    """Returns (or raises) the scripted items in order, then repeats the last one."""

    def __init__(self, script: list[str | Exception], model: str = "mock-small"):
        self.model = model
        self._script = list(script)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: float = 2.0) -> ModelResponse:
        self.prompts.append(prompt)
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, Exception):
            raise item
        return ModelResponse(text=item, model=self.model, input_tokens=100, output_tokens=50, latency_ms=10)
