"""End-to-end runs against Postgres."""

import json
from decimal import Decimal

from sqlalchemy import func, select, text

from app import runner
from app.llm import RateLimiter
from app.mock_model import MockModel
from app.runner import DEFAULT_DATA, run
from app.store import claim_adjustments, claims, model_attempts, save_result, triage_results
from app.triage import Policy, Tier, Triager
from tests.conftest import ScriptedModel, make_claim, make_raw, reply_json


def reliable_triager() -> Triager:
    return Triager([Tier(MockModel.reliable(), RateLimiter(1_000_000))], Policy(), sleep=lambda _: None)


def write_data(tmp_path, records) -> object:
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(records))
    return path


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_full_file_every_unique_claim_gets_exactly_one_result(engine):
    summary = run(engine, reliable_triager(), data=DEFAULT_DATA)

    unique = len({c["claim_id"] for c in json.loads(DEFAULT_DATA.read_text())})
    assert summary.ingest.duplicates_in_file == 8
    assert summary.ingest.conflicts == ()
    assert count(engine, claims) == unique
    assert count(engine, triage_results) == unique
    assert summary.failed == 0
    # The two remittances with no reason code are routed to a human.
    assert summary.by_status["human_review"] == 2


def test_rerun_is_idempotent_and_spends_nothing(engine):
    run(engine, reliable_triager(), data=DEFAULT_DATA)
    attempts_before = count(engine, model_attempts)

    second = run(engine, reliable_triager(), data=DEFAULT_DATA)

    assert second.ingest.inserted == 0
    assert second.processed == 0
    assert second.cost_usd == 0
    assert count(engine, model_attempts) == attempts_before


def test_resume_only_processes_claims_without_results(engine, tmp_path):
    data = write_data(tmp_path, [make_raw(f"CLM-{i}") for i in range(5)])
    first = run(engine, reliable_triager(), data=data, limit=2)
    second = run(engine, reliable_triager(), data=data)
    assert (first.processed, second.processed) == (2, 3)
    assert count(engine, triage_results) == 5


def test_conflicting_duplicate_is_reported_not_overwritten(engine, tmp_path):
    original = make_raw("CLM-1", codes=[("CO-50", "100.00")])
    changed = make_raw("CLM-1", codes=[("CO-45", "100.00")])
    summary = run(engine, reliable_triager(), data=write_data(tmp_path, [original, changed]))
    assert summary.ingest.conflicts == ("CLM-1",)
    with engine.connect() as conn:
        codes = conn.execute(select(claim_adjustments.c.code)).scalars().all()
    assert codes == ["CO-50"]


def test_invalid_record_is_rejected_without_failing_the_batch(engine, tmp_path):
    bad = make_raw("CLM-BAD")
    bad["lines"][0]["billed"] = 12.5
    summary = run(engine, reliable_triager(), data=write_data(tmp_path, [make_raw("CLM-OK"), bad]))
    assert [r.claim_id for r in summary.rejected] == ["CLM-BAD"]
    assert summary.processed == 1


def test_every_result_can_be_explained_from_the_database(engine, tmp_path):
    small = ScriptedModel([reply_json("duplicate", "appeal"), reply_json("medical_necessity", "appeal")])
    triager = Triager([Tier(small, RateLimiter(1_000_000))], Policy(), sleep=lambda _: None)
    run(engine, triager, data=write_data(tmp_path, [make_raw("CLM-1")]))

    with engine.connect() as conn:
        result = conn.execute(select(triage_results)).one()
        attempts = conn.execute(select(model_attempts).order_by(model_attempts.c.attempt_no)).all()
    assert (result.status, result.category, result.attempt_count) == ("done", "medical_necessity", 2)
    assert [a.outcome for a in attempts] == ["mismatch", "ok"]
    assert "model said duplicate/appeal" in attempts[0].detail
    assert "Primary denial: CO-50" in result.prompt
    assert attempts[0].raw_response is not None
    assert result.cost_usd == sum((a.cost_usd for a in attempts), Decimal("0"))


def test_money_is_stored_as_numeric(engine):
    with engine.connect() as conn:
        types = dict(
            conn.execute(
                text(
                    "select table_name || '.' || column_name, data_type "
                    "from information_schema.columns where table_schema = current_schema() "
                    "and column_name in ('amount', 'total_billed', 'cost_usd')"
                )
            ).all()
        )
    assert types and set(types.values()) == {"numeric"}


def test_saving_twice_keeps_one_result_and_one_audit_trail(engine, tmp_path):
    run(engine, reliable_triager(), data=write_data(tmp_path, [make_raw("CLM-1")]), limit=0)
    result = reliable_triager().triage(make_claim("CLM-1"))
    assert save_result(engine, result) is True
    assert save_result(engine, result) is False
    assert count(engine, model_attempts) == len(result.attempts)


def test_cli_prints_a_json_summary(engine, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "get_engine", lambda: engine)
    monkeypatch.setattr(
        runner, "default_tiers", lambda seed: [Tier(MockModel.reliable(), RateLimiter(1_000_000))]
    )
    data = write_data(tmp_path, [make_raw("CLM-1"), make_raw("CLM-2", codes=[])])

    assert runner.main(["--data", str(data)]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["processed"] == 2
    assert summary["by_status"] == {"done": 1, "human_review": 1}
