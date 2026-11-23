"""A fake LLM that behaves like a real one under load.

It is slow, has a per-second rate limit, sometimes times out or 500s, and
sometimes returns output you cannot trust: prose around the JSON, truncated
JSON, the wrong schema, or a confident answer with a made-up category.

Two models are available so model routing is a real trade-off:

    mock-small  cheap, fast, less accurate, flakier output
    mock-large  ~10x the price, slower, more accurate

Everything is driven by a seeded RNG so a run is reproducible.
"""

# Lets type hints refer to classes before they are defined (e.g. "MockModel" inside MockModel).
from __future__ import annotations

# json: builds the model's reply text.
import json
# random: the seeded random number generator that decides every failure.
import random
# re: regular expressions, used to find denial codes like "CO-50" in the prompt.
import re
# threading: a lock so several threads can call the model at once safely.
import threading
# time: real sleeping (latency) and a monotonic clock for the rate limiter.
import time
# deque: a fast double-ended queue, used as a sliding window of recent call times.
from collections import deque
# dataclass: auto-generates __init__ etc.; field: lets a dataclass field default to a fresh dict.
from dataclasses import dataclass, field


# Base exception for every model failure, so callers can catch them all with one except.
class ModelError(Exception):
    """Base class for every failure the model can raise."""


# Raised when more calls arrive in one second than the model's limit allows (like HTTP 429).
class RateLimitError(ModelError):
    # retry_after: how many seconds the caller should wait before trying again.
    def __init__(self, retry_after: float):
        # Build a readable message, e.g. "rate limited, retry after 0.42s".
        super().__init__(f"rate limited, retry after {retry_after:.2f}s")
        # Keep the number on the exception so a retry loop can sleep exactly that long.
        self.retry_after = retry_after


# Raised when the model takes longer than the caller's timeout.
class ModelTimeout(ModelError):
    # No extra behaviour; the class name alone tells the caller what happened.
    pass


# Raised to simulate the provider returning HTTP 500 (a server-side crash).
class ModelServerError(ModelError):
    # No extra behaviour; a transient error that is usually worth retrying.
    pass


# CARC group+code -> (category, recommended action). The model is asked to
# produce one of these categories; anything else is a hallucination.
DENIAL_TAXONOMY: dict[str, tuple[str, str]] = {
    # CO-16: claim is missing information -> fix it and send it again.
    "CO-16": ("missing_information", "correct_and_resubmit"),
    # CO-50: insurer says the service was not medically necessary -> appeal.
    "CO-50": ("medical_necessity", "appeal"),
    # CO-97: service is included in another billed service -> check the codes.
    "CO-97": ("bundling", "review_coding"),
    # CO-29: claim was filed too late -> hospital's fault, write it off.
    "CO-29": ("timely_filing", "write_off"),
    # CO-197: no prior authorization on file -> appeal.
    "CO-197": ("authorization", "appeal"),
    # CO-45: amount above the contracted rate -> expected, nothing to do.
    "CO-45": ("contractual_adjustment", "no_action"),
    # CO-18: duplicate claim -> nothing to do.
    "CO-18": ("duplicate", "no_action"),
    # CO-22: another insurer should pay first -> bill the secondary insurer.
    "CO-22": ("coordination_of_benefits", "bill_secondary"),
    # PR-1: patient's deductible -> bill the patient.
    "PR-1": ("patient_responsibility", "bill_patient"),
}

# Every allowed category, de-duplicated with a set and sorted for stable output.
VALID_CATEGORIES = sorted({c for c, _ in DENIAL_TAXONOMY.values()})
# Every allowed action, plus "human_review" (used when the model cannot decide).
VALID_ACTIONS = sorted({a for _, a in DENIAL_TAXONOMY.values()} | {"human_review"})

# Price in US dollars per 1,000 tokens, split into input (prompt) and output (reply).
PRICING_PER_1K_TOKENS = {
    # Small model: cheap.
    "mock-small": {"input": 0.0005, "output": 0.0015},
    # Large model: 10x the small model's price.
    "mock-large": {"input": 0.005, "output": 0.015},
}


