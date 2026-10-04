"""商品のジャンル分類(VR / 単体 / シリーズ / その他)。"""

from __future__ import annotations

from ..config import GenreCategory
from ..models import Product
from ..textutil import norm


def classify(p: Product, categories: list[GenreCategory]) -> str:
    genre_set = {norm(g) for g in p.genre_names}
    title = norm(p.title)
    for cat in categories:
        if cat.is_catch_all:
            return cat.code
        if cat.genres and genre_set & {norm(g) for g in cat.genres}:
            return cat.code
        if cat.title_keywords and any(norm(k) in title for k in cat.title_keywords):
            return cat.code
        if cat.has_series and p.series:
            return cat.code
    return categories[-1].code


def category_name(code: str, categories: list[GenreCategory]) -> str:
    for cat in categories:
        if cat.code == code:
            return cat.name
    return code
