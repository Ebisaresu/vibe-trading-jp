"""Reconciling a memo's citations with the saved filings."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


source_sheet = _load("source_sheet")
fact_check = _load("fact_check")
Page, Doc, Figure = source_sheet.Page, source_sheet.Doc, fact_check.Figure


def _docs() -> list[Doc]:
    tanshin = Doc(
        "決算短信", "決算短信", "決算短信", "6323_a.pdf",
        [
            Page(1, "1", "サマリー", "売上高 79,733 21.5 営業利益 23,096 42.4", 4),
            Page(2, "2", "注記", "発行済株式数 176,400,000株", 1),
            Page(10, "添付8", "補足情報", "半導体関連装置 39,348 124.4\n合計 41,995 121.1\n受注残高 103,863 229.8", 6),
        ],
    )
    deck = Doc(
        "決算説明資料", "決算説明資料", "説明資料", "6323_b.pdf",
        [
            Page(5, "5", "業績", "半導体関連装置のQ2受注高 785億円で前年同期比3.2倍 受注残高 1,038億円", 4),
            Page(9, "9", "営業利益", "人件費1,011、研究開発費22、のれん償却額1,489、その他販管費614", 4),
            Page(14, "14", "SEI", "905 1,320 +$414M 単位:百万ドル", 3),
            Page(20, "20", "図", "", 0),
        ],
    )
    return [tanshin, deck]


def test_figures_keep_units_and_drop_dates_counts_and_page_numbers() -> None:
    figs = fact_check.figures("2026年10月8日の開示で、第2四半期の売上高797億円(+21.5%)は6名のアナリスト予想を上回り、1株91.33円だった(決算説明資料5ページ)。")
    assert [(f.value, f.unit) for f in figs] == [(797.0, "億円"), (21.5, "%"), (91.33, "円")]
    assert fact_check.figures("3つの対策と、2交代と、10/9の反応") == []
    assert [f.value for f in fact_check.figures("| 売上高 | 79,733 | 65,602 |")] == [79733.0, 65602.0]


def test_the_same_figure_twice_is_one_check() -> None:
    assert len(fact_check.figures("55%にとどまる。55%は筆者の計算")) == 1


def test_matches_reconciles_units() -> None:
    nums = {79733.0, 103863.0, 121.1, 122.8, 91.33, 414.0, 1320.0, 1102.0}
    assert fact_check.matches(Figure(797.0, "億円", "797億円"), nums)  # floor of 百万円
    assert fact_check.matches(Figure(1038.0, "億円", "1,038億円"), nums)
    assert fact_check.matches(Figure(1039.0, "億円", "1,039億円"), nums)  # rounded
    assert not fact_check.matches(Figure(800.0, "億円", "800億円"), nums)
    assert fact_check.matches(Figure(21.1, "%", "21.1%"), nums)  # index form 121.1
    assert fact_check.matches(Figure(22.8, "%", "22.8%"), nums)
    assert fact_check.matches(Figure(91.3, "円", "91.3円"), nums)  # one decimal shown
    assert fact_check.matches(Figure(4.14, "億ドル", "4.14億ドル"), nums)  # 414百万ドル
    assert fact_check.matches(Figure(13.2, "億ドル", "13.20億ドル"), nums)
    assert fact_check.matches(Figure(110200.0, None, "110,200"), nums)  # 百万円 table cell vs 1,102億円 slide
    assert not fact_check.matches(Figure(2.1, "倍", "2.1倍"), nums)


def test_a_sentence_with_two_citations_is_one_scope_and_同_follows_the_last_document() -> None:
    text = "営業CFは87億円で、純利益159億円の55%だった(営業CFは決算説明資料14ページ、純利益は同18ページ)。\n\n受注残高は1,038億円(同5ページ)。"
    scopes, findings = fact_check.scopes(text)
    assert findings == []
    assert [len(s.targets) for s in scopes] == [2, 1]
    assert [t.labels for t in scopes[0].targets] == [["14"], ["18"]]
    assert scopes[1].targets[0].doc == "決算説明資料"


def test_同_inherits_添付_from_the_previous_citation() -> None:
    text = "3つのことを行った(決算短信 添付15〜16ページ)。差は資本剰余金から減らした(同15ページ)。"
    scopes, _ = fact_check.scopes(text)
    assert scopes[0].targets[0].labels == ["添付15", "添付16"]
    assert scopes[1].targets[0].labels == ["添付15"]


def test_同_without_an_antecedent_is_reported() -> None:
    _, findings = fact_check.scopes("受注残高は1,038億円(同5ページ)。")
    assert [f.rule for f in findings] == ["出典"]


def test_a_table_heading_citation_covers_the_rows_and_a_chart_fence_is_skipped() -> None:
    text = "### 中間期実績(百万円、決算短信)\n\n| 項目 | 当期 |\n|---|---:|\n| 売上高 | 79,733 |\n\n```chart\nrows:\n  - [a, 24699]\n```\n\n要約: 受注残高は1,038億円(決算説明資料5ページ)。"
    scopes, _ = fact_check.scopes(text)
    scopes = [s for s in scopes if fact_check.figures(s.text)]  # the header row carries no figure
    assert [s.text[:4] for s in scopes] == ["| 売上", "要約: "]
    assert scopes[0].targets[0].labels == []  # anywhere in the document


def test_another_dates_document_is_not_reconciled() -> None:
    scopes, _ = fact_check.scopes("下期の計画は1,038億円だった(7月の決算説明資料38ページ)。")
    assert scopes[0].targets[0].external
    findings, _, _ = fact_check.check("下期の計画は1,038億円だった(7月の決算説明資料38ページ)。", _docs())
    assert [(f.rule, f.hard) for f in findings] == [("出典", False)]


def test_check_passes_a_correct_citation_and_flags_a_wrong_page() -> None:
    ok, cites, checked = fact_check.check("受注高は785億円で前年同期比3.2倍だった(決算説明資料5ページ)。", _docs())
    assert ok == [] and (cites, checked) == (1, 2)
    wrong, _, _ = fact_check.check("受注残高は1,038億円だった(決算説明資料9ページ)。", _docs())
    assert [f.rule for f in wrong] == ["ページ"] and wrong[0].hard
    assert "決算説明資料5ページ" in wrong[0].hint


def test_a_derived_figure_is_a_warning_and_says_so() -> None:
    findings, _, _ = fact_check.check("4項目の合計は31億円である(決算説明資料9ページの注。筆者の計算)。", _docs())
    assert [(f.rule, f.hard) for f in findings] == [("ページ", False), ("数字", False)]
    findings, _, _ = fact_check.check("受注残高は1,038億円で、売上の2.1倍である(決算説明資料5ページ)。", _docs())
    assert [(f.rule, f.hard) for f in findings] == [("数字", False)]
    assert "筆者の計算なら「筆者の計算」と書く" in findings[0].hint


def test_an_image_only_page_cannot_be_checked() -> None:
    findings, _, _ = fact_check.check("ベトナム新工場は5.2倍になる(決算説明資料20ページ)。", _docs())
    assert [(f.rule, f.hard) for f in findings] == [("ページ", False)]
    assert "画像" in findings[0].hint


def test_missing_documents_and_pages() -> None:
    findings, _, _ = fact_check.check("システム費用は87億円(決算補足資料3ページ)。", _docs(), strict=True)
    assert [(f.rule, f.hard) for f in findings] == [("出典", True)]
    findings, _, _ = fact_check.check("システム費用は87億円(決算補足資料3ページ)。", _docs(), strict=False)
    assert [(f.rule, f.hard) for f in findings] == [("出典", False)]
    findings, _, _ = fact_check.check("受注残高は1,038億円(決算説明資料99ページ)。", _docs())
    assert [f.rule for f in findings] == ["出典"] and "99ページはない" in findings[0].hint


def test_dollar_figures_match_the_slide_in_millions() -> None:
    text = "営業利益は13.20億ドルで、4.14億ドル増えた(決算説明資料14ページ)。"
    findings, _, _ = fact_check.check(text, _docs())
    assert findings == []


def test_check_file_requires_a_filled_ledger(tmp_path: Path) -> None:
    memo = tmp_path / "6323" / "2026-10-08.md"
    memo.parent.mkdir()
    memo.write_text("受注残高は1,038億円(決算説明資料5ページ)。", encoding="utf-8")
    sources = tmp_path / "6323.sources.json"
    import json

    sources.write_text(json.dumps({"code": "6323", "date": "2026-10-08", "docs": [d.to_json() for d in _docs()]}, ensure_ascii=False), encoding="utf-8")
    ledger = tmp_path / "2026-10-08.sources.md"
    findings, _, _ = fact_check.check_file(memo, sources, ledger)
    assert [(f.rule, f.hard) for f in findings] == [("台帳", True)]
    findings, _, _ = fact_check.check_file(memo, sources, ledger, require_ledger=False)
    assert [(f.rule, f.hard) for f in findings] == [("台帳", False)]
    ledger.write_text("| 1 | x | 3 | 【】 |\n- 問い: 【】\n", encoding="utf-8")
    findings, _, _ = fact_check.check_file(memo, sources, ledger)
    assert findings[0].rule == "台帳" and "未記入が2行" in findings[0].hint
    ledger.write_text("| 1 | x | 3 | 読んだ |\n- 問い: 書いた\n", encoding="utf-8")
    assert fact_check.check_file(memo, sources, ledger)[0] == []
