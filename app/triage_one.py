"""Triage a single claim without touching the database, for debugging.

    python -m app.triage_one [--claim-id ID] [--seed N] [--pause]

Prints the result and every model attempt. With --pause it stops in the
debugger right before triage, so you can step into it (F11 in VS Code).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.runner import DEFAULT_DATA, default_tiers, load_records
from app.triage import Policy, Triager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--claim-id", default=None, help="defaults to the first valid claim in the file")
    parser.add_argument("--seed", type=int, default=1, help="seed the mock models (default 1)")
    parser.add_argument("--pause", action="store_true", help="stop in the debugger before triage")
    args = parser.parse_args(argv)

    claims = [claim for claim, _ in load_records(args.data)[0]]
    matches = [claim for claim in claims if args.claim_id in (None, claim.claim_id)]
    if not matches:
        parser.error(f"no valid claim with id {args.claim_id}")
    claim = matches[0]

    triager = Triager(default_tiers(args.seed), Policy())
    if args.pause:
        breakpoint()  # Step into the next line to follow one claim end to end.
    result = triager.triage(claim)

    report = {
        "claim_id": result.claim_id,
        "status": result.status.value,
        "category": result.category,
        "action": result.action,
        "model": result.model,
        "review_reason": result.review_reason,
        "cost_usd": str(result.cost_usd),
        "attempts": [
            {"model": a.model, "outcome": a.outcome.value, "detail": a.detail} for a in result.attempts
        ],
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
