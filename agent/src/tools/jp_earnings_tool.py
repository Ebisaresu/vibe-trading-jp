"""Japanese earnings as EVENTS — what was just filed, what is coming, was it a beat.

A daily report can miss an earnings print in three different ways, so this tool
covers three surfaces, plus the one that keeps a print readable afterwards. All
keyless:

``mode="disclosures"``
    TDnet (適時開示情報閲覧サービス), the exchange's own filing feed. This is the
    only surface that answers "what came out today" for the whole market —
    決算短信 and, just as report-relevant, 業績予想の修正. Paginated at 100 rows.

``mode="calendar"``
    Yahoo ``calendarEvents`` swept over a symbol list. Answers "who reports next"
    and, critically, flags whether the date is confirmed or Yahoo's estimate.
    Never present an estimated date as scheduled.

``mode="surprise"``
    Yahoo ``earningsHistory``. EPS actual against consensus with a surprise
    percentage, so a print can be characterised rather than merely noted.

``mode="fetch"``
    Saves the filing PDFs for named issues into the local archive and returns
    their paths. Two facts make this a mode rather than a convenience. Yahoo
    carries almost no quarterly statements for a JPX listing, so the 決算短信 is
    the only source for the quarter's revenue and operating income. And TDnet
    drops a filing after about a month, so a print that was not saved on the day
    cannot be read later. The archive sits under ``imports/``, which
    ``read_document`` already accepts.

TDnet quirks worth knowing before reading the parser:

* Codes carry a trailing check digit — ``65070`` is issue ``6507``, ``593A0`` is
  ``593A``. Five characters in, four out.
* The daily page is ``I_list_<page>_<YYYYMMDD>.html``, one-indexed and
  zero-padded to three digits. Pages exist only for business days.
* Rows are plain ``<td>`` cells with no stable class attributes, so the parser
  keys off row structure and column order, not CSS classes.
* A date outside the retention window answers exactly like a market holiday: no
  page, no error. Measured on 2026-09-27, the oldest date still served was
  2026-08-19, 39 days back.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

_TDNET_URL = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date}.html"
_TDNET_HOST_KEY = "tdnet"
_TDNET_MIN_INTERVAL = 1.0
_TDNET_TIMEOUT = 20.0
_TDNET_MAX_PAGES = 25  # 100 rows/page; peak 決算 days run past 1000 filings.

# TDnet publishes no retention figure. A month is the floor every measurement
# has cleared, so an empty page older than this is reported as unknowable
# rather than as a day on which nothing was filed.
_TDNET_RETENTION_FLOOR_DAYS = 30

_FETCH_CAP = 10  # One PDF per filing; a peak day has hundreds.
_FETCH_MAX_BYTES = 30 * 1024 * 1024
_PDF_MAGIC = b"%PDF"
_PDF_NAME_RE = re.compile(r"([0-9A-Za-z]+)\.pdf$")

_JST = dt.timezone(dt.timedelta(hours=9))

_YAHOO_SWEEP_CAP = 60  # calendarEvents is one HTTP round-trip per symbol.

# Filing-title classification. Order matters: a 訂正 of a 短信 is a correction,
# not a fresh print, and must not be counted as a new earnings event.
_KIND_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("correction", re.compile(r"訂正")),
    ("earnings", re.compile(r"決算短信")),
    ("guidance", re.compile(r"業績予想|配当予想|予想の修正")),
    ("earnings_material", re.compile(r"決算説明|決算補足")),
    ("buyback", re.compile(r"自己株式")),
    ("dividend", re.compile(r"配当")),
)

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _text(html: str) -> str:
    """Strip tags and collapse whitespace, including the &nbsp; TDnet emits."""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html).replace("\xa0", " ")).strip()


def _classify(title: str) -> str:
    for kind, rx in _KIND_RULES:
        if rx.search(title):
            return kind
    return "other"


def _bare_code(tdnet_code: str) -> str:
    """Drop TDnet's trailing check digit: ``65070`` -> ``6507``.

    Anything that is not the expected 5-character form is returned unchanged, so
    a format change upstream degrades to an unmatched code rather than a
    silently truncated one.
    """
    code = tdnet_code.strip()
    return code[:-1] if len(code) == 5 else code


def _parse_tdnet_page(html: str) -> list[dict[str, Any]]:
    """Extract disclosure rows from one TDnet daily page.

    Rows are identified structurally: a disclosure row is a ``<tr>`` whose first
    cell is a ``HH:MM`` timestamp. TDnet's own class attributes are inconsistent
    across pages, so keying off them drops rows silently.
    """
    out: list[dict[str, Any]] = []
    for row_html in _ROW_RE.findall(html):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 4:
            continue
        time_text = _text(cells[0])
        if not _TIME_RE.match(time_text):
            continue
        title_html = cells[3]
        title = _text(title_html)
        if not title:
            continue
        href = _HREF_RE.search(title_html)
        out.append({
            "time": time_text,
            "code": _bare_code(_text(cells[1])),
            "name": _text(cells[2]),
            "title": title,
            "kind": _classify(title),
            "url": ("https://www.release.tdnet.info/inbs/" + href.group(1)) if href else None,
        })
    return out


def _fetch_tdnet(date: str, max_pages: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Fetch every TDnet page for ``date`` (YYYYMMDD).

    Returns:
        ``(rows, pages_read, truncated)``. ``truncated`` is True when the page
        cap was hit, so a caller can say so rather than imply full coverage.
    """
    from backtest.loaders._http import throttled_get

    rows: list[dict[str, Any]] = []
    pages_read = 0
    for page in range(1, max_pages + 1):
        url = _TDNET_URL.format(page=page, date=date)
        resp = throttled_get(
            url, host_key=_TDNET_HOST_KEY, min_interval=_TDNET_MIN_INTERVAL,
            timeout=_TDNET_TIMEOUT,
        )
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        page_rows = _parse_tdnet_page(resp.content.decode("utf-8", "replace"))
        pages_read += 1
        if not page_rows:
            break
        rows.extend(page_rows)
        # A short page is the last page.
        if len(page_rows) < 100:
            break
    else:
        return rows, pages_read, True
    return rows, pages_read, False


