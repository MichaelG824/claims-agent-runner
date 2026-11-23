"""Regenerate data/denied_claims.json deterministically: python data/generate.py"""

import json
import random
from pathlib import Path

rng = random.Random(42)

PAYERS = ["Aetna", "BlueCross", "Cigna", "UnitedHealthcare", "Medicare", "Humana"]
CPT = ["99213", "99214", "71046", "80053", "93000", "36415", "45378", "27447", "70553"]
CODES = ["CO-16", "CO-50", "CO-97", "CO-29", "CO-197", "CO-45", "CO-18", "CO-22", "PR-1"]
FIRST = ["Ana", "Ben", "Chen", "Dana", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo"]
LAST = ["Lee", "Patel", "Garcia", "Kim", "Nguyen", "Smith", "Okafor", "Rossi"]


def money(lo: int, hi: int) -> str:
    return f"{rng.randint(lo * 100, hi * 100) / 100:.2f}"


claims = []
for i in range(1, 241):
    lines = []
    for n in range(1, rng.randint(1, 4) + 1):
        billed = money(80, 4000)
        allowed = f"{float(billed) * rng.uniform(0.3, 0.9):.2f}"
        lines.append({"line_no": n, "cpt": rng.choice(CPT), "billed": billed, "allowed": allowed})
    adjustments = [{"code": rng.choice(CODES), "amount": lines[0]["allowed"]}]
    if rng.random() < 0.15:
        adjustments.append({"code": rng.choice(CODES), "amount": money(10, 200)})
    if rng.random() < 0.04:
        adjustments = []  # remittance with no reason code
    claims.append(
        {
            "claim_id": f"CLM-{i:05d}",
            "payer": rng.choice(PAYERS),
            "patient": {
                "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                "dob": f"19{rng.randint(40, 99)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                "member_id": f"M{rng.randint(10_000_000, 99_999_999)}",
            },
            "service_date": f"2026-{rng.randint(6, 10):02d}-{rng.randint(1, 28):02d}",
            "lines": lines,
            "adjustments": adjustments,
            "clinical_note": "Patient presented with symptoms; see chart for details.",
        }
    )

# The same remittance sometimes arrives twice.
claims.extend(rng.sample(claims, 8))
rng.shuffle(claims)

Path(__file__).with_name("denied_claims.json").write_text(json.dumps(claims, indent=2) + "\n")
print(f"wrote {len(claims)} records")
