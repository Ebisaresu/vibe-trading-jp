"""Japan-market support: routing, classification, and the offline parsers.

Anything needing the network lives in the live probes under
``keyless-verification/``; these tests are pure and run offline.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from backtest.engines._market_hooks import _detect_market, code_currency
from backtest.loaders.registry import FALLBACK_CHAINS
from src.market_data import detect_source
from src.tools._jpx_universe import Issue, resolve
from src.tools.jp_earnings_tool import (
    JapanEarningsTool,
    _bare_code,
    _classify,
    _empty_reason,
    _parse_tdnet_page,
)
from src.tools.stock_news_tool import _fetch_kabutan_news  # noqa: F401 - import guard


class TestSourceRouting:
    """detect_source must send every JPX form to yfinance, and nothing else."""

    @pytest.mark.parametrize(
        "code",
        ["7203.T", "1306.T", "285A.T", "130A.T", "25935.T", "7203.t"],
    )
    def test_jpx_codes_route_to_yfinance(self, code: str) -> None:
        assert detect_source(code) == "yfinance"

    @pytest.mark.parametrize("code", ["AAPL.T", "TOYOTA.T", ".T", "A285.T"])
    def test_non_jpx_codes_do_not_match(self, code: str) -> None:
        # The rule is digit-led on purpose: a letter-led ticker carrying a .T
        # suffix is not a JPX code and must not be claimed.
        assert detect_source(code) != "yfinance"

    @pytest.mark.parametrize("code", ["^N225", "^GSPC", "^TNX", "^HSI"])
    def test_index_symbols_route_to_yfinance(self, code: str) -> None:
        assert detect_source(code) == "yfinance"


class TestMarketClassification:
    def test_jpx_code_is_jp_equity(self) -> None:
        assert _detect_market("7203.T") == "jp_equity"
        assert _detect_market("285A.T") == "jp_equity"

    def test_jp_settles_in_yen(self) -> None:
        assert code_currency("7203.T") == "JPY"

    def test_nikkei_classifies_as_japan_not_the_a_share_default(self) -> None:
        # Regression guard. Classifying .T without also classifying ^N225 would
        # make a Toyota-vs-Nikkei composite fail the currency guard by calling
        # the Nikkei Chinese — a pairing that is the natural shape of a JP
        # report, and that worked (silently, wrongly) before.
        assert _detect_market("^N225") == "jp_equity"
        assert code_currency("^N225") == "JPY"

    def test_us_indices_are_not_swept_into_japan(self) -> None:
        assert _detect_market("^GSPC") == "us_equity"
        assert code_currency("^GSPC") == "USD"

    def test_toyota_and_nikkei_share_one_currency(self) -> None:
        from backtest.engines.composite import _reject_mixed_currency

        _reject_mixed_currency(["7203.T", "^N225"])  # must not raise

    def test_mixing_japan_with_china_is_refused(self) -> None:
        from backtest.engines.composite import _reject_mixed_currency

        # Before .T was classified, this pooled yen into a yuan book silently.
        with pytest.raises(Exception):
            _reject_mixed_currency(["7203.T", "600519.SH"])

    def test_jp_has_a_fallback_chain_that_can_serve_it(self) -> None:
        assert FALLBACK_CHAINS["jp_equity"][0] == "yfinance"


class TestJpxUniverse:
    @staticmethod
    def _issues() -> list[Issue]:
        return [
            Issue("7203", "トヨタ自動車", "プライム（内国株式）", "輸送用機器", "自動車・輸送機", "TOPIX Core30"),
            Issue("3116", "トヨタ紡織", "プライム（内国株式）", "輸送用機器", "自動車・輸送機", "TOPIX Mid400"),
            Issue("285A", "キオクシアホールディングス", "プライム（内国株式）", "電気機器", "電機・精密", "TOPIX Mid400"),
            Issue("1306", "ＮＥＸＴ　ＦＵＮＤＳ　ＴＯＰＩＸ連動型上場投信", "ETF・ETN", "", "", ""),
        ]

    def test_symbol_appends_the_tokyo_suffix(self) -> None:
        assert self._issues()[0].symbol == "7203.T"

    def test_etf_is_not_a_domestic_equity(self) -> None:
        assert self._issues()[3].is_domestic_equity is False
        assert self._issues()[0].is_domestic_equity is True

    def test_topix500_membership(self) -> None:
        assert self._issues()[0].in_topix500 is True
        assert self._issues()[3].in_topix500 is False

    def test_exact_code_outranks_a_name_match(self) -> None:
        assert resolve("7203", self._issues())[0].code == "7203"

    def test_kanji_name_resolves(self) -> None:
        assert resolve("トヨタ自動車", self._issues())[0].code == "7203"

    def test_halfwidth_kana_folds_onto_fullwidth(self) -> None:
        # NFKC folding is what makes ﾄﾖﾀ findable; without it this returns [].
        codes = [i.code for i in resolve("ﾄﾖﾀ", self._issues())]
        assert "7203" in codes and "3116" in codes

    def test_empty_query_returns_nothing(self) -> None:
        assert resolve("   ", self._issues()) == []


_TDNET_PAGE = """
<table>
<tr><th>時刻</th><th>コード</th><th>会社名</th><th>表題</th></tr>
<tr>
  <td>13:40</td><td>65070</td><td>シンフォニア</td>
  <td><a href="140120260807.pdf">2027年3月期 第1四半期決算短信〔日本基準〕（連結）</a></td>
