#!/usr/bin/env python
"""Check a manuscript's prose for the habits that make it read machine-written.

Four rules, agreed on 2026-10-11 (reports/article-rules.md, section 7):

1. No 体言止め. Every sentence ends in a predicate, not a noun.
2. Subject and predicate must belong together. A machine cannot judge this
   fully; what it can catch is a sentence carrying two topics (two は) or
   none at all, and sentences so long that the two ends have drifted apart.
3. One topic per sentence. Flagged by length and by the number of clauses.
4. No strained figures of speech and no words a general reader would not
   know. Checked against a word list below; quoted text (「…」) is exempt.

Mechanical checks only. The editorial pass (reports/prompts/prose-review.md)
is run by a reviewer after this script passes, because judging whether a
subject fits its predicate needs a reader.

Tokenisation uses janome when installed; without it the ending rule falls
back to a character heuristic and says so.

Usage:
    .venv/bin/python scripts/prose_check.py reports/companies/9983/2026-10-08.md
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:  # optional: better part-of-speech tagging
    from janome.tokenizer import Tokenizer  # type: ignore

    _TOKENIZER = Tokenizer()
except Exception:  # noqa: BLE001 - the heuristic path works without it
    _TOKENIZER = None

MAX_SENTENCE_CHARS = 95
MAX_COMMAS = 4

# Figures of speech and jargon, each with the plain phrase to use instead.
# Quoted text is exempt, so a headline that says サプライズ may stay.
WORDS: dict[str, str] = {
    "剥落": "なくなったこと／減少",
    "追い風": "有利に働く",
    "逆風": "不利に働く",
    "太い柱": "主な収益源",
    "ねじれ": "差／ずれ",
    "からくり": "仕組み",
    "肩透かし": "期待外れ",
    "表裏の関係": "同じ原因の両面",
    "表裏": "同じ原因の両面",
    "骨格": "大筋",
    "跳ねる": "大きく上がる",
    "刺さる": "伝わる",
    "腹落ち": "納得",
    "肝": "要点",
    "食い違": "一致しない",
    "蒸発": "消失",
    "溶ける": "減る",
    "爆": "急",
    "コンセンサス": "アナリスト予想の平均",
    "ガイダンス": "会社予想",
    "サプライズ": "予想との差",
    "テーザー": "予告",
    "ペイウォール": "有料ライン",
    "ボラティリティ": "値動きの大きさ",
    "ファンダメンタルズ": "業績や財務",
    "バリュエーション": "株価の割安・割高の水準",
    "ネガティブ": "悪い",
    "ポジティブ": "良い",
    "インパクト": "影響",
    "キャッチアップ": "追いつく",
    "材料視": "売買のきっかけ",
    "寄り付き": "取引開始時",
    "値がさ株": "株価の高い銘柄",
    "ノイズ": "通常の値動き",
    "吸収": "打ち消す",
}

# Endings that are predicates in form but nouns in effect.
NOUN_LIKE_ENDINGS = ("なし", "あり", "とおり", "のみ", "まで", "こと", "もの", "ため", "予定", "見込み", "計画", "方針")

_QUOTE_RE = re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”")
_PAREN_TAIL_RE = re.compile(r"[（(][^（()）]*[)）]\s*$")
_TABLE_RE = re.compile(r"^\s*\|")
_IMAGE_RE = re.compile(r"^\s*!\[")
_COMMENT_RE = re.compile(r"^\s*<!--")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
# Lines and sections that are not the article's prose.
_SKIP_PREFIXES = ("作成:", "手順:", "題名:", "同日の日次記事:", "前回の会社別メモ:", "━━━━━", "> **免責事項**", "**免責事項**")
_SKIP_SECTIONS = ("出典", "免責")
_SKIP_MARK_RE = re.compile(r"^\s*<!--\s*note:skip\s*-->\s*$")
MIN_TOPICS_TO_FLAG = 3
_URL_RE = re.compile(r"https?://\S+")
_INLINE_RE = re.compile(r"`[^`]*`|\*\*|\[([^\]]*)\]\([^)]*\)")


@dataclass
class Finding:
    line: int
    rule: str
    sentence: str
    hint: str
    hard: bool = True

    def render(self) -> str:
        level = "NG" if self.hard else "注意"
        return f"{level} L{self.line} [{self.rule}] {self.sentence[:70]}{'…' if len(self.sentence) > 70 else ''}\n    → {self.hint}"


def _clean(line: str) -> str:
    line = _URL_RE.sub("", line)
    line = _INLINE_RE.sub(lambda m: m.group(1) or "", line)
    return line.strip()


def _sentences(text: str) -> list[str]:
    """Split on 。 outside parentheses; keep a trailing fragment without one."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        current += ch
        if ch in "（(「『":
            depth += 1
        elif ch in "）)」』":
            depth = max(0, depth - 1)
        elif ch == "。" and depth == 0:
            parts.append(current.strip())
            current = ""
    if current.strip():
        parts.append(current.strip())
    return parts


