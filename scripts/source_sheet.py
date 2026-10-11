#!/usr/bin/env python
"""Turn the saved filings of one company into a page-by-page ledger.

Why this exists: in three review rounds the analyst persona found, every
time, an answer that was sitting in the saved PDFs two pages past where the
memo stopped reading. A memo written from the summary page and a few slides
misses what the rest of the deck says. This script makes "read every page"
a step with an artifact, so it is done before the first sentence is written
and can be checked afterwards.

Two outputs:

* ``~/.vibe-trading/imports/tdnet/<date>/<code>.sources.json`` — every page's
  text, next to the PDFs and outside the repository (the text is the
  issuer's). ``scripts/fact_check.py`` reads it to verify the memo's page
  citations.
* ``reports/companies/<code>/<date>.sources.md`` — the ledger: one row per
  page with the machine-extracted heading and a 要点 cell the author fills in
  by reading the page. ``fact_check.py`` refuses a ledger with 【】 left in it.

Page labels follow what the memo cites: a 決算短信 attachment page carries
``― N ―`` in its running header and is labelled 添付N; a slide deck's printed
number is used when it can be read off the page; otherwise the PDF index.

Usage:
    .venv/bin/python scripts/source_sheet.py 6323 2026-10-08
    .venv/bin/python scripts/source_sheet.py 6323 2026-10-08 --find 受注残
    .venv/bin/python scripts/source_sheet.py 6323 2026-10-08 --force   # rebuild the ledger
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

ARCHIVE_ROOT = Path.home() / ".vibe-trading" / "imports" / "tdnet"
REPORTS_ROOT = Path(__file__).resolve().parents[1] / "reports" / "companies"

# Document kinds in the order the memo names them. Each entry: canonical name,
# then the strings that identify it on the first page.
_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("決算短信", ("決算短信",)),
    ("決算説明資料", ("決算説明資料", "決算説明会資料", "説明資料", "説明会資料", "Presentation")),
    ("決算補足資料", ("補足資料", "補足説明資料", "データブック", "Data Book", "Supplementary", "Consolidated Results")),
    ("お知らせ", ("お知らせ",)),
)

_ATTACH_RE = re.compile(r"^[―－\-‐–—]\s*(\d{1,3})\s*[―－\-‐–—]$")
_LONE_NUMBER_RE = re.compile(r"^(\d{1,3})$")
_TRAILING_NUMBER_RE = re.compile(r"(?:Reserved\.?|Copyright.*?|reserved\.?)\s+(\d{1,3})$")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PLACEHOLDER_RE = re.compile(r"【[^】]*】")


@dataclass
class Page:
    index: int  # 1-based PDF page
    label: str  # what a citation calls it: "添付3", "18", or the index
    heading: str
    text: str
    numbers: int = 0

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Doc:
    name: str  # canonical kind, suffixed when a kind repeats: 決算説明資料2
    kind: str
    title: str
    file: str
    pages: list[Page] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"name": self.name, "kind": self.kind, "title": self.title, "file": self.file, "pages": [p.to_json() for p in self.pages]}

    @classmethod
    def from_json(cls, d: dict) -> "Doc":
        return cls(d["name"], d["kind"], d["title"], d["file"], [Page(**p) for p in d["pages"]])

    def page_by_label(self, label: str) -> Page | None:
        for p in self.pages:
            if p.label == label:
                return p
        if label.isdigit():
            idx = int(label)
            for p in self.pages:
                if p.index == idx:
                    return p
        return None


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("　", " ")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def classify(first_page: str, position: int, filename: str = "") -> tuple[str, str]:
    """Return ``(kind, title)`` from the first page's text.

    A file saved by hand from the company site can name its kind in the
    filename (``9983_決算説明資料.pdf``): a deck whose cover says only
    「2026年8月期 業績」 cannot be classified from its text.
    """
    lines = _lines(first_page)
    head = "\n".join(lines[:6])
    whole = "\n".join(lines)
    kind = ""
    for name, keys in _KINDS:
        if name in filename:
            kind = name
            break
    for name, keys in _KINDS:
        if kind:
            break
        if any(k in head for k in keys):
            kind = name
            break
    if not kind:
        for name, keys in _KINDS:
            if any(k in whole for k in keys):
                kind = name
                break
    if not kind:
        kind = f"資料{position}"
    title = ""
    keys = next((k for name, k in _KINDS if name == kind), ())
    for ln in lines:
        # The line naming the document kind is its title: 2027年2月期 第2四半期 決算短信〔日本基準〕(連結)
        if any(k in ln for k in keys) and not ln.startswith(("上場会社名", "会社名")):
            title = ln
            break
    if not title:
        for ln in lines:
            if len(ln) >= 6 and not _LONE_NUMBER_RE.match(ln) and not re.match(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日$", ln):
                title = ln
                break
    return kind, (title or (lines[0] if lines else ""))[:80]


def page_label(lines: list[str], index: int, kind: str) -> str:
    """The number a reader would cite for this page."""
    probe = lines[:3] + lines[-3:]
    for ln in probe:
        m = _ATTACH_RE.match(ln)
        if m:
            # Only the 決算短信 has a summary followed by numbered attachments; a
            # 補足資料 with the same ― N ― footer is cited by the plain number.
            return f"添付{int(m.group(1))}" if kind == "決算短信" else str(int(m.group(1)))
    if kind != "決算短信":
        # A footer such as "All Rights Reserved. 10" is the printed number; a lone
        # "10" near the edge of the page may be a chart's axis tick, so it ranks second.
        for pattern in (_TRAILING_NUMBER_RE.search, _LONE_NUMBER_RE.match):
            for ln in probe:
                m = pattern(ln)
                if m and 1 <= int(m.group(1)) <= 400:
                    return str(int(m.group(1)))
    return str(index)


def dedupe_labels(pages: list[Page]) -> None:
    """Two pages must not answer to one citation; the later one keeps its PDF index."""
    seen: set[str] = set()
    for p in pages:
        if p.label in seen:
            p.label = str(p.index) if str(p.index) not in seen else f"p{p.index}"
        seen.add(p.label)


def page_heading(lines: list[str], running: set[str]) -> str:
    for ln in lines:
        if ln in running or _ATTACH_RE.match(ln) or _LONE_NUMBER_RE.match(ln):
            continue
        if ln.lower().startswith("copyright") or _TRAILING_NUMBER_RE.search(ln):
            continue
        if len(ln) < 2:
            continue
        return ln[:48]
    return "(文字なし。図のみのページ)" if not lines else lines[0][:48]


def build_doc(path: Path, position: int) -> Doc:
    import pypdfium2 as pdfium  # imported here so the pure functions stay testable

    pdf = pdfium.PdfDocument(str(path))
    texts = [normalise(pdf[i].get_textpage().get_text_range()) for i in range(len(pdf))]
    kind, title = classify(texts[0] if texts else "", position, path.name)
    freq = Counter(ln for t in texts for ln in set(_lines(t)))
    running = {ln for ln, n in freq.items() if n >= 3}
    doc = Doc(name=kind, kind=kind, title=title, file=path.name)
    for i, text in enumerate(texts, start=1):
        lines = _lines(text)
        doc.pages.append(
            Page(
                index=i,
                label=page_label(lines, i, kind),
                heading=page_heading(lines, running),
                text=text,
                numbers=len(_NUMBER_RE.findall(text)),
            )
        )
    dedupe_labels(doc.pages)
    return doc


def build(code: str, date: str) -> list[Doc]:
    folder = ARCHIVE_ROOT / date
    files = sorted(folder.glob(f"{code}_*.pdf"))
    if not files:
        raise SystemExit(f"no PDFs for {code} under {folder}; run jp_earnings(mode='fetch') first")
    docs = [build_doc(f, i) for i, f in enumerate(files, start=1)]
    seen: Counter[str] = Counter()
    for d in docs:
        seen[d.kind] += 1
        if seen[d.kind] > 1:
            d.name = f"{d.kind}{seen[d.kind]}"
    return docs


def sources_path(code: str, date: str) -> Path:
    return ARCHIVE_ROOT / date / f"{code}.sources.json"


def ledger_path(code: str, date: str) -> Path:
    return REPORTS_ROOT / code / f"{date}.sources.md"


def save_sources(docs: list[Doc], code: str, date: str) -> Path:
    out = sources_path(code, date)
    out.write_text(
        json.dumps({"code": code, "date": date, "docs": [d.to_json() for d in docs]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def load_sources(path: Path) -> list[Doc]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Doc.from_json(d) for d in data["docs"]]


def render_ledger(docs: list[Doc], code: str, date: str, today: str | None = None) -> str:
    today = today or dt.date.today().isoformat()
    total = sum(len(d.pages) for d in docs)
    out = [
        f"# 原資料の台帳: {code} {date} 開示分",
        "",
        f"作成: {today} / 保存先: `~/.vibe-trading/imports/tdnet/{date}/` / ページごとの本文は同じ場所の `{code}.sources.json`(リポジトリには置かない)",
        "",
        "使い方: 原稿を書く前に全ページを読み、「要点」を筆者が1行で埋める。数字と主語を入れ、見出しの写しにしない。",
        "記事に関係しないページは「—」と書く。【】を残したままでは `scripts/fact_check.py` が止まる。",
        f"全{total}ページ。読み終えたら末尾の「読んだ後に書くこと」を埋める。",
        "",
        "## 資料",
        "",
        "| 略称 | 表題 | ファイル | ページ数 |",
        "|---|---|---|---:|",
    ]
    for d in docs:
        out.append(f"| {d.name} | {d.title} | {d.file} | {len(d.pages)} |")
    for d in docs:
        out += ["", f"## {d.name}({len(d.pages)}ページ)", "", "| ページ | 見出し(機械抽出) | 数値の数 | 要点(筆者が記入) |", "|---|---|---:|---|"]
        for p in d.pages:
            heading = p.heading.replace("|", "｜")
            out.append(f"| {p.label} | {heading} | {p.numbers} | 【】 |")
    out += [
        "",
        "## 読んだ後に書くこと(筆者が記入)",
        "",
        "- 記事の核になる問い: 【】",
        "- その答えが載っている資料とページ: 【】",
        "- 会社が書いていないので筆者が計算する必要があるもの: 【】",
        "- 短信のサマリーにはなく、添付や説明資料で初めて分かったこと: 【】",
        "",
    ]
    return "\n".join(out)


def ledger_placeholders(path: Path) -> list[str]:
    """Rows of the ledger still carrying 【】. Empty when it has been filled."""
    found: list[str] = []
    for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # Only the rows the author fills in count; the instructions mention 【】 too.
        if line.startswith(("|", "- ")) and _PLACEHOLDER_RE.search(line):
            cell = line.split("|")[1].strip() if line.startswith("|") else line.strip()
            found.append(f"L{no} {cell[:30]}")
    return found


def find(docs: list[Doc], term: str, width: int = 60) -> list[str]:
    term_n = normalise(term)
    hits: list[str] = []
    for d in docs:
        for p in d.pages:
            for m in re.finditer(re.escape(term_n), p.text):
                start, end = max(0, m.start() - width // 2), m.end() + width // 2
                snippet = p.text[start:end].replace("\n", " ")
                hits.append(f"{d.name} {p.label}ページ: …{snippet}…")
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("code")
    parser.add_argument("date", help="disclosure date, YYYY-MM-DD")
    parser.add_argument("--find", metavar="TERM", help="list the pages mentioning TERM and stop")
    parser.add_argument("--force", action="store_true", help="overwrite a ledger that already exists")
    args = parser.parse_args(argv)

    src = sources_path(args.code, args.date)
    docs = load_sources(src) if src.exists() and not args.force else build(args.code, args.date)
    if not src.exists() or args.force:
        save_sources(docs, args.code, args.date)
    if args.find:
        hits = find(docs, args.find)
        print("\n".join(hits) if hits else f"「{args.find}」はどの資料にもない")
        return 0

    ledger = ledger_path(args.code, args.date)
    if ledger.exists() and not args.force:
        left = ledger_placeholders(ledger)
        print(f"ledger exists: {ledger} ({len(left)} rows still 【】)")
        return 0
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(render_ledger(docs, args.code, args.date), encoding="utf-8")
    for d in docs:
        print(f"{d.name}: {d.file} {len(d.pages)} pages — {d.title}")
    print(f"wrote {ledger}; page texts in {src}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
