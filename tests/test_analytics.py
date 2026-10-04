from __future__ import annotations

from datetime import date, datetime, timedelta

from fanza_poster.analytics.report import Dataset, build_report, judge_exit, slope, write_report
from fanza_poster.analytics.review import find_candidates
from fanza_poster.csvlog import write_rows
from fanza_poster.models import KIND_SINGLE, KIND_SUMMARY, R_POSTED, S_DONE, Slot
from fanza_poster.patterns import DIM_TONE, Patterns

from .conftest import JST

AUTO_START = date(2026, 10, 20)  # start_date=2026-10-06 の15日目


def _slot(day: date, style: str, kind: str = KIND_SINGLE, cid: str = "", genre: str = "SGL", band: str = "H21") -> Slot:
    return Slot(
        id=None, ops_date=day.isoformat(), seq=1, scheduled_at=f"{day.isoformat()}T21:00:00+09:00", kind=kind,
        band=band, style=style, genre=genre if kind == KIND_SINGLE else "",
        pattern_id=f"{style}-{band}-{genre}", content_ids=[cid or f"c-{day}-{style}"], status=S_DONE,
        result=R_POSTED, counted=True, tweet_url="https://x.com/me/status/1",
    )


def _seed(app, weeks: int, clicks_per_post: dict[int, dict[str, float]]):
    """weeks 週ぶん、毎日 TA/TB/TC/SA を1件ずつ投稿した状態と、IDごとのクリック数を作る。"""
    rows = []
    slots = []
    for w in range(1, weeks + 1):
        for d in range(7):
            day = AUTO_START + timedelta(days=7 * (w - 1) + d)
            for style in ("TA", "TB", "TC"):
                slots.append(_slot(day, style))
            slots.append(_slot(day, "SA", kind=KIND_SUMMARY))
        for style in ("TA", "TB", "TC", "SA"):
            cpp = clicks_per_post[w].get(style, 1.0)
            day = AUTO_START + timedelta(days=7 * (w - 1) + 3)
            rows.append([day.isoformat(), app.settings.affiliate_id_for(style), int(round(cpp * 7)), 1])
    with app.db.conn:
        for s in slots:
            app.db._insert_slot(s)
    write_rows(app.settings.path(app.cfg.analytics.clicks_by_id_csv), ["日付", "アフィリエイトID", "クリック数", "成果数"], rows)


def test_slope():
    assert slope([(1, 1.0), (2, 2.0), (3, 3.0)]) == 1.0
    assert slope([(1, 3.0), (2, 2.0)]) == -1.0
    assert slope([(1, 1.0)]) is None


def test_style_buckets_use_affiliate_ids(make_app):
    app = make_app(datetime(2026, 11, 3, 10, 0, tzinfo=JST), start_date="2026-10-06")
    _seed(app, 2, {1: {"TA": 2.0, "TB": 0.5, "TC": 1.0, "SA": 3.0}, 2: {"TA": 2.0, "TB": 0.5, "TC": 1.0, "SA": 3.0}})
    ds = Dataset(app)
    b = ds.style_buckets({1})
    assert b["TA"].posts == 7 and b["TA"].clicks == 14
    assert b["TB"].clicks == 4  # 0.5*7=3.5 → 4
    assert b["SA"].posts == 7 and b["SA"].clicks == 21


def test_review_finds_low_tone_and_requires_manual_approval(make_app):
    app = make_app(datetime(2026, 11, 3, 10, 0, tzinfo=JST), start_date="2026-10-06")
    data = {w: {"TA": 2.0, "TB": 0.3, "TC": 1.8, "SA": 3.0} for w in (1, 2)}
    _seed(app, 2, data)
    candidates, notes = find_candidates(Dataset(app), 2)
    tones = [c for c in candidates if c.dim == DIM_TONE]
    assert len(tones) == 1 and tones[0].code == "TB"
    # 候補を出すだけでは無効化されない(承認制)
    assert "TB" in Patterns(app.cfg, app.db).enabled(DIM_TONE)
    assert any("商品別" in n for n in notes)


def test_disabled_tone_is_not_used_for_new_plans(make_app):
    from fanza_poster.runner import Runner

    app = make_app(datetime(2026, 10, 20, 11, 50, tzinfo=JST), start_date="2026-10-06")
    Patterns(app.cfg, app.db).set_enabled(DIM_TONE, "TB", False, "test")
    Runner(app).tick()
    styles = {s.style for s in app.db.slots_for(app.ops_date()) if s.kind == KIND_SINGLE}
    assert "TB" not in styles and styles <= {"TA", "TC"}


def test_cannot_disable_last_enabled(make_app):
    import pytest

    app = make_app(datetime(2026, 10, 20, 11, 50, tzinfo=JST), start_date="2026-10-06")
    p = Patterns(app.cfg, app.db)
    p.set_enabled(DIM_TONE, "TA", False)
    p.set_enabled(DIM_TONE, "TB", False)
    with pytest.raises(ValueError):
        p.set_enabled(DIM_TONE, "TC", False)


def test_exit_judgement_at_week_8(make_app):
    app = make_app(datetime(2026, 12, 16, 10, 0, tzinfo=JST), start_date="2026-10-06")
    falling = {w: {s: 3.0 - 0.2 * w for s in ("TA", "TB", "TC", "SA")} for w in range(1, 9)}
    _seed(app, 8, falling)
    ds = Dataset(app)
    assert judge_exit(ds, 7).status == "判定前"
    j = judge_exit(ds, 8)
    assert j.status == "停止を提案" and j.slope < 0


def test_exit_judgement_continue_when_rising(make_app):
    app = make_app(datetime(2026, 12, 16, 10, 0, tzinfo=JST), start_date="2026-10-06")
    rising = {w: {s: 0.5 + 0.3 * w for s in ("TA", "TB", "TC", "SA")} for w in range(1, 9)}
    _seed(app, 8, rising)
    assert judge_exit(Dataset(app), 8).status == "継続"


def test_report_written(make_app):
    app = make_app(datetime(2026, 12, 16, 10, 0, tzinfo=JST), start_date="2026-10-06")
    _seed(app, 8, {w: {"TA": 1.0, "TB": 1.0, "TC": 1.0, "SA": 1.0} for w in range(1, 9)})
    week, md, rows = build_report(app)
    assert week == 8  # 12/16 は9週目 → 直近で終わった週は8週目
    assert "第8週" in md and "撤退判定" in md and "test-991" in md
    md_path, csv_path, _ = write_report(app, 8)
    assert md_path.exists() and csv_path.exists()


def test_report_before_auto_start(make_app):
    import pytest

    app = make_app(datetime(2026, 10, 10, 10, 0, tzinfo=JST), start_date="2026-10-06")
    with pytest.raises(ValueError):
        build_report(app)
