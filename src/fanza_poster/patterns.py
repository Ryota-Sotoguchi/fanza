"""パターンID(トーン・時間帯・ジャンル・まとめ形式)と、その有効/無効の管理。

パターンIDの形式:
  個別投稿  {トーン}-{時間帯}-{ジャンル}   例: TB-H21-VR
  まとめ投稿 {形式}-{時間帯}-{期間}       例: SA-H21-D(D=日次, W=週間)
"""

from __future__ import annotations

import random

from .config import AppConfig
from .db import Database

DIM_TONE = "tone"
DIM_BAND = "band"
DIM_GENRE = "genre"
DIM_SUMMARY = "summary_format"
DIMENSIONS = (DIM_TONE, DIM_BAND, DIM_GENRE, DIM_SUMMARY)
DIM_JA = {DIM_TONE: "トーン", DIM_BAND: "時間帯", DIM_GENRE: "ジャンル", DIM_SUMMARY: "まとめ形式"}


def single_pattern_id(tone: str, band: str, genre: str) -> str:
    return f"{tone}-{band}-{genre}"


def summary_pattern_id(fmt: str, band: str, period: str) -> str:
    return f"{fmt}-{band}-{period}"


class Patterns:
    def __init__(self, cfg: AppConfig, db: Database):
        self.cfg = cfg
        self.db = db

    def codes(self, dim: str) -> list[str]:
        if dim == DIM_TONE:
            return list(self.cfg.generation.tones)
        if dim == DIM_BAND:
            return [b.code for b in self.cfg.schedule.bands]
        if dim == DIM_GENRE:
            return [g.code for g in self.cfg.genre_categories]
        if dim == DIM_SUMMARY:
            return list(self.cfg.summary.formats)
        raise ValueError(f"不明な区分です: {dim}")

    def name(self, dim: str, code: str) -> str:
        if dim == DIM_TONE and code in self.cfg.generation.tones:
            return self.cfg.generation.tones[code].name
        if dim == DIM_GENRE:
            for g in self.cfg.genre_categories:
                if g.code == code:
                    return g.name
        if dim == DIM_SUMMARY and code in self.cfg.summary.formats:
            return self.cfg.summary.formats[code]
        if dim == DIM_BAND:
            for b in self.cfg.schedule.bands:
                if b.code == code:
                    return f"{b.start}〜{b.end}"
        return code

    def enabled(self, dim: str) -> list[str]:
        overrides = self.db.pattern_overrides()
        codes = self.codes(dim)
        active = [c for c in codes if not (overrides.get((dim, c)) and not overrides[(dim, c)]["enabled"])]
        # 全部無効になっていた場合は安全側で全部使う(set_enabled で防いでいるが念のため)
        return active or codes

    def disabled(self) -> list[tuple[str, str, str, str]]:
        rows = []
        for (dim, code), row in self.db.pattern_overrides().items():
            if not row["enabled"]:
                rows.append((dim, code, row["note"] or "", row["updated_at"] or ""))
        return rows

    def set_enabled(self, dim: str, code: str, enabled: bool, note: str = "") -> None:
        if code not in self.codes(dim):
            raise ValueError(f"{DIM_JA.get(dim, dim)}に {code} はありません(設定: {self.codes(dim)})")
        if not enabled:
            remaining = [c for c in self.enabled(dim) if c != code]
            if not remaining:
                raise ValueError(f"{DIM_JA[dim]}は最低1つ有効にしておく必要があります")
        self.db.set_pattern_enabled(dim, code, enabled, note)

    @staticmethod
    def least_used(codes: list[str], usage: dict[str, int], rng: random.Random) -> str:
        """使用回数が最も少ないものを選ぶ(同数ならランダム)。比較しやすいよう偏りを抑える。"""
        low = min(usage.get(c, 0) for c in codes)
        return rng.choice([c for c in codes if usage.get(c, 0) == low])
