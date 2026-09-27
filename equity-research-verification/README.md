# anthropics/financial-services の型は、鍵なしの日本株データで動くか

検証日: 2026-09-27(日本時間) / 対象: `keyless-daily-report` ブランチ `fbf4fae` + 本ブランチの修正
検証環境: macOS, Python 3.13.1

検証した組み合わせは2つ。

| 番号 | financial-services 側の型 | vibe-trading-jp 側のデータ | 成果物 |
|---|---|---|---|
| 1 | `equity-research/skills/earnings-analysis` | `jp_earnings`, `get_market_data`, `get_stock_profile`, `get_stock_news`, `read_document` | [日本オラクルの決算メモ](./sample-earnings-note_4716_2026-09-24.md) |
| 3 | `equity-research/skills/catalyst-calendar` | `jp_earnings(mode="calendar")`, JPX銘柄一覧 | [TOPIX Core30 の決算予定表](./sample-catalyst-calendar_2026-09-27.md) |

「鍵なし」の定義は [`keyless-verification/README.md`](../keyless-verification/README.md) と同じ。`env -i` の空環境で実行し、文章化は Claude Code 自身が行った。

---

## 1. 結論

**2つとも無料の範囲で成立する。ただし financial-services の型はそのままでは使えず、3か所の読み替えが必要。**

| 読み替え | 元の型 | 日本株・鍵なしでの置き換え |
|---|---|---|
| 一次資料 | SEC の四半期報告書(10-Q) | TDnet の決算短信と補足資料(PDF を直接取得して読む) |
| 比較の基準 | アナリスト予想の平均 | 会社予想に対する進捗率を主、アナリスト予想を従 |
| 結論の形 | 格付け・目標株価・売買の提案 | 仮説の確認項目と「見立てを捨てる条件」 |

## 2. 見つかった不具合と修正

### JPX 銘柄一覧が取得不能になっていた(修正済み)

JPX が配布形式を `data_j.xls` から `data_j.xlsx` に変更し、旧アドレスは 404 を返す。キャッシュの期限(7日)が切れた環境では、次の機能がすべて止まっていた。

- `screen_market(market="jp")`(騰落・売買代金)
- `search_symbol` の日本語名検索
- 銘柄一覧を使う決算予定の一括取得

修正は `agent/src/tools/_jpx_universe.py`。新旧両方のアドレスを新しい順に試す。読み込みは pandas がファイルの中身から形式を判別するので変更不要。

修正後の実測: 内国株式3,707銘柄、TOPIX 500 は492/492に値付け、`agent/tests/test_jp_support.py` ほか49件通過。

## 3. 実測結果

### 3.1 決算メモ(番号1)に必要なデータ

| 必要なもの | 取得手段 | 結果 |
|---|---|---|
| 決算短信の所在 | `jp_earnings(mode="disclosures", codes=[...])` | 取得可。短信と補足資料の2件、PDF のアドレス付き |
| 決算短信の本文 | PDF を取得 → `read_document` | 取得可。11,348文字、表の数値も読める |
| 補足資料の表 | 同上 | **数値は読めるが行の見出しが欠落する**。Claude Code の Read で PDF を画像として読めば完全に読める |
| 四半期の損益の時系列 | `get_financial_statements(period="quarter")` | **ほぼ空**。損益は1期分・1株利益のみ、キャッシュフローは0件 |
| 年次の損益 | `get_financial_statements(period="annual")` | 取得可。4期分 |
| アナリスト予想との差 | `jp_earnings(mode="surprise")` | 取得可。ただし1株利益のみで、実績値は丸められている(142.0、短信は142.37) |
| 株価の反応 | `get_market_data` | 取得可 |
| 報道の見出し | `get_stock_news` | 取得可。株探の見出し8件 |
| 決算説明会の質疑 | なし | 取得不可 |

### 3.2 決算予定表(番号3)に必要なデータ

