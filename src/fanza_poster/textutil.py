"""文字列の正規化と、Xの文字数(加重カウント)の計算。"""

from __future__ import annotations

import re
import unicodedata

URL_RE = re.compile(r"https?://[^\s]+")
X_MAX_WEIGHTED = 280
X_URL_WEIGHT = 23


def nfkc(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "")


def norm(text: str | None) -> str:
    """全角/半角・大文字/小文字の違いを吸収した比較用の文字列。"""
    return nfkc(text).lower()


def find_words(text: str, words: list[str]) -> list[str]:
    """text に含まれる words を返す(正規化して部分一致)。"""
    target = norm(text)
    return [w for w in words if w and norm(w) in target]


def mask_words(text: str, words: list[str], mask: str = "〇") -> str:
    """NG語を伏せ字にする(生成AIに露骨な語をそのまま渡さないため)。"""
    result = nfkc(text)
    for w in sorted({nfkc(w) for w in words if w}, key=len, reverse=True):
        result = re.sub(re.escape(w), mask * len(w), result, flags=re.IGNORECASE)
    return result


def char_weight(ch: str) -> int:
    """twitter-text の設定に合わせた1文字の重み(ラテン文字など=1、日本語など=2)。"""
    cp = ord(ch)
    if (
        0x0000 <= cp <= 0x10FF
        or 0x2000 <= cp <= 0x200D
        or 0x2010 <= cp <= 0x201F
        or 0x2032 <= cp <= 0x2037
    ):
        return 1
    return 2


def weighted_length(text: str) -> int:
    """Xの文字数カウント(上限280)。URLは長さに関係なく23として数える。"""
    text = unicodedata.normalize("NFC", text)
    total = 0
    pos = 0
    for m in URL_RE.finditer(text):
        total += sum(char_weight(c) for c in text[pos : m.start()])
        total += X_URL_WEIGHT
        pos = m.end()
    total += sum(char_weight(c) for c in text[pos:])
    return total


def fits_x(text: str) -> bool:
    return weighted_length(text) <= X_MAX_WEIGHTED


def truncate(text: str, max_chars: int, ellipsis: str = "…") -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(ellipsis):
        return text[:max_chars]
    return text[: max_chars - len(ellipsis)] + ellipsis


def squash_ws(text: str) -> str:
    return re.sub(r"\s+", "", nfkc(text))