# frozen=True makes instances immutable, so a profile cannot be changed after creation.
@dataclass(frozen=True)
class ModelProfile:
    # Normal response time range in milliseconds: (fastest, slowest).
    latency_ms: tuple[int, int]
    slow_tail_rate: float  # chance a call takes far longer than usual
    # Chance a call times out even without the slow tail.
    timeout_rate: float
    # Chance a call fails with a simulated HTTP 500.
    server_error_rate: float
    malformed_rate: float  # not parseable as JSON
    wrong_schema_rate: float  # valid JSON, wrong shape
    hallucination_rate: float  # valid shape, invented category
    misclassify_rate: float  # valid category, but the wrong one
    # Maximum calls allowed in any one-second window.
    rate_limit_per_sec: int


# The two real-world-like behaviour profiles, keyed by model name.
PROFILES = {
    # Small model: fast but flaky (about a third of calls go wrong in some way).
    "mock-small": ModelProfile(
        # Replies in 40-250 ms normally.
        latency_ms=(40, 250),
        # 2% of calls are extremely slow.
        slow_tail_rate=0.02,
        # 2% time out.
        timeout_rate=0.02,
        # 4% return HTTP 500.
        server_error_rate=0.04,
        # 8% return text that is not valid JSON.
        malformed_rate=0.08,
        # 6% return JSON with the wrong fields.
        wrong_schema_rate=0.06,
        # 5% invent a category that does not exist.
        hallucination_rate=0.05,
        # 10% pick a real category, but the wrong one (the dangerous, silent failure).
        misclassify_rate=0.10,
        # At most 20 calls per second.
        rate_limit_per_sec=20,
    ),
    # Large model: slower and pricier, but far more accurate.
    "mock-large": ModelProfile(
        # Replies in 300-1200 ms normally.
        latency_ms=(300, 1200),
        # 3% of calls are extremely slow.
        slow_tail_rate=0.03,
        # 2% time out.
        timeout_rate=0.02,
        # 3% return HTTP 500.
        server_error_rate=0.03,
        # 2% malformed JSON.
        malformed_rate=0.02,
        # 2% wrong schema.
        wrong_schema_rate=0.02,
        # 1% hallucinated category.
        hallucination_rate=0.01,
        # 2% wrong category.
        misclassify_rate=0.02,
        # At most 5 calls per second.
        rate_limit_per_sec=5,
    ),
}

# A perfect profile for unit tests: instant, never fails, effectively no rate limit.
RELIABLE = ModelProfile(
    # Zero latency.
    latency_ms=(0, 0),
    # Never slow.
    slow_tail_rate=0,
    # Never times out.
    timeout_rate=0,
    # Never returns 500.
    server_error_rate=0,
    # Always valid JSON.
    malformed_rate=0,
    # Always the right shape.
    wrong_schema_rate=0,
    # Never invents a category.
    hallucination_rate=0,
    # Never picks the wrong category.
    misclassify_rate=0,
    # 10,000 calls per second, so tests never hit the limit.
    rate_limit_per_sec=10_000,
)


# What a successful call returns. Immutable so it can be shared safely.
@dataclass(frozen=True)
class ModelResponse:
    # The raw reply text (meant to be JSON, but may not be).
    text: str
    # Which model answered, e.g. "mock-small".
    model: str
    # Approximate prompt size in tokens.
    input_tokens: int
    # Approximate reply size in tokens.
    output_tokens: int
    # How long the call took.
    latency_ms: int

    # Computed on access like an attribute: response.cost_usd.
    @property
    def cost_usd(self) -> float:
        # Look up this model's prices.
        price = PRICING_PER_1K_TOKENS[self.model]
        # Cost = input tokens priced at the input rate + output tokens at the output rate.
        return (
            # Input cost.
            self.input_tokens / 1000 * price["input"]
            # Plus output cost.
            + self.output_tokens / 1000 * price["output"]
        )


