# HKUDS/Vibe-Trading — APIキーゼロで日次市況レポートは作れるか

検証日: 2026-08-06 / 対象コミット: `a1bb7ac` (v0.1.13) / 検証環境: Linux, Python 3.13.14

「APIキーゼロ」の定義: **LLM の APIキーも、市場データの APIキーも一切設定しない**。
環境変数は `env -i`（あるいは `env={}`）で完全に空にした状態で実行し、`.env` は
リポジトリ直下・`agent/`・`~/.vibe-trading/` のいずれにも存在しない。

---

## 1. 結論

**作れる。ただし「Vibe-Trading 単体では作れない」。**

- **データ側は完全にキーレス。** 日次市況レポートに必要な入力は、空の環境で全部
  ライブ取得できた。実測 17/17 銘柄が解決（日本株・米株・香港・金利・為替・
  コモディティ・暗号資産）。
- **散文側はこのリポジトリでは書けない。** 決定論的な日次レポート生成器は
  **存在しない**。`premarket-brief.md` を含む「日次ブリーフ」は全部 LLM 向けの
  プロンプトテンプレートで、それを文章にする経路は例外なく
  `agent/src/providers/llm.py:1005` の `RuntimeError("LANGCHAIN_MODEL_NAME is not set")`
  で止まる。CLI も `vibe-trading run -p hello` が
  `Critical check failed - agent cannot start without a working LLM provider.` で拒否する。

したがって成立条件は一つ: **Claude Code 自身を LLM 役にする。** Vibe-Trading を
MCP サーバとして動かし、Claude Code が 59 個のツールを叩いてデータを集め、
Claude Code が文章を書く。環境にキーは一切入らない。

実際に生成したレポート: [`sample-daily-report_2026-08-06.md`](./sample-daily-report_2026-08-06.md)

## 2. 採用アーキテクチャ

```
Claude Code (LLM)
      │  stdio / MCP
      ▼
vibe-trading-mcp  ── 59 tools ──▶ Yahoo / SEC EDGAR / Eastmoney / Tencent
      │                            OKX / DuckDuckGo / arXiv / Polymarket
      ▼
Claude Code の Write ツールで .md を出力
```

**使うもの:** `get_market_data`, `get_stock_news`, `get_stock_profile`,
`get_sec_filings`, `get_financial_statements`, `web_search`, `read_url`,
`search_symbol`, `screen_market`, `get_sector_info`, `list_skills` / `load_skill`
（`load_skill("report-generate")` はレポート体裁のルーブリックとして有用）、
および純計算系の `analyze_options`, `factor_analysis`, `pattern_recognition`, `backtest`。

**使わないもの:** `run_swarm` / `retry_run`（59 個中この 2 つだけが内部で LLM を
呼び、キーなしでは失敗する。Claude Code 自体がオーケストレータなので不要）、
CLI、API サーバ、スケジューラ。MCP の `write_file` も書込先が
`agent/uploads` 等に制限される（`src/tools/path_utils.py:167-175`）ので、
Claude Code のネイティブ Write を使う。

**Ollama を選ばない理由:** 認証情報という意味ではキーレスだが、`LANGCHAIN_PROVIDER`
と `LANGCHAIN_MODEL_NAME` の明示設定＋デーモン常駐＋モデル pull が要る上、preflight が
`critical=True` で `localhost:11434` を叩くため停止中は起動できない。すでにフロンティア
モデルがセッションを駆動しているのに、ローカルモデルに書かせる意味がない。

## 3. セットアップ手順

**1) Python 3.13 の venv（3.14 では入らない — `pyproject.toml:9` が `>=3.11,<3.14`）**

```bash
cd /home/mtforestlab/projects/vibe-trading
uv venv --python 3.13 .venv
.venv/bin/uv pip install -e .   # または: source .venv/bin/activate && uv pip install -e .
```

**2) `.mcp.json`**（本リポジトリ直下に配置済み）

