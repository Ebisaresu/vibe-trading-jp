---
name: japan-market
description: Japanese equity market (JPX/TSE) analysis — Nikkei/TOPIX divergence, USD/JPY sensitivity, the 15:30 close against the US overnight session, TDnet earnings cadence, price limits and lot sizes, and the Prime/Standard/Growth segments. Use for any Tokyo-listed name or a Japanese daily market report.
category: asset-class
---
# Japanese Equity Market Analysis

## Overview

Tokyo is a two-index market with a currency overlay, trading in a session that
closes before its dominant external driver opens. Most analytical mistakes made
about it come from one of three places: reading the Nikkei as if it were the
market, ignoring the yen, or comparing a 15:30 JST close against a US number
from the wrong day. This skill covers those, plus the JPX mechanics that change
what a price move means.

## Core Concepts

### 1. The Nikkei is not the market

| Index | Construction | What it tells you |
|-------|-------------|-------------------|
| Nikkei 225 | **Price-weighted**, 225 names | Dominated by a handful of high-priced shares. A single name can move it. |
| TOPIX | Capitalisation-weighted, all Prime issues | Actual market direction and breadth |
| TOPIX Core30 / Large70 / Mid400 | Size cuts of TOPIX | Core30 + Large70 + Mid400 = TOPIX 500, the practical liquid universe |

The two diverge routinely — the Nikkei can fall while TOPIX and most individual
names rise, because a few index-heavy tech and retail names carry it. **When
they disagree, say so and lead with breadth**, not with the Nikkei's percentage.

*Data note:* there is no keyless source for the TOPIX index level. `^N225`
resolves; `998405.T` returns 404 and `^TPX` returns nothing. Use `1306.T`, the
NEXT FUNDS TOPIX ETF, as a **labelled proxy**. Never compute an NT ratio
(Nikkei ÷ TOPIX) from an ETF price — the units are unrelated. Report the NT
ratio as unavailable rather than approximating it.

### 2. The yen is a market-level variable, not a sector one

Roughly 40% of TOPIX revenue is earned abroad. A weaker yen mechanically lifts
reported earnings for exporters and compresses margins for importers and
domestic retail. Two consequences for a daily report:

- A Tokyo session that moved less than USD/JPY did is usually a currency story,
  not an equity one. Check `USDJPY=X` before attributing a move to news.
- Exporters (自動車, 電気機器, 精密機器) and domestic names (小売業, 電気・ガス業,
  陸運業) should be read as two different books on a big yen day.

Intervention risk matters at the extremes: MoF verbal intervention typically
precedes action, and a sharp yen reversal with no equity news is often the tell.

### 3. The 15:30 seam

The cash session runs 09:00–11:30 and 12:30–15:30 JST (the 前場/後場 split), then
closes **before** the US opens. The close moved from 15:00 to 15:30 on
5 November 2024, with a closing auction whose order-acceptance period runs
15:25–15:30; anything written before that date, and much written since, still
says 15:00. So:

- Tokyo's close reflects the *previous* US session. Comparing today's Nikkei
  close against today's S&P move is an off-by-one-session error.
- Overnight US moves and USD/JPY drift are priced at the next open, which is why
  Tokyo gaps more than it trends intraday.
- A filing stamped 15:00 now lands **inside** the session and is priced the same
  day. Only a filing at 15:30 or later is a next-session event. Read the TDnet
  timestamp before saying which session reacted.
- Nikkei futures (SGX, CME) trade through the gap and are the honest read on
  "where Tokyo would be now", though this repo has no keyless futures feed.

### 4. JPX mechanics that change what a move means

- **Lot size is 100 shares** for essentially all domestic equities. A ¥40,000
  share means a ¥4m minimum ticket, which is why high-priced names have thinner
  retail participation.