# Running totals for one MockModel instance; mutable because it is updated after every call.
@dataclass
class ModelStats:
    # Total calls that got past the rate limiter.
    calls: int = 0
    # Calls rejected by the rate limiter.
    rate_limited: int = 0
    # Calls that timed out.
    timeouts: int = 0
    # Calls that returned HTTP 500.
    server_errors: int = 0
    # Total prompt tokens across successful calls.
    input_tokens: int = 0
    # Total reply tokens across successful calls.
    output_tokens: int = 0
    # Total spend in dollars.
    cost_usd: float = 0.0
    # Count per outcome, e.g. {"ok": 180, "malformed": 16}; default_factory gives each instance its own dict.
    by_outcome: dict[str, int] = field(default_factory=dict)


# Matches denial codes like CO-50, PR-1, OA-23, PI-204 as whole words.
_CODE_RE = re.compile(r"\b(CO|PR|OA|PI)-\d{1,3}\b")


# The fake model itself.
class MockModel:
    """Thread-safe fake model. Call `complete(prompt)`.

    The prompt should contain the claim's denial code(s), e.g. "CO-50".
    The reply is meant to be JSON shaped like:

        {"category": str, "action": str, "confidence": float,
         "rationale": str, "appeal_draft": str | null}
    """

    # Set up a model instance.
    def __init__(
        # The instance being created.
        self,
        # Which model to imitate; defaults to the cheap one.
        model: str = "mock-small",
        # The bare * makes everything after it keyword-only.
        *,
        # RNG seed; the same seed gives the same sequence of failures.
        seed: int | None = None,
        # Optional custom profile; overrides the one looked up by model name.
        profile: ModelProfile | None = None,
        # If False, latency is simulated without actually sleeping (fast tests).
        sleep: bool = True,
    ):
        # Reject unknown model names early with a helpful message.
        if model not in PROFILES:
            # List the valid names in the error.
            raise ValueError(f"unknown model {model!r}; choose from {sorted(PROFILES)}")
        # Remember the model name (used for pricing).
        self.model = model
        # Use the custom profile if given, otherwise the named model's profile.
        self.profile = profile or PROFILES[model]
        # Remember whether to really sleep.
        self.sleep = sleep
        # Fresh counters for this instance.
        self.stats = ModelStats()
        # Private seeded RNG, so runs are reproducible.
        self._rng = random.Random(seed)
        # Lock guarding the RNG, stats and rate-limit window across threads.
        self._lock = threading.Lock()
        # Timestamps of calls in the last second, oldest on the left.
        self._recent_calls: deque[float] = deque()

    # Alternate constructor: MockModel.reliable() instead of MockModel(...).
    @classmethod
    def reliable(cls, model: str = "mock-small") -> "MockModel":
        """A model that never fails and never sleeps. Handy for unit tests."""
        # Fixed seed, perfect profile, no sleeping.
        return cls(model, seed=0, profile=RELIABLE, sleep=False)

    # The main entry point: send a prompt, get a reply or an exception.
    def complete(self, prompt: str, *, timeout_s: float = 2.0) -> ModelResponse:
        # Do all the random decisions under the lock so threads don't interleave RNG draws.
        with self._lock:
            # Raise RateLimitError if this call would exceed the per-second limit.
            self._check_rate_limit()
            # Count the call.
            self.stats.calls += 1
            # Short alias for "give me a random float in [0, 1)".
            roll = self._rng.random
            # Short alias for the profile.
            p = self.profile
            # Unpack the normal latency range.
            lo, hi = p.latency_ms
            # Pick this call's latency within the normal range.
            latency_ms = self._rng.randint(lo, hi)
            # Occasionally make it a slow-tail call.
            if roll() < p.slow_tail_rate:
                # Slow tail: 1.2x to 3x the timeout, so it is guaranteed to time out.
                latency_ms = int(timeout_s * 1000 * self._rng.uniform(1.2, 3.0))
            # Times out if latency exceeds the timeout, or by plain random chance.
            timed_out = latency_ms > timeout_s * 1000 or roll() < p.timeout_rate
            # Decide now whether this call will 500.
            server_error = roll() < p.server_error_rate
            # Draw the number that will pick the output quality (ok, malformed, ...).
            outcome_roll = roll()
            # Alias for "pick a random item from a list".
            pick = self._rng.choice
            # Random confidence between 0.55 and 0.99; note it is unrelated to correctness.
            confidence = round(self._rng.uniform(0.55, 0.99), 2)

        # Timeout path (outside the lock so other threads are not blocked while sleeping).
        if timed_out:
            # The caller waits the full timeout before giving up, as with a real API.
            self._wait(timeout_s * 1000)
            # Update the counter under the lock.
            with self._lock:
                # Count the timeout.
                self.stats.timeouts += 1
            # Tell the caller it timed out.
            raise ModelTimeout(f"no response within {timeout_s}s")

        # Simulate the time the model takes to respond.
        self._wait(latency_ms)

        # Server error path: the call took time and still failed.
        if server_error:
            # Update the counter under the lock.
            with self._lock:
                # Count the 500.
                self.stats.server_errors += 1
            # Tell the caller the server failed.
            raise ModelServerError("upstream returned HTTP 500")

        # Build the reply text and record which kind of outcome it was.
        text, outcome = self._generate(prompt, outcome_roll, confidence, pick)
        # Package the reply.
        response = ModelResponse(
            # The (possibly broken) reply text.
            text=text,
            # Which model answered.
            model=self.model,
            # Rough token estimate: about 4 characters per token, at least 1.
            input_tokens=max(1, len(prompt) // 4),
            # Same estimate for the reply.
            output_tokens=max(1, len(text) // 4),
            # How long it took.
            latency_ms=latency_ms,
        )
        # Update usage stats under the lock.
        with self._lock:
            # Add prompt tokens.
            self.stats.input_tokens += response.input_tokens
            # Add reply tokens.
            self.stats.output_tokens += response.output_tokens
            # Add the cost.
            self.stats.cost_usd += response.cost_usd
            # Increment the counter for this outcome, starting from 0 if new.
            self.stats.by_outcome[outcome] = self.stats.by_outcome.get(outcome, 0) + 1
        # Hand the reply to the caller. Even "ok" here may be malformed or wrong.
        return response

    # internals

    # Sliding-window rate limiter: at most N calls in any one second. Called under the lock.
    def _check_rate_limit(self) -> None:
        # Current time from a clock that never jumps backwards.
        now = time.monotonic()
        # Drop timestamps older than one second from the left of the window.
        while self._recent_calls and now - self._recent_calls[0] >= 1.0:
            # Remove the oldest timestamp.
            self._recent_calls.popleft()
        # If the window is already full, reject this call.
        if len(self._recent_calls) >= self.profile.rate_limit_per_sec:
            # Count the rejection.
            self.stats.rate_limited += 1
            # Time until the oldest call leaves the window.
            retry_after = 1.0 - (now - self._recent_calls[0])
            # Raise with at least 10 ms of wait, never zero or negative.
            raise RateLimitError(retry_after=max(retry_after, 0.01))
        # Allowed: record this call's timestamp.
        self._recent_calls.append(now)

    # Sleep for the given milliseconds, unless sleeping is turned off.
    def _wait(self, ms: float) -> None:
        # Only sleep when enabled and there is something to wait for.
        if self.sleep and ms > 0:
            # time.sleep takes seconds.
            time.sleep(ms / 1000)

    # Build the reply text. Returns (text, outcome name).
    def _generate(self, prompt, outcome_roll, confidence, pick) -> tuple[str, str]:
        # Short alias for the profile.
        p = self.profile
        # Every denial code mentioned in the prompt, in order.
        codes = [m.group(0) for m in _CODE_RE.finditer(prompt)]
        # Only the codes the taxonomy knows about.
        known = [c for c in codes if c in DENIAL_TAXONOMY]

        # If at least one code is known...
        if known:
            # ...use the FIRST known code only; any later codes are ignored.
            category, action = DENIAL_TAXONOMY[known[0]]
        # No known code at all.
        else:
            # Fall back to a generic category and ask for a human.
            category, action = "missing_information", "human_review"
            # And cap confidence at 0.6.
            confidence = min(confidence, 0.6)

        # Ordered list of bad outcomes and their probabilities.
        thresholds = [
            # Not valid JSON.
            ("malformed", p.malformed_rate),
            # Valid JSON, wrong shape.
            ("wrong_schema", p.wrong_schema_rate),
            # Invented category.
            ("hallucination", p.hallucination_rate),
            # Real but wrong category.
            ("misclassified", p.misclassify_rate),
        ]
        # Assume success until a threshold says otherwise.
        outcome = "ok"
        # Running total of probabilities, so each outcome gets its own slice of [0, 1).
        acc = 0.0
        # Walk the slices in order.
        for name, rate in thresholds:
            # Extend the running total by this outcome's probability.
            acc += rate
            # If the roll lands in this slice...
            if outcome_roll < acc:
                # ...this is the outcome.
                outcome = name
                # Stop looking.
                break

        # Misclassified: swap in a different, valid category.
        if outcome == "misclassified":
            # Any real category except the correct one. The action is NOT changed, so they now disagree.
            category = pick([c for c in VALID_CATEGORIES if c != category])
        # Hallucination: swap in a category that does not exist.
        if outcome == "hallucination":
            # One of three plausible-sounding fake categories.
            category = pick(["clinical_review_pending", "payer_error", "experimental_service"])

        # The reply in the documented shape.
        body = {
            # Possibly wrong category.
            "category": category,
            # Recommended action from the taxonomy.
            "action": action,
            # Random confidence; not a reliable signal.
            "confidence": confidence,
            # Template rationale naming the codes found.
            "rationale": f"Denial code(s) {', '.join(codes) or 'none found'} indicate {category}.",
            # Appeal letter only when the action is appeal.
            "appeal_draft": (
                # Generic boilerplate draft.
                f"To whom it may concern: we request reconsideration of this {category} denial..."
                # Only for appeals...
                if action == "appeal"
                # ...otherwise null.
                else None
            ),
        }

        # Wrong schema: break the shape in one of three ways.
        if outcome == "wrong_schema":
            # Pick which way to break it.
            variant = pick(["missing_field", "string_confidence", "nested"])
            # Variant 1: drop a required field.
            if variant == "missing_field":
                # Remove "action".
                body.pop("action")
            # Variant 2: wrong type.
            elif variant == "string_confidence":
                # Confidence becomes the word "high" instead of a number.
                body["confidence"] = "high"
            # Variant 3: wrapped in an extra object.
            else:
                # Everything moves under a "result" key.
                body = {"result": body}

        # Serialize the reply to a JSON string.
        text = json.dumps(body)
        # Malformed: corrupt the text in one of three ways.
        if outcome == "malformed":
            # Pick which way to corrupt it.
            variant = pick(["prose", "truncated", "fenced"])
            # Variant 1: chatty text around the JSON.
            if variant == "prose":
                # The JSON is in there, but the whole string won't parse.
                text = f"Sure! Here is my analysis of the claim:\n{text}\nLet me know if you need more."
            # Variant 2: cut off halfway, like a dropped connection.
            elif variant == "truncated":
                # Keep only the first half of the characters.
                text = text[: len(text) // 2]
            # Variant 3: wrapped in a markdown code fence.
            else:
                # ```json ... ``` around the JSON.
                text = f"```json\n{text}\n```"
        # Return the text and the outcome label (the caller of complete() never sees the label).
        return text, outcome
