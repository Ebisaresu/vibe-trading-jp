"""Prose rules: sentence endings, length, word list, and what is exempt."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prose_check.py"
_spec = importlib.util.spec_from_file_location("prose_check", _SCRIPT)
prose_check = importlib.util.module_from_spec(_spec)
sys.modules["prose_check"] = prose_check
_spec.loader.exec_module(prose_check)  # type: ignore[union-attr]


def _rules(text: str) -> list[str]:
    return [f.rule for f in prose_check.check_text(text)]


@pytest.mark.parametrize(
    "sentence",
    [
        "海外ユニクロが増益の78%を占めた。",
        "事業利益率は18.1%で、1.9ポイント改善した。",
        "次回は1月上旬に答え合わせをする。",
        "これは筆者の推論で、会社は明言していない。",
        "その差は税率が低かったためだと筆者は読む(筆者の推論。会社の開示ではない)。",
    ],
)
def test_predicate_endings_pass(sentence: str) -> None:
    assert "体言止め" not in _rules(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "増益1,789億円の78%は海外ユニクロ。",
        "事業利益率は18.1%で1.9ポイント改善。",
        "今期は900円。",
        "筆者の分解は次のとおり。",
        "前回の答え合わせはなし。",
        "期末配当は取締役会の決議が前提。",
    ],
)
def test_noun_endings_are_flagged(sentence: str) -> None:
    assert "体言止め" in _rules(sentence)


def test_period_inside_parentheses_does_not_split() -> None:
    text = "根拠は第4節と第5節である(筆者の推論。会社も株探も明言していない)。"
    assert "句点なし" not in _rules(text)
    assert "体言止め" not in _rules(text)


def test_word_list_hits_outside_quotes_only() -> None:
    assert "言葉" in _rules("円安は輸出企業に追い風となった。")
    assert "言葉" not in _rules("株探の見出しは「決算サプライズ限定的」であった。")


def test_label_bullets_tables_headings_and_sources_are_exempt() -> None:
    text = "\n".join([
        "## 1. 実績",
        "| 項目 | 値 |",
        "|---|---:|",
        "| 売上 | 100 |",
        "- 期末配当の決議: 2026-11-05(取締役会)",
        "作成: 2026-10-11",
        "## 出典",
        "- Yahoo Finance: 株価(2026-10-09 終値。筆者の予想ではない)",
        "> **免責事項**: 本資料は情報提供のみを目的とします。筆者は保有していません。",
    ])
    assert _rules(text) == []


def test_long_sentence_is_a_soft_warning() -> None:
    long = "会社の説明は、" + "国内ユニクロの既存店売上が減り、" * 6 + "というものである。"
    findings = prose_check.check_text(long)
    assert any(f.rule == "長い文" and not f.hard for f in findings)
