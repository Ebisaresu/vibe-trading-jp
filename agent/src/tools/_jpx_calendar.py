"""Tokyo trading calendar — which days the exchange is closed, fetched keyless.

Two official sources, neither sufficient alone:

* **JPX's own 休業日一覧.** Authoritative for the exchange, and the only source
  that lists the year-end closure as the exchange declares it. It covers the
  current year and the next, nothing earlier.
* **The Cabinet Office holiday file** (``syukujitsu.csv``). Every national
  holiday and substitute holiday since 1955, through next year. It knows
  nothing about the exchange, so the year-end closure (31 December and
  2–3 January; 1 January is a national holiday already) is added by rule.

The two are merged, and every closed day remembers which source named it. A
date both sources cover and only one of them closes is recorded as a
disagreement rather than resolved quietly.

Three things that bite:

* **Outside coverage is unknown, not open.** The Cabinet Office publishes next
  year's holidays each February. A weekday in a year neither source has reached
  is reported with ``is_trading_day: None``.
* **The holiday file is Shift_JIS** and its dates are unpadded (``2026/9/23``).
* **JPX's table is hand-edited HTML.** One row on the live page closes its cell
  with ``</td">``. The parser keys off the date pattern, not tag structure.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JPX_URL = "https://www.jpx.co.jp/corporate/about-jpx/calendar/index.html"
_CAO_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"
_JPX_HOST_KEY = "jpx"
_CAO_HOST_KEY = "cao"
_MIN_INTERVAL = 1.0
_TIMEOUT = 30.0

# Both sources change a handful of times a year. A week keeps a newly announced
# closure from going unnoticed for long.
_CACHE_TTL_S = 7 * 24 * 3600
_CACHE_NAME = "jpx_closed_days.json"
_CACHE_VERSION = 1

_JPX_ROW_RE = re.compile(
    r"(\d{4})/(\d{2})/(\d{2})（.）\s*</td[^>]*>\s*<td[^>]*>\s*([^<]+?)\s*</td", re.S
)
_CAO_ROW_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2}),(.+?)\s*$", re.M)

# Days the exchange closes that are not national holidays.
_YEAR_END = ((12, 31), (1, 2), (1, 3))
_YEAR_END_NAME = "年末年始休業日"

# The holiday file reaches back to 1955, but the exchange did not keep today's
# week then: Saturday sessions ran until 1989. Dates before this floor are
# reported as outside coverage rather than judged by modern rules.
_COVERAGE_FLOOR = dt.date(2000, 1, 1)

_JST = dt.timezone(dt.timedelta(hours=9))


def today_jst() -> dt.date:
    """Today's date in Tokyo, which is the only "today" the exchange has."""
    return dt.datetime.now(_JST).date()


def _parse_jpx(html: str) -> dict[dt.date, str]:
    """Extract closed days from JPX's 休業日一覧 page."""
    out: dict[dt.date, str] = {}
    for year, month, day, name in _JPX_ROW_RE.findall(html):
        try:
            date = dt.date(int(year), int(month), int(day))
        except ValueError:
            continue
        # JPX marks footnoted rows with a trailing ※.
        out[date] = name.strip().rstrip("※").strip()
    return out


def _parse_cao(payload: bytes) -> dict[dt.date, str]:
    """Extract national holidays from the Cabinet Office file."""
    text = payload.decode("cp932", "replace")
    out: dict[dt.date, str] = {}
    for year, month, day, name in _CAO_ROW_RE.findall(text):
        try:
            out[dt.date(int(year), int(month), int(day))] = name.strip()
        except ValueError:
            continue
    return out


def _merge(jpx: dict[dt.date, str], cao: dict[dt.date, str]) -> dict[str, Any]:
    """Combine both sources into one table with provenance.

    Returns:
        A JSON-ready mapping with ``days`` (ISO date -> name and sources),
        ``coverage`` and ``disagreements``.
    """
    days: dict[dt.date, dict[str, Any]] = {}

    def add(date: dt.date, name: str, source: str) -> None:
        entry = days.setdefault(date, {"name": name, "sources": []})
        if source not in entry["sources"]:
            entry["sources"].append(source)
        # A holiday's own name says more than JPX's generic 休業日.
        if source == "cao" or entry["name"] in ("休業日", "休日", ""):
            if name not in ("休日", ""):
                entry["name"] = name

    for date, name in cao.items():
        if date >= _COVERAGE_FLOOR:
            add(date, name, "cao")
    for date, name in jpx.items():
        if date >= _COVERAGE_FLOOR:
            add(date, name, "jpx")

    years = sorted({d.year for d in days})
    if years:
        for year in range(years[0], years[-1] + 1):
            for month, day in _YEAR_END:
                add(dt.date(year, month, day), _YEAR_END_NAME, "year_end_rule")

    # A disagreement is a weekday, inside the span both sources cover, that
    # exactly one of them closes. Weekends are closed whatever either says.
    disagreements: list[str] = []
    if jpx and cao:
        jpx_years = {d.year for d in jpx}
        for date, entry in days.items():
            if date.year not in jpx_years or date.weekday() >= 5:
                continue
            named_by_jpx = "jpx" in entry["sources"]
            named_otherwise = any(s != "jpx" for s in entry["sources"])
            if named_by_jpx != named_otherwise:
                disagreements.append(date.isoformat())

    return {
        "version": _CACHE_VERSION,
        "fetched_at": time.time(),
        "sources": [name for name, table in (("jpx", jpx), ("cao", cao)) if table],
        "coverage": {
            "first": dt.date(years[0], 1, 1).isoformat() if years else None,
            # Coverage ends with the last full year either source reached.
            "last": dt.date(years[-1], 12, 31).isoformat() if years else None,
        },
        "disagreements": sorted(disagreements),
        "days": {d.isoformat(): days[d] for d in sorted(days)},
    }


