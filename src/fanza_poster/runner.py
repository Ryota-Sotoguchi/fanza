"""定期実行(tick)の本体: 収集 → 計画 → 予定時刻を過ぎた枠を1件ずつ投稿。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .collect.collector import ensure_collected
from .collect.dmm_client import DmmApiError, parse_item, with_affiliate_id
from .generate.writer import GenerationAPIError, GenerationError
from .models import (
    KIND_SINGLE,
    P_DRAFTED,
    P_FAILED,
    P_PENDING,
    P_POSTED,
    P_SKIPPED,
    R_DRY_RUN,
    R_FAILED,
    R_HALTED,
    R_POSTED,
    R_REPLY_FAILED,
    R_SKIPPED,
    R_UNKNOWN,
    S_DONE,
    S_DRY_RUN,
    S_SKIPPED,
    Product,
    Slot,
)
from .patterns import single_pattern_id
from .post.base import DetectionError, PostFailed, ReplyFailed
from .post.detect import FREEZE_KINDS, KIND_JA
from .postlog import log_slot
from .safety import Safety, halted_message
from .schedule.phase import BEFORE_START, compute_phase
from .schedule.planner import Planner
from .timeutil import iso, parse_iso

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_HALTED = 2

ALLOWED_IMAGE_HOSTS = ("dmm.co.jp", "dmm.com")


class Runner:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.db = app.db
        self.safety = Safety(app)
        self.planner = Planner(app)

    # ---------------------------------------------------------------- tick
    def tick(self, force_dry_run: bool = False) -> int:
        info = self.safety.halted()
        if info:
            log.warning(halted_message(info))
            return EXIT_HALTED

        now = self.app.now()
        ops = self.app.ops_date(now)
        phase = compute_phase(ops, self.cfg, self.safety.ramp_frozen())
        log.info("運用日 %s: %s / 本日の予定件数 %d %s", ops, phase.label, phase.target_posts, phase.note)

        if phase.kind != BEFORE_START:
            ensure_collected(self.app, ops)
            try:
                self.planner.ensure_plan(ops, now, phase, force_dry_run)
            except GenerationAPIError as e:
                log.error("文面生成APIのエラーのため計画を作れませんでした(次回の起動で再試行): %s", e)
                self.safety.notify_once_per_day("generation_api", "文面を生成できません", str(e))

        processed = 0
        late = timedelta(minutes=self.cfg.schedule.late_skip_minutes)
        for slot in self.db.planned_slots():
            at = parse_iso(slot.scheduled_at)
            if at > now:
                break
            if now - at > late:
                minutes = int((now - at).total_seconds() // 60)
                self._skip(slot, f"予定時刻から{minutes}分遅れたためスキップ(PC停止・スリープなど)")
                continue
            if processed >= self.cfg.schedule.max_posts_per_tick:
                break
            self.process_slot(slot, force_dry_run)
            processed += 1
            if self.safety.halted():
                return EXIT_HALTED
        return EXIT_OK

    # ------------------------------------------------------------ 1件の処理
    def process_slot(self, slot: Slot, force_dry_run: bool = False) -> None:
        slot_date = date.fromisoformat(slot.ops_date)
        phase = compute_phase(slot_date, self.cfg, self.safety.ramp_frozen())

        if force_dry_run or slot.dry_run:
            self._finish_dry(slot)
            return
        limit = self.cfg.schedule.daily_hard_limit
        if self.db.counted_posts(slot_date) >= limit:
            self._skip(slot, f"1日の投稿上限({limit}件)に達したためスキップ")
            return

        try:
            ready = self._prepare(slot)
        except (GenerationAPIError, DmmApiError, requests.RequestException) as e:
            # API側の問題。枠は残して次回の起動で再試行する(遅れすぎたらスキップされる)
            log.error("投稿前の確認に失敗しました(次回再試行): %s", e)
            return
        if not ready:
            self._skip(slot, slot.error or "セール終了のためスキップ")
            return

        images = self._download_images(slot)
        try:
            self._post(slot, phase.target_posts, images)
        finally:
            for path in images:
                path.unlink(missing_ok=True)

    def _post(self, slot: Slot, target_posts: int, images: list[Path]) -> None:
        submitted = False
        result = R_FAILED
        try:
            with self.app.new_poster() as poster:
                poster.check_session()
                slot.tweet_url = poster.post(slot.text, images)
                submitted = True
                slot.counted = True
                self.app.sleep(self.app.rng.uniform(*self.cfg.post.reply_delay_sec))
                try:
                    slot.reply_url = poster.reply_to_own(slot.tweet_url, slot.reply_text)
                    result = R_POSTED
                except ReplyFailed as e:
                    result = R_REPLY_FAILED
                    slot.error = f"リンクのリプライに失敗: {e}"
        except DetectionError as e:
            slot.counted = submitted or e.submitted
            label = KIND_JA.get(e.kind, e.kind)
            self._finish(slot, R_HALTED, f"{label}を検知: {e.detail}")
            self.safety.halt(
                e.kind, f"{label}を検知しました", e.detail, freeze=e.kind in FREEZE_KINDS,
                current_target=target_posts,
            )
            return
        except PostFailed as e:
            slot.counted = submitted or e.submitted
            self._finish(slot, R_UNKNOWN if slot.counted else R_FAILED, e.detail)
            self.safety.record_failure(e.detail)
            return
        except Exception as e:  # noqa: BLE001 - ブラウザ起動失敗なども失敗として数える
            log.exception("投稿処理で予期しないエラー")
            slot.counted = submitted
            self._finish(slot, R_UNKNOWN if submitted else R_FAILED, f"{type(e).__name__}: {e}")
            self.safety.record_failure(str(e))
            return

        self._finish(slot, result)
        if result == R_POSTED:
            self.safety.record_success()
            log.info("投稿しました: %s %s", slot.pattern_id, slot.tweet_url)
        else:
            self.safety.record_failure(slot.error)

    # ------------------------------------------------------- 結果の記録
    def _finish(self, slot: Slot, result: str, error: str = "") -> None:
        slot.result = result
        slot.status = S_DONE
        if error:
            slot.error = error
        slot.executed_at = iso(self.app.now())
        self.db.update_slot(slot)
        if slot.kind == KIND_SINGLE and slot.content_ids:
            cid = slot.content_ids[0]
            if slot.tweet_url:
                self.db.set_product_status(cid, P_POSTED, "", pattern_id=slot.pattern_id, posted_at=slot.executed_at)
            elif result == R_UNKNOWN:
                # 投稿された可能性があるので、重複を避けるため再投稿しない
                self.db.set_product_status(cid, P_FAILED, "unknown", pattern_id=slot.pattern_id)
            else:
                self.db.set_product_status(cid, P_FAILED, result, pattern_id=slot.pattern_id)
        log_slot(self.app, slot)

    def _skip(self, slot: Slot, reason: str) -> None:
        slot.status = S_SKIPPED
        slot.result = R_SKIPPED
        slot.error = reason
        slot.executed_at = iso(self.app.now())
        self.db.update_slot(slot)
        log.info("スキップ: %s %s", slot.pattern_id, reason)
        log_slot(self.app, slot)

    def _finish_dry(self, slot: Slot) -> None:
        from .post.dryrun import append_dry_run

        slot.status = S_DRY_RUN
        slot.result = R_DRY_RUN
        slot.dry_run = True
        slot.executed_at = iso(self.app.now())
        self.db.update_slot(slot)
        if slot.kind == KIND_SINGLE and slot.content_ids:
            self.db.set_product_status(slot.content_ids[0], P_DRAFTED, "dry_run", pattern_id=slot.pattern_id)
        path = append_dry_run(self.app, slot)
        log.info("ドライラン(投稿せず出力): %s", path)
        log_slot(self.app, slot)

    # --------------------------------------------------- 投稿直前の再確認
    def _verify(self, p: Product, affiliate_id: str) -> Product | None:
        raw = self.app.dmm().find_item(p.floor, p.content_id, affiliate_id=affiliate_id)
        if raw is None:
            return None
        fresh = parse_item(raw, p.floor)
        if fresh is None:
            return None
        fresh.genre = p.genre
        fresh.affiliate_url = with_affiliate_id(fresh.affiliate_url or p.affiliate_url, affiliate_id)
        if not fresh.image_url:
            fresh.image_url = p.image_url
        if self.planner.filter.reason(fresh):
            return None
        return fresh

    def _prepare(self, slot: Slot) -> bool:
        if not self.cfg.safety.verify_before_post:
            return True
        if slot.kind == KIND_SINGLE:
            return self._prepare_single(slot)
        return self._prepare_summary(slot)

    def _prepare_single(self, slot: Slot) -> bool:
        min_rate = self.cfg.collect.min_discount_rate
        slot_date = date.fromisoformat(slot.ops_date)
        product = self.db.get_product(slot.content_ids[0])
        tried: set[str] = set()
        replacements = 0
        while product is not None:
            tried.add(product.content_id)
            fresh = self._verify(product, slot.affiliate_id)
            if fresh is not None and fresh.discount_rate >= min_rate:
                if not fresh.same_offer(product) or slot.content_ids != [product.content_id]:
                    try:
                        slot.text = self.planner.write_single(fresh, slot.style)
                    except GenerationError as e:
                        log.warning("再生成できないためスキップ: %s (%s)", product.content_id, e)
                        self.db.set_product_status(product.content_id, P_SKIPPED, "generation_failed")
                        product = self._next_replacement(slot, slot_date, tried, replacements)
                        replacements += 1
                        continue
                self.db.update_offer(fresh)
                slot.content_ids = [fresh.content_id]
                slot.genre = fresh.genre
                slot.pattern_id = single_pattern_id(slot.style, slot.band, fresh.genre)
                slot.reply_text = self.planner.reply_for(fresh, slot.affiliate_id)
                slot.image_urls = self.planner.images_for(fresh)
                self.db.update_slot(slot)
                return True
            note = "sale_ended" if fresh is None else "below_min_discount"
            log.info("セール終了または割引率が下限未満のため差し替え: %s", product.content_id)
            self.db.set_product_status(product.content_id, P_SKIPPED, note)
            product = self._next_replacement(slot, slot_date, tried, replacements)
            replacements += 1
        slot.error = slot.error or "セール終了で、差し替えできる候補がありませんでした"
        return False

    def _next_replacement(self, slot: Slot, slot_date: date, tried: set[str], count: int) -> Product | None:
        if count >= self.cfg.safety.max_replacements:
            slot.error = "差し替えの上限に達しました"
            return None
        return self.planner.replacement(slot_date, slot.genre, tried)

    def _prepare_summary(self, slot: Slot) -> bool:
        cfg = self.cfg.summary
        min_rate = self.cfg.collect.min_discount_rate
        slot_date = date.fromisoformat(slot.ops_date)
        period = slot.period or self.planner.period_for(slot_date)
        queue = list(slot.content_ids)
        queue += [p.content_id for p in self.planner.ranking(slot_date, period, set(queue))]
        verified: list[Product] = []
        checked = 0
        for cid in queue:
            if len(verified) >= cfg.top_n or checked >= cfg.verify_max_checks:
                break
            p = self.db.get_product(cid)
            if p is None:
                continue
            fresh = self._verify(p, slot.affiliate_id)
            checked += 1
            if fresh is not None and fresh.discount_rate >= min_rate:
                self.db.update_offer(fresh)
                verified.append(fresh)
            elif p.status == P_PENDING:
                self.db.set_product_status(cid, P_SKIPPED, "sale_ended" if fresh is None else "below_min_discount")
        if len(verified) < cfg.min_items:
            slot.error = f"セールが続いている商品が{len(verified)}件しかないため、まとめ投稿をスキップ"
            return False
        verified.sort(key=lambda p: (-p.discount_rate, p.price, p.content_id))
        labels = self.planner.labels_for(verified, known=slot.labels)
        slot.text, slot.reply_text = self.planner.render_summary(
            slot.style, period, verified, labels, slot.affiliate_id
        )
        slot.content_ids = [p.content_id for p in verified]
        slot.labels = labels
        slot.image_urls = self.planner.images_for(verified[0])
        self.db.update_slot(slot)
        return True

    # ------------------------------------------------------------ 画像
    def _download_images(self, slot: Slot) -> list[Path]:
        paths: list[Path] = []
        tmp = self.app.settings.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        for i, url in enumerate(slot.image_urls):
            host = (urlsplit(url).hostname or "").lower()
            if not any(host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS):
                log.warning("DMM以外の画像URLは使いません: %s", url)
                continue
            dest = tmp / f"slot{slot.id}_{i}.jpg"
            try:
                if self.app.image_fetcher is not None:
                    paths.append(self.app.image_fetcher(url, dest))
                else:
                    resp = requests.get(url, timeout=30)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                    paths.append(dest)
            except Exception as e:  # noqa: BLE001
                log.warning("画像を取得できなかったため画像なしで投稿します: %s (%s)", url, e)
        return paths
