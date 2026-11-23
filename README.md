# claims-agent-runner (practice)

A practice repo shaped like a take-home AI engineering interview: Python, Postgres, a mock model, and a denied-claims file.
The `main` branch is the untouched scaffold.
The `feat/triage-pipeline` branch is a reference solution, written to show good practice rather than to fit in two hours.

## Setup

```sh
make db-up        # Postgres 16 on localhost:5433 (5432 is left free for the real interview repo)
make bootstrap    # venv + deps
make check        # ruff, mypy --strict, and the test suite
make run          # triage every claim; ARGS="--seed 7 --workers 1" for a reproducible run
```

Override the database with `DATABASE_URL="postgresql+psycopg://user:pass@host:port/db"`.

## What is here

- `app/mock_model.py` - a fake LLM that is slow, rate limited, sometimes fails, and sometimes lies.
  Read it the way you would skim theirs.
- `app/db.py` - a SQLAlchemy engine.
- `data/denied_claims.json` - 248 denied-claim remittance records (regenerate with `python data/generate.py`).
- `PROBLEM.md` - the task.
  Open it when you start the clock.
- `FOLLOWUPS.md` - interviewer curveballs, kept locally and not committed.
  Do not open it until the times it names.

## How the solution works

```
denied_claims.json
  -> parse (Pydantic, Decimal money)      app/domain.py
  -> dedupe + insert once                  app/store.py   ingest()
  -> CARC lookup = expected answer         app/rules.py
  -> PHI-free prompt                       app/prompt.py
  -> mock-small, then mock-large           app/triage.py  (paced by app/llm.py RateLimiter)
  -> validate + cross-check vs lookup      app/validate.py
  -> result + every attempt, one txn       app/store.py   save_result()
```

| Module | Responsibility |
| --- | --- |
| `app/domain.py` | Input and output shapes; refuses float money and malformed model replies |
| `app/rules.py` | Picks the primary adjustment and looks up the expected category and action |
| `app/prompt.py` | Builds the prompt without the patient block |
| `app/validate.py` | Recovers JSON from fences or prose, validates it, and names every rejection |
| `app/llm.py` | Client-side rate limiter and the attempt record used for the audit trail |
| `app/triage.py` | Retry, backoff and escalation policy for one claim; no database access |
| `app/store.py` | Schema, idempotent ingest, and atomic result writes |
| `app/runner.py` | CLI: ingest, then triage pending claims on a thread pool |

### Tables

- `claims` - one row per claim, with the raw record as JSONB and how many times it arrived.
- `claim_adjustments` - every CARC code and amount, as `NUMERIC(12,2)`.
- `triage_results` - exactly one row per claim: status, category, action, confidence, rationale, appeal draft, model, review reason, the prompt sent, and cost.
- `model_attempts` - every model call: outcome, raw reply, why it was accepted or rejected, latency, tokens and cost.

To see why a claim got its result, read its `triage_results` row and then its `model_attempts` rows in order.

### Assumptions

These are calls the task leaves open; each one is a single place to change.

1. The primary denial is the known adjustment with the largest amount; ties keep remittance order.
2. The CARC lookup is the source of truth for category and action.
   The model must agree with it, and its value is the rationale and the appeal draft.
3. A claim with no reason code, or only unknown codes, goes straight to `human_review` without a model call.
4. When no model reply passes validation, the claim goes to `human_review` with the lookup's category and action as a suggestion and a reason listing every failure.
5. Patient name, date of birth and member ID never go to the model.
   The clinical note does, because an appeal needs it.
6. A duplicate remittance with identical content is counted, not reprocessed.
   One with different content is reported as a conflict and the first version is kept.
7. A result is terminal.
   Re-running only processes claims without one, so a crashed run resumes and a finished run costs nothing.
8. Model confidence is stored because the task asks for it, but nothing relies on it: the mock's confidence is random.

## Good practice in this solution, and what it avoids

| Do | Instead of |
| --- | --- |
| Parse once at the edge into typed objects (`Claim`, `ModelReply`) | Passing raw dicts around and re-checking fields everywhere |
| `Decimal` in code and `NUMERIC` in Postgres; floats rejected at parse time | `float(amount)`, which turns 0.1 + 0.2 into 0.30000000000000004 |
| Check the model against a deterministic answer | Trusting any reply that parses, which lets 10% silent misclassifications through |
| Name every rejection (`malformed`, `mismatch`, ...) and store it | `except Exception: pass`, or a bare boolean "valid" |
| Retry transient errors with capped, jittered backoff; honour `retry_after` | Retrying in a tight loop, or not retrying at all |
| Pace requests below the limit with a shared limiter | Firing as fast as possible and living on 429s |
| Cheap model first, expensive model only on failure | Always using the large model, or never escalating |
| Route what can't be settled to a human with a reason | Guessing, or dropping the claim |
| Write the result and its attempts in one transaction with `ON CONFLICT DO NOTHING` | Separate writes that can leave a result without its audit trail, or duplicates on re-run |
| Keep `triage.py` free of I/O and inject `sleep`, clocks and models | Tests that need the network, real sleeps, or luck |
| Test the real mock's failure profile across seeds and assert no wrong answer is ever accepted | Testing only the happy path with the reliable model |
| One failed claim is logged and retried next run | One exception aborting the whole batch |

## Not done yet

- No locking between two runners started at once; both could pay for the same claim, though only one result is saved.
- Attempts for a claim that crashes mid-triage are not saved, so that spend is not recorded.
- Schema is created with `create_all`; a real deployment would use migrations.
- No way yet to re-triage a `human_review` claim after a fix.

## How to run the session

1. Start a 2-hour timer and screen-record yourself with your AI panel visible.
2. Read `PROBLEM.md`, then spend about 5 minutes clarifying out loud before any code.
3. Work in small AI-driven steps, read every diff, and keep `make test` green.
4. Open each `FOLLOWUPS.md` section at its time mark and adapt without losing your structure.
5. Afterwards, rewatch the first 15 minutes and the first follow-up.