def _empty_reason(date: str, today: dt.date) -> str:
    """Say why a TDnet date came back with no rows.

    An empty page has four causes that read identically on the wire, and a
    report that treats them alike will state "no filings" for a day it simply
    could not see.

    Args:
        date: The requested date as YYYYMMDD.
        today: Today's date in Tokyo.

    Returns:
        ``future``, ``weekend``, ``beyond_retention`` or
        ``market_holiday_or_no_filings``.
    """
    day = dt.datetime.strptime(date, "%Y%m%d").date()
    if day > today:
        return "future"
    if day.weekday() >= 5:
        return "weekend"
    if (today - day).days > _TDNET_RETENTION_FLOOR_DAYS:
        return "beyond_retention"
    return "market_holiday_or_no_filings"


def _archive_dir(date: str) -> Path:
    """Return the archive folder for one filing date (YYYYMMDD)."""
    from src.config.paths import get_runtime_root

    return get_runtime_root() / "imports" / "tdnet" / f"{date[:4]}-{date[4:6]}-{date[6:]}"


def _save_pdf(url: str, target: Path) -> tuple[int, bool]:
    """Download one TDnet PDF to ``target`` unless it is already there.

    Returns:
        ``(size_in_bytes, was_cached)``.

    Raises:
        ValueError: If the body is not a PDF or exceeds the size cap. TDnet
            answers a removed filing with an HTML page and status 200, so the
            status code alone cannot be trusted.
    """
    from backtest.loaders._http import throttled_get

    if target.is_file() and target.stat().st_size > 0:
        return target.stat().st_size, True
    resp = throttled_get(
        url, host_key=_TDNET_HOST_KEY, min_interval=_TDNET_MIN_INTERVAL,
        timeout=_TDNET_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.content
    if not body.startswith(_PDF_MAGIC):
        raise ValueError("response is not a PDF; the filing may have left TDnet")
    if len(body) > _FETCH_MAX_BYTES:
        raise ValueError(f"PDF is {len(body)} bytes, over the {_FETCH_MAX_BYTES} cap")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so an interrupted download cannot be mistaken for a
    # saved filing on the next run.
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(target)
    return len(body), False


def _sweep_calendar(symbols: list[str]) -> list[dict[str, Any]]:
    """Read the next earnings date for each symbol from Yahoo calendarEvents."""
    from backtest.loaders.yahoo_client import get_quote_summary

    out: list[dict[str, Any]] = []
    for symbol in symbols:
        entry: dict[str, Any] = {"symbol": symbol}
        try:
            summary = get_quote_summary(symbol, ["calendarEvents"]) or {}
            earnings = (summary.get("calendarEvents") or {}).get("earnings") or {}
            dates = [
                d.get("fmt") for d in (earnings.get("earningsDate") or [])
                if isinstance(d, dict) and d.get("fmt")
            ]
            entry["earnings_dates"] = dates
            # Yahoo guesses a date from last year's cadence when the company has
            # not announced one. Reporting a guess as scheduled is the failure
            # mode this flag exists to prevent.
            entry["is_estimate"] = bool(earnings.get("isEarningsDateEstimate"))
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not sink the sweep
            logger.debug("calendarEvents failed for %s: %s", symbol, exc)
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return out


def _sweep_surprise(symbols: list[str], quarters: int) -> list[dict[str, Any]]:
    """Read recent EPS actual-vs-estimate history for each symbol."""
    from backtest.loaders.yahoo_client import get_quote_summary

    def _raw(node: Any) -> Any:
        return node.get("raw") if isinstance(node, dict) else None

    out: list[dict[str, Any]] = []
    for symbol in symbols:
        entry: dict[str, Any] = {"symbol": symbol}
        try:
            summary = get_quote_summary(symbol, ["earningsHistory"]) or {}
            history = (summary.get("earningsHistory") or {}).get("history") or []
            entry["history"] = [
                {
                    "quarter": (h.get("quarter") or {}).get("fmt"),
                    "eps_actual": _raw(h.get("epsActual")),
                    "eps_estimate": _raw(h.get("epsEstimate")),
                    "surprise_pct": (
                        round(_raw(h.get("surprisePercent")) * 100, 2)
                        if _raw(h.get("surprisePercent")) is not None else None
                    ),
                }
                for h in history[-quarters:]
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug("earningsHistory failed for %s: %s", symbol, exc)
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return out


def _normalize_symbols(raw: Any) -> list[str]:
    """Accept a list or comma string; append .T to bare JPX codes."""
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(s).strip() for s in raw]
    else:
        return []
    out = []
    for item in items:
        if not item:
            continue
        out.append(item if "." in item else f"{item}.T")
    return out


class JapanEarningsTool(BaseTool):
    """Japanese earnings events: TDnet filings, upcoming dates, EPS surprise."""

    name = "jp_earnings"
    description = (
        "Japanese (JPX/TSE) earnings events. Read-only, no auth. Three modes:\n"
        "  mode='disclosures' — every TDnet 適時開示 filed on a date, across the "
        "whole market. Filter with kind='earnings' (決算短信), 'guidance' "
        "(業績予想の修正), 'correction' (訂正), 'earnings_material', 'buyback', "
        "'dividend', or codes=['7203'] for specific issues. This is the only way "
        "to see an unexpected print from a name not on a watch list.\n"
        "  mode='calendar' — next earnings date per symbol, with is_estimate "
        "telling you whether the company confirmed it or Yahoo guessed.\n"
        "  mode='surprise' — recent EPS actual vs consensus and surprise %.\n"
        "  mode='fetch' — save the filing PDFs for codes=[...] on a date into the "
        "local archive and return their paths for read_document. The 決算短信 is "
        "the only source for a Japanese quarter's revenue and operating income, "
        "and TDnet drops filings after about a month, so fetch on the day.\n"
        "Symbols may be '7203' or '7203.T'; alphanumeric codes like '285A' work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["disclosures", "calendar", "surprise", "fetch"],
                "description": "Which earnings surface to read.",
            },
            "date": {
                "type": "string",
                "description": (
                    "mode='disclosures' and 'fetch'. Filing date as YYYY-MM-DD "
                    "or YYYYMMDD. Defaults to today (JST). TDnet publishes pages "
                    "for business days only and keeps them about a month."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "mode='disclosures' and 'fetch'. Filter by classification: "
                    "earnings, guidance, correction, earnings_material, buyback, "
                    "dividend, other. Omit for everything."
                ),
            },
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Symbols or bare JPX codes. Required for 'calendar', "
                    "'surprise' and 'fetch'; optional filter for 'disclosures'."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return (default 50).",
            },
            "quarters": {
                "type": "integer",
                "description": "mode='surprise' only. Quarters of history (default 4).",
            },
        },
        "required": ["mode"],
    }

    def execute(self, **kwargs: Any) -> str:
        mode = str(kwargs.get("mode") or "").strip()
        if mode == "disclosures":
            return self._disclosures(kwargs)
        if mode in ("calendar", "surprise"):
            return self._yahoo_sweep(mode, kwargs)
        if mode == "fetch":
            return self._fetch(kwargs)
        return self._error(
            f"unknown mode {mode!r}; expected 'disclosures', 'calendar', "
            "'surprise' or 'fetch'"
        )

    @staticmethod
    def _resolve_date(kwargs: dict[str, Any]) -> tuple[str | None, str | None]:
        """Return ``(YYYYMMDD, None)`` or ``(None, error message)``."""
        raw_date = str(kwargs.get("date") or "").strip()
        if not raw_date:
            # TDnet is a Tokyo service; "today" must be today in Tokyo, or a
            # report generated in the evening UTC asks for tomorrow's page.
            return dt.datetime.now(_JST).strftime("%Y%m%d"), None
        digits = raw_date.replace("-", "").replace("/", "")
        if not re.fullmatch(r"\d{8}", digits):
            return None, f"invalid date {raw_date!r}; expected YYYY-MM-DD"
        try:
            dt.datetime.strptime(digits, "%Y%m%d")
        except ValueError:
            return None, f"invalid date {raw_date!r}; no such calendar day"
        return digits, None

    def _disclosures(self, kwargs: dict[str, Any]) -> str:
        date, problem = self._resolve_date(kwargs)
        if date is None:
            return self._error(problem or "invalid date")

        try:
            rows, pages, truncated = _fetch_tdnet(date, _TDNET_MAX_PAGES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tdnet fetch failed for %s: %s", date, exc)
            return self._error(f"tdnet fetch failed: {exc}")

        total = len(rows)
        kind = str(kwargs.get("kind") or "").strip()
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        codes = {c.split(".")[0].strip() for c in _normalize_symbols(kwargs.get("codes"))}
        if codes:
            rows = [r for r in rows if r["code"] in codes]

        limit = self._limit(kwargs, default=50)
        matched = len(rows)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1

        data: dict[str, Any] = {
            "mode": "disclosures",
            "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "filings_on_date": total,
            "matched": matched,
            "returned": min(matched, limit),
            "pages_read": pages,
            "truncated": truncated,
            "kind_counts": counts,
            "rows": rows[:limit],
        }
        if total == 0:
            data["empty_reason"] = _empty_reason(date, dt.datetime.now(_JST).date())
        return self._ok("jp", "tdnet", data)

    def _fetch(self, kwargs: dict[str, Any]) -> str:
        codes = {c.split(".")[0].strip() for c in _normalize_symbols(kwargs.get("codes"))}
        if not codes:
            return self._error("mode='fetch' requires 'codes'")
        date, problem = self._resolve_date(kwargs)
        if date is None:
            return self._error(problem or "invalid date")

        try:
            rows, _pages, _truncated = _fetch_tdnet(date, _TDNET_MAX_PAGES)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tdnet fetch failed for %s: %s", date, exc)
            return self._error(f"tdnet fetch failed: {exc}")

        total = len(rows)
        kind = str(kwargs.get("kind") or "").strip()
        rows = [
            r for r in rows
            if r["code"] in codes and r["url"] and (not kind or r["kind"] == kind)
        ]
        matched = len(rows)
        folder = _archive_dir(date)

        saved: list[dict[str, Any]] = []
        for row in rows[:_FETCH_CAP]:
            name = _PDF_NAME_RE.search(row["url"])
            entry = {k: row[k] for k in ("time", "code", "name", "title", "kind", "url")}
            if name is None:
                entry["error"] = "unexpected filing address"
                saved.append(entry)
                continue
            target = folder / f"{row['code']}_{name.group(1)}.pdf"
            try:
                size, cached = _save_pdf(row["url"], target)
                entry.update({"path": str(target), "bytes": size, "cached": cached})
            except Exception as exc:  # noqa: BLE001 - one bad filing must not sink the rest
                logger.debug("tdnet pdf failed for %s: %s", row["url"], exc)
                entry["error"] = str(exc)[:200]
            saved.append(entry)

        data: dict[str, Any] = {
            "mode": "fetch",
            "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "filings_on_date": total,
            "matched": matched,
            "saved": sum(1 for e in saved if "path" in e),
            "failed": sum(1 for e in saved if "error" in e),
            "skipped_over_cap": max(0, matched - _FETCH_CAP),
            "archive": str(folder),
            "rows": saved,
        }
        if total == 0:
            data["empty_reason"] = _empty_reason(date, dt.datetime.now(_JST).date())
        return self._ok("jp", "tdnet", data)

    def _yahoo_sweep(self, mode: str, kwargs: dict[str, Any]) -> str:
        symbols = _normalize_symbols(kwargs.get("codes"))
        if not symbols:
            return self._error(f"mode={mode!r} requires 'codes'")
        if len(symbols) > _YAHOO_SWEEP_CAP:
            return self._error(
                f"{len(symbols)} symbols exceeds the {_YAHOO_SWEEP_CAP} cap for "
                f"mode={mode!r} (one HTTP call per symbol); split the request"
            )

        if mode == "calendar":
            rows = _sweep_calendar(symbols)
        else:
            quarters = kwargs.get("quarters")
            quarters = quarters if isinstance(quarters, int) and 1 <= quarters <= 12 else 4
            rows = _sweep_surprise(symbols, quarters)

        limit = self._limit(kwargs, default=len(rows))
        return self._ok("jp", "yahoo", {
            "mode": mode,
            "requested": len(symbols),
            "failed": sum(1 for r in rows if "error" in r),
            "rows": rows[:limit],
        })

    @staticmethod
    def _limit(kwargs: dict[str, Any], *, default: int) -> int:
        raw = kwargs.get("limit")
        if isinstance(raw, int) and raw > 0:
            return min(raw, 500)
        return default

    @staticmethod
    def _ok(market: str, source: str, data: dict[str, Any]) -> str:
        return json.dumps(
            {"ok": True, "market": market, "source": source, "data": data},
            ensure_ascii=False,
        )

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)
