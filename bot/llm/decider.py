"""
Claude API integration — the only paid call in the pipeline.

Responsibilities
----------------
1. Build the user message from a DeciderContext.
2. Call the Claude API with the active versioned system prompt.
3. Parse the response strictly — JSON only, validated against Decision.
4. Track input/output token usage against a daily budget.
5. Retry on transient failures with exponential backoff.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from bot.config import settings
from bot.llm.context import render_context_for_prompt
from bot.llm.prompts import active_prompt, EXAMPLES_DECISION_V2, EXAMPLES_DECISION_V4
from bot.storage.models import DeciderContext, Decision

logger = logging.getLogger(__name__)


class DeciderError(Exception):
    """Base — Claude returned something we cannot turn into a Decision."""


class NonJsonResponseError(DeciderError):
    """Claude wrapped JSON in prose, used code fences, or returned plain text."""


class BudgetExceededError(DeciderError):
    """The next call would push today's spend over the budget."""


@dataclass
class TokenBudget:
    """In-memory daily token budget tracker."""

    daily_limit: int
    spent_today: int = 0
    day_started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def _maybe_roll_day(self) -> None:
        now = datetime.now(timezone.utc)
        if now.date() != self.day_started.date():
            logger.info("token_budget: rolling day - yesterday spent %d", self.spent_today)
            self.spent_today = 0
            self.day_started = now

    def can_afford(self, est_input: int, est_output_max: int = 1024) -> bool:
        self._maybe_roll_day()
        return (self.spent_today + est_input + est_output_max) <= self.daily_limit

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self._maybe_roll_day()
        self.spent_today += input_tokens + output_tokens


_default_budget: TokenBudget | None = None


def default_budget() -> TokenBudget:
    global _default_budget
    if _default_budget is None:
        _default_budget = TokenBudget(daily_limit=settings.anthropic_daily_token_budget)
    return _default_budget


@dataclass(frozen=True)
class DeciderResult:
    decision: Decision
    raw_response: str
    model: str
    prompt_version: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)


def _extract_json_object(text: str) -> str:
    cleaned = text.strip()
    m = _CODE_FENCE_RE.match(cleaned)
    if m:
        cleaned = m.group(1).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    candidates = _JSON_OBJECT_RE.findall(cleaned)
    if not candidates:
        raise NonJsonResponseError(f"No JSON object in response. Got: {text[:200]!r}...")
    return max(candidates, key=len)


def parse_decision(raw_text: str) -> Decision:
    json_str = _extract_json_object(raw_text)
    try:
        data: Any = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise NonJsonResponseError(
            f"Malformed JSON: {exc}; first 200 chars: {json_str[:200]!r}"
        ) from exc
    if not isinstance(data, dict):
        raise DeciderError(f"Expected JSON object, got {type(data).__name__}")
    try:
        return Decision(**data)
    except ValidationError as exc:
        raise DeciderError(f"Decision validation failed: {exc}") from exc


def _estimate_input_tokens(system_text: str, user_text: str) -> int:
    return (len(system_text) + len(user_text)) // 4 + 100


def decide(
    ctx: DeciderContext,
    *,
    client: Any | None = None,
    budget: TokenBudget | None = None,
    max_attempts: int = 2,
    max_output_tokens: int = 1024,
) -> DeciderResult:
    """Ask Claude what to do. Returns a validated DeciderResult."""
    if client is None:
        import anthropic
        if not settings.anthropic_api_key:
            raise DeciderError(
                "ANTHROPIC_API_KEY not configured — set it in .env to use the decider."
            )
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    budget = budget or default_budget()

    prompt_version, system_text, prompt_h = active_prompt()
    user_text = render_context_for_prompt(ctx)

    # ── Prompt caching ────────────────────────────────────────────────────
    # System prompt je označen jako cacheable — Anthropic ho zapamatuje na
    # ~1h a účtuje jen cache_read (90% sleva oproti plné ceně input tokenů).
    # Podmínka: min. 1024 tokenů v cacheable bloku (system prompt splňuje).
    system_with_cache: list[dict] = [
        {
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Few-shot příklady — také cacheable (mění se jen s verzí promptu)
    few_shot_messages: list[dict] = []
    # Use V4 examples if available (includes short examples), fall back to V2
    _examples = EXAMPLES_DECISION_V4 if EXAMPLES_DECISION_V4 else EXAMPLES_DECISION_V2
    for i, ex in enumerate(_examples):
        few_shot_messages.append({"role": "user", "content": ex["user"]})
        # Poslední few-shot assistant blok označíme jako cacheable — tím
        # cachujeme celý prefix (system + few-shot) najednou.
        is_last = (i == len(_examples) - 1)
        assistant_content: Any = (
            [{"type": "text", "text": json.dumps(ex["assistant"]),
              "cache_control": {"type": "ephemeral"}}]
            if is_last
            else json.dumps(ex["assistant"])
        )
        few_shot_messages.append({"role": "assistant", "content": assistant_content})

    # Aktuální trigger — nekachujeme (pokaždé jiný kontext)
    few_shot_messages.append({"role": "user", "content": user_text})

    est_in = _estimate_input_tokens(system_text, user_text)
    if not budget.can_afford(est_in, max_output_tokens):
        raise BudgetExceededError(
            f"Estimated cost {est_in}+{max_output_tokens} tokens would exceed daily "
            f"budget (already spent {budget.spent_today} of {budget.daily_limit})"
        )

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        start = time.monotonic()
        try:
            response = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=max_output_tokens,
                system=system_with_cache,
                messages=few_shot_messages,
                extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            )
            latency_ms = int((time.monotonic() - start) * 1000)

            raw_text = "".join(
                block.text for block in response.content
                if getattr(block, "type", None) == "text"
            )

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            cache_create = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
            budget.record(input_tokens, output_tokens)

            if cache_create:
                logger.debug("cache: WRITE %d tokens (prvni volani / novy prompt)", cache_create)
            if cache_read:
                logger.debug("cache: HIT %d tokens (~90%% sleva)", cache_read)

            decision = parse_decision(raw_text)

            return DeciderResult(
                decision=decision,
                raw_response=raw_text,
                model=settings.anthropic_model,
                prompt_version=prompt_version,
                prompt_hash=prompt_h,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                attempts=attempt,
            )

        except NonJsonResponseError as exc:
            last_exc = exc
            logger.warning("decider attempt %d: non-JSON response - retrying", attempt)
            if attempt < max_attempts:
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            raise
        except DeciderError:
            raise
        except Exception as exc:
            last_exc = exc
            logger.warning("decider attempt %d: %s - retrying", attempt, exc)
            if attempt < max_attempts:
                time.sleep(1.0 * (2 ** (attempt - 1)))
                continue
            raise DeciderError(f"All {max_attempts} attempts failed; last: {exc}") from exc

    raise DeciderError(f"unexpected fall-through; last={last_exc}")
