"""運用フェーズ(慣らし期間 / 自動投稿の何週目か)と、その日の投稿件数。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..config import AppConfig

BEFORE_START = "before_start"
BURN_IN = "burn_in"
AUTO = "auto"

PHASE_JA = {BEFORE_START: "開始前", BURN_IN: "慣らし運用(ドライラン)", AUTO: "自動投稿"}


@dataclass
class Phase:
    kind: str
    day: int  # 開始日を1日目とした日数(開始前は0以下)
    week: int  # 自動投稿の週(慣らし期間・開始前は0)
    target_posts: int  # その日の投稿件数(まとめ投稿を含む)
    dry_run: bool
    note: str = ""

    @property
    def label(self) -> str:
        if self.kind == AUTO:
            return f"{PHASE_JA[AUTO]} {self.week}週目"
        if self.kind == BURN_IN:
            return f"{PHASE_JA[BURN_IN]} {self.day}日目"
        return PHASE_JA[self.kind]


def auto_start_date(cfg: AppConfig) -> date:
    return cfg.start_date + timedelta(days=cfg.burn_in_days)


def week_of(d: date, cfg: AppConfig) -> int:
    """自動投稿の何週目か。自動投稿開始前は0。"""
    start = auto_start_date(cfg)
    if d < start:
        return 0
    return (d - start).days // 7 + 1


def week_range(week: int, cfg: AppConfig) -> tuple[date, date]:
    start = auto_start_date(cfg) + timedelta(days=7 * (week - 1))
    return start, start + timedelta(days=6)


def posts_for_week(week: int, cfg: AppConfig) -> int:
    plan = cfg.schedule.weekly_posts
    return plan[min(week, len(plan)) - 1]


def compute_phase(ops_date: date, cfg: AppConfig, ramp_frozen: int | None = None) -> Phase:
    day = (ops_date - cfg.start_date).days + 1
    hard = cfg.schedule.daily_hard_limit
    if day < 1:
        return Phase(BEFORE_START, day, 0, 0, True, f"開始日は {cfg.start_date.isoformat()} です")
    if day <= cfg.burn_in_days:
        target = cfg.schedule.burn_in_daily_posts
        if target is None:
            target = cfg.schedule.weekly_posts[0]
        return Phase(BURN_IN, day, 0, min(target, hard), True, "慣らし期間のため手動投稿用テキストを出力します")
    week = week_of(ops_date, cfg)
    target = posts_for_week(week, cfg)
    notes = []
    if ramp_frozen is not None and target > ramp_frozen:
        target = ramp_frozen
        notes.append(f"警告・制限の検知により件数の引き上げを停止中({ramp_frozen}件で据え置き)")
    if target > hard:
        target = hard
        notes.append(f"1日の上限 {hard} 件を適用")
    return Phase(AUTO, day, week, target, cfg.dry_run, " / ".join(notes))