| 必要なもの | 結果 |
|---|---|
| 決算予定日 | 31銘柄で失敗0、所要25.5秒。確定20、推定11 |
| 確定と推定の区別 | 取得可。推定日が祝日(11/3)に当たる例を1件確認 |
| 発表時刻 | 取得不可 |
| 日本銀行の会合・経済指標 | `web_search` で公式日程表に到達可。構造化はされない |
| 休場日 | 道具に一覧がない |

### 3.3 鍵なしで動かなかったもの

| 道具 | 症状 |
|---|---|
| `read_url` | 外部の読み取りサービスが匿名利用を拒否(HTTP 401)。回線によって結果が変わる見込み。TDnet の PDF は直接取得で代替できた |
| `read_document` | 既定では決められたフォルダ以外を読めない。`VIBE_TRADING_ALLOWED_FILE_ROOTS` に保存先を指定して解決(認証情報ではない) |

## 4. 設計上の注意点

検証後に対応したものには【対応済み】を付けた。

1. **TDnet は過去39日分しか残らない。** 実測で 2026-08-19 は取得でき、08-18 は0件だった。8月上旬の決算集中期の短信は既に取得できない。決算メモを継続するなら、開示当日に PDF を手元へ保存する運用が必須。【対応済み】`jp_earnings(mode="fetch")` を追加。保存先は `~/.vibe-trading/imports/tdnet/<日付>/` で、`read_document` が設定なしで読める。
2. **期限切れと休場日の区別がつかない。** `jp_earnings` はどちらも「0件・成功」を返す。【対応済み】0件のときは `empty_reason`(`future` / `weekend` / `beyond_retention` / `market_holiday_or_no_filings`)を返す。休場日の一覧(下の7)を足したあとは `market_holiday` と `no_filings` も区別する。
3. **四半期の数値は決算短信が唯一の出所。** Yahoo Finance に頼る設計にすると、日本株の四半期分析は成立しない。
4. **公開リポジトリに PDF を置かない。** 開示資料の著作権は発行会社にある。【対応済み】保存先をリポジトリの外(`~/.vibe-trading/`)にした。
5. **`japan-market` スキルの取引時間が古かった。** 「12:30–15:00」「15:00 の引け」と書かれていたが、東京証券取引所は2024年11月5日から取引終了を15:30に延長している。【対応済み】スキルと日次レポートの指示書を修正。2026-08-19〜09-25 の決算短信・業績予想の修正758件を調べると、15:30ちょうどが35%、それ以降が31%、取引時間中が33%で、15:00の開示31件は現在は取引時間中にあたる。
7. **休場日の一覧がなかった。**【対応済み】`jp_earnings(mode="trading_days")` を追加。出所は日本取引所グループの休業日一覧と内閣府の祝日一覧の2つで、2026〜2027年は両者が完全に一致した(食い違いがあれば返り値に出る)。対象は2000年から2027年末まで。それより先の平日は「営業日」ではなく「不明」と返す。決算予定の推定日が休場日に当たる場合は `on_closed_day` が付く(三菱商事の11/3で確認)。
6. **手順の定着。**【対応済み】今回の読み替えを `agent/src/skills/japan-equity-research/SKILL.md` にまとめた。

## 5. 再現手順

```bash
python3.13 -m venv .venv && .venv/bin/pip install -e .
```

```bash
env -i HOME=$HOME PATH=/usr/bin:/bin VIBE_TRADING_ALLOWED_FILE_ROOTS=/path/to/pdf-folder \
  .venv/bin/python -c "import sys; sys.path.insert(0,'agent'); from src.tools.jp_earnings_tool import JapanEarningsTool; print(JapanEarningsTool().execute(mode='disclosures', date='2026-09-24', codes=['4716']))"
```

## 6. ライセンス

financial-services は Apache License 2.0。本検証では型の構成を参照しただけで、ファイルの複製は含まない。今後スキルの文面を取り込む場合は、同ライセンスの表示と変更箇所の明記が必要。