- **Daily price limits (値幅制限)** cap moves by price band. A name at ストップ高 or
  ストップ安 has not "risen 17%" — it has hit an administrative ceiling with unmet
  demand behind it, and the real move continues next session. Always describe a
  limit move as a limit move.
- **Segments**: プライム (largest, strictest governance), スタンダード, グロース.
  A Growth-segment move on light volume is not market-wide information.
- **Alphanumeric codes**: since 2024 new listings carry codes like `285A`
  (Kioxia). Four-digit-only assumptions silently drop ~178 issues, several of
  them among the most traded.
- **Settlement is T+2**; the ex-dividend concentration around the March and
  September fiscal ends distorts late-month breadth.

### 5. Earnings cadence

The Japanese fiscal year overwhelmingly ends **31 March**, so Q1 短信 land in late
July–early August, and the annual print in early-to-mid May. During those weeks
several hundred filings a day arrive — clustered at the 15:30 close and the
hours after it, which is why the next morning gaps.

Not all of them wait for the close. Across the 758 決算短信 and 業績予想の修正
filed between 19 August and 25 September 2026, 35% were stamped exactly 15:30,
31% later, and 33% arrived during trading hours — 11:00, 13:00 and 12:00 being
the common intraday slots. That window is off-peak, so treat the split as
indicative; the point is that an intraday print is ordinary, not an exception.

Two filing types matter and are often confused:

- **決算短信** — the earnings release itself.
- **業績予想の修正** — a revision to the company's own full-year guidance. In
  Japan this frequently moves the stock *more* than the quarter did, because
  company guidance, not sell-side consensus, is the reference point.
- A **（訂正）** prefix marks a correction to an earlier filing. It is not a new
  print and must not be counted as one.

## Tooling

| Question | Call |
|----------|------|
| Prices, indices, FX | `get_market_data(codes=["^N225","7203.T","USDJPY=X"])` — routes to yfinance automatically |
| Breadth, most-traded, sectors | `screen_market(market="jp", sort_by="amount")` — TOPIX 500 by default, ~5s |
| What filed today | `jp_earnings(mode="disclosures", kind="earnings")` — TDnet, whole market |
| Guidance revisions | `jp_earnings(mode="disclosures", kind="guidance")` |
| Who reports next | `jp_earnings(mode="calendar", codes=[...])` — check `is_estimate` |
| Was it a beat | `jp_earnings(mode="surprise", codes=[...])` |
| Company news | `get_stock_news(code="7203.T")` — merges the English wire with kabutan's Japanese wire and 開示 |
| Fundamentals | `get_stock_profile(ticker="7203.T")`, `get_financial_statements(code="7203.T")` — absolute JPY |
| Name → code | `search_symbol(query="トヨタ自動車")` — kanji and half-width kana both resolve |

## Method

- Lead with breadth and the Nikkei/TOPIX relationship, not the Nikkei alone.
- Check USD/JPY before attributing any market-level move to equity news.
- State the session boundary explicitly when citing an overseas number.
- Separate what moved from why. Only assert a cause when a retrieved source
  states it; otherwise report the move and the coincident filing as two facts.
- A move inside the instrument's recent daily range is noise. Say so instead of
  narrating it.
- Breadth counts from `screen_market` are approximate — they carry a coverage
  denominator and an as-of timestamp. Quote them as approximate.

## Known Gaps

These have no keyless source. Declare them as gaps rather than estimating:

- **TOPIX index level** (use `1306.T` as a labelled ETF proxy; no NT ratio)
- **Nikkei VI** — never substitute realised volatility for it
- **JPX options chains** — `get_options_chain("7203.T")` returns `ok: true` with
  an empty chain. Zero length means absence of *data*, not absence of activity.
- **Economic-release calendar** and **BOJ meeting schedule** — reachable only as
  unstructured pages via `web_search` / `read_url`
- **Investor-type flows** (JPX weekly 投資部門別売買状況)
- **Per-issue turnover value** — `screen_market` reports close × volume, labelled
  `amount_estimate`
