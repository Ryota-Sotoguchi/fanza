"""セール商品の収集: API取得 → 割引率で絞り込み → 除外 → ジャンル分類 → 保存。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from ..csvlog import append_rows
from ..models import Product
from .dmm_client import DmmApiError, parse_item
from .filters import ExclusionFilter
from .genre import classify

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger(__name__)

MAX_COLLECT_ATTEMPTS_PER_DAY = 3


@dataclass
class CollectStats:
    fetched: int = 0
    unique: int = 0
    below_min: int = 0
    excluded: int = 0
    kept: int = 0
    excluded_rows: list[tuple[str, str, str, str]] = field(default_factory=list)


def fetch_sale_products(app: "App") -> tuple[list[Product], CollectStats]:
    cfg = app.cfg.collect
    dmm = app.dmm()
    filt = ExclusionFilter(app.cfg.exclude)
    stats = CollectStats()
    seen: dict[str, Product] = {}
    for floor in cfg.floors:
        for sort in cfg.sorts:
            for page in range(cfg.scan_pages):
                try:
                    items = dmm.item_list(
                        floor, sort=sort, hits=cfg.hits_per_page, offset=1 + page * cfg.hits_per_page
                    )
                except DmmApiError as e:
                    if page == 0:
                        raise
                    # 2ページ目以降の失敗は、取得済みの分で続ける
                    log.warning("%s/%s の%dページ目を取得できませんでした: %s", floor, sort, page + 1, e)
                    break
                stats.fetched += len(items)
                for raw in items:
                    p = parse_item(raw, floor)
                    if p is not None and p.content_id not in seen:
                        seen[p.content_id] = p
                if len(items) < cfg.hits_per_page:
                    break
    stats.unique = len(seen)
    kept: list[Product] = []
    for p in seen.values():
        if p.discount_rate < cfg.min_discount_rate:
            stats.below_min += 1
            continue
        reason = filt.reason(p)
        if reason:
            stats.excluded_rows.append((p.content_id, p.floor, p.title, reason))
            continue
        p.genre = classify(p, app.cfg.genre_categories)
        kept.append(p)
    stats.excluded = len(stats.excluded_rows)
    kept.sort(key=lambda p: (-p.discount_rate, p.price, p.content_id))
    kept = kept[: cfg.max_items]
    stats.kept = len(kept)
    return kept, stats


def collect(app: "App", ops_date: date) -> CollectStats:
    products, stats = fetch_sale_products(app)
    for p in products:
        app.db.upsert_product(p, ops_date)
    if stats.excluded_rows:
        path = app.settings.log_dir / "excluded" / f"{ops_date.isoformat()}.csv"
        append_rows(path, ["商品ID", "フロア", "タイトル", "除外理由"], stats.excluded_rows)
    app.db.record_collection(ops_date, True, stats.fetched, stats.kept, stats.excluded)
    log.info(
        "収集完了: 取得%d件(重複除く%d件) / 割引率%d%%未満 %d件 / 除外 %d件 / 候補として保存 %d件",
        stats.fetched, stats.unique, app.cfg.collect.min_discount_rate, stats.below_min, stats.excluded, stats.kept,
    )
    return stats


def ensure_collected(app: "App", ops_date: date) -> bool:
    """その運用日の収集が済んでいなければ実行する。失敗しても既存の候補で計画は続ける。"""
    row = app.db.collection(ops_date)
    if row is not None and row["success"]:
        return True
    if row is not None and row["attempts"] >= MAX_COLLECT_ATTEMPTS_PER_DAY:
        return False
    try:
        collect(app, ops_date)
        return True
    except Exception as e:  # noqa: BLE001 - 収集失敗で全体を止めない
        log.exception("収集に失敗しました")
        app.db.record_collection(ops_date, False, error=str(e)[:500])
        return False