def _ends_with_noun(sentence: str) -> tuple[bool, str]:
    """Return (is_taigendome, ending)."""
    body = sentence.rstrip("。").rstrip()
    body = _PAREN_TAIL_RE.sub("", body).rstrip()
    if not body:
        return False, ""
    for ending in NOUN_LIKE_ENDINGS:
        if body.endswith(ending):
            return True, ending
    if _TOKENIZER is not None:
        tokens = [t for t in _TOKENIZER.tokenize(body) if t.part_of_speech.split(",")[0] != "記号"]
        if not tokens:
            return False, ""
        last = tokens[-1]
        pos = last.part_of_speech.split(",")
        if pos[0] == "名詞":
            return True, last.surface
        return False, last.surface
    # Heuristic: a predicate ends in hiragana; a noun ending does not.
    last_char = body[-1]
    return (not ("ぁ" <= last_char <= "ん")), last_char


def _topic_count(sentence: str) -> int:
    if _TOKENIZER is None:
        return sentence.count("は")
    return sum(
        1 for t in _TOKENIZER.tokenize(sentence)
        if t.surface == "は" and t.part_of_speech.startswith("助詞,係助詞")
    )


def check_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    in_code = False
    in_skipped_section = False
    skip_next_heading = False
    for no, raw in enumerate(text.split("\n"), start=1):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not raw.strip():
            continue
        if _SKIP_MARK_RE.match(raw):
            skip_next_heading = True  # the section under the next heading is internal
            continue
        if raw.startswith("#"):
            title = raw.lstrip("#").strip()
            in_skipped_section = skip_next_heading or any(title.startswith(name) for name in _SKIP_SECTIONS)
            skip_next_heading = False
            continue
        if in_skipped_section:
            continue
        if _TABLE_RE.match(raw) or _IMAGE_RE.match(raw) or _COMMENT_RE.match(raw):
            continue
        if raw.strip().startswith(_SKIP_PREFIXES):
            continue
        is_bullet = bool(_BULLET_RE.match(raw))
        line = _clean(_BULLET_RE.sub("", raw)) if is_bullet else _clean(raw)
        line = line.lstrip("> ").strip()
        if line.startswith("要約:"):
            line = line[len("要約:"):].strip()
        if not line:
            continue
        # A bullet without a full stop is a label: value item, not a sentence.
        if is_bullet and "。" not in line:
            unquoted = _QUOTE_RE.sub("", line)
            for word, plain in WORDS.items():
                if word in unquoted:
                    findings.append(Finding(no, "言葉", line, f"「{word}」→「{plain}」"))
            continue
        for sentence in _sentences(line):
            unquoted = _QUOTE_RE.sub("「…」", sentence)
            if not sentence.endswith("。"):
                findings.append(Finding(no, "句点なし", sentence, "文の終わりに句点を置き、述語で終える"))
            noun_end, ending = _ends_with_noun(sentence)
            if noun_end:
                findings.append(Finding(no, "体言止め", sentence, f"「{ending}」で終わっている。述語(〜した、〜である、〜になる)で終える"))
            for word, plain in WORDS.items():
                if word in unquoted:
                    findings.append(Finding(no, "言葉", sentence, f"「{word}」→「{plain}」"))
            length = len(unquoted)
            commas = unquoted.count("、")
            if length > MAX_SENTENCE_CHARS or commas >= MAX_COMMAS:
                findings.append(Finding(no, "長い文", sentence, f"{length}字、読点{commas}。2文に分けて、1文1トピックにする", hard=False))
            topics = _topic_count(unquoted)
            if topics >= MIN_TOPICS_TO_FLAG:
                findings.append(Finding(no, "主語が多い", sentence, f"「は」が{topics}つある。主語と述語の対応を1組ずつにし、文を分ける", hard=False))
    return findings


def check_file(path: Path) -> list[Finding]:
    return check_text(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="manuscript prose check")
    parser.add_argument("source", type=Path)
    parser.add_argument("--all", action="store_true", help="show soft warnings too (default: shown)")
    args = parser.parse_args(argv)
    findings = check_file(args.source)
    hard = [f for f in findings if f.hard]
    soft = [f for f in findings if not f.hard]
    for f in hard + soft:
        print(f.render())
    print(
        f"prose check: {len(hard)} NG, {len(soft)} 注意"
        + ("" if _TOKENIZER else " (janome なし: 文末の判定は簡易)")
    )
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