</tr>
<tr>
  <td>13:30</td><td>31060</td><td>クラボウ</td>
  <td><a href="140120260807b.pdf">業績予想の修正に関するお知らせ</a></td>
</tr>
<tr>
  <td>12:00</td><td>593A0</td><td>Ｇ－ティアフォー</td>
  <td><a href="c.pdf">（訂正）「2027年3月期 第1四半期決算短信」の一部訂正</a></td>
</tr>
<tr><td>not-a-time</td><td>x</td><td>y</td><td>z</td></tr>
</table>
"""


class TestTdnetParser:
    def test_only_timestamped_rows_are_taken(self) -> None:
        # The header row and the malformed row must both be dropped; TDnet's
        # class attributes are inconsistent, so parsing keys off row shape.
        assert len(_parse_tdnet_page(_TDNET_PAGE)) == 3

    def test_check_digit_is_stripped(self) -> None:
        assert _bare_code("65070") == "6507"
        assert _bare_code("593A0") == "593A"

    def test_unexpected_code_length_passes_through(self) -> None:
        assert _bare_code("6507") == "6507"

    def test_correction_outranks_earnings(self) -> None:
        # A 訂正 of a 短信 is not a fresh print and must not be counted as one.
        assert _classify("（訂正）「2027年3月期 第1四半期決算短信」の一部訂正") == "correction"
        assert _classify("2027年3月期 第1四半期決算短信〔日本基準〕（連結）") == "earnings"
        assert _classify("業績予想の修正に関するお知らせ") == "guidance"

    def test_rows_carry_code_name_kind_and_url(self) -> None:
        row = _parse_tdnet_page(_TDNET_PAGE)[0]
        assert row["code"] == "6507"
        assert row["name"] == "シンフォニア"
        assert row["kind"] == "earnings"
        assert row["url"].startswith("https://www.release.tdnet.info/inbs/")


class TestJapanEarningsToolValidation:
    def test_unknown_mode_is_rejected(self) -> None:
        payload = json.loads(JapanEarningsTool().execute(mode="bogus"))
        assert payload["ok"] is False

    def test_calendar_requires_codes(self) -> None:
        payload = json.loads(JapanEarningsTool().execute(mode="calendar"))
        assert payload["ok"] is False and "codes" in payload["error"]

    def test_malformed_date_is_rejected(self) -> None:
        payload = json.loads(
            JapanEarningsTool().execute(mode="disclosures", date="7 August")
        )
        assert payload["ok"] is False and "date" in payload["error"]

    def test_sweep_is_capped(self) -> None:
        # One HTTP round-trip per symbol, so an unbounded list must be refused
        # rather than quietly hammering Yahoo.
        payload = json.loads(
            JapanEarningsTool().execute(mode="calendar", codes=[f"{i:04d}" for i in range(200)])
        )
        assert payload["ok"] is False and "cap" in payload["error"]


class TestEmptyReason:
    """An empty TDnet page must say why, because four causes look identical."""

    TODAY = dt.date(2026, 9, 27)

    @pytest.mark.parametrize(
        ("date", "reason"),
        [
            ("20260928", "future"),
            ("20260926", "weekend"),
            # 秋分の日. Indistinguishable from a quiet day without a holiday table.
            ("20260923", "market_holiday_or_no_filings"),
            ("20260828", "market_holiday_or_no_filings"),
            # 31 days back: past the floor, so the zero is not evidence.
            ("20260827", "beyond_retention"),
            ("20260807", "beyond_retention"),
        ],
    )
    def test_reason(self, date: str, reason: str) -> None:
        assert _empty_reason(date, self.TODAY) == reason


class TestFetchMode:
    """mode='fetch' archives filings; the network is stubbed throughout."""

    ROW = {
        "time": "15:30",
        "code": "4716",
        "name": "日本オラクル",
        "title": "2027年5月期第1四半期決算短信〔日本基準〕(非連結)",
        "kind": "earnings",
        "url": "https://www.release.tdnet.info/inbs/140120260916537545.pdf",
    }

    class _Response:
        def __init__(self, body: bytes) -> None:
            self.content = body
            self.status_code = 200

        def raise_for_status(self) -> None:
            return None

    def _stub(self, monkeypatch, tmp_path, body: bytes, calls: list[str]) -> None:
        import backtest.loaders._http as http
        import src.tools.jp_earnings_tool as tool

        monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
        other = dict(self.ROW, code="7203", url=self.ROW["url"].replace("545", "999"))
        monkeypatch.setattr(tool, "_fetch_tdnet", lambda date, pages: ([self.ROW, other], 1, False))

        def fake_get(url: str, **_kwargs):
            calls.append(url)
            return self._Response(body)

        monkeypatch.setattr(http, "throttled_get", fake_get)

    def test_requires_codes(self) -> None:
        payload = json.loads(JapanEarningsTool().execute(mode="fetch", date="2026-09-24"))
        assert payload["ok"] is False
        assert "codes" in payload["error"]

    def test_saves_only_the_requested_issue(self, monkeypatch, tmp_path) -> None:
        calls: list[str] = []
        self._stub(monkeypatch, tmp_path, b"%PDF-1.7 body", calls)
        payload = json.loads(
            JapanEarningsTool().execute(mode="fetch", date="2026-09-24", codes=["4716.T"])
        )
        data = payload["data"]
        assert (data["matched"], data["saved"], data["failed"]) == (1, 1, 0)
        saved = tmp_path / "imports" / "tdnet" / "2026-09-24" / "4716_140120260916537545.pdf"
        assert data["rows"][0]["path"] == str(saved)
        assert saved.read_bytes() == b"%PDF-1.7 body"
        assert calls == [self.ROW["url"]]

    def test_second_run_reads_the_archive(self, monkeypatch, tmp_path) -> None:
        calls: list[str] = []
        self._stub(monkeypatch, tmp_path, b"%PDF-1.7 body", calls)
        tool = JapanEarningsTool()
        tool.execute(mode="fetch", date="2026-09-24", codes=["4716"])
        payload = json.loads(tool.execute(mode="fetch", date="2026-09-24", codes=["4716"]))
        assert payload["data"]["rows"][0]["cached"] is True
        assert len(calls) == 1

    def test_html_answer_is_not_saved_as_a_filing(self, monkeypatch, tmp_path) -> None:
        # TDnet answers a removed filing with a 200 and an HTML page.
        calls: list[str] = []
        self._stub(monkeypatch, tmp_path, b"<html>not found</html>", calls)
        payload = json.loads(
            JapanEarningsTool().execute(mode="fetch", date="2026-09-24", codes=["4716"])
        )
        data = payload["data"]
        assert (data["saved"], data["failed"]) == (0, 1)
        assert not list((tmp_path / "imports").rglob("*.pdf"))
