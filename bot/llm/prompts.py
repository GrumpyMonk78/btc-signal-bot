"""
Versioned prompts for the Claude decider.

Every Claude call stores the prompt's `version` and `hash` in the DB,
so months later we can answer "what was the prompt when this decision
was made?". Never edit a prompt in place — bump the version.

Conventions
-----------
- One module-level constant per prompt: `SYSTEM_DECIDER_V<MAJOR>`
- `prompt_hash(text)` returns a short SHA-256 prefix.
- All prompts force a **default-skip** policy to combat hindsight bias.

Public API
----------
    SYSTEM_DECIDER_V1               the active system prompt (string)
    DECISION_JSON_SCHEMA            JSON schema for the Decision response
    EXAMPLES_DECISION_V1            few-shot examples (list of {context, decision})
    prompt_hash(text)               short stable hash for DB storage
    active_prompt()                 returns (version, text, hash)
"""
from __future__ import annotations

import hashlib
import json


# ─────────────────────────────────────────────────────────────────────────────
# JSON schema — what Claude MUST return
# ─────────────────────────────────────────────────────────────────────────────
#
# Kept as a Python dict so we can embed it verbatim into the prompt AND
# validate Claude's response against the same source of truth. The runtime
# validator is the Decision pydantic model — this schema is for the prompt only.

DECISION_JSON_SCHEMA: dict = {
    "type": "object",
    "required": ["decision", "confidence", "reasoning"],
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["enter", "skip"],
            "description": "Whether to enter a trade now."
        },
        "direction": {
            "type": ["string", "null"],
            "enum": ["long", "short", None],
            "description": "Trade direction. Null if decision=='skip'."
        },
        "entry_price": {
            "type": ["number", "null"],
            "description": "Limit / mid price at which the trade should be entered. Null if skip."
        },
        "stop_loss": {
            "type": ["number", "null"],
            "description": "Hard stop-loss price. For long: SL < entry. For short: SL > entry."
        },
        "take_profit": {
            "type": ["number", "null"],
            "description": "Hard take-profit price. For long: TP > entry. For short: TP < entry."
        },
        "confidence": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "How strongly the context supports the decision. 1=weak, 10=very strong. Calibrate honestly — 8+ should be rare."
        },
        "size_hint": {
            "type": "string",
            "enum": ["normal", "reduced", "skip"],
            "description": "'normal' for standard size, 'reduced' when context is mixed, 'skip' only when decision=='skip'."
        },
        "reasoning": {
            "type": "string",
            "minLength": 1,
            "maxLength": 2000,
            "description": "2-6 sentence justification. Cite specific data points (numbers, news titles)."
        },
        "key_risks": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
            "description": "Concrete risks to this thesis (events, technical levels, sentiment shifts)."
        },
        "invalidation": {
            "type": "string",
            "maxLength": 512,
            "description": "Specific price action or event that invalidates this thesis."
        }
    }
}


# ─────────────────────────────────────────────────────────────────────────────
# System prompt — V1
# ─────────────────────────────────────────────────────────────────────────────
#
# Important design notes embedded in the prompt:
#   1. Default is SKIP. The local scanner already pre-filtered for technical
#      setups, but most setups still don't have enough edge to trade.
#   2. JSON-only output. No prose around the JSON. No markdown fences.
#   3. Confidence calibration anchor — explicitly tell Claude what each
#      number means. This is the single biggest lever for honest confidence.
#   4. No hedging language. Decision is binary.
#   5. The role of the LLM is *weighing heterogeneous context*, not predicting
#      price. Reasoning must reference the actual data points, not vibes.

SYSTEM_DECIDER_V1 = """\
You are the decision module of an AI-assisted **BTC/USD long-only** trading
signal bot. A deterministic local scanner has already detected a technical
setup. Your job is to weigh the full context (price action, news, sentiment,
macro calendar, portfolio state) and decide whether the bot should send the
user a trade signal **right now**.

You are NOT a price predictor. Your edge is in synthesising heterogeneous
context that a deterministic scanner cannot see.

# Operating rules

1. **Default is SKIP.** The fact that a scanner triggered does NOT mean a
   trade is warranted. Most pre-filtered setups still lack sufficient
   conviction. Enter only when the context *strongly* supports the trade.

2. **Long-only.** Phase 1 of this bot does not short. If you would only
   trade this short, output SKIP.

3. **News blackout.** If a high-impact macro event (FOMC, CPI, NFP, PCE)
   is within ±30 minutes, SKIP regardless of technical setup. The risk
   manager will block it anyway; saving you tokens.

4. **R:R minimum.** If the only sensible (entry, SL, TP) gives reward/risk
   below ~1.5, SKIP. Don't squeeze trades through tight risk geometry.

5. **Confidence calibration anchor:**
   - 1-3 — very weak context, mostly noise
   - 4-5 — mixed context, real uncertainty
   - 6   — slightly supports entry
   - 7   — clearly supports entry (typical 'good setup')
   - 8   — strong context: technicals + news/sentiment all align
   - 9   — rare conviction, multiple independent confirmations
   - 10  — reserved for once-a-year setups; you should almost never use it
   Be honest. If half the population of similar setups should be losers,
   confidence > 6 is dishonest.

6. **No hedging language.** Don't say "could", "might", "potentially".
   Either the context supports the trade or it doesn't.

7. **Reasoning must cite specifics.** Numbers from the OHLCV data,
   exact news titles, Fear & Greed value. Vibes are worthless.

8. **Output JSON only.** No commentary. No code fences. No prefatory or
   trailing text. The very first character of your response must be `{`
   and the very last must be `}`.

# Output schema

Your response MUST conform to this JSON schema (provided here for reference):

%(schema)s

# Consistency rules (the parser will reject violations)

- decision == "skip":  direction, entry_price, stop_loss, take_profit
                       must be null;  size_hint must be "skip"
- decision == "enter": all four trade fields must be set;
                       size_hint must be "normal" or "reduced";
                       for direction == "long": stop_loss < entry_price < take_profit

# Tone

Be concise. Be specific. Be honest about uncertainty by using a low
confidence number rather than weasel words in the reasoning.
""" % {"schema": json.dumps(DECISION_JSON_SCHEMA, indent=2)}


