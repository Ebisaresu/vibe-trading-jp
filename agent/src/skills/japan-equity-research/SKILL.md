---
name: japan-equity-research
description: Single-company research on a Tokyo-listed name from keyless sources — a post-earnings memo built from the 決算短信 itself, an upcoming-earnings calendar that separates confirmed dates from guessed ones, and a quarter-by-quarter check of what the print confirmed. Measures against company guidance (進捗率) rather than sell-side consensus, and ends in conditions to watch, never in a rating, a price target or a buy/sell call. Use after a Japanese company reports, when building a watch-list calendar, or when a published report needs a company-level section. Load `japan-market` first.
category: analysis
---
# Japanese Equity Research: Earnings Memo, Calendar, Thesis Check

## Overview

Three deliverables, one discipline: every figure comes from a tool call made in
this run, and the conclusion is a list of things to check next, not an
instruction to trade.

The shapes are adapted from the `equity-research` plugin in
anthropics/financial-services (Apache License 2.0). That plugin assumes a US
issuer and a paid data terminal. Three substitutions make it work for Tokyo
with no keys, and each one changes the analysis, not just the wording:

| The US template assumes | For a Tokyo listing use | Why |
|---|---|---|
| Form 10-Q from SEC EDGAR | 決算短信 and 補足資料 PDFs from TDnet | Yahoo carries almost no quarterly statements for a JPX listing. The filing is the only source for the quarter's revenue and operating income. |
| Beat or miss against sell-side consensus | 進捗率 against the company's own full-year guidance, consensus second | Company guidance is the reference point in Japan. Consensus for most names rests on two or three analysts. |
| Rating, price target, trade idea | Check items and the condition that would retire the view | Giving buy/sell calls for a fee can fall under investment-advisory regulation in Japan. A report is not advice; keep it that way. |

Read `japan-market` before this skill. It covers the session boundary, price
limits and filing types that the steps below rely on.

## Workflow A: Post-Earnings Memo

### Step 1. Find and save the filing — on the day

```
jp_earnings(mode="disclosures", date="2026-09-24", codes=["4716"])
jp_earnings(mode="fetch",       date="2026-09-24", codes=["4716"])
read_document(file_path=<path from fetch>)
```

`fetch` writes the PDFs under `imports/tdnet/<date>/`, which `read_document`
accepts without configuration. **TDnet drops a filing after about a month**
(39 days when measured), so a print that was not saved on the day cannot be
read later. A company usually files two documents together: the 決算短信 and a
補足資料 or 説明資料. Save both.

If `disclosures` returns no rows, read `empty_reason` before writing anything.
`beyond_retention` means the date could not be seen. It does not mean nothing
was filed. `market_holiday` means the exchange was closed.

### Step 2. Read the two documents differently

- **決算短信** extracts cleanly as text. Page 1 carries the summary table, the
  balance sheet summary, the dividend and the full-year guidance, including
  whether guidance was revised (直近に公表されている業績予想からの修正の有無).
- **補足資料** is usually a spreadsheet printed to PDF. Text extraction returns
  the numbers and **drops the row labels**, so a column of figures arrives with
  nothing saying which is cloud and which is hardware. Read those pages as
  images instead, or reconcile every figure against the segment table in the
  決算短信 before using it.

Units: a filing prints 百万円 and truncates (百万円未満切捨て).
`get_financial_statements` returns absolute yen. Never mix the two in one table.

### Step 2b. Read every page, and leave a record of it

```
.venv/bin/python scripts/source_sheet.py 6323 2026-10-08
.venv/bin/python scripts/source_sheet.py 6323 2026-10-08 --find 受注残高
```

The first call writes two things: the page texts, next to the PDFs and
outside the repository, and a reading sheet at
`reports/companies/<code>/<date>.sources.md` with one row per page. Fill the
要点 cell of every row by reading the page — a figure and a subject, not the
heading copied — and the four closing bullets (the question the memo asks,
the pages that answer it, what the company does not state and you will have
to compute, what the summary page did not say). Write the memo from the
reading sheet, not from the summary page.

