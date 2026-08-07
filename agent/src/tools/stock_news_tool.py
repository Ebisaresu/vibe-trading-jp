"""Read-only news tool: per-stock and global financial headlines.

Two public, no-auth news surfaces are wrapped behind one BaseTool contract:

* China A-share (and general China-market finance) headlines come from
  Eastmoney's free ``search-api`` news-list endpoint. Like every Eastmoney
  surface it rate-limits by source IP, so the request routes through the frozen,
  IP-throttled :mod:`backtest.loaders.eastmoney_client` rather than touching the
  host directly.
* US / HK headlines come from Yahoo Finance's public v1 search-news surface via
  the frozen, IP-throttled :mod:`backtest.loaders.yahoo_client`.

The tool never re-implements provider plumbing and never issues an un-throttled
request: every outbound call goes through a frozen client.

Scopes:

* ``stock`` (default) — headlines for a single security named by ``code``.
* ``global`` — broad market headlines, no ``code`` required.

A failure for one upstream is reported as an error envelope; the tool never
raises out of :meth:`StockNewsTool.execute`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from backtest.loaders import eastmoney_client, yahoo_client

from src.agent.tools import BaseTool

logger = logging.getLogger(__name__)

# Eastmoney free news search endpoint (JSON list of CMS articles). It is the
# same surface the site's search box calls; no auth, IP-throttled.
_EM_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"

# A-share / China-market suffixes that route to the Eastmoney news surface.
_EM_SUFFIXES = ("SH", "SZ", "BJ")
# Suffixes that route to Yahoo's search-news surface.
_YAHOO_SUFFIXES = ("US", "HK")
# Japan (JPX). Yahoo's v1 search endpoint is NAME-keyed and cannot be handed a
# JPX code — searching "7203" returns nothing useful and a kanji name makes it
# reply HTTP 400. The yfinance news feed is SYMBOL-keyed, so it takes "7203.T"
# directly, including post-2024 alphanumeric codes like "285A.T".
_JP_SUFFIXES = ("T",)

# Coverage is skewed toward globally-followed names: 7974.T returns 9/10
# on-topic headlines, 8306.T closer to 2/10, because the feed is
# English-language and US-editorial. Kabutan backfills the domestic names.
_KABUTAN_URL = "https://kabutan.jp/stock/news"
_KABUTAN_HOST_KEY = "kabutan"
_KABUTAN_MIN_INTERVAL = 1.0
_KABUTAN_TIMEOUT = 20.0

# Default broad-market query used when ``scope='global'`` carries no code.
_GLOBAL_QUERY = "财经"

# Bounds so a noisy upstream can never return an unbounded payload.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
# Per-article body trim so the envelope stays compact for the LLM.
_SNIPPET_CHARS = 280


def _clamp_limit(raw: Any) -> int:
    """Coerce a caller-supplied ``limit`` into the supported ``1.._MAX_LIMIT`` range.

    Args:
        raw: The raw ``limit`` value from the tool arguments (any type).

    Returns:
        An integer in ``[1, _MAX_LIMIT]``, falling back to ``_DEFAULT_LIMIT``
        when ``raw`` is missing or non-numeric.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_LIMIT
    if value < 1:
        return 1
    return min(value, _MAX_LIMIT)


def _suffix_of(code: str) -> str:
    """Return the upper-cased exchange suffix of a symbol, or ``""`` when none."""
    if "." not in code:
        return ""
    return code.rpartition(".")[2].strip().upper()


def _bare_query(code: str) -> str:
    """Strip any exchange suffix to the bare code used as a news search term."""
    return code.strip().split(".", 1)[0].strip()


def _snippet(text: Any) -> str:
    """Trim an article body to a bounded plain-text snippet.

    Args:
        text: Raw body/summary value (any type).

    Returns:
        A whitespace-collapsed snippet capped at ``_SNIPPET_CHARS`` characters,
        or ``""`` when ``text`` is not a usable string.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SNIPPET_CHARS:
        return collapsed
    return collapsed[:_SNIPPET_CHARS].rstrip() + "…"


def _decode_jsonp(payload: Any) -> Any:
    """Decode an Eastmoney response that may arrive JSON or JSONP-wrapped.

    The search endpoint usually returns a JSON object, but can echo a
    ``callback(...)`` JSONP envelope. A single outer call wrapper is stripped
    before parsing.

    Args:
        payload: The decoded body from the throttled client (``dict`` already, or
            a raw ``str`` when JSONP-wrapped).

    Returns:
        The decoded object, or ``None`` when nothing parseable is found.
    """
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str):
        return None
    start = payload.find("(")
    end = payload.rfind(")")
    inner = payload[start + 1 : end] if start != -1 and end > start else payload
    try:
        return json.loads(inner)
    except (ValueError, TypeError):
        return None


def _em_article(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one Eastmoney CMS article into a compact, named record.

    Args:
        raw: A single article dict from ``result.cmsArticleWebOld``.

    Returns:
        A flat ``{title, url, source, published, snippet}`` record.
    """
    return {
        "title": _snippet(raw.get("title")),
        "url": raw.get("url"),
        "source": raw.get("mediaName"),
        "published": raw.get("date"),
        "snippet": _snippet(raw.get("content")),
    }