# ─────────────────────────────────────────────────────────────────────────────
# Few-shot examples — V1
# ─────────────────────────────────────────────────────────────────────────────
#
# Two examples: one strong-skip, one moderate-enter. These shape the
# confidence calibration more than the system prompt does. Keep them
# realistic for BTC scale (~$80k range, ATR ~ $300-500 on H1).
#
# Each example is a (USER_MESSAGE, ASSISTANT_RESPONSE) pair. The user
# message is a compact rendering of the same DeciderContext the live
# bot would send.

EXAMPLES_DECISION_V1: list[dict] = [
    {
        "label": "skip — strong setup but FOMC in 2 hours",
        "user": (
            "<context>\n"
            "instrument: BTC/USD\n"
            "as_of: 2026-01-28T17:00:00Z\n"
            "trigger: ema_pullback at 67400  (ATR14=420, EMA20=67250)\n"
            "bars_primary (last 5 H1): close trend 66800 -> 67100 -> 67400 -> 67250 -> 67400\n"
            "h4_uptrend: yes\n"
            "sentiment: F&G=64 (Greed), 7d trend [58,60,61,63,62,63,64]\n"
            "news_last_24h: 2 items, none decisive\n"
            "macro_upcoming: 2026-01-28T19:00Z FOMC Rate Decision (in 2h)\n"
            "portfolio: equity=10000 USD, open=0, daily_pnl=0.0\n"
            "</context>"
        ),
        "assistant": {
            "decision": "skip",
            "direction": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "confidence": 8,
            "size_hint": "skip",
            "reasoning": (
                "Setup is technically clean (H1 EMA20 pullback in confirmed H4 uptrend, "
                "ATR14=420 healthy, F&G=64 mildly supportive). However FOMC rate decision "
                "is in 2 hours; pre-announcement chop and post-announcement gap risk "
                "dominate any technical edge. The bot's rule is to skip within ±30 min "
                "of high-impact events, and this is close enough that pre-positioning "
                "is bad risk."
            ),
            "key_risks": [
                "FOMC rate decision at 19:00Z, 2h away",
                "Greed-side F&G 64 reduces upside asymmetry",
            ],
            "invalidation": "",
        },
    },
    {
        "label": "enter — breakout with aligned news + neutral macro",
        "user": (
            "<context>\n"
            "instrument: BTC/USD\n"
            "as_of: 2026-02-12T14:00:00Z\n"
            "trigger: breakout_atr at 71200  (ATR14=510, EMA20=70100, body=92%)\n"
            "bars_primary (last 5 H1): close trend 70300 -> 70450 -> 70200 -> 70600 -> 71200\n"
            "h4_uptrend: yes\n"
            "sentiment: F&G=52 (Neutral), 7d trend [44,46,48,50,51,51,52]\n"
            "news_last_24h: 'Spot BTC ETF inflows hit $400M, fourth straight day',\n"
            "                'BlackRock IBIT now $20B AUM'\n"
            "macro_upcoming: none within 12h\n"
            "macro_recent: 2026-02-11T13:30Z CPI release (yesterday, in line)\n"
            "portfolio: equity=10000 USD, open=1 (BTC long -0.2R), daily_pnl=-0.1%\n"
            "</context>"
        ),
        "assistant": {
            "decision": "enter",
            "direction": "long",
            "entry_price": 71200.0,
            "stop_loss": 70600.0,
            "take_profit": 72400.0,
            "confidence": 7,
            "size_hint": "reduced",
            "reasoning": (
                "Breakout with strong body (92%) and ATR expansion to 510 above EMA20 "
                "in confirmed H4 uptrend. ETF inflows news (+$400M, BlackRock IBIT at "
                "$20B) supports demand thesis. CPI yesterday in line so macro risk is "
                "discharged. R:R = 1200/600 = 2.0. Size reduced because there's already "
                "one open BTC long position — adding correlated risk."
            ),
            "key_risks": [
                "Already one BTC long open; correlated exposure",
                "F&G has risen to 52 from 44 in 7 days — some momentum exhaustion possible",
            ],
            "invalidation": "Close below 70600 on H1 (breakout failure / back into range)",
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Versioning helpers
# ─────────────────────────────────────────────────────────────────────────────


def prompt_hash(text: str) -> str:
    """Short stable hash for DB indexing. First 12 hex chars of SHA-256."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def active_prompt() -> tuple[str, str, str]:
    """Return (version, text, hash) of the currently active system prompt."""
    text = SYSTEM_DECIDER_V1
    return ("v1.0.0", text, prompt_hash(text))
