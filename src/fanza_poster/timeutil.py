"""日時の扱い。1日の区切り(既定5:00)で「運用日」を決める。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def ops_date_of(dt: datetime, start_hour: int) -> date:
    """start_hour より前の時刻は前日の運用日として扱う(0時台の投稿は前日分)。"""
    return (dt - timedelta(hours=start_hour)).date()


def ops_midnight(ops_date: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(ops_date, time(0, 0), tzinfo=tz)


def at_minutes(ops_date: date, minutes: float, tz: ZoneInfo) -> datetime:
    """運用日の0:00からの経過分で日時を作る(25:00 は翌1:00)。"""
    base = ops_midnight(ops_date, tz)
    # 夏時間のない地域を前提に、壁時計の加算で計算する
    return (base + timedelta(minutes=minutes)).replace(microsecond=0)


def minutes_since_midnight(dt: datetime, ops_date: date, tz: ZoneInfo) -> float:
    return (dt.astimezone(tz) - ops_midnight(ops_date, tz)).total_seconds() / 60.0


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_hm(dt: datetime) -> str:
    return dt.strftime("%m/%d %H:%M")
