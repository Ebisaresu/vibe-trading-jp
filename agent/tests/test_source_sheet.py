"""The page ledger: document kinds, page labels, headings, and the filled-in check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "source_sheet.py"
_spec = importlib.util.spec_from_file_location("source_sheet", _SCRIPT)
source_sheet = importlib.util.module_from_spec(_spec)
sys.modules["source_sheet"] = source_sheet
_spec.loader.exec_module(source_sheet)  # type: ignore[union-attr]

Page = source_sheet.Page
Doc = source_sheet.Doc


def test_classify_reads_the_kind_and_title_off_the_cover() -> None:
    cover = "2027年2月期 第2四半期(中間期)決算短信〔日本基準〕(連結)\n2026年10月8日\n上場会社名 ローツェ株式会社 上場取引所 東"
    assert source_sheet.classify(cover, 1) == ("決算短信", "2027年2月期 第2四半期(中間期)決算短信〔日本基準〕(連結)")
    notice = "2026 年 10 月8日\n各 位\n会 社 名 ローツェ株式会社\n通期連結業績予想の修正及び配当予想の修正に関するお知らせ"
    assert source_sheet.classify(notice, 2) == ("お知らせ", "通期連結業績予想の修正及び配当予想の修正に関するお知らせ")


def test_classify_takes_the_kind_from_the_filename_when_the_cover_says_nothing() -> None:
    cover = "2026年8月期 業績 および\n2027年8月期 業績予想\n2026年10月8日"
    kind, _ = source_sheet.classify(cover, 3)
    assert kind == "資料3"
    kind, _ = source_sheet.classify(cover, 3, "9983_決算説明資料.pdf")
    assert kind == "決算説明資料"


def test_page_labels_follow_what_a_memo_cites() -> None:
    footer = ["ローツェ(株)(6323)決算短信", "― 8 ―", "3.補足情報"]
    assert source_sheet.page_label(footer, 10, "決算短信") == "添付8"
    # A 補足資料 with the same footer is cited by its plain number.
    assert source_sheet.page_label(footer, 10, "決算補足資料") == "8"
    deck = ["Copyright(C) Seven & i Holdings Co., Ltd. All Rights Reserved. 14", "SEI_中間期業績"]
    assert source_sheet.page_label(deck, 14, "決算説明資料") == "14"
    lone = ["18", "2027年2月期 通期連結業績予想の修正"]
    assert source_sheet.page_label(lone, 18, "決算説明資料") == "18"
    assert source_sheet.page_label(["受注高・売上高", "24,699 22,958"], 10, "決算説明資料") == "10"


def test_a_footer_number_beats_an_axis_tick() -> None:
    lines = ["10", "受注高の推移", "Copyright(C) X Co. All Rights Reserved. 12"]
    assert source_sheet.page_label(lines, 12, "決算説明資料") == "12"


def test_duplicate_labels_fall_back_to_the_pdf_index() -> None:
    pages = [Page(9, "9", "", ""), Page(10, "10", "", ""), Page(11, "10", "", "")]
    source_sheet.dedupe_labels(pages)
    assert [p.label for p in pages] == ["9", "10", "11"]
    pages = [Page(1, "1", "", ""), Page(2, "3", "", ""), Page(3, "3", "", "")]
    source_sheet.dedupe_labels(pages)
    assert [p.label for p in pages] == ["1", "3", "p3"]


def test_heading_skips_running_headers_and_page_numbers() -> None:
    running = {"ローツェ(株)(6323)2027年2月期第2四半期(中間期)決算短信"}
    lines = ["ローツェ(株)(6323)2027年2月期第2四半期(中間期)決算短信", "― 8 ―", "3.補足情報", "生産、受注及び販売の状況"]
    assert source_sheet.page_heading(lines, running) == "3.補足情報"
    assert source_sheet.page_heading([], set()) == "(文字なし。図のみのページ)"


def test_ledger_has_one_row_per_page_and_the_fill_in_check_counts_only_rows(tmp_path: Path) -> None:
    docs = [
        Doc("決算短信", "決算短信", "決算短信", "6323_a.pdf", [Page(1, "1", "サマリー", "x", 5), Page(3, "添付1", "目次", "y", 2)]),
        Doc("決算説明資料", "決算説明資料", "説明資料", "6323_b.pdf", [Page(1, "1", "表紙", "", 0)]),
    ]
    text = source_sheet.render_ledger(docs, "6323", "2026-10-08", today="2026-10-11")
    assert "| 添付1 | 目次 | 2 | 【】 |" in text
    assert text.count("| 【】 |") == 3
    assert "## 決算説明資料(1ページ)" in text
    path = tmp_path / "2026-10-08.sources.md"
    path.write_text(text, encoding="utf-8")
    left = source_sheet.ledger_placeholders(path)
    # 3 page rows + 4 closing bullets; the instruction line that mentions 【】 is not counted.
    assert len(left) == 7
    filled = text.replace("| 【】 |", "| 読んだ |").replace(": 【】", ": 書いた")
    path.write_text(filled, encoding="utf-8")
    assert source_sheet.ledger_placeholders(path) == []


def test_find_reports_document_and_page() -> None:
    docs = [Doc("決算説明資料", "決算説明資料", "", "x.pdf", [Page(5, "5", "業績", "半導体関連装置の受注残高 1,038億円で前年同期比2.3倍", 3)])]
    hits = source_sheet.find(docs, "受注残高")
    assert len(hits) == 1 and hits[0].startswith("決算説明資料 5ページ:")
    assert source_sheet.find(docs, "配当") == []


def test_page_by_label_accepts_the_index_as_a_fallback() -> None:
    doc = Doc("決算短信", "決算短信", "", "x.pdf", [Page(1, "1", "", ""), Page(2, "2", "", ""), Page(3, "添付1", "", "")])
    assert doc.page_by_label("添付1").index == 3
    assert doc.page_by_label("3").index == 3
    assert doc.page_by_label("添付9") is None
