"""Japanese market breadth and turnover, computed client-side from JPX + Yahoo.

Eastmoney's push2 ``clist`` endpoint — which backs the A-share/US/HK screener —
does carry a Tokyo universe, but it is not dependable from outside mainland
China: measured here, ``push2`` returned non-JSON on every attempt and
``push2delay`` succeeded roughly half the time with 15-19s read timeouts. A
daily report cannot be built on that.

So Japan is screened differently: take the universe from JPX's own listing
master (see :mod:`._jpx_universe`) and price it through the loader layer that
already serves ``.T`` symbols. TOPIX 500 (~492 issues) prices in about five
seconds, which is what makes this viable as a daily step.

Two honesty constraints are baked into the output shape:

* Breadth is **approximate**. Repeated runs disagree by tens of issues because
  bars update intraday and not every issue's last bar shares a date. The payload
  therefore carries ``as_of``, a coverage denominator, and a ``stale`` bucket
  rather than presenting a clean advancers/decliners pair.
* Turnover is derived as close x volume. JPX does not publish a keyless
  per-issue turnover-value feed, and yfinance returns share volume only.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Chunk size for the batched OHLCV fetch. The loader issues one upstream request
# per chunk; larger chunks are faster but a single bad symbol taints more of the
# batch, and Yahoo starts shedding very long symbol lists.
_CHUNK = 60

# Calendar days of history to request. Needs to span a weekend plus a public
# holiday so every issue has at least two sessions to difference.
_LOOKBACK_DAYS = 12

_UNIVERSES = ("topix500", "prime", "all")


def _previous_and_last(rows: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    if not rows:
        return None, None
    if len(rows) == 1:
        return None, rows[-1]
    return rows[-2], rows[-1]


def _select(universe: str) -> list[Any]:
    from src.tools import _jpx_universe as jpx

    issues = jpx.load_universe()
    if universe == "topix500":
        return jpx.topix500(issues)
    if universe == "prime":
        return [i for i in jpx.domestic_equities(issues) if i.segment.startswith("プライム")]
    return jpx.domestic_equities(issues)


def screen_japan(
    *,
    sort_by: str = "change_pct",
    top_n: int = 30,
    universe: str = "topix500",
    as_of: str | None = None,
) -> dict[str, Any]:
    """Rank Japanese issues and summarise market breadth.

    Args:
        sort_by: ``change_pct``, ``amount`` (close x volume), or ``volume``.
        top_n: How many ranked rows to return.
        universe: ``topix500`` (default, ~492), ``prime`` (~1558) or ``all``
            (~3713 domestic equities).
        as_of: Report date as YYYY-MM-DD. Defaults to today in Tokyo.

    Returns:
        A payload with ``rows``, a ``breadth`` summary and the coverage
        denominators needed to state the numbers honestly.

    Raises:
        ValueError: On an unknown universe or sort key.
    """
    if universe not in _UNIVERSES:
        raise ValueError(f"universe must be one of {list(_UNIVERSES)}")
    if sort_by not in ("change_pct", "amount", "volume"):
        raise ValueError("sort_by must be one of ['change_pct', 'amount', 'volume']")

    from src.market_data import fetch_market_data

    tokyo_today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    end = dt.date.fromisoformat(as_of) if as_of else tokyo_today
    start = end - dt.timedelta(days=_LOOKBACK_DAYS)

    issues = _select(universe)
    by_symbol = {i.symbol: i for i in issues}
    symbols = list(by_symbol)

    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for offset in range(0, len(symbols), _CHUNK):
        chunk = symbols[offset:offset + _CHUNK]
        try:
            fetched = fetch_market_data(
                codes=chunk, start_date=start.isoformat(), end_date=end.isoformat(),
                source="yfinance", max_rows=0,
            )
        except Exception as exc:  # noqa: BLE001 - a dead chunk must not sink the screen
            logger.warning("jp screen chunk %d failed: %s", offset // _CHUNK, exc)
            unresolved.extend(chunk)
            continue
        unresolved.extend(fetched.pop("_unresolved", []))
        for symbol, bars in fetched.items():
            issue = by_symbol.get(symbol)
            if not issue or not isinstance(bars, list):
                continue
            prev, last = _previous_and_last(bars)
            if not last or not prev or not prev.get("close"):
                continue
            close = last["close"]
            volume = last.get("volume") or 0
            rows.append({
                "code": issue.code,
                "symbol": symbol,
                "name": issue.name,
                "segment": issue.segment,
                "sector33": issue.sector33,
                "size": issue.size,
                "date": str(last.get("trade_date", ""))[:10],
                "close": round(close, 2),
                "change_pct": round((close / prev["close"] - 1) * 100, 2),
                "volume": volume,
                # JPX publishes no keyless per-issue turnover value; close x
                # volume is the standard proxy and is labelled as derived.
                "amount_estimate": round(close * volume),
            })

    # Bars whose last session predates the newest session in the set are stale
    # (trading halt, or a slow upstream). Counting them as "flat" would inflate
    # the unchanged bucket, so they get their own bucket.
    latest_date = max((r["date"] for r in rows), default="")
    fresh = [r for r in rows if r["date"] == latest_date]
    stale = len(rows) - len(fresh)

    advancers = sum(1 for r in fresh if r["change_pct"] > 0)
    decliners = sum(1 for r in fresh if r["change_pct"] < 0)
    unchanged = len(fresh) - advancers - decliners

    key = {"change_pct": "change_pct", "amount": "amount_estimate", "volume": "volume"}[sort_by]
    ranked = sorted(fresh, key=lambda r: r[key], reverse=True)[:top_n]

    return {
        "market": "jp",
        "universe": universe,
        "sort_by": sort_by,
        "as_of": latest_date or end.isoformat(),
        "requested_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(
            timespec="seconds"
        ),
        "coverage": {
            "universe_size": len(symbols),
            "priced": len(rows),
            "counted": len(fresh),
            "stale_bars": stale,
            "unresolved": len(unresolved),
        },
        "breadth": {
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "advance_decline_ratio": (
                round(advancers / decliners, 2) if decliners else None
            ),
            "note": (
                "Approximate. Counted over the issues whose latest bar is dated "
                f"{latest_date or 'n/a'} ({len(fresh)} of {len(symbols)}); bars "
                "update intraday, so repeated runs differ by tens of issues."
            ),
        },
        "rows": ranked,
    }
