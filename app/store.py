"""Postgres schema and persistence.

Money columns are NUMERIC. Results are written in one transaction per claim
together with every model attempt, so a result never exists without its audit
trail and a re-run never duplicates either.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine

from app.domain import Claim
from app.triage import Source, Status, TriageResult

metadata = MetaData()

claims = Table(
    "claims",
    metadata,
    Column("claim_id", Text, primary_key=True),
    Column("payer", Text, nullable=False),
    Column("service_date", Date, nullable=False),
    Column("total_billed", Numeric(12, 2), nullable=False),
    # The record exactly as received, for audit and for reprocessing.
    Column("raw", JSONB, nullable=False),
    Column("received_count", Integer, nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

claim_adjustments = Table(
    "claim_adjustments",
    metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("claim_id", Text, ForeignKey("claims.claim_id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("code", Text, nullable=False),
    Column("amount", Numeric(12, 2), nullable=False),
    UniqueConstraint("claim_id", "position"),
)

triage_results = Table(
    "triage_results",
    metadata,
    Column("claim_id", Text, ForeignKey("claims.claim_id"), primary_key=True),
    Column("status", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("primary_code", Text),
    Column("category", Text),
    Column("action", Text),
    Column("confidence", Numeric(3, 2)),
    Column("rationale", Text),
    Column("appeal_draft", Text),
    Column("model", Text),
    Column("review_reason", Text),
    Column("prompt", Text),
    Column("attempt_count", Integer, nullable=False),
    Column("cost_usd", Numeric(12, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(f"status in {tuple(s.value for s in Status)}", name="status_valid"),
    CheckConstraint(f"source in {tuple(s.value for s in Source)}", name="source_valid"),
    # Every human_review row must say why; the specialist relies on it.
    CheckConstraint("status <> 'human_review' or review_reason is not null", name="review_has_reason"),
)

model_attempts = Table(
    "model_attempts",
    metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("claim_id", Text, ForeignKey("claims.claim_id"), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("model", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("detail", Text, nullable=False),
    Column("raw_response", Text),
    Column("latency_ms", Integer),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(12, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("claim_id", "attempt_no"),
)


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)


@dataclass(frozen=True)
class IngestReport:
    records: int
    unique: int
    inserted: int
    duplicates_in_file: int
    # Same claim_id, different content: kept the stored version, needs a human.
    conflicts: tuple[str, ...]


def ingest(engine: Engine, records: Iterable[tuple[Claim, dict[str, Any]]]) -> IngestReport:
    """Insert claims once. Safe to run repeatedly over the same file."""
    by_id: dict[str, tuple[Claim, dict[str, Any], int]] = {}
    conflicts: set[str] = set()
    total = 0
    for claim, raw in records:
        total += 1
        if existing := by_id.get(claim.claim_id):
            if existing[1] != raw:
                conflicts.add(claim.claim_id)
            by_id[claim.claim_id] = (existing[0], existing[1], existing[2] + 1)
        else:
            by_id[claim.claim_id] = (claim, raw, 1)

    inserted = 0
    with engine.begin() as conn:
        for claim, raw, count in by_id.values():
            row = conn.execute(
                insert(claims)
                .values(
                    claim_id=claim.claim_id,
                    payer=claim.payer,
                    service_date=claim.service_date,
                    total_billed=claim.total_billed,
                    raw=raw,
                    received_count=count,
                )
                .on_conflict_do_nothing(index_elements=["claim_id"])
                .returning(claims.c.claim_id)
            ).first()
            if row is None:
                stored = conn.execute(
                    select(claims.c.raw).where(claims.c.claim_id == claim.claim_id)
                ).scalar_one()
                if _canonical(stored) != _canonical(raw):
                    conflicts.add(claim.claim_id)
                continue
            inserted += 1
            if claim.adjustments:
                conn.execute(
                    claim_adjustments.insert(),
                    [
                        {"claim_id": claim.claim_id, "position": i, "code": a.code, "amount": a.amount}
                        for i, a in enumerate(claim.adjustments)
                    ],
                )

    return IngestReport(
        records=total,
        unique=len(by_id),
        inserted=inserted,
        duplicates_in_file=total - len(by_id),
        conflicts=tuple(sorted(conflicts)),
    )


def pending_claims(engine: Engine) -> list[Claim]:
    """Claims with no result yet. Results are terminal, so this is the resume point."""
    query = (
        select(claims.c.raw)
        .outerjoin(triage_results, triage_results.c.claim_id == claims.c.claim_id)
        .where(triage_results.c.claim_id.is_(None))
        .order_by(claims.c.claim_id)
    )
    with engine.connect() as conn:
        return [Claim.model_validate(raw) for raw in conn.execute(query).scalars()]


def save_result(engine: Engine, result: TriageResult) -> bool:
    """Write the result and its attempts atomically. False if already saved."""
    with engine.begin() as conn:
        written = conn.execute(
            insert(triage_results)
            .values(
                claim_id=result.claim_id,
                status=result.status.value,
                source=result.source.value,
                primary_code=result.primary_code,
                category=result.category,
                action=result.action,
                confidence=result.confidence,
                rationale=result.rationale,
                appeal_draft=result.appeal_draft,
                model=result.model,
                review_reason=result.review_reason,
                prompt=result.prompt,
                attempt_count=len(result.attempts),
                cost_usd=result.cost_usd,
            )
            .on_conflict_do_nothing(index_elements=["claim_id"])
            .returning(triage_results.c.claim_id)
        ).first()
        if written is None:
            return False
        _insert_attempts(conn, result)
    return True


def _insert_attempts(conn: Connection, result: TriageResult) -> None:
    if not result.attempts:
        return
    conn.execute(
        model_attempts.insert(),
        [
            {
                "claim_id": result.claim_id,
                "attempt_no": n,
                "model": a.model,
                "outcome": a.outcome.value,
                "detail": a.detail,
                "raw_response": a.raw_response,
                "latency_ms": a.latency_ms,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "cost_usd": a.cost_usd,
            }
            for n, a in enumerate(result.attempts, start=1)
        ],
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True)
