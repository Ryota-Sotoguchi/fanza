"""週次集計で下位のパターンを無効化する(手動承認制)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..patterns import DIM_BAND, DIM_GENRE, DIM_JA, DIM_SUMMARY, DIM_TONE

if TYPE_CHECKING:
    from .report import Bucket, Dataset


@dataclass
class Candidate:
    dim: str
    code: str
    name: str
    posts: int
    clicks: int
    cpp: float
    mean_cpp: float


def _pick_worst(ds: "Dataset", dim: str, buckets: dict[str, "Bucket"]) -> tuple[Candidate | None, str | None]:
    a = ds.cfg.analytics
    enabled = ds.patterns.enabled(dim)
    eligible = {c: buckets[c] for c in enabled if c in buckets and buckets[c].posts >= a.review_min_posts}
    if len(enabled) < 2:
        return None, None
    if len(eligible) < 2:
        return None, f"{DIM_JA[dim]}: 投稿数{a.review_min_posts}件以上のものが2つ未満のため判定しません"
    posts = sum(b.posts for b in eligible.values())
    clicks = sum(b.clicks for b in eligible.values())
    mean = clicks / posts if posts else 0.0
    code, worst = min(eligible.items(), key=lambda kv: kv[1].cpp or 0.0)
    cpp = worst.cpp or 0.0
    if mean > 0 and cpp < mean * a.review_threshold_ratio:
        return Candidate(dim, code, ds.patterns.name(dim, code), worst.posts, worst.clicks, cpp, mean), None
    return None, None


def find_candidates(ds: "Dataset", end_week: int) -> tuple[list[Candidate], list[str]]:
    a = ds.cfg.analytics
    weeks = {w for w in range(end_week - a.review_weeks + 1, end_week + 1) if w >= 1}
    candidates: list[Candidate] = []
    notes: list[str] = [f"判定期間: 第{min(weeks)}〜{max(weeks)}週" if weeks else "判定期間なし"]

    styles = ds.style_buckets(weeks)
    for dim in (DIM_TONE, DIM_SUMMARY):
        c, note = _pick_worst(ds, dim, styles)
        if c:
            candidates.append(c)
        if note:
            notes.append(note)

    if ds.has_item_data:
        buckets, _ = ds.item_buckets(weeks)
        for dim in (DIM_GENRE, DIM_BAND):
            c, note = _pick_worst(ds, dim, buckets[dim])
            if c:
                candidates.append(c)
            if note:
                notes.append(note)
    else:
        notes.append("商品別クリックのデータがないため、ジャンル・時間帯は判定していません")
    return candidates, notes