```json
{
  "mcpServers": {
    "vibe-trading": {
      "command": "/home/mtforestlab/projects/vibe-trading/.venv/bin/vibe-trading-mcp",
      "args": [],
      "env": {
        "VIBE_TRADING_HOME": "/home/mtforestlab/.vibe-trading",
        "VIBE_TRADING_DATA_CACHE": "1"
      }
    }
  }
}
```

`env` は空でも動く。`VIBE_TRADING_DATA_CACHE=1` はレート制限対策として推奨。
なお SEC EDGAR の User-Agent がプレースホルダ（`vibe-trading@example.com`,
`backtest/loaders/sec_edgar_client.py:55-58`）なので、EDGAR を多用するなら
`VIBE_TRADING_SEC_UA="YourOrg/1.0 (you@example.com)"` を足す。これは認証情報ではない。

**3) 承認**

`claude mcp list` で `vibe-trading: ... ⏸ Pending approval` と出る。`claude` を
起動して一度承認すれば接続される。

**4) 疎通確認**

```bash
env -i HOME=$HOME PATH=/usr/bin:/bin .venv/bin/python \
  keyless-verification/collect_bundle.py --end $(date +%F)
```

## 4. 実測結果

### 4.1 サーバ起動とツール登録（空環境）

| 項目 | 結果 |
|---|---|
| `mcp_server` の import | OK, 2.5s |
| stdio JSON-RPC ハンドシェイク | OK — `serverInfo: Vibe-Trading v0.1.13`, proto `2025-06-18`, exit 0 |
| `tools/list` | **59 tools** |
| Claude Code の `.mcp.json` 認識 | OK（承認待ち状態で列挙される） |

環境が本当に空だった証拠: 同一プロセス内で `get_macro_series` が
`{"ok": false, "error": "FRED_API_KEY is not configured"}` を返している。

### 4.2 データ取得（2026-08-06 ライブ、キーゼロ）

`collect_bundle.py` の出力そのまま。**17/17 解決、`_unresolved` なし。**

```
^N225     2026-08-06 close=   65,683.26  1d= -0.93%   ^GSPC   7,739.13  +0.20%
1306.T    2026-08-06 close=      423.70  1d= +0.57%   ^IXIC  26,455.57  +0.35%
7203.T    2026-08-06 close=    2,983.50  1d= +2.37%   ^DJI   54,338.27  -0.02%
6758.T    2026-08-06 close=    3,626.00  1d= +1.71%   ^VIX      15.74   -0.44%
8306.T    2026-08-06 close=    3,574.00  1d= +0.93%   ^HSI   25,530.28  -1.49%
USDJPY=X               158.15  +0.29%    ^TNX  4.64%  GC=F 4,325.90  CL=F 76.00
BTC-USDT  2026-08-04    64,386.20        ETH-USDT 2026-08-04  1,873.71
```

その他、空環境で成功を確認したツール: `get_stock_news`（Yahoo 個別 / Eastmoney
グローバル、当日配信）、`get_stock_profile`（Yahoo、PER・ベータ・目標株価まで）、
`get_sec_filings`（SEC EDGAR、CIK 0000320193 / 8-K 40件）、`web_search`
（DuckDuckGo ほか 6 エンジン）、`search_symbol`、`list_skills`（23.5KB）、
`load_skill`、`read_url`（nikkei.com から 8,036 文字をライブ取得）。

### 4.3 安定性

- `web_search`: 6 連続実行で 6/6 成功、各 5 件。空振り（silent blockade）は再現せず。
- `read_url`: nikkei.com はライブ。**reuters.com は HTTP 403**
  （`AbuseAlleviationError` — 共有インフラ由来のブロック、当環境固有ではない）。
- Eastmoney (`push2delay.eastmoney.com`): `screen_market` / `get_sector_info` が
  15秒タイムアウトを頻発。HK は本日 3 回とも失敗。A株・米株は 3 回目で成功。
  **リトライ必須。**

## 5. 「auto ルーティング」の落とし穴（最重要）

日本株と全指数は `source="auto"` では取れない。原因は
`agent/src/market_data.py:49-54`:

```python
def detect_source(code: str) -> str:
    for pattern, source in _SOURCE_PATTERNS:
        if pattern.match(code):
            return source
    return "tushare"          # ← 未マッチは全部ここ
```

`_SOURCE_PATTERNS`（同 21-46 行）に **`.T`（東証）のパターンも `^INDEX` の
パターンも無い**。さらに `FALLBACK_CHAINS`（`agent/backtest/loaders/registry.py:136-150`）
に `jp_equity` エントリ自体が存在しない。結果、tushare（トークン必須）→ akshare
（当環境から到達不可）と落ちて `_unresolved` になる。

実測（`env -i`, 2026-08-06）:

| symbol | `auto` | `source="yfinance"` |
|---|---|---|
| `7203.T`, `^N225`, `1306.T` | ✗ unresolved | ✓ |
| `^GSPC`, `^DJI`, `^IXIC`, `^VIX`, `^HSI`, `^TNX` | ✗ unresolved | ✓ |
| `USDJPY=X`, `JPY=X`, `GC=F`, `CL=F`, `EWJ.US` | ✓ | — |
| `7203.JP`, `7203`, `N225` | ✗ | ✗（Yahoo に存在しない表記） |

**回避策:** 指数と日本株には常に `source: "yfinance"` を明示する。
`collect_bundle.py` の `BASKETS` はこの前提で書いてある。

**恒久対応（未適用・提案）:** `_SOURCE_PATTERNS` に 2 行足せば `auto` で通る。

```python
    (re.compile(r"^\d{4}\.T$", re.I), "yfinance"),   # 東証
    (re.compile(r"^\^[A-Z0-9]+$", re.I), "yfinance"), # ^N225 / ^GSPC などの指数
```

加えて `FALLBACK_CHAINS` に `"jp_equity": ["yfinance", "yahoo", "local"]` を追加。
upstream への PR 候補。**このフォークにはまだ適用していない**（検証結果を素の
upstream 挙動として残すため）。

## 6. キーなしで失われるもの

**明確に拒否される（59 中 2）**

- `get_macro_series` — `FRED_API_KEY` 必須（`src/tools/fred_macro_tool.py:106`）。
  **マクロブリーフにとってはこれが唯一の実質的な壁。** CPI・失業率・FF金利の
  時系列が取れない。ただし米10年債利回りは `^TNX` で代替可能。
- `iwencai_search` — `VIBE_TRADING_IWENCAI_KEY` 必須。

**登録されるが中身が返らない**

- `get_northbound_flow` — `{"ok": true, "history": []}`。上流（Stock Connect）側の
  提供終了であってキーの問題ではない。レポートに載せないこと。

**キーではないが使えない**

- `trading_*` 8 個中 6 個 — IBKR TWS/Gateway か OAuth トークンが要る。
- 先物 / マクロ系列 / ETF 以外のファンド — キーレスのデータ源がゼロ。
  akshare の `_fetch_one`（`backtest/loaders/akshare_loader.py:134-156`）には
  先物・マクロの分岐が無い（`markets` には宣言されているのに）。
- `baostock` / `pykrx` / `mootdx` — 既定インストールに含まれず、常に unavailable。
- 日本市場の breadth / スクリーニング — `screen_market` の対象は `a` / `us` / `hk` のみ。

**LLM 依存で消える面:** `run_swarm`, `retry_run`, スケジューラ, CLI エージェント,
Web セッション実行系。

## 7. 既知の癖（実測確認済み・レポート品質に直結）

**A. `screen_market` の数値が 100 倍。両市場で確認。**
`src/tools/market_screener_tool.py:71-89` の `_num` が素の `float()` で、Eastmoney の
小数桁フィールド `f1` を要求していない（`:138-146`）。

```
us: {"code":"WYHG","price":12430.0,"change_pct":27209.0}  → 実際は $124.30, +272.09%
a : {"code":"603468","price":3749.0,"change_pct":11472.0} → 実際は ¥37.49, +114.72%
```

`price` / `change` / `change_pct` / `turnover_rate` を **100 で割る**こと。
`get_sector_info` は影響なし。

