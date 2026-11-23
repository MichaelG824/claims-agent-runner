import json
from pathlib import Path

from sqlalchemy import text

from app.db import get_engine
from app.mock_model import VALID_CATEGORIES, MockModel

DATA = Path(__file__).parent.parent / "data" / "denied_claims.json"


def test_database_connects():
    with get_engine().connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


def test_sample_claims_load():
    claims = json.loads(DATA.read_text())
    assert len(claims) > 200
    assert {"claim_id", "lines", "adjustments"} <= claims[0].keys()


def test_reliable_model_returns_valid_json():
    reply = MockModel.reliable().complete("Denied claim with adjustment CO-50")
    body = json.loads(reply.text)
    assert body["category"] in VALID_CATEGORIES
    assert body["action"] == "appeal"
