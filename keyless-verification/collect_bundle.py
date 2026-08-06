#!/usr/bin/env python
"""Collect every input a daily market report needs, using zero API keys.

Talks to the Vibe-Trading MCP server in-process. Intended to be run under a
genuinely empty environment to prove no credential leaks in:

    env -i HOME=$HOME PATH=/usr/bin:/bin .venv/bin/python keyless-verification/collect_bundle.py

Two routing facts drive the BASKETS table below:

* ``market_data.detect_source`` (agent/src/market_data.py:49-54) falls through
  to ``tushare`` for anything its regex table misses. There is no pattern for
  Tokyo ``.T`` codes or for caret-prefixed indices, and no ``jp_equity`` entry
  in ``FALLBACK_CHAINS``, so those symbols never reach yfinance and come back
  under ``_unresolved``. Pinning ``source="yfinance"`` bypasses the table.
* Yahoo's ``=X`` / ``=F`` suffixes and the ``-USDT`` crypto form *are* in the
  table, so they route correctly under ``auto``.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys

import mcp_server
from fastmcp import Client

# (label, codes, source) — pin the source wherever detect_source() misroutes.
BASKETS: list[tuple[str, list[str], str]] = [
    ("JP equity", ["^N225", "1306.T", "7203.T", "6758.T", "8306.T"], "yfinance"),
    ("US equity", ["^GSPC", "^IXIC", "^DJI", "^VIX"], "yfinance"),
    ("Asia ex-JP", ["^HSI"], "yfinance"),
    ("Rates", ["^TNX"], "yfinance"),
    ("FX", ["USDJPY=X", "EURJPY=X"], "auto"),
    ("Commodities", ["GC=F", "CL=F"], "auto"),
    ("Crypto", ["BTC-USDT", "ETH-USDT"], "auto"),
]

NEWS_SYMBOLS = ["AAPL.US", "NVDA.US"]
SEARCHES = [
    "Nikkei 225 today close",
    "Bank of Japan policy",
    "Fed rate decision",
]

TIMEOUT = 120


async def _call(client: Client, name: str, argv: dict) -> str:
    res = await asyncio.wait_for(client.call_tool(name, argv), timeout=TIMEOUT)
    return "".join(getattr(b, "text", "") or "" for b in getattr(res, "content", res))


async def collect(start: str, end: str) -> dict:
    bundle: dict = {
        "as_of": end,
        "window_start": start,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "prices": {},
        "unresolved": [],
        "news": {},
        "search": {},
    }

    async with Client(mcp_server.mcp) as client:
        for label, codes, source in BASKETS:
            payload = json.loads(await _call(client, "get_market_data", {
                "codes": codes, "start_date": start, "end_date": end,
                "source": source, "max_rows": 0,
            }))
            bundle["unresolved"] += payload.pop("_unresolved", [])
            for symbol, entry in payload.items():
                rows = entry.get("data", entry) if isinstance(entry, dict) else entry
                if not rows:
                    continue
                first, last = rows[0], rows[-1]
                prev = rows[-2] if len(rows) > 1 else last
                bundle["prices"][symbol] = {
                    "basket": label,
                    "n_bars": len(rows),
                    "last_date": last["trade_date"][:10],
                    "close": round(last["close"], 4),
                    "chg_1d_pct": round((last["close"] / prev["close"] - 1) * 100, 2),
                    "chg_period_pct": round((last["close"] / first["close"] - 1) * 100, 2),
                    "volume": last.get("volume"),
                }
            print(f"[{label}] {len(codes)} requested -> {len(payload)} resolved", file=sys.stderr)

        bundle["news"]["global"] = json.loads(
            await _call(client, "get_stock_news", {"scope": "global", "limit": 12}))
        for symbol in NEWS_SYMBOLS:
            bundle["news"][symbol] = json.loads(
                await _call(client, "get_stock_news", {"code": symbol, "limit": 6}))
        for query in SEARCHES:
            bundle["search"][query] = json.loads(
                await _call(client, "web_search", {"query": query}))

    return bundle


def main() -> int:
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--end", default=today.isoformat(), help="report date (YYYY-MM-DD)")
    ap.add_argument("--start", default=(today - dt.timedelta(days=30)).isoformat(),
                    help="history window start (YYYY-MM-DD)")
    ap.add_argument("--out", default="bundle.json")
    args = ap.parse_args()

    bundle = asyncio.run(collect(args.start, args.end))
    with open(args.out, "w") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=1)

    print(f"\nwrote {args.out}: resolved={len(bundle['prices'])} unresolved={bundle['unresolved']}")
    for symbol, p in bundle["prices"].items():
        print(f"  {symbol:12s} {p['last_date']} close={p['close']:>12,.2f} "
              f"1d={p['chg_1d_pct']:+6.2f}% window={p['chg_period_pct']:+7.2f}% bars={p['n_bars']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
