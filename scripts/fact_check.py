#!/usr/bin/env python
"""Reconcile a memo's page citations with the saved filings.

The memo cites its sources inline: ``(決算説明資料18ページ)``, ``(決算短信 添付8ページ)``,
``(同5ページ)``, or a table heading such as ``### 中間期実績(百万円、決算短信)``.
For every citation this script takes the numbers in the same sentence (or the
same table row) and looks for each one on the cited page of the saved PDF,
using the page texts that ``scripts/source_sheet.py`` wrote.

What it reports:

* **NG** — a document the archive does not hold, a page the document does not
  have, ``同`` with nothing before it, or a cited page that carries none of the
  sentence's numbers (the page number is probably wrong). The ledger from
  ``source_sheet.py`` must exist and have no 【】 left: reading every page is
  part of the procedure, not an option.
* **注意** — a single number that is not on the cited page. The hint says where
  else in the same document it appears, or that no document carries it, in
  which case it is either the author's own arithmetic (say so in the text) or
  a mistake. A sentence that says 筆者の計算 / 算出 / 換算 is held to the
  lighter standard: its figures are expected to be derived.

Units are reconciled: 797億円 in the memo matches 79,733 (百万円) in the filing;
21.5% matches 21.5, 21.54, or the index form 121.5. Dates, fiscal periods,
quarter numbers and page numbers are not treated as figures.

Usage:
    .venv/bin/python scripts/fact_check.py reports/companies/6323/2026-10-08.md
    .venv/bin/python scripts/fact_check.py reports/companies/6323/2026-10-08.md --where 1,038億円
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import source_sheet  # noqa: E402

# Aliases a memo may use for each document kind, longest first inside the regex.
ALIASES: dict[str, str] = {
    "決算説明会資料": "決算説明資料",
    "決算説明資料": "決算説明資料",
    "説明会資料": "決算説明資料",
    "説明資料": "決算説明資料",
    "決算短信": "決算短信",
    "短信": "決算短信",
    "決算補足説明資料": "決算補足資料",
    "業績補足資料": "決算補足資料",
    "決算補足資料": "決算補足資料",
    "補足説明資料": "決算補足資料",
    "補足資料": "決算補足資料",
    "お知らせ": "お知らせ",
}
_ALIAS_ALT = "|".join(sorted(ALIASES, key=len, reverse=True))
CITE_RE = re.compile(
    rf"(?P<doc>{_ALIAS_ALT}|同)\s*の?\s*(?P<att>添付)?\s*(?P<p1>\d{{1,3}})"
    rf"(?:\s*[〜~\-]\s*(?P<p2>\d{{1,3}}))?\s*ページ"
)
DOC_ONLY_RE = re.compile(rf"(?P<doc>{_ALIAS_ALT})")

# Figures that are not figures: dates, periods, counts of things, page numbers.
_STRIP_RES = [
    re.compile(p)
    for p in (
        r"\d{4}年\d{1,2}月期", r"\d{4}年\d{1,2}月\d{1,2}日", r"\d{4}年\d{1,2}月", r"\d{4}年度", r"\d{4}年",
        r"\d{1,2}月\d{1,2}日", r"\d{1,2}[〜~]\d{1,2}月", r"\d{1,2}月", r"\d{1,2}/\d{1,2}", r"\d{1,2}:\d{2}",
        r"第\d四半期", r"\d+ページ", r"\d{1,2}日", r"\d+か月", r"\d+営業日", r"\d+年", r"\d+期", r"\d+人", r"\d+名",
        r"\d+回", r"\d+項目", r"\d+つ", r"\d+社", r"\d+本", r"\d+枚", r"\d+割", r"\d+行", r"\d+件", r"\d+段階",
        r"\d+台", r"\d+四半期", r"\d+週", r"\d+時", r"\d+交代", r"\d+シフト", r"(?<![\d,])1株",
    )
]
NUMBER_RE = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?\s*"
    r"(?P<unit>億円|百万円|万円|円|億ドル|百万ドル|ドル|%|ポイント|倍|株)?"
)
_CALC_RE = re.compile(r"筆者|算出|計算|換算|推論|逆算")
# A citation to a document that is not this disclosure's: the July deck, last
# year's filing, the company site. Those are not in the archive for this date.
_EXTERNAL_RE = re.compile(r"(?:\d{1,2}月の|前回の|前期の|前年の|前年同期の|昨年の|期初の|第\d四半期の|会社サイトの|過去の)\s*$")
# Fewer numbers than this on a cited page means the slide is an image; the
# text layer cannot confirm or deny anything.
_IMAGE_PAGE_NUMBERS = 3
_PAREN_OPEN, _PAREN_CLOSE = "(（", ")）"


@dataclass
class Finding:
    line: int
    rule: str
    text: str
    hint: str
    hard: bool = True

    def render(self) -> str:
        level = "NG" if self.hard else "注意"
        return f"{level} L{self.line} [{self.rule}] {self.text[:70]}{'…' if len(self.text) > 70 else ''}\n    → {self.hint}"


@dataclass
class Figure:
    value: float
    unit: str | None
    raw: str

    @property
    def specific(self) -> bool:
        """Large or comma-formatted figures rarely appear on a page by coincidence."""
        return self.value >= 100 or "," in self.raw


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def page_numbers(text: str) -> set[float]:
    """Every number on a page, commas removed."""
    out: set[float] = set()
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", _norm(text)):
        token = m.group(0).replace(",", "")
        try:
            out.add(float(token))
        except ValueError:
            continue
    return out


def figures(text: str) -> list[Figure]:
    """Numbers in a sentence or table row that a filing could carry."""
    text = _norm(text)
    text = CITE_RE.sub(" ", text)
    for rx in _STRIP_RES:
        text = rx.sub(" ", text)
    out: list[Figure] = []
    seen: set[tuple[float, str | None]] = set()
    for m in NUMBER_RE.finditer(text):
        num, dec, unit = m.group("num"), m.group("dec") or "", m.group("unit")
        if not unit and "," not in num and not dec:
            continue  # a bare integer: a count, an ordinal, a label
        fig = Figure(float(num.replace(",", "") + dec), unit, m.group(0).strip())
        if (fig.value, fig.unit) not in seen:  # the same figure twice in a sentence is one check
            seen.add((fig.value, fig.unit))
            out.append(fig)
    return out


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def matches(fig: Figure, nums: set[float]) -> bool:
    if any(_close(fig.value, n) for n in nums):
        return True
    decimals = len(fig.raw.split(".")[1].rstrip("%倍円株ポイント").strip()) if "." in fig.raw else 0
    if fig.unit in ("億円", "億ドル"):
        # The filing prints 百万円 (or 百万ドル); the memo rounds or truncates to 億,
        # or keeps two decimals (4.14億ドル for 414百万ドル). A slide that already
        # prints 億円 may carry a decimal the memo dropped (570.4 for 570億円).
        return any(
            _close(n / 100, fig.value)
            or (n >= 100 and (math.floor(n / 100) == fig.value or round(n / 100) == fig.value))
            or (decimals <= 1 and _close(round(n, decimals), fig.value))
            for n in nums
        )
    if fig.unit == "万円":
        return any(_close(n * 100, fig.value) for n in nums)
    if fig.unit in ("百万円", "百万ドル", None) and fig.value >= 1000 and float(fig.value).is_integer():
        # The memo's table keeps the filing's 百万円; a slide shows the same figure in 億円.
        return any(_close(n, math.floor(fig.value / 100)) or _close(n, round(fig.value / 100)) for n in nums)
    decimals = len(fig.raw.split(".")[1].rstrip("%倍円株ポイント").strip()) if "." in fig.raw else 0
    if decimals <= 1 and any(_close(round(n, decimals), fig.value) for n in nums):
        # The memo shows one decimal; the filing may carry two.
        return True
    if fig.unit == "%" and -100 < fig.value < 100:
        # A filing prints a change as an index (前年同期比 122.8%); the memo writes +22.8%.
        return any(_close(round(n, decimals), fig.value + 100) for n in nums)
    return False


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    depth, current = 0, ""
    for ch in text:
        current += ch
        if ch in _PAREN_OPEN:
            depth += 1
        elif ch in _PAREN_CLOSE:
            depth = max(0, depth - 1)
        elif ch == "。" and depth == 0:
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


@dataclass
class Target:
    alias: str
    doc: str | None  # canonical kind; None when unresolved
    labels: list[str]  # page labels; empty means "anywhere in the document"
    external: bool = False  # another date's document (7月の決算説明資料): not in this archive


@dataclass
class Scope:
    """One sentence or table row and every citation it carries."""

    line: int
    text: str
    targets: list[Target] = field(default_factory=list)


class _Resolver:
    def __init__(self) -> None:
        self.last_doc: str | None = None
        self.last_attached = False  # the previous citation of last_doc used 添付 page numbers
        self.findings: list[Finding] = []

    def cite(self, m: re.Match, line_no: int) -> Target:
        alias = m.group("doc")
        if alias != "同" and _EXTERNAL_RE.search(m.string[: m.start()]):
            qualifier = _EXTERNAL_RE.search(m.string[: m.start()]).group(0).strip()
            return Target(qualifier + alias, None, [], external=True)
        attached = bool(m.group("att"))
        if alias == "同":
            doc = self.last_doc
            if doc is None:
                self.findings.append(Finding(line_no, "出典", m.group(0), "「同」の前に資料名の引用がない"))
            # 「同15ページ」 after 「決算短信 添付15〜16ページ」 means attachment page 15.
            attached = attached or self.last_attached
        else:
            doc = ALIASES.get(alias)
            self.last_doc = doc
        self.last_attached = attached
        p1, p2 = int(m.group("p1")), int(m.group("p2") or m.group("p1"))
        prefix = "添付" if attached else ""
        return Target(alias, doc, [f"{prefix}{p}" for p in range(p1, p2 + 1)])

    def doc_only(self, m: re.Match) -> Target:
        doc = ALIASES[m.group("doc")]
        self.last_doc = doc
        self.last_attached = False
        return Target(m.group("doc"), doc, [])


def scopes(memo: str) -> tuple[list[Scope], list[Finding]]:
    """Every citation in the memo with the text it governs."""
    out: list[Scope] = []
    res = _Resolver()
    table_target: Target | None = None
    in_fence = False

    for no, raw in enumerate(memo.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("#"):
            table_target = None
            m = CITE_RE.search(line)
            if m:
                table_target = res.cite(m, no)
            else:
                m2 = DOC_ONLY_RE.search(line)
                if m2:
                    table_target = res.doc_only(m2)
            continue
        if line.startswith("|"):
            if set(line) <= set("|-: "):
                continue
            inline = [res.cite(m, no) for m in CITE_RE.finditer(line)]
            if inline:
                out.append(Scope(no, line, inline))
            elif table_target:
                out.append(Scope(no, line, [table_target]))
            continue
        if not line or line.startswith(("<!--", "作成:", "手順:", "題名:")):
            continue  # a blank line sits between a heading and its table; it does not end the heading's reach
        table_target = None  # prose does
        for sentence in _sentences(line):
            targets = [res.cite(m, no) for m in CITE_RE.finditer(sentence)]
            if targets:
                out.append(Scope(no, sentence, targets))
    return out, res.findings


def check(memo: str, docs: list[source_sheet.Doc], *, strict: bool = True) -> tuple[list[Finding], int, int]:
    """Return findings, the number of citations and the number of figures checked.

    ``strict`` decides whether a citation to a document the archive does not
    hold is a failure (the procedure for a new memo) or a warning (a memo
    written before the archive held every document it cites).
    """
    by_kind: dict[str, list[source_sheet.Doc]] = {}
    for d in docs:
        by_kind.setdefault(d.kind, []).append(d)
    nums_of: dict[tuple[str, str], set[float]] = {(d.name, p.index): page_numbers(p.text) for d in docs for p in d.pages}
    scope_list, findings = scopes(memo)
    cites = sum(len(s.targets) for s in scope_list)
    checked = 0
    for sc in scope_list:
        figs = figures(sc.text)
        calc = bool(_CALC_RE.search(sc.text))
        cited: list[tuple[source_sheet.Doc, list[source_sheet.Page]]] = []
        for t in sc.targets:
            if t.external:
                findings.append(Finding(sc.line, "出典", sc.text, f"「{t.alias}」は今回の開示の資料ではないので照合していない(手元の資料で確かめる)", hard=False))
                continue
            if t.doc is None:
                if t.alias != "同":
                    findings.append(Finding(sc.line, "出典", sc.text, f"「{t.alias}」は資料の種類として読めない"))
                continue
            candidates = by_kind.get(t.doc, [])
            if not candidates:
                held = ", ".join(d.name for d in docs)
                hint = f"「{t.doc}」が保存されていない(保存済み: {held})。会社サイトから取った資料は <code>_<名前>.pdf として同じ場所に置く"
                findings.append(Finding(sc.line, "出典", sc.text, hint, hard=strict))
                continue
            doc = candidates[0]
            pages: list[source_sheet.Page] = []
            for label in t.labels:
                p = doc.page_by_label(label)
                if p is None:
                    span = f"{doc.pages[0].label}〜{doc.pages[-1].label}"
                    findings.append(Finding(sc.line, "出典", sc.text, f"{doc.name}に{label}ページはない(全{len(doc.pages)}ページ、表記は{span})"))
                else:
                    pages.append(p)
            if t.labels and not pages:
                continue
            cited.append((doc, pages or doc.pages, bool(t.labels)))
        if not figs or not cited:
            continue
        checked += len(figs)
        union: set[float] = set().union(*(nums_of[(d.name, p.index)] for d, ps, _ in cited for p in ps))
        # A sentence may cite two pages for two figures; the page is wrong only
        # when none of the cited pages carries any of the sentence's figures.
        explicit = [(d, ps) for d, ps, is_page in cited if is_page]
        if explicit and not any(matches(f, union) for f in figs):
            labels = "、".join(f"{d.name}{'・'.join(p.label for p in ps)}ページ" for d, ps in explicit)
            if sum(len(nums_of[(d.name, p.index)]) for d, ps in explicit for p in ps) < _IMAGE_PAGE_NUMBERS:
                findings.append(Finding(sc.line, "ページ", sc.text, f"{labels}は文字層に数字がない(画像のページ)。目で確かめる", hard=False))
                continue
            where = _where(figs, docs)
            hint = f"{labels}に文中の数字が一つもない。" + (f"見つかった場所: {where}" if where else "どの資料にもない")
            if calc:
                hint += "(算出の文なので、計算の元の数字がそのページにあれば可)"
            findings.append(Finding(sc.line, "ページ", sc.text, hint, hard=not calc))
            if not calc:
                continue  # the page is wrong; listing each figure as missing adds nothing
        for f in figs:
            if matches(f, union):
                continue
            findings.append(Finding(sc.line, "数字", sc.text, _miss_hint(f, cited, docs, nums_of, calc), hard=False))
    return findings, cites, checked


def _miss_hint(
    f: Figure,
    cited: list[tuple[source_sheet.Doc, list[source_sheet.Page]]],
    docs: list[source_sheet.Doc],
    nums_of: dict[tuple[str, str], set[float]],
    calc: bool,
) -> str:
    if f.specific:
        for doc, pages, _ in cited:
            elsewhere = [p.label for p in doc.pages if p not in pages and matches(f, nums_of[(doc.name, p.index)])]
            if elsewhere:
                return f"{f.raw} は{doc.name}の{'・'.join(elsewhere[:4])}ページにある"
        other = [d.name for d in docs if all(d is not c for c, _, _ in cited) and any(matches(f, nums_of[(d.name, p.index)]) for p in d.pages)]
        if other:
            return f"{f.raw} は引用した資料にはなく、{'・'.join(other)}にある"
    tail = "(算出の文なので、筆者の計算なら可)" if calc else "(筆者の計算なら「筆者の計算」と書く。違うなら転記の誤り)"
    return f"{f.raw} は引用したページにない" + tail


def _where(figs: list[Figure], docs: list[source_sheet.Doc]) -> str:
    hits: list[str] = []
    for d in docs:
        for p in d.pages:
            nums = page_numbers(p.text)
            found = [f.raw for f in figs if f.specific and matches(f, nums)]
            if found:
                hits.append(f"{d.name}{p.label}ページ({', '.join(found[:3])})")
    return "、".join(hits[:4])


def where(token: str, docs: list[source_sheet.Doc]) -> list[str]:
    figs = figures(token + " ")
    if not figs:
        figs = [Figure(float(_norm(token).replace(",", "")), None, token)]
    out: list[str] = []
    for d in docs:
        for p in d.pages:
            if any(matches(f, page_numbers(p.text)) for f in figs):
                out.append(f"{d.name} {p.label}ページ: {p.heading}")
    return out


def locate_sources(memo: Path) -> tuple[Path, Path]:
    code, date = memo.parent.name, memo.stem.split(".")[0]
    return source_sheet.sources_path(code, date), source_sheet.ledger_path(code, date)


def check_file(
    memo: Path, sources: Path | None = None, ledger: Path | None = None, *, require_ledger: bool = True
) -> tuple[list[Finding], int, int]:
    """Check one memo. With ``require_ledger`` off, a missing ledger or page texts
    is a warning: memos written before the ledger existed (2026-10-11) have none."""
    auto_sources, auto_ledger = locate_sources(memo)
    sources = sources or auto_sources
    ledger = ledger or auto_ledger
    if not sources.exists():
        hint = f"原資料の本文がない: {sources}(scripts/source_sheet.py を先に実行)"
        return [Finding(0, "読了表", str(memo), hint, hard=require_ledger)], 0, 0
    docs = source_sheet.load_sources(sources)
    findings: list[Finding] = []
    if not ledger.exists():
        findings.append(Finding(0, "読了表", str(ledger), "原資料の読了表がない(scripts/source_sheet.py を実行し、全ページの要点を埋める)", hard=require_ledger))
    else:
        left = source_sheet.ledger_placeholders(ledger)
        if left:
            findings.append(Finding(0, "読了表", ledger.name, f"読了表に未記入が{len(left)}行ある: {', '.join(left[:5])}{'…' if len(left) > 5 else ''}"))
    more, cites, checked = check(memo.read_text(encoding="utf-8"), docs, strict=require_ledger)
    return findings + more, cites, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("memo", type=Path)
    parser.add_argument("--sources", type=Path, default=None, help="<code>.sources.json; found from the memo path by default")
    parser.add_argument("--ledger", type=Path, default=None, help="<date>.sources.md; found from the memo path by default")
    parser.add_argument("--where", metavar="FIGURE", help="list the pages carrying FIGURE (e.g. 1,038億円) and stop")
    args = parser.parse_args(argv)
    if args.where:
        src = args.sources or locate_sources(args.memo)[0]
        hits = where(args.where, source_sheet.load_sources(src))
        print("\n".join(hits) if hits else f"{args.where} はどの資料にもない")
        return 0
    findings, cites, checked = check_file(args.memo, args.sources, args.ledger)
    for f in findings:
        print(f.render())
    hard = sum(1 for f in findings if f.hard)
    print(f"fact check: {hard} NG, {len(findings) - hard} 注意 ({cites} citations, {checked} figures)")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
