"""投稿時刻のランダム配分。時間帯ごとの重み(21時以降を多め)と最小間隔を守る。"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..config import Band

UNKNOWN_BAND = "HX"


@dataclass
class TimeSlot:
    minute: float  # 運用日の0:00からの経過分(25:00 = 1500)
    band: str
    kind: str  # single / summary


def band_of(minute: float, bands: list[Band]) -> str:
    for b in bands:
        if b.start_min <= minute < b.end_min:
            return b.code
    return UNKNOWN_BAND


def _segments(bands: list[Band], earliest: float) -> list[tuple[float, float, float]]:
    segs = []
    for b in bands:
        start = max(float(b.start_min), earliest)
        end = float(b.end_min)
        if end - start >= 1:
            segs.append((start, end, b.weight * (end - start)))
    return segs


def _pick(segs: list[tuple[float, float, float]], rng: random.Random) -> float:
    total = sum(w for _, _, w in segs)
    r = rng.uniform(0, total)
    for start, end, w in segs:
        if r <= w:
            return rng.uniform(start, end)
        r -= w
    start, end, _ = segs[-1]
    return rng.uniform(start, end)


def _far_enough(m: float, taken: list[float], gap: float) -> bool:
    return all(abs(m - t) >= gap for t in taken)


def allocate(
    n_single: int,
    with_summary: bool,
    enabled_bands: list[Band],
    all_bands: list[Band],
    summary_window: tuple[int, int],
    min_gap: float,
    earliest: float,
    rng: random.Random,
) -> list[TimeSlot]:
    """投稿時刻を決める。earliest(分)より前には置かない。"""
    window_end = max(b.end_min for b in all_bands)
    times: list[TimeSlot] = []
    taken: list[float] = []

    if with_summary:
        s0, s1 = max(float(summary_window[0]), earliest), float(summary_window[1])
        if s1 - s0 >= 1:
            m = rng.uniform(s0, s1)
        else:
            segs = _segments(enabled_bands, earliest)
            m = _pick(segs, rng) if segs else None
        if m is not None and m < window_end:
            times.append(TimeSlot(m, band_of(m, all_bands), "summary"))
            taken.append(m)

    segs = _segments(enabled_bands, earliest)
    if not segs:
        return sorted(times, key=lambda t: t.minute)

    gap = float(min_gap)
    placed = 0
    while placed < n_single:
        for _ in range(3000):
            m = _pick(segs, rng)
            if _far_enough(m, taken, gap):
                break
        else:
            # 間隔を守れないときは少しずつ緩める(窓が短い日など)
            gap *= 0.7
            if gap < 3:
                gap = 0
            continue
        taken.append(m)
        times.append(TimeSlot(m, band_of(m, all_bands), "single"))
        placed += 1
    return sorted(times, key=lambda t: t.minute)
