#!/usr/bin/env python
"""Turn a report manuscript (Markdown) into the version pasted into note.

note's editor does not render Markdown tables, so every table becomes either
a PNG (the default for four data rows or more) or a "label: value" list. The
manuscript stays the source of truth; this output is regenerated each time.

Conventions the manuscript follows (reports/article-rules.md, section 10):

* ``<!-- note:image -->`` or ``<!-- note:list -->`` on the line before a
  table overrides the size rule.
* A line starting with ``要約:`` right after a table becomes the caption under
  its image, for readers who cannot see the picture. A table rendered as an
  image without one leaves ``【要約を書く】`` in the output.
* Internal lines (作成:, 手順:, 同日の日次記事:, 前回の会社別メモ:, and the
  source line that names the local archive) are dropped.
* The paywall marker becomes an instruction to set note's paid line there.

Usage:
    .venv/bin/python scripts/note_export.py reports/companies/9983/2026-10-08.md
"""

from __future__ import annotations

import argparse
import re
import textwrap
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

PAYWALL = "━━━━━ ここから有料部分 ━━━━━"
PAYWALL_OUT = "（ここに有料ラインを設定）"
DROP_PREFIXES = ("作成:", "手順:", "同日の日次記事:", "前回の会社別メモ:")
DROP_CONTAINS = ("保存済み",)
SUMMARY_PREFIX = "要約:"
MISSING_SUMMARY = "【要約を書く】"
IMAGE_MIN_ROWS = 4

_LEADING_NUMBER_RE = re.compile(r"^\d+(\.\d+)*\.\s*")
# Widest a cell may be before its text wraps, in display columns (CJK = 2).
_WRAP_WIDTH = 44

