"""
Tests for the news filter (the network-free part).

We don't hit the live RSS feeds in unit tests — that's flaky and slow.
The fetch_news entrypoint is verified manually via dump_context. Here we
only exercise the keyword logic, which is the part that matters for
correctness.
"""
from __future__ import annotations

from bot.data.news import is_btc_relevant


def test_btc_keyword_matches():
    assert is_btc_relevant("Bitcoin hits new high")
    assert is_btc_relevant("BTC ETF inflows accelerate")
    assert is_btc_relevant("SEC approves spot Bitcoin ETF")
    assert is_btc_relevant("Powell signals rate cut next quarter")
    assert is_btc_relevant("CPI prints below consensus")


def test_unrelated_stories_filtered_out():
    assert not is_btc_relevant("Solana DeFi protocol launches new feature")
    assert not is_btc_relevant("Cardano roadmap updated for 2026")
    assert not is_btc_relevant("Apple Q4 earnings beat expectations")


def test_altcoin_with_btc_mention_still_relevant():
    # If BTC is explicitly named even in an altcoin context, treat as relevant
    # — these often discuss BTC dominance, flows, etc.
    assert is_btc_relevant("Solana rally outpaces Bitcoin in November")
    assert is_btc_relevant("Bitcoin and XRP both retest support")


def test_macro_keywords():
    # FOMC / Fed coverage is relevant even without "BTC" in the headline.
    assert is_btc_relevant("FOMC minutes hint at dovish pivot")
    assert is_btc_relevant("NFP smashes consensus, dollar surges")
