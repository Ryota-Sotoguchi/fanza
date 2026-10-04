"""除外リスト(未成年を連想させるジャンル・キーワードなど)の判定。"""

from __future__ import annotations

from ..config import ExcludeConfig
from ..models import Product
from ..textutil import norm


class ExclusionFilter:
    def __init__(self, cfg: ExcludeConfig):
        self.genres = {norm(g) for g in cfg.genres if g}
        self.keywords = [norm(k) for k in cfg.keywords if k]
        self.content_ids = {c.strip() for c in cfg.content_ids if c}
        self.makers = {norm(m) for m in cfg.makers if m}

    def reason(self, p: Product) -> str | None:
        """除外する理由。除外しない場合は None。"""
        if p.content_id in self.content_ids:
            return "除外リストの商品ID"
        if p.maker and norm(p.maker) in self.makers:
            return f"除外メーカー: {p.maker}"
        for g in p.genre_names:
            if norm(g) in self.genres:
                return f"除外ジャンル: {g}"
        fields = {
            "タイトル": p.title,
            "ジャンル": " ".join(p.genre_names),
            "シリーズ": p.series,
            "レーベル": p.label,
            "メーカー": p.maker,
            "キャンペーン": p.campaign_title,
        }
        for field_name, value in fields.items():
            target = norm(value)
            if not target:
                continue
            for kw in self.keywords:
                if kw in target:
                    return f"除外キーワード「{kw}」({field_name})"
        return None
