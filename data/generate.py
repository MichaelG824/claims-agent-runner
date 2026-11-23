"""Regenerate data/denied_claims.json deterministically: python data/generate.py"""

# json: write the output file.
import json
# random: generate fake data.
import random
# Path: locate the output file next to this script.
from pathlib import Path

# Seeded RNG: the same seed (42) always produces the same file.
rng = random.Random(42)

# Insurance companies to choose from.
PAYERS = ["Aetna", "BlueCross", "Cigna", "UnitedHealthcare", "Medicare", "Humana"]
# CPT procedure codes (office visits, chest X-ray, lab panel, ECG, blood draw, colonoscopy, knee replacement, brain MRI).
CPT = ["99213", "99214", "71046", "80053", "93000", "36415", "45378", "27447", "70553"]
# Denial reason codes; exactly the ones in DENIAL_TAXONOMY.
CODES = ["CO-16", "CO-50", "CO-97", "CO-29", "CO-197", "CO-45", "CO-18", "CO-22", "PR-1"]
# Fake first names.
FIRST = ["Ana", "Ben", "Chen", "Dana", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo"]
# Fake last names.
LAST = ["Lee", "Patel", "Garcia", "Kim", "Nguyen", "Smith", "Okafor", "Rossi"]


# Random dollar amount between lo and hi, as a string with 2 decimals (e.g. "1123.91").
def money(lo: int, hi: int) -> str:
    # Pick whole cents as an integer, convert to dollars, format to 2 decimals.
    return f"{rng.randint(lo * 100, hi * 100) / 100:.2f}"


# All generated claims.
claims = []
# 240 unique claims, numbered 1 to 240.
for i in range(1, 241):
    # Service lines (individual billed items) for this claim.
    lines = []
    # Between 1 and 4 lines, numbered from 1.
    for n in range(1, rng.randint(1, 4) + 1):
        # What the hospital charged: $80 to $4,000.
        billed = money(80, 4000)
        # What the insurer allows: 30-90% of billed. Note: computed with float, not Decimal.
        allowed = f"{float(billed) * rng.uniform(0.3, 0.9):.2f}"
        # Add the line with a random procedure code.
        lines.append({"line_no": n, "cpt": rng.choice(CPT), "billed": billed, "allowed": allowed})
    # One denial code, for the full allowed amount of line 1.
    adjustments = [{"code": rng.choice(CODES), "amount": lines[0]["allowed"]}]
    # 15% of claims get a second denial code with a small amount.
    if rng.random() < 0.15:
        # Second adjustment: random code, $10 to $200.
        adjustments.append({"code": rng.choice(CODES), "amount": money(10, 200)})
    # 4% of claims get no denial code at all.
    if rng.random() < 0.04:
        adjustments = []  # remittance with no reason code
    # Build the claim record and add it.
    claims.append(
        {
            # Zero-padded id, e.g. CLM-00038.
            "claim_id": f"CLM-{i:05d}",
            # Random insurer.
            "payer": rng.choice(PAYERS),
            # Patient details (this is the PHI that should stay out of prompts).
            "patient": {
                # Random full name.
                "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                # Birth date between 1940 and 1999; day capped at 28 so every month is valid.
                "dob": f"19{rng.randint(40, 99)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                # 8-digit member id with an M prefix.
                "member_id": f"M{rng.randint(10_000_000, 99_999_999)}",
            },
            # Service date between June and October 2026.
            "service_date": f"2026-{rng.randint(6, 10):02d}-{rng.randint(1, 28):02d}",
            # The service lines built above.
            "lines": lines,
            # The denial codes built above.
            "adjustments": adjustments,
            # The same placeholder note for every claim, so appeal drafts have no real evidence.
            "clinical_note": "Patient presented with symptoms; see chart for details.",
        }
    )

# The same remittance sometimes arrives twice.
# Append 8 random claims a second time (the 8 duplicate claim_ids).
claims.extend(rng.sample(claims, 8))
# Shuffle so the duplicates are not all at the end.
rng.shuffle(claims)

# Write denied_claims.json next to this script, pretty-printed, with a trailing newline.
Path(__file__).with_name("denied_claims.json").write_text(json.dumps(claims, indent=2) + "\n")
# Report how many records were written (248).
print(f"wrote {len(claims)} records")
