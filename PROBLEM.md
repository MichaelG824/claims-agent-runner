# Denial triage runner

You work on the provider side: a hospital's revenue-cycle team.
Every night the hospital receives remittances from insurers, and a few hundred claims come back denied.
Today a billing specialist works each denial by hand.

Build a service that runs every denied claim in `data/denied_claims.json` through the model and records, for each claim:

- the denial category and recommended action, from the taxonomy in `app/mock_model.py`
- the model's confidence and rationale
- an appeal draft when the action is `appeal`

Results must be stored in Postgres so the billing team can work the queue the next morning.

You will not finish.
It is open-ended on purpose.
Get a thin slice working end to end first, then harden it.

## Constraints

- Use `MockModel` exactly as provided; do not change its failure rates to make your life easier.
- A billing specialist must be able to see why a claim got its result.
- Treat money as money.

## Things worth asking before you start

Write down the answers you assume.
There is no interviewer here, so make the call yourself, label it as an assumption, and move on.
