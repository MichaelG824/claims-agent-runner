# claims-agent-runner (practice)

A practice repo shaped like a take-home AI engineering interview: Python, Postgres, a mock model, and three green tests.
There is no solution here on purpose.

## Setup

```sh
make db-up        # Postgres 16 on localhost:5433 (5432 is left free for the real interview repo)
make bootstrap    # venv + deps
make test         # should print: 3 passed
```

Override the database with `DATABASE_URL="postgresql+psycopg://user:pass@host:port/db"`.

## What is here

- `app/mock_model.py` - a fake LLM that is slow, rate limited, sometimes fails, and sometimes lies.
  Read it the way you would skim theirs.
- `app/db.py` - a SQLAlchemy engine and nothing else.
- `data/denied_claims.json` - 248 denied-claim remittance records (regenerate with `python data/generate.py`).
- `PROBLEM.md` - the task.
  Open it when you start the clock.
- `FOLLOWUPS.md` - interviewer curveballs, kept locally and not committed.
  Do not open it until the times it names.

## How to run the session

1. Start a 2-hour timer and screen-record yourself with your AI panel visible.
2. Read `PROBLEM.md`, then spend about 5 minutes clarifying out loud before any code.
3. Work in small AI-driven steps, read every diff, and keep `make test` green.
4. Open each `FOLLOWUPS.md` section at its time mark and adapt without losing your structure.
5. Afterwards, rewatch the first 15 minutes and the first follow-up.