**B. `_provenance` のソース名が嘘をつく。** `src/market_data.py:174` は「要求した」
ソース名を記録するが、`registry.get_loader_cls_with_fallback`
（`registry.py:229-239`）が別クラスに差し替えている場合がある。キーなしで
`source="finnhub"` を要求すると Yahoo のバーが `"source": "finnhub",
"fallback_used": false` で返る。**レポートに出所を書くときは provenance を信用しない。**

**C. `sentiment` ツールは使わないこと。** LLM ではなく約130語の ASCII 辞書
（`src/tools/sentiment_tool.py:23-49`）。`"low"` が positive と negative の両方に
入っており `"stock hit a new low"` が **+0.33**。日本語・中国語は空白トークナイザ
（`:52-55`）なので全部 0.0。見出しの判断は自分でやる。

**D. Eastmoney の見出しに生の `<em>` が混ざる。**
`src/tools/stock_news_tool.py:88-103` がプロバイダの HTML を除去していない。

**E. `get_stock_news` の個別銘柄は PR ノイズが多い。** AAPL の 1 位が
「Oceania Cruises® Launches Curated Conversations」だった。Yahoo v1 の
キーワード検索であって編集済みフィードではない。`web_search` で補完すべき。
一方、`scope="global"`（Eastmoney）は当日の市況ヘッドラインとして質が高い。

**F. `.SH/.SZ/.BJ/.US/.HK` 以外のサフィックスはニュース取得不可。**
`src/tools/stock_news_tool.py:353-360` がハードエラー。実測:

```
7203.T  → ok=False  "unsupported market for code '7203.T';
                     expected suffix in ('SH','SZ','BJ','US','HK')"
7203.JP → 同上
TM.US   → ok=True, 3 articles
```

**日本株の個別ニュースは直接は取れない。** ADR がある銘柄は `TM.US` のように
米国側コードで代替できる。それ以外は `web_search` / `read_url` で補う。

**G. Yahoo は素のクライアントを 429 で弾く。** 生 `curl` は 429 だが、
`backtest/loaders/_http.py:33-38` が Chrome UA を詐称しているのでリポジトリ経由なら
通る。ただし全ソースにプロセスローカルなスロットル（Eastmoney 1.0s / Yahoo 0.6s /
Stooq 0.6s / Sina 0.5s, `_http.py:46-85`）があり、プロセス間では協調しない。

**H. HOME が書込不可でも MCP 面は縮退しない。** `HOME=/proc/nonexistent` で起動しても
`tools/list` は 59 のまま（実測）。内部の `BaseTool` レジストリ側は goal-store 系の
登録に失敗するが、MCP 経由の本構成には影響しない。

## 8. 未解決 / 次の一手

1. `_SOURCE_PATTERNS` への 2 行パッチ（§5）を当てて `auto` で日本株を通す →
   upstream へ PR。
2. `screen_market` の 100 倍バグ（§7-A）は upstream の実バグ。Issue 起票候補。
3. 日本の経済指標カレンダーと breadth のキーレス調達手段が無い。
   `read_url` + 日経/JPX の公開ページで代替するかを別途検証。
4. GitHub 上の正式フォークは未作成（`gh` CLI もトークンも当環境に無いため）。
   現状はローカルクローン + `upstream` リモートのみ。手順は下記。

```bash
# GitHub の Web UI で HKUDS/Vibe-Trading を Fork したあと:
cd /home/mtforestlab/projects/vibe-trading
git remote add origin git@github.com:<あなたのID>/Vibe-Trading.git
git remote -v            # upstream = HKUDS, origin = 自分のフォーク
git checkout -b keyless-daily-report
git add .mcp.json keyless-verification/
git commit -m "Add keyless daily-report verification harness"
git push -u origin keyless-daily-report
```

## 9. このディレクトリの中身

| ファイル | 内容 |
|---|---|
| `README.md` | 本検証レポート |
| `collect_bundle.py` | キーゼロでレポート用データ一式を集めるスクリプト |
| `sample-daily-report_2026-08-06.md` | 実際に生成した日次市況レポート（成果物の証拠） |
