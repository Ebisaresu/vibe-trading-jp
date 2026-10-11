"""Chart declarations in a manuscript: parsing, limits, and a render smoke test."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


note_charts = _load("note_charts")
note_export = _load("note_export")

BAR = """type: bar
title: 四半期別の営業利益
unit: 億円
series: [今期, 前期]
rows:
  - [第1四半期, 1050, 651]
  - [第2四半期, 1272, 1433]
"""


def test_bar_parses_with_series() -> None:
    c = note_charts.parse_chart(BAR)
    assert c.type == "bar" and c.series == ["今期", "前期"] and len(c.rows) == 2


def test_series_count_must_match_rows() -> None:
    with pytest.raises(ValueError):
        note_charts.parse_chart(BAR.replace("[今期, 前期]", "[今期]"))


def test_more_than_three_series_is_refused() -> None:
    spec = "type: line\nseries: [a, b, c, d]\nrows:\n  - [x, 1, 2, 3, 4]\n"
    with pytest.raises(ValueError):
        note_charts.parse_chart(spec)


def test_donut_caps_at_six_parts() -> None:
    rows = "\n".join(f"  - [p{i}, 1]" for i in range(7))
    with pytest.raises(ValueError):
        note_charts.parse_chart(f"type: donut\nrows:\n{rows}\n")


@pytest.mark.parametrize(
    "spec",
    [
        BAR,
        "type: hbar\ntitle: 寄与\nrows:\n  - [海外, 703]\n  - [国内, -119]\n  - [その他, -387]\n",
        "type: waterfall\nrows:\n  - [前期, 905, total]\n  - [商品等, 25]\n  - [ガソリン, 398]\n  - [販管費, -8]\n  - [今期, 1320, total]\n",
        "type: donut\nrows:\n  - [海外, 78]\n  - [国内, 7]\n  - [その他, 15]\n",
        "type: line\nseries: [既存店]\nrows:\n  - [1Q, 2.0]\n  - [2Q, 0.3]\n",
    ],
)
def test_every_form_renders_a_png(tmp_path: Path, spec: str) -> None:
    chart = note_charts.parse_chart(spec, "見出し")
    out = tmp_path / "c.png"
    note_charts.render_chart(chart, out, None)
    assert out.stat().st_size > 1000


def test_exporter_numbers_charts_separately_from_tables(tmp_path: Path) -> None:
    src = tmp_path / "m.md"
    src.write_text(
        "# 題\n\n## 1. 節\n\n```chart\n" + BAR + "```\n\n要約: 第2四半期は前年を下回った。\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |\n| 7 | 8 |\n\n要約: 表。\n",
        encoding="utf-8",
    )
    text = note_export.export(src, render=False).read_text(encoding="utf-8")
    assert "![図1: 四半期別の営業利益](m.images/chart01.png)\n第2四半期は前年を下回った。" in text
    assert "![表1: 節](m.images/01.png)" in text
