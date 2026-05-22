"""
Smoke test for the Alpaca data layer.

What it does
------------
1. Loads `bot.config.settings` from `.env`.
2. Verifies the Alpaca credentials are present (friendly message if not).
3. Fetches BTC/USD bars on the primary and context timeframes.
4. Prints a short summary: rows, date range, last close, and last 3 bars.

Run
---
    python -m scripts.smoke_fetch

Exit codes
----------
    0 — fetch succeeded
    1 — credentials missing
    2 — fetch failed (network / API error)
"""
from __future__ import annotations

import sys
import traceback

from bot.config import settings


def main() -> int:
    print("─" * 70)
    print(f"  BTC AI Signal Bot — data layer smoke test")
    print(f"  instrument={settings.instrument}")
    print(f"  primary={settings.timeframe_primary}  context={settings.timeframe_context}")
    print(f"  alpaca_paper={settings.alpaca_paper}")
    print("─" * 70)

    missing = settings.required_for_data()
    if missing:
        print()
        print("  ✗ Cannot run smoke test — missing env vars:")
        for name in missing:
            print(f"      - {name}")
        print()
        print("  Fix: copy .env.example to .env and fill in Alpaca paper keys.")
        print("       https://app.alpaca.markets/ → Paper Trading → API Keys")
        return 1

    try:
        from bot.data.market import default_provider

        provider = default_provider()
        primary, context = provider.fetch_primary_and_context(
            settings.instrument, limit_primary=200, limit_context=200
        )
    except Exception as exc:  # noqa: BLE001
        print()
        print(f"  ✗ Fetch failed: {exc.__class__.__name__}: {exc}")
        print()
        traceback.print_exc()
        return 2

    def _summary(name: str, df) -> None:
        print()
        print(f"  ▸ {name} ({settings.timeframe_primary if name == 'primary' else settings.timeframe_context})")
        if df.empty:
            print("      <empty> — did the venue return no bars?")
            return
        print(f"      rows:     {len(df)}")
        print(f"      from:     {df.index[0].isoformat()}")
        print(f"      to:       {df.index[-1].isoformat()}")
        print(f"      last:     close={df['close'].iloc[-1]:.2f}  volume={df['volume'].iloc[-1]:.4f}")
        print(f"      last 3 bars:")
        for ts, row in df.tail(3).iterrows():
            print(
                f"        {ts.isoformat()}  O={row['open']:.2f}  H={row['high']:.2f}  "
                f"L={row['low']:.2f}  C={row['close']:.2f}  V={row['volume']:.4f}"
            )

    _summary("primary", primary)
    _summary("context", context)

    print()
    print("  ✓ Data layer OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
