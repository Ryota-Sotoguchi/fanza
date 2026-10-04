"""まとめ投稿(割引率TOP5)の文面。数字はAPIの値だけをテンプレートに入れる。"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Product
from ..textutil import find_words, fits_x, truncate, weighted_length
from .validate import yen

PERIOD_DAILY = "D"
PERIOD_WEEKLY = "W"


@dataclass
class SummaryEntry:
    rank: int
    content_id: str
    label: str
    price: int
    list_price: int
    discount_rate: int
    price_from: bool
    affiliate_url: str


def _sa(period: str, entries: list[SummaryEntry]) -> tuple[str, list[str], str]:
    when = "今週" if period == PERIOD_WEEKLY else "本日"
    header = f"【{when}のFANZA動画セール 割引率TOP{len(entries)}】"
    lines = [f"{e.rank}位 {e.discount_rate}%OFF {e.label}" for e in entries]
    return header, lines, "リンクはリプ欄から"


def _sb(period: str, entries: list[SummaryEntry]) -> tuple[str, list[str], str]:
    when = "今週" if period == PERIOD_WEEKLY else "本日"
    header = f"{when}の割引率ランキング(FANZA動画)"
    lines = [f"{e.rank}. {e.label} {yen(e.price, e.price_from)}/{e.discount_rate}%OFF" for e in entries]
    return header, lines, ""


SUMMARY_FORMATS = {"SA": _sa, "SB": _sb}


def render_summary(fmt: str, period: str, entries: list[SummaryEntry], pr_suffix: str) -> str:
    header, lines, tail = SUMMARY_FORMATS[fmt](period, entries)
    parts = [header, *lines]
    if tail:
        parts.append(tail)
    return "\n".join(parts).rstrip() + pr_suffix


def build_summary_text(
    fmt: str, period: str, entries: list[SummaryEntry], pr_suffix: str, label_max_chars: int
) -> str:
    """Xの文字数上限に収まるまでラベルを短くして組み立てる。"""
    for max_chars in range(label_max_chars, 3, -1):
        shortened = [
            SummaryEntry(**{**e.__dict__, "label": truncate(e.label, max_chars)}) for e in entries
        ]
        text = render_summary(fmt, period, shortened, pr_suffix)
        if fits_x(text):
            return text
    raise ValueError(f"まとめ投稿がXの文字数上限に収まりません({weighted_length(text)})")


def build_summary_reply(entries: list[SummaryEntry], line_template: str, footer: str) -> str:
    lines = [line_template.format(rank=e.rank, url=e.affiliate_url) for e in entries]
    if footer:
        lines.append(footer)
    text = "\n".join(lines)
    if not fits_x(text):
        raise ValueError("まとめ投稿のリプライがXの文字数上限に収まりません(top_n を減らしてください)")
    return text


def fallback_label(p: Product, max_chars: int, ng_words: list[str], genre_name: str) -> str:
    """生成AIのラベルが使えない場合の代替ラベル(出演者名やメーカー名から作る)。"""
    candidates = []
    if p.actresses:
        candidates.append(f"{p.actresses[0]}出演作")
        candidates.append(p.actresses[0])
    if p.maker:
        candidates.append(f"{p.maker}作品")
    candidates.append(f"{genre_name}作品")
    for c in candidates:
        if not find_words(c, ng_words) and not any(ch.isdigit() for ch in c):
            return truncate(c, max_chars)
    return truncate("セール対象作品", max_chars)
