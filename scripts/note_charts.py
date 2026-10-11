"""Draw the charts a manuscript declares, for the note version of an article.

A manuscript declares a chart as a fenced block:

    ```chart
    type: bar            # bar | hbar | waterfall | donut | line
    title: 四半期別の営業利益
    unit: 億円
    series: [今期, 前期]   # bar and line only; at most three
    rows:
      - [第1四半期, 1050, 651]
      - [第2四半期, 1272, 1433]
    ```

    要約: 第2四半期は前年同期を下回った。

The exporter turns each block into a PNG and a `![図N: title]` line, with the
要約 line beneath it exactly as it does for a table.

Forms and the job each does (reports/article-rules.md, section 11):

* ``bar``       — compare a few categories, optionally against a prior period.
* ``hbar``      — one series of contributions that can be negative; the sign
                  picks the color, so a reader sees where the gain came from.
* ``waterfall`` — walk from one total to another through its drivers. A row
                  ending in ``total`` is drawn as a full bar from zero.
* ``donut``     — part of a whole, at most six parts; the author folds the
                  rest into その他 before declaring it.
* ``line``      — a value over ordered periods, at most three series.

Colors follow the data-viz reference palette: blue and orange for the first
two series, aqua for a third, blue for gains and red for losses on diverging
forms, a neutral gray for totals. Text never wears a series color.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SERIES = ("#2a78d6", "#eb6834", "#1baf7a")  # categorical slots 1–3, light surface
POSITIVE, NEGATIVE, TOTAL = "#2a78d6", "#e34948", "#c3c2b7"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
DONUT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7")
MAX_SERIES = 3
MAX_DONUT = 6


@dataclass
class Chart:
    type: str
    title: str
    rows: list[list[Any]]
    unit: str = ""
    series: list[str] = field(default_factory=list)
    heading: str = ""
    summary: str | None = None


def parse_chart(text: str, heading: str = "") -> Chart:
    """Parse the YAML inside a ```chart fence into a Chart, validating it."""
    import yaml

    spec = yaml.safe_load(text) or {}
    kind = str(spec.get("type", "bar")).strip()
    if kind not in ("bar", "hbar", "waterfall", "donut", "line"):
        raise ValueError(f"unknown chart type {kind!r}")
    rows = spec.get("rows") or []
    if not rows:
        raise ValueError("chart has no rows")
    rows = [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]
    series = [str(s) for s in (spec.get("series") or [])]
    if kind in ("bar", "line"):
        width = max(len(r) for r in rows) - 1
        if not series:
            series = [""] * width
        if len(series) != width:
            raise ValueError(f"series has {len(series)} names but rows carry {width} values")
        if len(series) > MAX_SERIES:
            raise ValueError(f"at most {MAX_SERIES} series; fold the rest or facet")
    if kind == "donut" and len(rows) > MAX_DONUT:
        raise ValueError(f"donut takes at most {MAX_DONUT} parts; fold the rest into その他")
    return Chart(
        type=kind,
        title=str(spec.get("title") or heading or ""),
        rows=rows,
        unit=str(spec.get("unit") or ""),
        series=series,
        heading=heading,
    )


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _fmt(value)


def _setup(font: Path | None):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager as fm
    import matplotlib.pyplot as plt

    if font is not None:
        fm.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = fm.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.size"] = 9
    return plt


def _frame(ax, *, horizontal: bool = False) -> None:
    """Recessive axes: hairline grid on the value axis, no box."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(1)
    ax.tick_params(colors=INK2, length=0)
    ax.grid(axis="y" if not horizontal else "x", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)
    import matplotlib.ticker as mt

    axis = ax.xaxis if horizontal else ax.yaxis
    axis.set_major_formatter(mt.FuncFormatter(lambda v, _p: _fmt(v)))


def _finish(fig, ax, chart: Chart, out: Path) -> None:
    if chart.title:
        ax.set_title(chart.title, loc="left", fontsize=10.5, color=INK, pad=10)
    if chart.unit:
        ax.text(0, 1.01, f"({chart.unit})", transform=ax.transAxes, fontsize=8, color=INK2, ha="left", va="bottom")
    fig.patch.set_facecolor(SURFACE)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight", pad_inches=0.15, facecolor=SURFACE)


def _bar(plt, chart: Chart, out: Path) -> None:
    import numpy as np

    labels = [str(r[0]) for r in chart.rows]
    n = len(chart.series)
    x = np.arange(len(labels))
    width = min(0.7 / n, 0.32)
    fig, ax = plt.subplots(figsize=(max(4.5, 1.1 * len(labels) + 2.5), 3.6))
    for i, name in enumerate(chart.series):
        vals = [float(r[i + 1]) if i + 1 < len(r) and r[i + 1] is not None else 0.0 for r in chart.rows]
        pos = x + (i - (n - 1) / 2) * (width + 0.02)
        bars = ax.bar(pos, vals, width=width, color=SERIES[i], label=name or None, zorder=3)
        if len(labels) * n <= 12:
            for b, v in zip(bars, vals):
                ax.annotate(_fmt(v), (b.get_x() + b.get_width() / 2, v), xytext=(0, 3 if v >= 0 else -3),
                            textcoords="offset points", ha="center", va="bottom" if v >= 0 else "top",
                            fontsize=8, color=INK)
    ax.set_xticks(x, labels)
    ax.axhline(0, color=GRID, linewidth=1)
    _frame(ax)
    if n >= 2:
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0, -0.12), ncol=n)
    _finish(fig, ax, chart, out)


def _hbar(plt, chart: Chart, out: Path) -> None:
    labels = [str(r[0]) for r in chart.rows]
    vals = [float(r[1]) for r in chart.rows]
    fig, ax = plt.subplots(figsize=(6.5, 0.42 * len(labels) + 1.3))
    y = list(range(len(labels)))[::-1]
    colors = [POSITIVE if v >= 0 else NEGATIVE for v in vals]
    ax.barh(y, vals, height=0.55, color=colors, zorder=3)
    span = max(abs(v) for v in vals) or 1
    for yi, v in zip(y, vals):
        ax.annotate(_signed(v), (v, yi), xytext=(4 if v >= 0 else -4, 0), textcoords="offset points",
                    ha="left" if v >= 0 else "right", va="center", fontsize=8, color=INK)
    ax.set_yticks(y, labels)
    ax.axvline(0, color=GRID, linewidth=1)
    lo, hi = min(0, min(vals)), max(0, max(vals))
    ax.set_xlim(lo - span * 0.18, hi + span * 0.18)
    _frame(ax, horizontal=True)
    _finish(fig, ax, chart, out)


def _waterfall(plt, chart: Chart, out: Path) -> None:
    labels, vals, totals = [], [], []
    for r in chart.rows:
        labels.append(str(r[0]))
        vals.append(float(r[1]))
        totals.append(len(r) > 2 and str(r[2]).lower() == "total")
    fig, ax = plt.subplots(figsize=(max(5, 1.0 * len(labels) + 2), 3.8))
    running = 0.0
    prev_top = None
    for i, (v, is_total) in enumerate(zip(vals, totals)):
        if is_total:
            bottom, height, color = 0.0, v, TOTAL
            running = v
        else:
            bottom, height = running, v
            color = POSITIVE if v >= 0 else NEGATIVE
            running += v
        ax.bar(i, height, bottom=bottom, width=0.55, color=color, zorder=3)
        top = bottom + height
        label = _fmt(v) if is_total else _signed(v)
        ax.annotate(label, (i, max(bottom, top)), xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8, color=INK)
        if prev_top is not None:
            ax.plot([i - 1 + 0.275, i - 0.275], [prev_top, prev_top], color=INK2, linewidth=0.8, zorder=2)
        prev_top = running if not is_total else v
    ax.set_xticks(range(len(labels)), labels)
    ax.axhline(0, color=GRID, linewidth=1)
    _frame(ax)
    _finish(fig, ax, chart, out)


def _donut(plt, chart: Chart, out: Path) -> None:
    labels = [str(r[0]) for r in chart.rows]
    vals = [max(float(r[1]), 0.0) for r in chart.rows]
    total = sum(vals) or 1
    fig, ax = plt.subplots(figsize=(6.2, 3.6))
    wedges, _ = ax.pie(vals, colors=DONUT[: len(vals)], startangle=90, counterclock=False,
                       wedgeprops={"width": 0.38, "edgecolor": SURFACE, "linewidth": 2})
    legend_labels = [f"{l}  {_fmt(v)}（{v / total * 100:.0f}%）" for l, v in zip(labels, vals)]
    ax.legend(wedges, legend_labels, frameon=False, fontsize=8.5, loc="center left", bbox_to_anchor=(1.0, 0.5))
    ax.set_aspect("equal")
    fig.patch.set_facecolor(SURFACE)
    if chart.title:
        ax.set_title(chart.title, loc="left", fontsize=10.5, color=INK, pad=10)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight", pad_inches=0.15, facecolor=SURFACE)


def _line(plt, chart: Chart, out: Path) -> None:
    labels = [str(r[0]) for r in chart.rows]
    fig, ax = plt.subplots(figsize=(max(5, 0.8 * len(labels) + 2.5), 3.6))
    for i, name in enumerate(chart.series):
        vals = [float(r[i + 1]) if i + 1 < len(r) and r[i + 1] is not None else None for r in chart.rows]
        ax.plot(range(len(labels)), vals, color=SERIES[i], linewidth=2, solid_capstyle="round",
                marker="o", markersize=5, markeredgecolor=SURFACE, markeredgewidth=1.5, label=name or None, zorder=3)
        last = next((k for k in range(len(vals) - 1, -1, -1) if vals[k] is not None), None)
        if last is not None:
            ax.annotate(_fmt(vals[last]), (last, vals[last]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8, color=INK)
    ax.set_xticks(range(len(labels)), labels)
    _frame(ax)
    if len(chart.series) >= 2:
        ax.legend(frameon=False, fontsize=8, loc="upper left", bbox_to_anchor=(0, -0.12), ncol=len(chart.series))
    _finish(fig, ax, chart, out)


def render_chart(chart: Chart, out: Path, font: Path | None) -> None:
    plt = _setup(font)
    draw = {"bar": _bar, "hbar": _hbar, "waterfall": _waterfall, "donut": _donut, "line": _line}[chart.type]
    draw(plt, chart, out)
    plt.close("all")