def _fetch_eastmoney_news(query: str, limit: int) -> list[dict[str, Any]]:
    """Fetch China-market news headlines for a query from Eastmoney.

    Args:
        query: Free-text search term (bare code or keyword).
        limit: Maximum number of articles to return.

    Returns:
        A capped list of compact article records; empty when none.

    Raises:
        requests.RequestException: Network failure, propagated to the caller.
        requests.HTTPError: Non-2xx response status.
        ValueError: Body is not valid JSON.
    """
    param = json.dumps(
        {
            "uid": "",
            "keyword": query,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": limit,
                }
            },
        },
        ensure_ascii=False,
    )
    payload = eastmoney_client.get_json(
        _EM_NEWS_URL,
        params={"cb": "", "param": param, "_": "0"},
    )
    decoded = _decode_jsonp(payload)
    if not isinstance(decoded, dict):
        return []
    result = decoded.get("result")
    if not isinstance(result, dict):
        return []
    articles = result.get("cmsArticleWebOld")
    if not isinstance(articles, list):
        return []
    return [_em_article(a) for a in articles if isinstance(a, dict)][:limit]


def _yahoo_article(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one Yahoo search-news item into the shared article shape.

    Args:
        raw: A single news dict from :func:`yahoo_client.search_news`.

    Returns:
        A flat ``{title, url, source, published, snippet}`` record.
    """
    published = None
    try:
        timestamp = float(raw.get("providerPublishTime"))
        published = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return {
        "title": _snippet(raw.get("title")),
        "url": raw.get("link"),
        "source": raw.get("publisher"),
        "published": published,
        "snippet": _snippet(raw.get("summary")),
    }


def _fetch_yahoo_news(query: str, limit: int) -> list[dict[str, Any]]:
    """Fetch US/HK news articles for a query via Yahoo search.

    Args:
        query: Free-text search term (bare ticker or keyword).
        limit: Maximum number of records to return.

    Returns:
        A capped list of compact article records; empty when none.

    Raises:
        requests.RequestException: Network/HTTP failure, propagated to caller.
    """
    articles = yahoo_client.search_news(query, limit)
    return [
        _yahoo_article(article) for article in articles if isinstance(article, dict)
    ][:limit]


def _fetch_yfinance_news(symbol: str, limit: int) -> list[dict[str, Any]]:
    """Fetch Japanese-listing news from the symbol-keyed yfinance feed.

    Args:
        symbol: Full symbol including suffix, e.g. ``"7203.T"``.
        limit: Maximum number of records to return.

    Returns:
        Compact article records; empty when the feed has nothing.
    """
    import yfinance as yf

    out: list[dict[str, Any]] = []
    for item in (yf.Ticker(symbol).news or [])[:limit]:
        if not isinstance(item, dict):
            continue
        # yfinance nests the payload under "content" on current versions and
        # returns it flat on older ones.
        body = item.get("content") if isinstance(item.get("content"), dict) else item
        provider = body.get("provider")
        source = provider.get("displayName") if isinstance(provider, dict) else provider
        url = body.get("canonicalUrl") or body.get("clickThroughUrl") or {}
        out.append({
            "title": str(body.get("title") or "").strip(),
            "url": (url.get("url") if isinstance(url, dict) else url) or body.get("link"),
            "source": source or "yahoo",
            "published": body.get("pubDate") or body.get("displayTime"),
            "snippet": _snippet(body.get("summary") or body.get("description") or ""),
        })
    return [a for a in out if a["title"]]


def _strip_html(fragment: str) -> str:
    """Strip tags and collapse whitespace, including the &nbsp; kabutan emits."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment).replace("\xa0", " ")).strip()


_KABUTAN_TABLE_RE = re.compile(
    # Class TOKEN match: the real attribute is "s_news_list mgbt0", so anchoring
    # on the exact value finds nothing.
    r'<table[^>]*class="[^"]*\bs_news_list\b[^"]*"[^>]*>(.*?)</table>', re.S
)
_KABUTAN_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_KABUTAN_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_KABUTAN_CTG_RE = re.compile(r'<div[^>]*class="[^"]*\bnewslist_ctg\b[^"]*"[^>]*>(.*?)</div>', re.S)
_KABUTAN_LINK_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _fetch_kabutan_news(code: str, limit: int) -> list[dict[str, Any]]:
    """Fetch Japanese-language issuer news and 開示 from kabutan.

    The yfinance feed is English and US-editorial, so it covers globally-traded
    names well and domestic financials/utilities poorly. Kabutan carries the
    Japanese wire and the issuer's own disclosures, which is what the thin half
    of the coverage actually needs.

    Args:
        code: Bare JPX code without a suffix, e.g. ``"8306"``.
        limit: Maximum number of records to return.
    """
    from backtest.loaders._http import throttled_get

    resp = throttled_get(
        _KABUTAN_URL, host_key=_KABUTAN_HOST_KEY, min_interval=_KABUTAN_MIN_INTERVAL,
        timeout=_KABUTAN_TIMEOUT, params={"code": code},
    )
    resp.raise_for_status()
    table = _KABUTAN_TABLE_RE.search(resp.content.decode("utf-8", "replace"))
    if not table:
        return []

    out: list[dict[str, Any]] = []
    for row in _KABUTAN_ROW_RE.findall(table.group(1)):
        link = _KABUTAN_LINK_RE.search(row)
        if not link:
            continue
        href, title_html = link.groups()
        title = _strip_html(title_html)
        if not title:
            continue
        stamp = _KABUTAN_TIME_RE.search(row)
        category = _KABUTAN_CTG_RE.search(row)
        out.append({
            "title": title,
            "url": href if href.startswith("http") else f"https://kabutan.jp{href}",
            "source": f"kabutan/{_strip_html(category.group(1))}" if category else "kabutan",
            "published": stamp.group(1) if stamp else None,
            "snippet": "",
        })
        if len(out) >= limit:
            break
    return out


def _merge_articles(groups: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Merge provider results newest-first, dropping duplicate titles."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in [a for group in groups for a in group]:
        key = article["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(article)
    # Undated rows sort last rather than crashing the comparison.
    merged.sort(key=lambda a: (a.get("published") or ""), reverse=True)
    return merged[:limit]


class StockNewsTool(BaseTool):
    """Read-only per-stock and global financial news headlines."""

    name = "get_stock_news"
    description = (
        "Fetch recent financial news headlines, read-only and no auth. Markets: "
        "China A-share (SH/SZ/BJ) returns Eastmoney news ARTICLES "
        "(title/url/source/published/snippet) under 'articles'. US (.US) and Hong "
        "Kong (.HK) return Yahoo Finance news articles with the same fields. "
        "Japan (.T) merges the English yfinance wire with kabutan's Japanese "
        "wire and issuer 開示, newest first — alphanumeric JPX codes such as "
        "285A.T work. Use scope 'stock' with a 'code', or scope 'global' (no "
        "code) for broad China-market finance articles. For Japanese earnings "
        "filings specifically, jp_earnings reads TDnet across the whole market. "
        'Example: {"code": "600519.SH", "scope": "stock", "limit": 10}.'
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Symbol whose news to fetch, e.g. '600519.SH', 'AAPL.US', "
                    "'00700.HK'. Required when scope='stock'; ignored when "
                    "scope='global'. The exchange suffix selects the upstream: "
                    "SH/SZ/BJ -> Eastmoney, US/HK -> Yahoo Finance."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["stock", "global"],
                "description": (
                    "'stock' (default) for one security named by 'code'; "
                    "'global' for broad China-market finance headlines."
                ),
                "default": "stock",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Maximum number of headlines to return (1-50). Default 20."
                ),
                "default": _DEFAULT_LIMIT,
            },
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        """Fetch news headlines for one stock or the broad market.

        Args:
            **kwargs: ``scope`` ('stock' | 'global', default 'stock'), ``code``
                (required when scope='stock'), and optional ``limit`` (1-50).

        Returns:
            A JSON string envelope. On success:
            ``{"ok": true, "market": <market>, "source": <source>,
            "data": {...}}``. On failure: ``{"ok": false, "error": "..."}``.
        """
        scope = kwargs.get("scope", "stock")
        if scope not in ("stock", "global"):
            return self._error(
                f"invalid scope: {scope!r}; expected 'stock' or 'global'"
            )

        limit = _clamp_limit(kwargs.get("limit"))

        if scope == "global":
            return self._run_global(limit)
        return self._run_stock(kwargs.get("code"), limit)

    def _run_global(self, limit: int) -> str:
        """Fetch broad China-market headlines from Eastmoney.

        Args:
            limit: Maximum number of headlines.

        Returns:
            A success or error JSON envelope.
        """
        try:
            articles = _fetch_eastmoney_news(_GLOBAL_QUERY, limit)
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure as envelope
            logger.warning("global news fetch failed: %s", exc)
            return self._error(f"eastmoney news fetch failed: {exc}")
        return self._ok(
            "global", "eastmoney", {"scope": "global", "articles": articles}
        )

    def _run_stock(self, code_arg: Any, limit: int) -> str:
        """Fetch single-security headlines, routing by exchange suffix.

        Args:
            code_arg: Raw ``code`` argument (any type).
            limit: Maximum number of headlines.

        Returns:
            A success or error JSON envelope.
        """
        if not isinstance(code_arg, str) or not code_arg.strip():
            return self._error(
                "missing required parameter: code (required when scope='stock')"
            )

        code = code_arg.strip()
        suffix = _suffix_of(code)
        query = _bare_query(code)
        if not query:
            return self._error(f"invalid code: {code!r}")

        if suffix in _EM_SUFFIXES:
            return self._stock_via_eastmoney(code, query, limit)
        if suffix in _YAHOO_SUFFIXES:
            return self._stock_via_yahoo(code, query, limit)
        if suffix in _JP_SUFFIXES:
            return self._stock_via_japan(code, query, limit)
        return self._error(
            f"unsupported market for code {code!r}; expected suffix in "
            f"{_EM_SUFFIXES + _YAHOO_SUFFIXES + _JP_SUFFIXES}"
        )

    def _stock_via_eastmoney(self, code: str, query: str, limit: int) -> str:
        """Fetch A-share headlines from Eastmoney for one code."""
        try:
            articles = _fetch_eastmoney_news(query, limit)
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure as envelope
            logger.warning("eastmoney news fetch failed for %s: %s", code, exc)
            return self._error(f"eastmoney news fetch failed: {exc}")
        return self._ok(
            "a_share",
            "eastmoney",
            {"scope": "stock", "code": code, "articles": articles},
        )

    def _stock_via_yahoo(self, code: str, query: str, limit: int) -> str:
        """Fetch US/HK news articles from Yahoo for one code."""
        market = "hk" if _suffix_of(code) == "HK" else "us"
        try:
            articles = _fetch_yahoo_news(query, limit)
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure as envelope
            logger.warning("yahoo news fetch failed for %s: %s", code, exc)
            return self._error(f"yahoo news fetch failed: {exc}")
        return self._ok(
            market,
            "yahoo",
            {"scope": "stock", "code": code, "articles": articles},
        )

    def _stock_via_japan(self, code: str, query: str, limit: int) -> str:
        """Fetch Japanese-listing news from yfinance and kabutan together.

        The two feeds are complementary rather than redundant — yfinance is
        English wire coverage weighted toward globally-followed names, kabutan
        is the Japanese wire plus the issuer's own 開示 — so both are queried and
        merged newest-first. Either failing alone is logged and skipped; only a
        total failure becomes an error envelope.
        """
        groups: list[list[dict[str, Any]]] = []
        failures: list[str] = []

        for label, fetch in (
            ("yfinance", lambda: _fetch_yfinance_news(code, limit)),
            ("kabutan", lambda: _fetch_kabutan_news(query, limit)),
        ):
            try:
                groups.append(fetch())
            except Exception as exc:  # noqa: BLE001 - one dead feed must not kill the other
                logger.warning("%s news fetch failed for %s: %s", label, code, exc)
                failures.append(f"{label}: {exc}")

        if not groups:
            return self._error(f"japan news fetch failed ({'; '.join(failures)})")

        articles = _merge_articles(groups, limit)
        payload: dict[str, Any] = {"scope": "stock", "code": code, "articles": articles}
        if failures:
            payload["partial"] = failures
        return self._ok("jp", "yfinance+kabutan", payload)

    @staticmethod
    def _ok(market: str, source: str, data: dict[str, Any]) -> str:
        """Render a success envelope as a JSON string.

        Args:
            market: Market label (e.g. ``"a_share"``, ``"us"``, ``"global"``).
            source: Upstream provider name (``"eastmoney"`` or ``"yahoo"``).
            data: The payload mapping.

        Returns:
            ``{"ok": true, "market": ..., "source": ..., "data": ...}`` as JSON.
        """
        return json.dumps(
            {"ok": True, "market": market, "source": source, "data": data},
            ensure_ascii=False,
        )

    @staticmethod
    def _error(message: str) -> str:
        """Render a failure envelope as a JSON string.

        Args:
            message: Human-readable error text.

        Returns:
            ``{"ok": false, "error": message}`` as a JSON string.
        """
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)