Why: in three reviews the analyst persona found, every time, the answer to
the memo's own question sitting two pages past where the memo stopped reading
(the customer mix on 添付9, the segment split of the revised guidance on slide
18, a loss-provision reversal in the balance sheet). A reading sheet with a 【】 left
in it fails Step 6b.

A document taken from the company site (a deck TDnet did not carry) goes in
the same folder as `<code>_<kind>.pdf`, for example `9983_決算説明資料.pdf`;
the kind in the filename is what the memo will cite.

### Step 3. Build the comparison

| Row | Formula | Source |
|---|---|---|
| Year-on-year change | filing states it; recompute to confirm | 決算短信 |
| 進捗率 this year | cumulative result ÷ full-year guidance | 決算短信 |
| 進捗率 last year | same quarter last year ÷ last year's full-year actual | 決算短信, `get_financial_statements(period="annual")` |
| Against consensus | filing EPS ÷ consensus EPS − 1 | `jp_earnings(mode="surprise")` |

- When guidance is a range, give 進捗率 against both ends.
- A quarter's growth rate set against a full-year growth guide compares three
  months with twelve. Say so in the table.
- Yahoo rounds the actual (142.0 where the filing says 142.37). Recompute the
  surprise from the filing and note the difference.
- State how many analysts stand behind a consensus figure. `get_stock_profile`
  reports `eps_analysts`; two is not a consensus.

### Step 4. Decompose the change, and make it tie

Break the change in operating income by segment or by cost line. **The parts
must sum exactly to the total.** If they do not, a row is missing or a label is
wrong; find it before writing prose. A bridge that ties is the check that the
補足資料 was read correctly.

### Step 5. Read the price reaction

```
get_market_data(codes=["4716.T", "1306.T", "^N225"], start_date=<30 trading days back>)
```

1. **Which session reacted.** A filing stamped 15:30 or later is priced the next
   trading day. One stamped earlier is priced the same day. The next trading
   day is not always tomorrow: take it from
   `jp_earnings(mode="trading_days", date=<filing date>)`.
2. **Price limit.** Take the band from the previous close and compare it with
   the day's high or low. If the limit was reached, describe a limit move, not a
   percentage.

| Previous close (yen) | Limit (yen, either way) |
|---|---|
| under 1,000 | 150 (under 700: 100; under 500: 80; under 200: 50; under 100: 30) |
| 1,000 to under 1,500 | 300 |
| 1,500 to under 2,000 | 400 |
| 2,000 to under 3,000 | 500 |
| 3,000 to under 5,000 | 700 |
| 5,000 to under 7,000 | 1,000 |
| 7,000 to under 10,000 | 1,500 |
| 10,000 to under 15,000 | 3,000 |
| 15,000 to under 20,000 | 4,000 |
| 20,000 to under 30,000 | 5,000 |
| 30,000 to under 50,000 | 7,000 |

3. **Noise.** Compare the move with the mean absolute daily change over the
   prior 20 sessions. Below that mean, call it noise.
4. **Market.** Set the move beside `1306.T` (labelled as the TOPIX proxy) and
   `^N225` for the same day.
5. **Cause.** Quote a headline from `get_stock_news` only when it states the
   cause. Otherwise report the move and the filing as two facts.

### Step 6. Write the memo

Fixed order. Keep it to what a reader can verify. When the memo is for
publication, `reports/article-rules.md` in this repository decides the paywall
position, the title form and the cross-references; it takes precedence here.

0. Three-line summary — result, guidance status, price reaction
1. Results and what they are measured against
2. Where the change came from (the bridge from Step 4)
3. Financial position — explain any large balance-sheet move; a special
   dividend shrinks net assets without any deterioration in the business
4. Price reaction (Step 5)
5. Check items — what this print confirmed, what the next one must show, and
   the condition under which the view is dropped
6. Next dates — with `is_estimate` stated
7. What could not be sourced
8. Sources with links, and the disclaimer

