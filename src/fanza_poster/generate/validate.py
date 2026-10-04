"""生成文の検証: 事実(数字)の照合・NG語・文字数・#PR の付与。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Product
from ..textutil import URL_RE, X_MAX_WEIGHTED, find_words, mask_words, nfkc, weighted_length


@dataclass
class Facts:
    """生成AIに渡す商品データ。これ以外の事実(数字)は文面に書かせない。"""

    content_id: str
    title: str
    genre_names: list[str]
    actresses: list[str]
    maker: str
    series: str
    price: int
    list_price: int
    discount_rate: int
    price_from: bool
    release_date: str
    sale_end: str
    campaign_title: str
    genre_category: str = ""

    @classmethod
    def from_product(cls, p: Product, ng_words: list[str], genre_category: str = "") -> "Facts":
        return cls(
            content_id=p.content_id,
            title=mask_words(p.title, ng_words),
            # 露骨な語を含むジャンル名は渡さない
            genre_names=[g for g in p.genre_names if not find_words(g, ng_words)],
            actresses=list(p.actresses),
            maker=p.maker,
            series=p.series if not find_words(p.series, ng_words) else "",
            price=p.price,
            list_price=p.list_price,
            discount_rate=p.discount_rate,
            price_from=p.price_from,
            release_date=p.release_date,
            sale_end=p.sale_end,
            campaign_title=p.campaign_title if not find_words(p.campaign_title, ng_words) else "",
            genre_category=genre_category,
        )


def yen(value: int, price_from: bool = False) -> str:
    return f"{value:,}円" + ("〜" if price_from else "")


def date_ja(value: str) -> str:
    """'2026-10-05' → '2026年10月5日'"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m:
        return ""
    return f"{int(m.group(1))}年{int(m.group(2))}月{int(m.group(3))}日"


def datetime_ja(value: str) -> str:
    """'2026-10-10 09:59' → '10月10日 9:59'"""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", value or "")
    if not m:
        return ""
    text = f"{int(m.group(1))}月{int(m.group(2))}日"
    if m.group(3):
        text += f" {int(m.group(3))}:{m.group(4)}"
    return text


def _ints(text: str) -> set[int]:
    return {int(x) for x in re.findall(r"\d+", _num_text(text))}


def _num_text(text: str) -> str:
    """数字の照合用に正規化(全角→半角、桁区切りのカンマを除去)。"""
    t = nfkc(text)
    return re.sub(r"(?<=\d),(?=\d{3})", "", t)


def allowed_numbers(f: Facts) -> set[int]:
    allowed = {f.price, f.list_price, f.discount_rate}
    if f.discount_rate % 10 == 0:
        allowed.add(f.discount_rate // 10)  # 「7割引」など
    for value in (f.release_date, f.sale_end):
        for n in re.findall(r"\d+", value or ""):
            allowed.add(int(n))
            if len(n) == 4:
                allowed.add(int(n) % 100)
    for text in [f.title, f.maker, f.series, f.campaign_title, *f.genre_names, *f.actresses]:
        allowed |= _ints(text)
    return allowed


@dataclass
class ValidationResult:
    ok: bool
    body: str
    final_text: str
    errors: list[str] = field(default_factory=list)


def clean_body(text: str) -> str:
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "「『\"" and t[-1] in "」』\"":
        t = t[1:-1].strip()
    # 生成AIが付けた #PR は取り除き、末尾にシステムで付け直す
    t = re.sub(r"[#＃]\s*[PＰ][RＲ]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def compose(body: str, pr_suffix: str) -> str:
    return body.rstrip() + pr_suffix


def _has_emoji(text: str) -> bool:
    return any(ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF for c in text)


def validate_single(
    raw_text: str,
    facts: Facts,
    *,
    min_length: int,
    max_length: int,
    ng_words: list[str],
    pr_suffix: str,
) -> ValidationResult:
    body = clean_body(raw_text)
    errors: list[str] = []
    final = compose(body, pr_suffix)

    if not body:
        return ValidationResult(False, body, final, ["本文が空です"])

    length = len(body.replace("\n", ""))
    if length < min_length or length > max_length:
        errors.append(f"文字数が{length}字です({min_length}〜{max_length}字にしてください)")
    if body.count("\n") > 2:
        errors.append("改行が多すぎます(2回まで)")
    if URL_RE.search(body) or re.search(r"www\.|\.co\.jp|\.com", body, re.IGNORECASE):
        errors.append("URLを含めないでください")
    if "#" in body or "＃" in body:
        errors.append("ハッシュタグを含めないでください")
    if "@" in body or "＠" in body:
        errors.append("@(メンション)を含めないでください")
    if _has_emoji(body):
        errors.append("絵文字を含めないでください")
    hits = find_words(body, ng_words)
    if hits:
        errors.append(f"使えない語が含まれています: {', '.join(hits)}")

    num_text = _num_text(body)
    allowed = allowed_numbers(facts)
    for n in sorted({int(x) for x in re.findall(r"\d+", num_text)}):
        if n not in allowed:
            errors.append(f"商品データにない数字「{n}」が含まれています")
    for m in re.finditer(r"(\d+)\s*%", num_text):
        if int(m.group(1)) != facts.discount_rate:
            errors.append(f"割引率は{facts.discount_rate}%です(「{m.group(0)}」は誤り)")
    for m in re.finditer(r"(\d+)\s*割", num_text):
        if facts.discount_rate % 10 != 0 or int(m.group(1)) != facts.discount_rate // 10:
            errors.append(f"「{m.group(0)}」は割引率{facts.discount_rate}%と一致しません。%で書いてください")
    for m in re.finditer(r"(\d+)\s*円", num_text):
        if int(m.group(1)) not in (facts.price, facts.list_price):
            errors.append(f"価格「{m.group(0)}」は商品データと一致しません")

    if not final.rstrip().endswith("#PR"):
        errors.append("末尾が #PR になっていません")
    if weighted_length(final) > X_MAX_WEIGHTED:
        errors.append("Xの文字数上限を超えています")

    # 重複したメッセージを除く
    uniq = list(dict.fromkeys(errors))
    return ValidationResult(not uniq, body, final, uniq)


def validate_label(label: str, max_chars: int, ng_words: list[str]) -> str | None:
    """まとめ投稿用ラベルを検証して整形する。使えない場合は None。"""
    t = clean_body(label).replace("\n", " ").strip()
    if not t:
        return None
    if re.search(r"\d", nfkc(t)) or re.search(r"[#＃@＠]", t) or URL_RE.search(t) or _has_emoji(t):
        return None
    if find_words(t, ng_words):
        return None
    if len(t) > max_chars:
        t = t[: max_chars - 1] + "…"
    return t