_OVERRIDE_RE = re.compile(r"^<!--\s*note:(image|list)\s*-->\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Japanese-capable fonts, most specific first. Linux hosts carry Noto; macOS
# ships Hiragino. PingFang is a last resort: it draws kana and kanji, but
# with Chinese glyph forms.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
    "/usr/share/fonts/truetype/ipaexg.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)


@dataclass
class Table:
    header: list[str]
    align: list[str]  # "left" or "right" per column
    rows: list[list[str]]
    heading: str  # nearest heading above, used as the image title
    mode: str  # "image" or "list"
    summary: str | None = None


@dataclass
class Document:
    title: str
    parts: list[object] = field(default_factory=list)  # str lines or Table


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [_BOLD_RE.sub(r"\1", c).strip() for c in inner.split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", line.strip()))


def _alignment(sep_line: str) -> list[str]:
    out = []
    for cell in _cells(sep_line):
        out.append("right" if cell.endswith(":") and not cell.startswith(":") else "left")
    return out


def parse(text: str) -> Document:
    """Split the manuscript into prose lines and tables."""
    lines = text.split("\n")
    doc = Document(title="")
    heading = ""
    override: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if not doc.title and line.startswith("# "):
            doc.title = line[2:].strip()
            i += 1
            continue
        m = _OVERRIDE_RE.match(line.strip())
        if m:
            override = m.group(1)
            i += 1
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            header = _cells(line)
            align = _alignment(lines[i + 1])
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            mode = override or ("image" if len(rows) >= IMAGE_MIN_ROWS else "list")
            table = Table(header, align, rows, heading, mode)
            override = None
            # A 要約: line may follow, possibly after one blank line.
            k = j
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k < len(lines) and lines[k].strip().startswith(SUMMARY_PREFIX):
                table.summary = lines[k].strip()[len(SUMMARY_PREFIX):].strip()
                j = k + 1
            doc.parts.append(table)
            i = j
            continue
        if line.startswith("#"):
            heading = _LEADING_NUMBER_RE.sub("", line.lstrip("#").strip())
        doc.parts.append(line)
        i += 1
    return doc


def _keep(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(DROP_PREFIXES):
        return False
    if stripped.startswith("-") and any(s in stripped for s in DROP_CONTAINS):
        return False
    return True


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _font_path() -> Path | None:
    for candidate in _FONT_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    try:  # the repository's own resolver, which may download Noto
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
        from src.shadow_account.fonts import cjk_font_path

        return cjk_font_path(allow_download=False)
    except Exception:  # noqa: BLE001 - rendering falls back to DejaVu
        return None


def _wrap(text: str, width: int) -> str:
    """Wrap ``text`` to ``width`` display columns, counting CJK as two."""
    if _display_width(text) <= width:
        return text
    lines: list[str] = []
    current = ""
    for ch in text:
        if _display_width(current + ch) > width and current:
            lines.append(current)
            current = ch
        else:
            current += ch
    if current:
        lines.append(current)
    return "\n".join(lines)


def render_table(table: Table, out_path: Path) -> None:
    """Draw one table to a PNG with matplotlib.

    matplotlib's table neither wraps text nor grows a row to fit it, so both
    are done here: cells are wrapped to a display-width budget and each row's
    height follows its tallest cell.
    """
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import font_manager as fm
    import matplotlib.pyplot as plt

    font = _font_path()
    if font is not None:
        fm.fontManager.addfont(str(font))
        family = fm.FontProperties(fname=str(font)).get_name()
        plt.rcParams["font.family"] = family
    plt.rcParams["axes.unicode_minus"] = False

    ncols = len(table.header)
    grid = [table.header] + [[(r[c] if c < len(r) else "") for c in range(ncols)] for r in table.rows]
    grid = [[_wrap(cell, _WRAP_WIDTH) for cell in row] for row in grid]
    widths = [
        max(max(_display_width(part) for part in row[c].split("\n")) for row in grid)
        for c in range(ncols)
    ]
    widths = [max(w, 4) for w in widths]
    total = sum(widths)
    line_counts = [max(cell.count("\n") + 1 for cell in row) for row in grid]
    fig_w = min(max(total * 0.11 + 0.6, 4.0), 13.0)
    fig_h = 0.22 * sum(line_counts) + 0.10 * len(grid) + (0.45 if table.heading else 0.2)

    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=200)
    ax.axis("off")
    cell_text = grid[1:]
    tbl = ax.table(
        cellText=cell_text,
        colLabels=grid[0],
        colWidths=[w / total for w in widths],
        cellLoc="left",
        loc="upper center",
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.45)
    base_h = None
    for (row, col), cell in tbl.get_celld().items():
        if base_h is None:
            base_h = cell.get_height()
        cell.set_height(base_h * (0.55 + 0.45 * line_counts[row]))
        cell.set_edgecolor("#c8c8c8")
        cell.set_linewidth(0.6)
        cell.PAD = 0.04
        if row == 0:
            cell.set_facecolor("#eef2f7")
            cell.set_text_props(weight="bold")
        elif row % 2 == 0:
            cell.set_facecolor("#fafafa")
        if row > 0 and table.align[col] == "right":
            cell.set_text_props(ha="right")
            cell._loc = "right"
    if table.heading:
        ax.set_title(table.heading, fontsize=10, loc="left", pad=6)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.15, facecolor="white")
    plt.close(fig)


def _list_lines(table: Table) -> list[str]:
    """Render a small table as bullets: ``label: first value（rest）``.

    The first value column reads as the row's answer; the remaining columns
    go in parentheses with their header, so a 根拠 or a prior-year figure
    stays attached to the number it explains.
    """
    out = []
    for row in table.rows:
        label = row[0] if row else ""
        first = row[1] if len(row) > 1 else ""
        rest = [f"{h}: {v}" for h, v in zip(table.header[2:], row[2:]) if v]
        line = f"- {label}: {first}"
        if rest:
            line += "（" + "、".join(rest) + "）"
        out.append(line)
    return out


def export(src: Path, out_dir: Path | None = None, *, render: bool = True) -> Path:
    """Write ``<stem>.note.md`` and its images; return the output path."""
    doc = parse(src.read_text(encoding="utf-8"))
    out_dir = out_dir or src.parent
    out_md = out_dir / f"{src.stem}.note.md"
    img_dir = out_dir / f"{src.stem}.images"

    lines: list[str] = [f"題名: {doc.title}", ""]
    image_no = 0
    for part in doc.parts:
        if isinstance(part, Table):
            if part.mode == "image":
                image_no += 1
                img = img_dir / f"{image_no:02d}.png"
                if render:
                    render_table(part, img)
                rel = img.relative_to(out_dir)
                lines.append(f"![表{image_no}: {part.heading}]({rel})")
                lines.append(part.summary or MISSING_SUMMARY)
            else:
                lines.extend(_list_lines(part))
                if part.summary:
                    lines.append(part.summary)
            continue
        if part.strip() == PAYWALL:
            lines.append(PAYWALL_OUT)
            continue
        if _keep(part):
            lines.append(part)

    # Collapse runs of blank lines left by dropped material.
    cleaned: list[str] = []
    for line in lines:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line)
    out_md.write_text("\n".join(cleaned).rstrip("\n") + "\n", encoding="utf-8")
    return out_md


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="manuscript .md")
    parser.add_argument("--out-dir", type=Path, default=None, help="defaults to the manuscript's folder")
    parser.add_argument("--no-render", action="store_true", help="write the text only, skip the PNGs")
    args = parser.parse_args(argv)
    out = export(args.source, args.out_dir, render=not args.no_render)
    text = out.read_text(encoding="utf-8")
    missing = text.count(MISSING_SUMMARY)
    print(f"wrote {out} ({len(text)} chars, {text.count('![表')} images, {missing} summaries missing)")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
