"""Turn raw model text into a trusted reply, or say exactly why not."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from app.domain import ModelReply
from app.mock_model import VALID_ACTIONS, VALID_CATEGORIES
from app.rules import Expectation

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class Outcome(StrEnum):
    OK = "ok"
    MALFORMED = "malformed"
    WRONG_SCHEMA = "wrong_schema"
    HALLUCINATED = "hallucinated"
    MISMATCH = "mismatch"
    BAD_APPEAL_DRAFT = "bad_appeal_draft"
    # Transport failures; recorded by the client, never produced here.
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reply: ModelReply | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.OK


def extract_json(text: str) -> object:
    """Parse JSON that may be wrapped in a code fence or surrounded by prose.

    Raises ValueError when no complete JSON object can be recovered, e.g. a
    truncated reply. Recovery is safe because the result is still validated.
    """
    candidates = [text.strip()]
    if fenced := _FENCE_RE.search(text):
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON object in reply")


def validate_reply(text: str, expected: Expectation) -> Verdict:
    try:
        data = extract_json(text)
    except ValueError as exc:
        return Verdict(Outcome.MALFORMED, detail=str(exc))

    try:
        reply = ModelReply.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in exc.errors())
        return Verdict(Outcome.WRONG_SCHEMA, detail=f"invalid fields: {fields}")

    if reply.category not in VALID_CATEGORIES or reply.action not in VALID_ACTIONS:
        return Verdict(Outcome.HALLUCINATED, reply, f"not in taxonomy: {reply.category}/{reply.action}")

    # A valid-looking answer that disagrees with the CARC lookup is the
    # dangerous case: it would pass every check above.
    if (reply.category, reply.action) != (expected.category, expected.action):
        return Verdict(
            Outcome.MISMATCH,
            reply,
            f"model said {reply.category}/{reply.action}, "
            f"{expected.primary.code if expected.primary else '?'} means "
            f"{expected.category}/{expected.action}",
        )

    has_draft = bool(reply.appeal_draft and reply.appeal_draft.strip())
    if has_draft != (reply.action == "appeal"):
        return Verdict(
            Outcome.BAD_APPEAL_DRAFT, reply, "appeal_draft must be present exactly when action is appeal"
        )

    return Verdict(Outcome.OK, reply)
