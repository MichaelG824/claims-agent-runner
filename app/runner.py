"""Nightly entry point: ingest the remittance file, then triage pending claims.

    python -m app.runner [--data PATH] [--workers N] [--seed N] [--limit N]

Re-running is safe: ingest skips known claims and triage only picks up claims
without a result, so a crashed run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.engine import Engine

from app.db import get_engine
from app.domain import Claim
from app.llm import RateLimiter
from app.mock_model import MockModel
from app.store import IngestReport, create_schema, ingest, pending_claims, save_result
from app.triage import Policy, Tier, Triager, TriageResult

log = logging.getLogger("runner")

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "denied_claims.json"

# Pace a little under each model's published limit to leave headroom.
SMALL_RATE_PER_SEC = 18
LARGE_RATE_PER_SEC = 4


@dataclass(frozen=True)
class Rejected:
    index: int
    claim_id: str | None
    error: str


@dataclass
class RunSummary:
    ingest: IngestReport | None = None
    rejected: list[Rejected] = field(default_factory=list)
    processed: int = 0
    already_saved: int = 0
    failed: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal = Decimal("0")
    seconds: float = 0.0


def load_records(path: Path) -> tuple[list[tuple[Claim, dict[str, Any]]], list[Rejected]]:
    """Parse every record; a bad record is reported, never fatal to the batch."""
    valid: list[tuple[Claim, dict[str, Any]]] = []
    rejected: list[Rejected] = []
    for index, raw in enumerate(json.loads(path.read_text())):
        try:
            valid.append((Claim.model_validate(raw), raw))
        except ValidationError as exc:
            claim_id = raw.get("claim_id") if isinstance(raw, dict) else None
            rejected.append(Rejected(index, claim_id, f"{exc.error_count()} validation error(s)"))
    return valid, rejected


def default_tiers(seed: int | None) -> list[Tier]:
    """Cheap model first, the 10x model only for claims the cheap one can't settle."""
    large_seed = None if seed is None else seed + 1
    return [
        Tier(MockModel("mock-small", seed=seed), RateLimiter(SMALL_RATE_PER_SEC)),
        Tier(MockModel("mock-large", seed=large_seed), RateLimiter(LARGE_RATE_PER_SEC)),
    ]


def run(
    engine: Engine,
    triager: Triager,
    *,
    data: Path = DEFAULT_DATA,
    workers: int = 8,
    limit: int | None = None,
) -> RunSummary:
    started = time.monotonic()
    summary = RunSummary()

    create_schema(engine)
    records, summary.rejected = load_records(data)
    summary.ingest = ingest(engine, records)

    todo = pending_claims(engine)[:limit]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(triager.triage, claim): claim.claim_id for claim in todo}
        for future in as_completed(futures):
            claim_id = futures[future]
            try:
                result = future.result()
                saved = save_result(engine, result)
            except Exception:
                # One bad claim must not sink the batch. It has no result row,
                # so the next run picks it up again.
                log.exception("triage failed for %s", claim_id)
                summary.failed += 1
                continue
            _record(summary, result, saved)

    summary.seconds = round(time.monotonic() - started, 2)
    return summary


def _record(summary: RunSummary, result: TriageResult, saved: bool) -> None:
    if not saved:
        summary.already_saved += 1
        return
    summary.processed += 1
    _bump(summary.by_status, result.status.value)
    _bump(summary.by_model, result.model or "none")
    summary.cost_usd += result.cost_usd


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed the mock models; fully reproducible only with --workers 1",
    )
    parser.add_argument("--limit", type=int, default=None, help="triage at most N pending claims")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    triager = Triager(default_tiers(args.seed), Policy())
    summary = run(get_engine(), triager, data=args.data, workers=args.workers, limit=args.limit)
    print(json.dumps(asdict(summary), indent=2, default=str))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
