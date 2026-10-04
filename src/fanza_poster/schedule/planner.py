"""1日の投稿計画: 件数・時刻・パターン・商品を決め、文面を事前に生成する。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from ..collect.dmm_client import with_affiliate_id
from ..collect.filters import ExclusionFilter
from ..collect.genre import category_name
from ..generate.summary import (
    PERIOD_DAILY,
    PERIOD_WEEKLY,
    SummaryEntry,
    build_summary_reply,
    build_summary_text,
    fallback_label,
)
from ..generate.validate import Facts
from ..generate.writer import GenerationAPIError, GenerationError
from ..models import (
    KIND_SINGLE,
    KIND_SUMMARY,
    P_DRAFTED,
    P_SKIPPED,
    R_DRY_RUN,
    S_DRY_RUN,
    S_PLANNED,
    Product,
    Slot,
)
from ..patterns import DIM_BAND, DIM_GENRE, DIM_SUMMARY, DIM_TONE, Patterns, single_pattern_id, summary_pattern_id
from ..timeutil import at_minutes, iso, minutes_since_midnight
from .phase import BEFORE_START, Phase
from .slots import TimeSlot, allocate

if TYPE_CHECKING:
    from ..app import App

log = logging.getLogger(__name__)

USAGE_LOOKBACK_DAYS = 14


class Planner:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.patterns = Patterns(self.cfg, self.db)
        self.filter = ExclusionFilter(self.cfg.exclude)

    # ------------------------------------------------------------- 共通
    def facts(self, p: Product) -> Facts:
        return Facts.from_product(p, self.app.ng_words(), category_name(p.genre, self.cfg.genre_categories))

    def write_single(self, p: Product, tone: str) -> str:
        return self.app.writer().write_single(self.facts(p), tone)

    def reply_for(self, p: Product, affiliate_id: str) -> str:
        return self.cfg.post.reply_template.format(url=with_affiliate_id(p.affiliate_url, affiliate_id))

    def images_for(self, p: Product) -> list[str]:
        return [p.image_url] if self.cfg.post.attach_image and p.image_url else []

    def candidates(self, genre: str, ops_date: date, exclude_ids: set[str]) -> list[Product]:
        """個別投稿の候補。除外リストが変更されていても反映されるよう、ここでも判定する。"""
        min_seen = ops_date - timedelta(days=self.cfg.collect.candidate_max_age_days)
        result = []
        for p in self.db.candidates(genre, min_seen, self.cfg.collect.min_discount_rate, exclude_ids, limit=30):
            reason = self.filter.reason(p)
            if reason:
                self.db.set_product_status(p.content_id, P_SKIPPED, f"excluded: {reason}")
                continue
            result.append(p)
        return result

    def ranking(self, ops_date: date, period: str, exclude_ids: set[str] | None = None) -> list[Product]:
        start = ops_date - timedelta(days=6) if period == PERIOD_WEEKLY else ops_date
        items = self.db.ranking(
            start, ops_date, self.cfg.collect.min_discount_rate, self.patterns.enabled(DIM_GENRE), exclude_ids or ()
        )
        return [p for p in items if not self.filter.reason(p)]

    def period_for(self, ops_date: date) -> str:
        return PERIOD_WEEKLY if ops_date.weekday() == self.cfg.summary.weekly_weekday else PERIOD_DAILY

    def labels_for(self, products: list[Product], known: dict[str, str] | None = None) -> dict[str, str]:
        known = dict(known or {})
        need = [p for p in products if p.content_id not in known]
        max_chars = self.cfg.summary.label_max_chars
        if need:
            try:
                known.update(self.app.writer().summary_labels([self.facts(p) for p in need], max_chars))
            except (GenerationAPIError, GenerationError) as e:
                log.warning("ラベル生成に失敗したため代替ラベルを使います: %s", e)
        for p in products:
            if p.content_id not in known:
                name = category_name(p.genre, self.cfg.genre_categories)
                known[p.content_id] = fallback_label(p, max_chars, self.app.ng_words(), name)
        return {p.content_id: known[p.content_id] for p in products}

    def render_summary(
        self, fmt: str, period: str, products: list[Product], labels: dict[str, str], affiliate_id: str
    ) -> tuple[str, str]:
        entries = [
            SummaryEntry(
                rank=i + 1,
                content_id=p.content_id,
                label=labels[p.content_id],
                price=p.price,
                list_price=p.list_price,
                discount_rate=p.discount_rate,
                price_from=p.price_from,
                affiliate_url=with_affiliate_id(p.affiliate_url, affiliate_id),
            )
            for i, p in enumerate(products)
        ]
        text = build_summary_text(
            fmt, period, entries, self.cfg.generation.pr_suffix, self.cfg.summary.label_max_chars
        )
        reply = build_summary_reply(entries, self.cfg.post.summary_reply_line, self.cfg.post.summary_reply_footer)
        return text, reply

    # ------------------------------------------------------------- 計画
    def ensure_plan(self, ops_date: date, now: datetime, phase: Phase, force_dry_run: bool = False) -> bool:
        """その日の計画がなければ作る。作った場合 True。"""
        if self.db.get_plan(ops_date) is not None:
            return False
        dry = phase.dry_run or force_dry_run
        sched = self.cfg.schedule
        if phase.kind == BEFORE_START or phase.target_posts <= 0:
            self.db.insert_plan(ops_date, phase.kind, phase.week, 0, dry, phase.note or "投稿なし", [])
            return True

        now_min = minutes_since_midnight(now, ops_date, self.app.tz)
        earliest = max(now_min + 3, float(sched.window_start_min))
        if earliest >= sched.window_end_min - 5:
            self.db.insert_plan(ops_date, phase.kind, phase.week, 0, dry, "投稿時間帯を過ぎていたため計画なし", [])
            return True

        target = min(phase.target_posts, sched.daily_hard_limit)
        with_summary = self.cfg.summary.enabled and target >= 1
        n_single = target - (1 if with_summary else 0)
        enabled_bands = [b for b in sched.bands if b.code in self.patterns.enabled(DIM_BAND)]
        times = allocate(
            n_single, with_summary, enabled_bands, sched.bands, self.cfg.summary.window_min,
            sched.min_gap_minutes, earliest, self.app.rng,
        )

        since = ops_date - timedelta(days=USAGE_LOOKBACK_DAYS)
        usage = {
            DIM_TONE: self.db.usage_counts("style", since, KIND_SINGLE),
            DIM_SUMMARY: self.db.usage_counts("style", since, KIND_SUMMARY),
            DIM_GENRE: self.db.usage_counts("genre", since, KIND_SINGLE),
        }
        used = set(self.db.reserved_content_ids())
        slots: list[Slot] = []

        for t in times:
            if t.kind == KIND_SUMMARY:
                s = self._build_summary(ops_date, t, usage, used, dry)
                if s is not None:
                    slots.append(s)
                    used.update(s.content_ids)
                else:
                    t.kind = KIND_SINGLE  # まとめを作れない日は個別投稿にする
        for t in times:
            if t.kind != KIND_SINGLE:
                continue
            s = self._build_single(ops_date, t, usage, used, dry)
            if s is not None:
                slots.append(s)
                used.update(s.content_ids)

        slots.sort(key=lambda s: s.scheduled_at)
        for i, s in enumerate(slots, start=1):
            s.seq = i
        note = phase.note
        if len(slots) < target:
            note = (note + " / " if note else "") + f"候補不足のため{len(slots)}件"
        self.db.insert_plan(ops_date, phase.kind, phase.week, target, dry, note, slots)
        log.info("%s の計画を作成: %d件(%s)", ops_date, len(slots), "ドライラン" if dry else "自動投稿")
        if dry:
            self._emit_dry_run(ops_date, phase, slots)
        return True

    def _base_slot(self, ops_date: date, t: TimeSlot, dry: bool) -> Slot:
        at = at_minutes(ops_date, t.minute, self.app.tz)
        return Slot(
            id=None,
            ops_date=ops_date.isoformat(),
            seq=0,
            scheduled_at=iso(at),
            kind=t.kind,
            band=t.band,
            style="",
            status=S_DRY_RUN if dry else S_PLANNED,
            result=R_DRY_RUN if dry else "",
            dry_run=dry,
        )

    def _build_single(
        self, ops_date: date, t: TimeSlot, usage: dict[str, dict[str, int]], used: set[str], dry: bool
    ) -> Slot | None:
        tone = Patterns.least_used(self.patterns.enabled(DIM_TONE), usage[DIM_TONE], self.app.rng)
        genres = self.patterns.enabled(DIM_GENRE)
        pools = {g: self.candidates(g, ops_date, used) for g in genres}
        order = sorted(
            [g for g in genres if pools[g]], key=lambda g: (usage[DIM_GENRE].get(g, 0), self.app.rng.random())
        )
        for genre in order:
            for p in pools[genre][:5]:
                try:
                    text = self.write_single(p, tone)
                except GenerationError as e:
                    log.warning("文面を生成できないためスキップ: %s (%s)", p.content_id, e)
                    self.db.set_product_status(p.content_id, P_SKIPPED, "generation_failed")
                    continue
                af = self.app.settings.affiliate_id_for(tone)
                slot = self._base_slot(ops_date, t, dry)
                slot.style = tone
                slot.genre = genre
                slot.pattern_id = single_pattern_id(tone, t.band, genre)
                slot.content_ids = [p.content_id]
                slot.text = text
                slot.reply_text = self.reply_for(p, af)
                slot.image_urls = self.images_for(p)
                slot.affiliate_id = af
                usage[DIM_TONE][tone] = usage[DIM_TONE].get(tone, 0) + 1
                usage[DIM_GENRE][genre] = usage[DIM_GENRE].get(genre, 0) + 1
                return slot
        log.warning("個別投稿の候補がありません(%s)", t.band)
        return None

    def _build_summary(
        self, ops_date: date, t: TimeSlot, usage: dict[str, dict[str, int]], used: set[str], dry: bool
    ) -> Slot | None:
        period = self.period_for(ops_date)
        top = self.ranking(ops_date, period)[: self.cfg.summary.top_n]
        if len(top) < self.cfg.summary.min_items:
            log.info("まとめ投稿の候補が%d件のため、まとめ投稿は作りません", len(top))
            return None
        fmt = Patterns.least_used(self.patterns.enabled(DIM_SUMMARY), usage[DIM_SUMMARY], self.app.rng)
        af = self.app.settings.affiliate_id_for(fmt)
        labels = self.labels_for(top)
        text, reply = self.render_summary(fmt, period, top, labels, af)
        slot = self._base_slot(ops_date, t, dry)
        slot.style = fmt
        slot.period = period
        slot.pattern_id = summary_pattern_id(fmt, t.band, period)
        slot.content_ids = [p.content_id for p in top]
        slot.labels = labels
        slot.text = text
        slot.reply_text = reply
        slot.image_urls = self.images_for(top[0])
        slot.affiliate_id = af
        usage[DIM_SUMMARY][fmt] = usage[DIM_SUMMARY].get(fmt, 0) + 1
        return slot

    def _emit_dry_run(self, ops_date: date, phase: Phase, slots: list[Slot]) -> None:
        from ..post.dryrun import write_manual_file
        from ..postlog import log_slot

        now = iso(self.app.now())
        for s in slots:
            s.executed_at = now
            self.db.update_slot(s)
            if s.kind == KIND_SINGLE:
                self.db.set_product_status(s.content_ids[0], P_DRAFTED, "manual_draft", pattern_id=s.pattern_id)
            log_slot(self.app, s)
        path = write_manual_file(self.app, ops_date, phase.label, slots)
        log.info("手動投稿用のテキストを出力しました: %s", path)

    # ------------------------------------------------------- 投稿時の差し替え
    def replacement(self, ops_date: date, genre: str, exclude_ids: set[str]) -> Product | None:
        """セール終了などで投稿できなくなった商品の代わり(同じジャンルを優先)。"""
        used = exclude_ids | self.db.reserved_content_ids()
        genres = [genre] + [g for g in self.patterns.enabled(DIM_GENRE) if g != genre]
        for g in genres:
            pool = self.candidates(g, ops_date, used)
            if pool:
                return pool[0]
        return None
