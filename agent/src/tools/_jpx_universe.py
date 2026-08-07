"""JPX listing master — the Japanese equity universe, fetched keyless from JPX.

JPX publishes the full list of TSE-listed issues as a single Excel file
(``data_j.xls``, ~830KB, refreshed monthly). It needs no key, no cookie and no
session bootstrap, and it is the only source in this repo that carries Japanese
company names, market segment, the 33-sector classification and TOPIX size
class together. The screener, the symbol resolver and the earnings sweep all
key off it.

Two shapes in this file bite if you assume the obvious:

* **Codes are not integers.** Since JPX's 2024 alphanumeric rollout a code may
  contain letters (``285A`` = Kioxia), and seven issues carry five characters.
  Everything here treats codes as strings and never zero-pads or int-casts.
* **Column headers are Japanese and have moved before.** Lookup is by regex
  against the header row, not by exact equality, so a cosmetic rename upstream
  degrades to a clear error instead of a KeyError deep in a caller.
"""

from __future__ import annotations

import io
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
_HOST_KEY = "jpx"
_MIN_INTERVAL = 1.0
_TIMEOUT = 30.0

# JPX refreshes the master monthly; a week keeps us current without re-pulling
# 830KB on every report run.
_CACHE_TTL_S = 7 * 24 * 3600
_CACHE_NAME = "jpx_data_j.xls"

# Header regexes, in the column order we expose. Matching is a substring search
# on the raw header so "33業種区分" survives a whitespace or bracket change.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("date", r"日付"),
    ("code", r"コード"),
    ("name", r"銘柄名"),
    ("segment", r"市場.*区分"),
    ("sector33_code", r"33業種コード"),
    ("sector33", r"33業種区分"),
    ("sector17_code", r"17業種コード"),
    ("sector17", r"17業種区分"),
    ("size_code", r"規模コード"),
    ("size", r"規模区分"),
)

# JPX writes a literal "-" where a field does not apply (every ETF row, for
# instance, has "-" for all four sector columns).
_BLANK = {"-", "", "nan", "None"}

# 規模区分 values that together form TOPIX 500. Verified against the live file:
# Core30=31 + Large70=68 + Mid400=393 = 492 issues.
_TOPIX500_SIZES = frozenset({"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"})

# Domestic common stock lives in the three 内国株式 segments. ETFs, REITs, PRO
# Market and the handful of 外国株式 listings are excluded from "equities" but
# stay in the raw table so a caller can ask for them by segment.
_DOMESTIC_RE = re.compile(r"内国株式")


@dataclass(frozen=True)
class Issue:
    """One JPX-listed issue."""

    code: str
    """Bare JPX code, e.g. "7203" or "285A"."""
    name: str
    """Japanese issue name as JPX writes it, e.g. "トヨタ自動車"."""
    segment: str
    """市場・商品区分, e.g. "プライム（内国株式）"."""
    sector33: str
    """33業種区分, or "" for issues JPX does not classify."""
    sector17: str
    size: str
    """規模区分, e.g. "TOPIX Core30", or "" when unclassified."""

    @property
    def symbol(self) -> str:
        """The yfinance/Yahoo symbol for this issue."""
        return f"{self.code}.T"

    @property
    def is_domestic_equity(self) -> bool:
        """True for the three 内国株式 segments (Prime/Standard/Growth)."""
        return bool(_DOMESTIC_RE.search(self.segment))

    @property
    def in_topix500(self) -> bool:
        return self.size in _TOPIX500_SIZES


def _cache_path() -> Path:
    from src.config.paths import get_runtime_root

    return get_runtime_root() / "cache" / _CACHE_NAME


def _read_cache(ttl_s: float) -> bytes | None:
    """Return cached workbook bytes when present and fresher than ``ttl_s``."""
    path = _cache_path()
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > ttl_s:
        logger.debug("jpx master cache is %.0fs old (ttl %.0fs), refetching", age, ttl_s)
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_cache(payload: bytes) -> None:
    """Persist the workbook, tolerating an unwritable HOME."""
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a truncated
        # workbook that later reads would treat as a valid cache hit.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    except OSError as exc:
        logger.debug("could not cache the jpx master: %s", exc)