def _cache_path() -> Path:
    from src.config.paths import get_runtime_root

    return get_runtime_root() / "cache" / _CACHE_NAME


def _read_cache() -> tuple[dict[str, Any] | None, bool]:
    """Return ``(table, is_fresh)``. A stale table is still returned."""
    path = _cache_path()
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    if not isinstance(table, dict) or table.get("version") != _CACHE_VERSION:
        return None, False
    age = time.time() - float(table.get("fetched_at") or 0)
    return table, age <= _CACHE_TTL_S


def _write_cache(table: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("could not cache the jpx calendar: %s", exc)


def _fetch() -> dict[str, Any] | None:
    """Fetch and merge both sources. One failing does not sink the other."""
    from backtest.loaders._http import throttled_get

    jpx: dict[dt.date, str] = {}
    cao: dict[dt.date, str] = {}
    try:
        resp = throttled_get(
            _JPX_URL, host_key=_JPX_HOST_KEY, min_interval=_MIN_INTERVAL, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        jpx = _parse_jpx(resp.content.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - the other source may still answer
        logger.warning("jpx closed-day page failed: %s", exc)
    try:
        resp = throttled_get(
            _CAO_URL, host_key=_CAO_HOST_KEY, min_interval=_MIN_INTERVAL, timeout=_TIMEOUT
        )
        resp.raise_for_status()
        cao = _parse_cao(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cabinet office holiday file failed: %s", exc)
    if not jpx and not cao:
        return None
    return _merge(jpx, cao)


class TradingCalendar:
    """Closed days of the Tokyo exchange, with the span they are known for."""

    def __init__(self, table: dict[str, Any], *, stale: bool = False) -> None:
        self._days: dict[dt.date, dict[str, Any]] = {
            dt.date.fromisoformat(k): v for k, v in (table.get("days") or {}).items()
        }
        coverage = table.get("coverage") or {}
        self.first: dt.date | None = (
            dt.date.fromisoformat(coverage["first"]) if coverage.get("first") else None
        )
        self.last: dt.date | None = (
            dt.date.fromisoformat(coverage["last"]) if coverage.get("last") else None
        )
        self.sources: list[str] = list(table.get("sources") or [])
        self.disagreements: list[str] = list(table.get("disagreements") or [])
        self.stale = stale

    def covers(self, day: dt.date) -> bool:
        return self.first is not None and self.last is not None and self.first <= day <= self.last

    def status(self, day: dt.date) -> dict[str, Any]:
        """Describe one date.

        Returns:
            ``is_trading_day`` is True, False, or None when ``day`` lies outside
            coverage and is not a weekend. ``reason`` is ``weekend``,
            ``holiday``, ``year_end``, ``outside_coverage`` or None.
        """
        out: dict[str, Any] = {
            "date": day.isoformat(),
            "weekday": "月火水木金土日"[day.weekday()],
            "is_trading_day": True,
            "reason": None,
            "name": None,
        }
        entry = self._days.get(day)
        if entry is not None:
            year_end_only = entry["sources"] == ["year_end_rule"] or entry["name"] == _YEAR_END_NAME
            out.update(
                is_trading_day=False,
                reason="year_end" if year_end_only else "holiday",
                name=entry["name"],
            )
        elif day.weekday() >= 5:
            out.update(is_trading_day=False, reason="weekend")
        elif not self.covers(day):
            out.update(is_trading_day=None, reason="outside_coverage")
        return out

    def is_trading_day(self, day: dt.date) -> bool | None:
        return self.status(day)["is_trading_day"]

    def _step(self, day: dt.date, delta: int) -> dt.date | None:
        cursor = day
        # Golden Week plus a weekend is the longest closure; 30 is far past it.
        for _ in range(30):
            cursor += dt.timedelta(days=delta)
            state = self.is_trading_day(cursor)
            if state is None:
                return None
            if state:
                return cursor
        return None

    def next_trading_day(self, day: dt.date) -> dt.date | None:
        """The first trading day after ``day``, or None if coverage runs out."""
        return self._step(day, 1)

    def previous_trading_day(self, day: dt.date) -> dt.date | None:
        """The last trading day before ``day``, or None if coverage runs out."""
        return self._step(day, -1)

    def closed_weekdays(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        """Closed days between ``start`` and ``end`` that are not weekends."""
        return [
            {"date": d.isoformat(), "weekday": "月火水木金土日"[d.weekday()], "name": v["name"]}
            for d, v in sorted(self._days.items())
            if start <= d <= end and d.weekday() < 5
        ]


def load_calendar(*, force_refresh: bool = False) -> TradingCalendar | None:
    """Return the trading calendar, or None when it cannot be established.

    A stale cache is preferred to no calendar: holidays do not move once
    published, so an old table is wrong only about closures announced since.
    """
    cached, fresh = (None, False) if force_refresh else _read_cache()
    if cached is not None and fresh:
        return TradingCalendar(cached)
    table = _fetch()
    if table is not None:
        _write_cache(table)
        return TradingCalendar(table)
    if cached is not None:
        logger.warning("jpx calendar refresh failed; using the stale cache")
        return TradingCalendar(cached, stale=True)
    return None
