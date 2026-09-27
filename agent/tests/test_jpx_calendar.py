"""Tokyo trading calendar: the two parsers, the merge, and the date arithmetic.

Pure and offline. The live sources are exercised by the probes recorded in
``equity-research-verification/``.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import src.tools._jpx_calendar as cal
import src.tools.jp_earnings_tool as tool
from src.tools._jpx_calendar import TradingCalendar, _merge, _parse_cao, _parse_jpx
from src.tools.jp_earnings_tool import JapanEarningsTool, _empty_reason, _flag_closed_dates

# Copied from the live page, including the row whose cell closes with </td">.
JPX_HTML = """
<table class="overtable">
<tr><th width="50%">日付</th><th width="50%">名称</th></tr>
<tr><td class="a-center">2026/01/01（木）</td><td class="a-center">元日</td></tr>
<tr><td class="a-center">2026/01/02（金）</td><td class="a-center">休業日</td></tr>
<tr><td class="a-center">2026/05/06（水）</td><td class="a-center">振替休日</td></tr>
<tr><td class="a-center">2026/08/11（火）</td><td class="a-center">山の日</td></tr>
<tr><td class="a-center">2026/09/21（月）</td><td class="a-center">敬老の日</td></tr>
<tr><td class="a-center">2026/09/22（火）</td><td class="a-center">休日※</td></tr>
<tr><td class="a-center">2026/09/23（水）</td><td class="a-center">秋分の日</td></tr>
<tr><td class="a-center">2026/12/31（木）</td"><td class="a-center">休業日</td></tr>
</table>
"""

CAO_CSV = (
    "国民の祝日・休日月日,国民の祝日・休日名称\r\n"
    "2026/1/1,元日\r\n"
    "2026/5/6,休日\r\n"
    "2026/8/11,山の日\r\n"
    "2026/9/21,敬老の日\r\n"
    "2026/9/22,休日\r\n"
    "2026/9/23,秋分の日\r\n"
).encode("cp932")


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar(_merge(_parse_jpx(JPX_HTML), _parse_cao(CAO_CSV)))


class TestParsers:
    def test_jpx_reads_the_malformed_row(self) -> None:
        days = _parse_jpx(JPX_HTML)
        assert days[dt.date(2026, 12, 31)] == "休業日"
        assert len(days) == 8

    def test_jpx_drops_the_footnote_mark(self) -> None:
        assert _parse_jpx(JPX_HTML)[dt.date(2026, 9, 22)] == "休日"

    def test_cao_reads_shift_jis_and_unpadded_dates(self) -> None:
        days = _parse_cao(CAO_CSV)
        assert days[dt.date(2026, 9, 23)] == "秋分の日"
        assert dt.date(2026, 1, 1) in days


class TestMerge:
    def test_year_end_is_added_by_rule(self) -> None:
        table = _merge({}, _parse_cao(CAO_CSV))
        assert table["days"]["2026-12-31"]["sources"] == ["year_end_rule"]
        assert "2026-01-02" in table["days"]

    def test_the_specific_name_wins_over_the_generic_one(self) -> None:
        table = _merge(_parse_jpx(JPX_HTML), _parse_cao(CAO_CSV))
        assert table["days"]["2026-05-06"]["name"] == "振替休日"
        assert table["days"]["2026-01-01"]["name"] == "元日"

    def test_agreeing_sources_report_no_disagreement(self) -> None:
        assert _merge(_parse_jpx(JPX_HTML), _parse_cao(CAO_CSV))["disagreements"] == []

    def test_a_closure_only_one_source_names_is_reported(self) -> None:
        jpx = dict(_parse_jpx(JPX_HTML))
        jpx[dt.date(2026, 6, 10)] = "臨時休業日"
        assert _merge(jpx, _parse_cao(CAO_CSV))["disagreements"] == ["2026-06-10"]

    def test_dates_before_the_floor_are_left_out(self) -> None:
        table = _merge({}, {dt.date(1985, 1, 15): "成人の日", dt.date(2026, 1, 1): "元日"})
        assert "1985-01-15" not in table["days"]
        assert table["coverage"]["first"] == "2026-01-01"


class TestStatus:
    @pytest.mark.parametrize(
        ("day", "trading", "reason"),
        [
            (dt.date(2026, 9, 18), True, None),
            (dt.date(2026, 9, 22), False, "holiday"),
            (dt.date(2026, 9, 26), False, "weekend"),
            (dt.date(2026, 12, 31), False, "year_end"),
            (dt.date(2026, 1, 2), False, "year_end"),
            # Neither source has reached 2028: unknown, not open.
            (dt.date(2028, 1, 5), None, "outside_coverage"),
            # A weekend is closed whether or not the year is covered.
            (dt.date(2028, 1, 8), False, "weekend"),
        ],
    )
    def test_status(self, calendar: TradingCalendar, day, trading, reason) -> None:
        state = calendar.status(day)
        assert (state["is_trading_day"], state["reason"]) == (trading, reason)

    def test_silver_week_is_stepped_over(self, calendar: TradingCalendar) -> None:
        assert calendar.next_trading_day(dt.date(2026, 9, 18)) == dt.date(2026, 9, 24)
        assert calendar.previous_trading_day(dt.date(2026, 9, 24)) == dt.date(2026, 9, 18)

    def test_stepping_out_of_coverage_returns_none(self, calendar: TradingCalendar) -> None:
        assert calendar.next_trading_day(dt.date(2026, 12, 30)) is None

    def test_closed_weekdays_excludes_weekends(self, calendar: TradingCalendar) -> None:
        listed = calendar.closed_weekdays(dt.date(2026, 9, 19), dt.date(2026, 9, 27))
        assert [row["date"] for row in listed] == ["2026-09-21", "2026-09-22", "2026-09-23"]


class TestCache:
    def test_a_stale_cache_is_used_when_the_refresh_fails(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
        table = _merge(_parse_jpx(JPX_HTML), _parse_cao(CAO_CSV))
        table["fetched_at"] = 0
        cal._write_cache(table)
        monkeypatch.setattr(cal, "_fetch", lambda: None)
        loaded = cal.load_calendar()
        assert loaded is not None and loaded.stale is True
        assert loaded.is_trading_day(dt.date(2026, 9, 23)) is False

    def test_no_cache_and_no_network_is_none(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
        monkeypatch.setattr(cal, "_fetch", lambda: None)
        assert cal.load_calendar() is None


class TestEarningsToolUsesTheCalendar:
    TODAY = dt.date(2026, 9, 27)

    @pytest.mark.parametrize(
        ("date", "reason"),
        [
            ("20260923", "market_holiday"),
            ("20260921", "market_holiday"),
            # Past retention, and still known to be a holiday.
            ("20260811", "market_holiday"),
            ("20260925", "no_filings"),
            ("20260807", "beyond_retention"),
            ("20260926", "weekend"),
        ],
    )
    def test_empty_reason(self, calendar: TradingCalendar, date: str, reason: str) -> None:
        assert _empty_reason(date, self.TODAY, calendar) == reason

    def test_an_estimate_on_a_holiday_is_flagged(self, calendar: TradingCalendar) -> None:
        rows = [
            {"symbol": "8058.T", "earnings_dates": ["2026-09-23"], "is_estimate": True},
            {"symbol": "9983.T", "earnings_dates": ["2026-09-24"], "is_estimate": False},
        ]
        _flag_closed_dates(rows, calendar)
        assert rows[0]["on_closed_day"] == [
            {"date": "2026-09-23", "reason": "holiday", "name": "秋分の日"}
        ]
        assert "on_closed_day" not in rows[1]

    def test_trading_days_mode(self, monkeypatch, calendar: TradingCalendar) -> None:
        monkeypatch.setattr(tool, "_load_calendar", lambda: calendar)
        payload = json.loads(JapanEarningsTool().execute(mode="trading_days", date="2026-09-22"))
        data = payload["data"]
        assert data["is_trading_day"] is False
        assert data["previous_trading_day"] == "2026-09-18"
        assert data["next_trading_day"] == "2026-09-24"

    def test_trading_days_without_a_calendar_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(tool, "_load_calendar", lambda: None)
        payload = json.loads(JapanEarningsTool().execute(mode="trading_days", date="2026-09-22"))
        assert payload["ok"] is False