def _fetch() -> bytes:
    from backtest.loaders._http import throttled_get

    resp = throttled_get(
        _JPX_URL, host_key=_HOST_KEY, min_interval=_MIN_INTERVAL, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.content


def _resolve_columns(headers: list[Any]) -> dict[str, Any]:
    """Map our field names onto the workbook's actual header labels.

    Raises:
        ValueError: If a required column has no match, naming the field. A
            clear failure here beats a KeyError surfacing inside a caller.
    """
    resolved: dict[str, Any] = {}
    for field, pattern in _COLUMNS:
        rx = re.compile(pattern)
        match = next((h for h in headers if rx.search(str(h))), None)
        if match is None:
            raise ValueError(
                f"JPX master is missing the {field!r} column "
                f"(looked for /{pattern}/ in {headers!r})"
            )
        resolved[field] = match
    return resolved


def _clean(value: Any) -> str:
    text = str(value).strip()
    return "" if text in _BLANK else text


def load_universe(*, ttl_s: float = _CACHE_TTL_S, force_refresh: bool = False) -> list[Issue]:
    """Return every issue in the JPX listing master.

    Args:
        ttl_s: Cache lifetime in seconds. Pass 0 to always refetch.
        force_refresh: Skip the cache entirely.

    Returns:
        Every row of the master, including ETFs, REITs and PRO Market — filter
        with :func:`domestic_equities` or :func:`topix500` for the usual cuts.

    Raises:
        ValueError: If the workbook's columns cannot be resolved.
    """
    import pandas as pd

    payload = None if force_refresh else _read_cache(ttl_s)
    if payload is None:
        payload = _fetch()
        _write_cache(payload)

    # dtype=str keeps "285A" a string and stops pandas turning "1301" into an
    # int and then into "1301.0" on the way back out.
    frame = pd.read_excel(io.BytesIO(payload), dtype=str)
    cols = _resolve_columns(list(frame.columns))

    issues: list[Issue] = []
    for row in frame.to_dict("records"):
        code = _clean(row[cols["code"]])
        if not code:
            continue
        issues.append(
            Issue(
                code=code,
                name=_clean(row[cols["name"]]),
                segment=_clean(row[cols["segment"]]),
                sector33=_clean(row[cols["sector33"]]),
                sector17=_clean(row[cols["sector17"]]),
                size=_clean(row[cols["size"]]),
            )
        )
    logger.debug("loaded %d issues from the jpx master", len(issues))
    return issues


def domestic_equities(issues: list[Issue] | None = None) -> list[Issue]:
    """Return only Prime/Standard/Growth 内国株式 issues (~3700)."""
    return [i for i in (issues if issues is not None else load_universe()) if i.is_domestic_equity]


def topix500(issues: list[Issue] | None = None) -> list[Issue]:
    """Return the TOPIX 500 constituents (~492 issues).

    This is the default report universe: broad enough to read the market's
    direction, small enough to price in a few seconds.
    """
    return [i for i in (issues if issues is not None else load_universe()) if i.in_topix500]


def by_code(issues: list[Issue] | None = None) -> dict[str, Issue]:
    """Index the universe by bare code ("7203" -> Issue)."""
    return {i.code: i for i in (issues if issues is not None else load_universe())}


def _normalize(text: str) -> str:
    """Fold a Japanese query for matching.

    NFKC collapses half-width katakana and full-width latin onto their canonical
    forms, so ``ﾄﾖﾀ``, ``トヨタ`` and ``ＴＯＹＯＴＡ`` all become comparable.
    """
    return unicodedata.normalize("NFKC", text).strip().casefold()


def resolve(query: str, issues: list[Issue] | None = None, limit: int = 10) -> list[Issue]:
    """Resolve a Japanese name, kana, or bare code to candidate issues.

    Exact code matches rank first, then name prefix matches, then substring
    matches. Yahoo's own search endpoint returns HTTP 400 for non-ASCII queries,
    which is why this local table exists.

    Args:
        query: A code ("7203"), or a name in kanji/kana/romaji.
        issues: Pre-loaded universe, or None to load it.
        limit: Maximum candidates to return.
    """
    pool = issues if issues is not None else load_universe()
    needle = _normalize(query)
    if not needle:
        return []

    exact_code = [i for i in pool if i.code.casefold() == needle]
    prefix = [i for i in pool if _normalize(i.name).startswith(needle) and i not in exact_code]
    substring = [
        i for i in pool
        if needle in _normalize(i.name) and i not in exact_code and i not in prefix
    ]
    return (exact_code + prefix + substring)[:limit]