Cite the page inline wherever a figure or a quotation comes from a filing:
`(決算説明資料18ページ)`, `(決算短信 添付8ページ)`, `(同5ページ)` for the same
document, and `(百万円、決算短信)` in a table heading for the whole table. A
figure you computed says so in the same sentence (筆者の計算).

### Step 6b. Reconcile the memo with the pages it cites

```
.venv/bin/python scripts/fact_check.py reports/companies/6323/2026-10-08.md
.venv/bin/python scripts/fact_check.py reports/companies/6323/2026-10-08.md --where 1,038億円
```

For every citation the script takes the figures in that sentence or table
row and looks for each on the cited page, reconciling units (797億円 against
79,733 百万円; +22.8% against an index of 122.8). It fails on a page the
document does not have, a cited page carrying none of the sentence's figures,
a document the archive does not hold, and a reading sheet with 【】 left in it. A
single figure missing from the page is a warning that says where else the
document carries it, or that nothing does — which means the figure is either
your own arithmetic (say so) or a transcription error. `--where` finds the
page for a figure you have not cited yet. `scripts/note_export.py` runs the
same check; it stops on a failure.

## Workflow B: Earnings Calendar

```
jp_earnings(mode="calendar", codes=[...])      # at most 60 symbols per call
```

About one second per symbol. Build the list from the JPX universe
(`screen_market(market="jp")`) or from the names already in the report.

- Print **確定 / 推定** on every row. `is_estimate: true` is a date Yahoo
  extrapolated from last year, not a schedule.
- A row carrying `on_closed_day` has a date on which the exchange is shut. No
  company schedules a release for a holiday, so print that row as 日付未定
  rather than repeating the date.
- List the weekday closures inside the window from
  `jp_earnings(mode="trading_days", days=<window>)`, so a gap in the calendar
  reads as a holiday and not as a quiet week.
- Sort by date, and split "next two weeks" from the rest.
- Replace an estimated date once the company announces. Companies file the
  announcement on TDnet (決算発表日のお知らせ), and `disclosures` will carry it.
- State what the calendar leaves out: time of day, central bank meetings,
  economic releases, shareholder meetings.

## Workflow C: Thesis Check

For a name followed across quarters, keep a table and update it at each print.

| Check item | This print | What the next print must show |
|---|---|---|
| A measurable claim | the figure, with last year's beside it | the figure that would confirm or break it |

- Every item must be able to fail. "Growth continues" cannot; "cloud revenue
  growth stays above 30%" can.
- Note the base effect. Growth of 19% against a prior-year fall of 27% is a
  different fact from 19% against a prior-year rise.
- End with one retirement condition: the observation that would drop the view.
- The upstream `thesis-tracker` skill ends in hold / add / trim / exit. For
  anything that will be published, stop at the check table.

## Hard Rules

1. Every figure is the return value of a tool call made in this run. No figure
   from memory, and no typical value standing in for a missing one.
2. No rating, no price target, no buy or sell call, no position sizing.
   Analyst targets that a tool returns are not repeated either.
3. Company guidance first, consensus second, and the analyst count beside any
   consensus figure.
4. A decomposition that does not sum to its total is not published.
5. A limit move is described as a limit move, with the band arithmetic shown.
6. Estimated dates are labelled as estimates on every appearance.
7. Filing PDFs stay in the local archive. They are the issuer's documents and
   are not committed to a public repository; link to TDnet instead and note
   that the link expires.
8. What could not be sourced is listed, not filled.
9. Every page of every saved filing is read before the memo is written, and
   the reading sheet (Step 2b) shows it. The memo is not published until
   `scripts/fact_check.py` (Step 6b) passes.

## Known Gaps

- **Earnings call content** — no keyless transcript source
- **Consensus for revenue and operating income** — EPS only
- **Quarterly statement history** — one or two periods at most from Yahoo; the
  archive of saved filings is what builds a time series
- **Time of day of an upcoming release** — inferable only from the company's
  past filing times on TDnet, within the retention window
- **Trading days beyond next year** — the calendar ends where its sources do
  (`coverage.last`). A later weekday is reported as unknown, not as open.
