# json: parse the claims file and the model's reply.
import json
# Path: build file paths that work on any OS.
from pathlib import Path

# text: wraps a raw SQL string so SQLAlchemy can run it.
from sqlalchemy import text

# The app's database engine factory.
from app.db import get_engine
# The list of valid categories and the fake model.
from app.mock_model import VALID_CATEGORIES, MockModel

# Path to the claims file: this file's folder -> its parent (repo root) -> data/denied_claims.json.
DATA = Path(__file__).parent.parent / "data" / "denied_claims.json"


# Test 1: Postgres is running and reachable.
def test_database_connects():
    # Open a connection; the with block closes it afterwards.
    with get_engine().connect() as conn:
        # Run "select 1" and check the single value that comes back is 1.
        assert conn.execute(text("select 1")).scalar() == 1


# Test 2: the sample data file exists and has the expected shape.
def test_sample_claims_load():
    # Read the file and parse it into a list of dicts.
    claims = json.loads(DATA.read_text())
    # There should be more than 200 records (248 including duplicates).
    assert len(claims) > 200
    # The first record has at least these keys (<= on sets means "is a subset of").
    assert {"claim_id", "lines", "adjustments"} <= claims[0].keys()


# Test 3: the never-failing model returns valid JSON with the right answer.
def test_reliable_model_returns_valid_json():
    # Ask the reliable model about a CO-50 denial.
    reply = MockModel.reliable().complete("Denied claim with adjustment CO-50")
    # The reply text must parse as JSON.
    body = json.loads(reply.text)
    # The category must be one of the real ones.
    assert body["category"] in VALID_CATEGORIES
    # CO-50 (medical necessity) should map to "appeal".
    assert body["action"] == "appeal"
