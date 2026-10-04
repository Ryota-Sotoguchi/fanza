"""収集 → 計画 → 投稿 → 記録 の流れを、偽のAPI・ブラウザで確認する。"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta

from fanza_poster.models import (
    P_DRAFTED,
    P_POSTED,
    P_SKIPPED,
    R_HALTED,
    R_POSTED,
    R_REPLY_FAILED,
    R_SKIPPED,
    S_DRY_RUN,
)
from fanza_poster.runner import EXIT_HALTED, EXIT_OK, Runner
from fanza_poster.safety import Safety
from fanza_poster.timeutil import parse_iso

from .conftest import JST, catalog, raw_item

# 2026-10-20 は start_date=2026-10-06 から15日目(自動投稿1週目)
AUTO_DAY_W1 = datetime(2026, 10, 20, 11, 50, tzinfo=JST)


def _read_log(app) -> list[dict]:
    path = app.settings.data_dir / "posts_log.csv"
    with path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _run_until_next_post(app, runner):
    """次の予定時刻の1分後に時計を進めて tick を実行する。"""
    planned = app.db.planned_slots()
    assert planned
    app.clock.dt = parse_iso(planned[0].scheduled_at) + timedelta(minutes=1)
    return runner.tick(), planned[0]


def test_burn_in_outputs_manual_drafts_without_posting(make_app):
    app = make_app(datetime(2026, 10, 6, 11, 50, tzinfo=JST), start_date="2026-10-06")
    runner = Runner(app)
    assert runner.tick() == EXIT_OK

    slots = app.db.slots_for(app.ops_date())
    assert len(slots) == 6  # 慣らし期間の下書き件数(まとめ1件を含む)
    assert all(s.status == S_DRY_RUN and s.dry_run for s in slots)
    assert sum(1 for s in slots if s.kind == "summary") == 1
    assert app.post_log == []  # 投稿していない

    manual = app.settings.output_dir / "manual" / "2026-10-06.txt"
    text = manual.read_text(encoding="utf-8-sig")
    assert "▼本文" in text and "▼リプライ" in text and "#PR" in text

    for s in slots:
        assert s.text.rstrip().endswith("#PR")
        assert "http" not in s.text  # 本文にURLを入れない
        assert "af_id=" in s.reply_text
        if s.kind == "single":
            assert app.db.get_product(s.content_ids[0]).status == P_DRAFTED
            assert f"af_id={app.settings.affiliate_id_for(s.style)}" in s.reply_text
    rows = _read_log(app)
    assert len(rows) == 6 and all(r["ドライラン"] == "はい" for r in rows)

    # 時刻が進んでも投稿はしない
    app.clock.dt = datetime(2026, 10, 7, 0, 30, tzinfo=JST)
    assert runner.tick() == EXIT_OK
    assert app.post_log == []


def test_auto_posting_posts_text_then_self_reply(make_app):
    app = make_app(AUTO_DAY_W1, start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    slots = app.db.slots_for(app.ops_date())
    assert len(slots) == 6  # 1週目は6件(まとめ込み)
    assert all(s.status == "planned" for s in slots)
    times = [parse_iso(s.scheduled_at) for s in slots]
    assert all(datetime(2026, 10, 20, 12, 0, tzinfo=JST) <= t <= datetime(2026, 10, 21, 1, 0, tzinfo=JST) for t in times)

    code, slot = _run_until_next_post(app, runner)
    assert code == EXIT_OK
    posts = [x for x in app.post_log if x[0] == "post"]
    replies = [x for x in app.post_log if x[0] == "reply"]
    assert len(posts) == 1 and len(replies) == 1
    assert "http" not in posts[0][1] and posts[0][1].rstrip().endswith("#PR")
    assert posts[0][2]  # APIの画像を添付
    assert replies[0][1].startswith("https://x.com/myaccount/status/")  # 自分の投稿へのリプライ
    assert "af_id=test-99" in replies[0][2]

    done = [s for s in app.db.slots_for(app.ops_date()) if s.id == slot.id][0]
    assert done.result == R_POSTED and done.counted
    if done.kind == "single":
        assert app.db.get_product(done.content_ids[0]).status == P_POSTED
    row = _read_log(app)[-1]
    assert row["結果"] == "投稿済み" and row["パターンID"] == done.pattern_id


def test_one_post_per_tick_and_no_duplicates(make_app):
    app = make_app(AUTO_DAY_W1, start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    app.clock.dt = datetime(2026, 10, 21, 0, 59, tzinfo=JST)  # 全枠の予定時刻を過ぎた状態
    late_window = app.cfg.schedule.late_skip_minutes
    runner.tick()
    posts = [x for x in app.post_log if x[0] == "post"]
    assert len(posts) <= 1  # 1回の起動で最大1件
    skipped = [s for s in app.db.slots_for(app.ops_date()) if s.result == R_SKIPPED]
    assert all("遅れ" in s.error for s in skipped)
    assert late_window == 60

    # 同じ商品は二度と個別投稿しない
    app.clock.dt = datetime(2026, 10, 21, 11, 50, tzinfo=JST)
    runner.tick()
    posted_ids = [s.content_ids[0] for s in app.db.all_slots() if s.kind == "single" and s.result == R_POSTED]
    next_ids = [s.content_ids[0] for s in app.db.slots_for(app.ops_date()) if s.kind == "single"]
    assert not set(posted_ids) & set(next_ids)


def test_daily_hard_limit_is_never_exceeded(make_app):
    app = make_app(AUTO_DAY_W1, start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    _run_until_next_post(app, runner)
    app.cfg.schedule.daily_hard_limit = 1  # 上限に達した状態にする
    code, slot = _run_until_next_post(app, runner)
    assert code == EXIT_OK
    s = [x for x in app.db.slots_for(app.ops_date()) if x.id == slot.id][0]
    assert s.result == R_SKIPPED and "上限" in s.error
    assert len([x for x in app.post_log if x[0] == "post"]) == 1


def test_three_consecutive_failures_halt(make_app):
    app = make_app(AUTO_DAY_W1, poster_mode="fail", start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    for _ in range(2):
        code, _ = _run_until_next_post(app, runner)
        assert code == EXIT_OK
    code, _ = _run_until_next_post(app, runner)
    assert code == EXIT_HALTED
    safety = Safety(app)
    assert safety.halted()["kind"] == "consecutive_failures"
    assert safety.ramp_frozen() is None  # 連続失敗では件数の据え置きはしない
    assert app.notifier.sent  # Windows通知
    assert (app.settings.data_dir / "HALTED.txt").exists()
    # 停止中は何もしない
    before = len(app.post_log)
    app.clock.dt += timedelta(hours=1)
    assert runner.tick() == EXIT_HALTED
    assert len(app.post_log) == before
    # 再開
    safety.resume()
    assert safety.halted() is None and safety.consecutive_failures() == 0


def test_captcha_halts_immediately_and_freezes_ramp(make_app):
    app = make_app(AUTO_DAY_W1, poster_mode="captcha", start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    code, slot = _run_until_next_post(app, runner)
    assert code == EXIT_HALTED
    safety = Safety(app)
    assert safety.halted()["kind"] == "captcha"
    assert safety.ramp_frozen() == 6  # 1週目の件数で据え置き
    s = [x for x in app.db.slots_for(app.ops_date()) if x.id == slot.id][0]
    assert s.result == R_HALTED and not s.counted
    assert app.post_log == []
    assert any("CAPTCHA" in t for t, _ in app.notifier.sent)

    # 再開しても、3週目(本来12件)の件数は6件のまま
    safety.resume()
    app.poster_mode = "ok"
    app.clock.dt = datetime(2026, 11, 3, 11, 50, tzinfo=JST)
    runner.tick()
    assert len(app.db.slots_for(app.ops_date())) == 6
    safety.unfreeze_ramp()


def test_reply_failure_counts_as_failure_but_post_is_recorded(make_app):
    app = make_app(AUTO_DAY_W1, poster_mode="reply_fail", start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    code, slot = _run_until_next_post(app, runner)
    s = [x for x in app.db.slots_for(app.ops_date()) if x.id == slot.id][0]
    assert s.result == R_REPLY_FAILED and s.counted and s.tweet_url
    assert Safety(app).consecutive_failures() == 1


def test_sale_ended_item_is_replaced_before_posting(make_app):
    app = make_app(AUTO_DAY_W1, start_date="2026-10-06")
    runner = Runner(app)
    runner.tick()
    target = next(s for s in app.db.planned_slots() if s.kind == "single")
    original = target.content_ids[0]
    app.fake_dmm.end_sale(original)
    # 対象の枠より前の枠は処理済みにしておく
    for s in app.db.planned_slots():
        if s.id == target.id:
            break
        s.status, s.result = "skipped", "skipped"
        app.db.update_slot(s)
    app.clock.dt = parse_iso(target.scheduled_at) + timedelta(minutes=1)
    runner.tick()
    s = [x for x in app.db.slots_for(app.ops_date()) if x.id == target.id][0]
    assert s.result == R_POSTED
    assert s.content_ids[0] != original
    assert app.db.get_product(original).status == P_SKIPPED


def test_excluded_items_never_collected(make_app):
    items = catalog(20) + [raw_item("bad00001", title="女子校生の休日"), raw_item("bad00002", genres=("制服",))]
    app = make_app(datetime(2026, 10, 6, 11, 50, tzinfo=JST), items=items, start_date="2026-10-06")
    Runner(app).tick()
    assert app.db.get_product("bad00001") is None
    assert app.db.get_product("bad00002") is None
    excluded = (app.settings.log_dir / "excluded" / "2026-10-06.csv").read_text(encoding="utf-8-sig")
    assert "bad00001" in excluded and "bad00002" in excluded


def test_below_min_discount_not_collected(make_app):
    items = catalog(20) + [raw_item("low00001", price=1500, list_price=1980)]
    app = make_app(datetime(2026, 10, 6, 11, 50, tzinfo=JST), items=items, start_date="2026-10-06")
    Runner(app).tick()
    assert app.db.get_product("low00001") is None


def test_sunday_summary_is_weekly(make_app):
    # 2026-10-25 は日曜日
    app = make_app(datetime(2026, 10, 25, 11, 50, tzinfo=JST), start_date="2026-10-06")
    Runner(app).tick()
    summary = [s for s in app.db.slots_for(app.ops_date()) if s.kind == "summary"]
    assert len(summary) == 1 and summary[0].period == "W"
    assert "今週" in summary[0].text and summary[0].pattern_id.endswith("-W")
