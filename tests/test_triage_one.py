import json

import pytest

from app import triage_one


def test_prints_the_result_and_every_attempt(capsys):
    assert triage_one.main(["--claim-id", "CLM-00038", "--seed", "1"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["claim_id"] == "CLM-00038"
    assert report["status"] == "done"
    assert (report["category"], report["action"]) == ("bundling", "review_coding")
    # Seed 1 makes the first reply fail validation, which is why the README uses it.
    assert [a["outcome"] for a in report["attempts"]] == ["wrong_schema", "ok"]


def test_unknown_claim_id_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        triage_one.main(["--claim-id", "CLM-NOPE"])

    assert exc.value.code == 2
    assert "no valid claim with id CLM-NOPE" in capsys.readouterr().err
