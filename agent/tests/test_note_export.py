"""note export: manuscript parsing and the text-side rules, without rendering."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "note_export.py"
_spec = importlib.util.spec_from_file_location("note_export", _SCRIPT)
note_export = importlib.util.module_from_spec(_spec)
# dataclasses resolve annotations through sys.modules, so the module must be
# registered before it executes.
sys.modules["note_export"] = note_export
_spec.loader.exec_module(note_export)  # type: ignore[union-attr]

MANUSCRIPT = """# 【決算メモ】テスト(0000)2026年 — 結論

作成: 2026-10-11 / 対象開示: 短信
手順: japan-equity-research

## 0. 3行サマリ

1. 一行目。

## 1. 実績

| 項目 | 当期 | 前期 | 増減率 |
|---|---:|---:|---:|
| 売上 | 100 | 90 | +11.1% |
| 営業利益 | 20 | 15 | +33.3% |
| 純利益 | 12 | 10 | +20.0% |
| 1株利益 | 1.2 | 1.0 | +20.0% |

要約: 売上+11%、営業利益+33%。

━━━━━ ここから有料部分 ━━━━━

## 4. 株価の反応

同日の日次記事: なし。

<!-- note:list -->
| 項目 | 値 | 根拠 |
|---|---:|---|
| 終値 | 1,000円 | 前日比**−4.0%** |
| 出来高 | 2,000株 | 平均の1.9倍 |
| 高値 | 1,050円 | |
| 安値 | 990円 | |

| 項目 | 第4四半期 | 前年同期 |
|---|---:|---:|
| 売上 | 30 | 25 |

- 短信のリンク
- 短信は `~/x/imports/tdnet/` に保存済み。

<!-- note:cards -->
| 確認項目 | 今回の結果 | 次回の確認点 |
|---|---|---|
| 成長が続くか | +26% | 30%台を保つか |
| 利益は横ばいか | +1.9% | |

<!-- note:skip -->
## 8. 取得できなかったもの

- 説明会の質疑

## 出典

- 短信
"""


def _export(tmp_path: Path) -> str:
    src = tmp_path / "memo.md"
    src.write_text(MANUSCRIPT, encoding="utf-8")
    out = note_export.export(src, render=False)
    return out.read_text(encoding="utf-8")


def test_title_moves_to_the_top_line(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert text.startswith("題名: 【決算メモ】テスト(0000)2026年 — 結論\n")
    assert "# 【決算メモ】" not in text


def test_internal_lines_are_dropped(tmp_path: Path) -> None:
    text = _export(tmp_path)
    for needle in ("作成:", "手順:", "同日の日次記事:", "保存済み"):
        assert needle not in text
    assert "- 短信のリンク" in text


def test_paywall_marker_becomes_an_instruction(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert note_export.PAYWALL not in text
    assert note_export.PAYWALL_OUT in text


def test_large_table_becomes_an_image_with_its_summary(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert "![表1: 実績](memo.images/01.png)" in text
    assert "\n売上+11%、営業利益+33%。\n" in text
    assert "| 売上 | 100 |" not in text


def test_override_renders_a_large_table_as_a_list(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert "- 終値: 1,000円（根拠: 前日比−4.0%）" in text
    assert "- 高値: 1,050円\n" in text
    assert "<!-- note:list -->" not in text


def test_small_table_is_a_list_by_default(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert "- 売上: 30（前年同期: 25）" in text


def test_missing_summary_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "memo.md"
    src.write_text(MANUSCRIPT.replace("要約: 売上+11%、営業利益+33%。\n", ""), encoding="utf-8")
    text = note_export.export(src, render=False).read_text(encoding="utf-8")
    assert note_export.MISSING_SUMMARY in text


@pytest.mark.parametrize(
    ("text", "width", "lines"),
    [("短い", 10, 1), ("あいうえおかきくけこさしすせそ", 10, 3), ("abcdefghij", 10, 1), ("abcdefghijk", 10, 2)],
)
def test_wrap_counts_cjk_as_two_columns(text: str, width: int, lines: int) -> None:
    assert note_export._wrap(text, width).count("\n") + 1 == lines


def test_cards_render_one_block_per_row(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert "**成長が続くか**\n- 今回の結果: +26%\n- 次回の確認点: 30%台を保つか\n" in text
    assert "**利益は横ばいか**\n- 今回の結果: +1.9%\n" in text


def test_skipped_section_is_dropped_up_to_the_next_heading(tmp_path: Path) -> None:
    text = _export(tmp_path)
    assert "取得できなかったもの" not in text
    assert "説明会の質疑" not in text
    assert "## 出典\n\n- 短信" in text


def test_placeholders_are_counted(tmp_path: Path, capsys) -> None:
    src = tmp_path / "memo.md"
    src.write_text(MANUSCRIPT + "\n筆者は保有して【いません/います】。\n", encoding="utf-8")
    assert note_export.main([str(src), "--no-render"]) == 1
    assert "1 placeholders left" in capsys.readouterr().out
