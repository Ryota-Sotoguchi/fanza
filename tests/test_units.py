from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import pytest

from fanza_poster.collect.dmm_client import discount_of, extract_pricing, parse_item, with_affiliate_id
from fanza_poster.collect.filters import ExclusionFilter
from fanza_poster.collect.genre import classify
from fanza_poster.config import ConfigError, Secrets, load_secrets, parse_config
from fanza_poster.generate.summary import SummaryEntry, build_summary_reply, build_summary_text
from fanza_poster.generate.validate import Facts, validate_label, validate_single
from fanza_poster.post.detect import classify as detect
from fanza_poster.schedule.phase import AUTO, BEFORE_START, BURN_IN, compute_phase
from fanza_poster.schedule.slots import allocate
from fanza_poster.textutil import weighted_length

from .conftest import load_config_dict, make_settings, raw_item


# ------------------------------------------------------------------ 設定
def test_shipped_config_loads():
    cfg = parse_config(load_config_dict())
    assert cfg.collect.floors == ["videoa"]
    assert cfg.generation.model == "claude-haiku-4-5"
    assert cfg.schedule.weekly_posts == [6, 8, 12]


def test_env_example_values_are_reported_as_placeholders(tmp_path):
    example = Path(__file__).resolve().parents[1] / ".env.example"
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    secrets = load_secrets(tmp_path)
    assert secrets.missing() == []
    assert secrets.placeholders() == ["DMM_API_ID", "DMM_AFFILIATE_ID", "ANTHROPIC_API_KEY"]
    assert Secrets("apiid", "me-990", "sk-ant-real").placeholders() == []


@pytest.mark.parametrize("floor", ["anime", "doujin", "comic"])
def test_blocked_floors_rejected(floor):
    data = load_config_dict()
    data["collect"]["floors"] = ["videoa", floor]
    with pytest.raises(ConfigError):
        parse_config(data)


def test_videoc_can_be_enabled():
    data = load_config_dict()
    data["collect"]["floors"] = ["videoa", "videoc"]
    assert parse_config(data).collect.floors == ["videoa", "videoc"]


def test_tracking_ids_resolve_and_validate(tmp_path):
    s = make_settings(tmp_path)
    assert s.affiliate_id_for("TA") == "test-991"
    assert s.affiliate_id_for("SB") == "test-995"
    assert s.tracking_map()["test-992"] == "TB"
    data = load_config_dict()
    data["tracking"]["affiliate_ids"]["TB"] = 991  # 重複
    with pytest.raises(ConfigError):
        parse_config(data)
    data = load_config_dict()
    del data["tracking"]["affiliate_ids"]["TC"]  # 不足
    with pytest.raises(ConfigError):
        parse_config(data)
    data = load_config_dict()
    data["tracking"]["affiliate_ids"]["TA"] = 123  # 範囲外
    with pytest.raises(ConfigError):
        parse_config(data)


# ------------------------------------------------------------- 文字数
def test_weighted_length():
    assert weighted_length("abc") == 3
    assert weighted_length("あいう") == 6
    assert weighted_length("https://al.dmm.co.jp/?lurl=very-long-url&af_id=x-991") == 23
    assert weighted_length("見て https://example.com/a") == 2 * 2 + 1 + 23


# ---------------------------------------------------------- APIの解析
def test_parse_item_uses_cheapest_delivery_and_floors_discount():
    p = parse_item(raw_item("abc00001", price=990, list_price=1980), "videoa")
    assert p.price == 990 and p.list_price == 1980 and p.discount_rate == 50
    assert p.price_from is True
    assert p.release_date == "2026-09-01"
    assert p.sale_end == "2026-10-31 23:59"
    assert p.image_url.endswith("pl.jpg")
    assert discount_of(989, 1980) == 50  # 50.05% → 50
    assert discount_of(1000, 1980) == 49  # 49.49% → 49(切り捨て)


def test_extract_pricing_without_deliveries():
    pr = extract_pricing({"price": "500", "list_price": "2,000"})
    assert (pr.price, pr.list_price, pr.discount_rate, pr.price_from) == (500, 2000, 75, False)


def test_with_affiliate_id_replaces_only_af_id():
    url = "https://al.dmm.co.jp/?lurl=https%3A%2F%2Fwww.dmm.co.jp%2F&af_id=test-990&ch=api"
    assert with_affiliate_id(url, "test-992") == url.replace("test-990", "test-992")


# ------------------------------------------------------------ 除外・分類
def test_exclusion_filter_normalizes_width_and_case(tmp_path):
    cfg = make_settings(tmp_path).config
    f = ExclusionFilter(cfg.exclude)
    assert f.reason(parse_item(raw_item("a1", title="ＪＫの休日"), "videoa"))
    assert f.reason(parse_item(raw_item("a2", genres=("単体作品", "制服")), "videoa"))
    assert f.reason(parse_item(raw_item("a3", title="放課後の話"), "videoa"))
    assert f.reason(parse_item(raw_item("a4", series="学園もの"), "videoa"))
    assert f.reason(parse_item(raw_item("a5"), "videoa")) is None


def test_genre_classification(tmp_path):
    cats = make_settings(tmp_path).config.genre_categories
    assert classify(parse_item(raw_item("v", genres=("VR専用", "単体作品")), "videoa"), cats) == "VR"
    assert classify(parse_item(raw_item("s", genres=("単体作品",)), "videoa"), cats) == "SGL"
    assert classify(parse_item(raw_item("r", genres=("ドラマ",), series="休日"), "videoa"), cats) == "SER"
    assert classify(parse_item(raw_item("o", genres=("ドラマ",)), "videoa"), cats) == "OTH"


# --------------------------------------------------------------- 文面検証
def _facts(**kw) -> Facts:
    base = dict(
        content_id="abc", title="穏やかな休日の物語", genre_names=["ドラマ"], actresses=["山田花子"], maker="メーカーA",
        series="", price=495, list_price=1980, discount_rate=75, price_from=False, release_date="2026-09-01",
        sale_end="2026-10-31 23:59", campaign_title="",
    )
    base.update(kw)
    return Facts(**base)


GOOD = ("山田花子さん出演のドラマ作品がセール中です。通常1,980円のところ495円、75%OFFで配信されています。"
        "発売日は2026年9月1日。セールは10月31日 23:59までです。")


def _validate(text, facts=None):
    return validate_single(text, facts or _facts(), min_length=50, max_length=120,
                           ng_words=["巨乳", "制服"], pr_suffix="\n#PR")


def test_validate_good_text_gets_pr_suffix():
    r = _validate(GOOD)
    assert r.ok, r.errors
    assert r.final_text.endswith("\n#PR")
    assert r.final_text.count("#PR") == 1


def test_validate_strips_model_added_pr():
    r = _validate(GOOD + " #PR")
    assert r.ok and r.final_text.count("#PR") == 1


@pytest.mark.parametrize(
    "bad,expected",
    [
        (GOOD.replace("75%OFF", "80%OFF"), "割引率"),
        (GOOD.replace("495円", "500円"), "価格"),
        (GOOD + "累計10000本突破。", "数字"),
        (GOOD.replace("ドラマ作品", "巨乳作品"), "使えない語"),
        (GOOD + "https://example.com", "URL"),
        (GOOD + "#セール", "ハッシュタグ"),
        ("短すぎる文。", "文字数"),
    ],
)
def test_validate_rejects(bad, expected):
    r = _validate(bad)
    assert not r.ok
    assert any(expected in e for e in r.errors), r.errors


def test_validate_label():
    assert validate_label("山田花子出演のドラマ", 12, ["巨乳"]) == "山田花子出演のドラマ"
    assert validate_label("10本セット", 12, []) is None
    assert validate_label("巨乳もの", 12, ["巨乳"]) is None
    assert len(validate_label("とても長いラベルになってしまった例です", 8, [])) == 8


# ------------------------------------------------------------ まとめ投稿
def test_summary_fits_x_limit_with_long_labels():
    entries = [
        SummaryEntry(i + 1, f"c{i}", "とても長い作品ラベルが入る場合の例です", 1980 - i, 9980, 80 - i, True,
                     f"https://al.dmm.co.jp/?lurl=x&af_id=test-994&n={i}")
        for i in range(5)
    ]
    for fmt in ("SA", "SB"):
        for period in ("D", "W"):
            text = build_summary_text(fmt, period, entries, "\n#PR", 12)
            assert weighted_length(text) <= 280
            assert text.endswith("#PR")
    reply = build_summary_reply(entries, "{rank}位 {url}", "#PR")
    assert reply.count("https://") == 5 and weighted_length(reply) <= 280


# ------------------------------------------------------------ フェーズ
def test_phase_burn_in_then_weekly_ramp(tmp_path):
    cfg = make_settings(tmp_path, start_date="2026-10-06").config
    assert compute_phase(date(2026, 10, 5), cfg).kind == BEFORE_START
    p1 = compute_phase(date(2026, 10, 6), cfg)
    assert p1.kind == BURN_IN and p1.dry_run and p1.day == 1
    p14 = compute_phase(date(2026, 10, 19), cfg)
    assert p14.kind == BURN_IN and p14.day == 14
    p15 = compute_phase(date(2026, 10, 20), cfg)
    assert (p15.kind, p15.week, p15.target_posts, p15.dry_run) == (AUTO, 1, 6, False)
    assert compute_phase(date(2026, 10, 27), cfg).target_posts == 8
    assert compute_phase(date(2026, 11, 3), cfg).target_posts == 12
    assert compute_phase(date(2026, 12, 25), cfg).target_posts == 12
    # 警告検知で据え置き
    assert compute_phase(date(2026, 11, 3), cfg, ramp_frozen=6).target_posts == 6


def test_phase_respects_hard_limit(tmp_path):
    cfg = make_settings(tmp_path, start_date="2026-01-01", schedule={"daily_hard_limit": 10}).config
    assert compute_phase(date(2026, 6, 1), cfg).target_posts == 10


# ------------------------------------------------------------- 時刻配分
def test_allocation_window_gap_and_weighting(tmp_path):
    sched = make_settings(tmp_path).config.schedule
    late = 0
    total = 0
    for seed in range(200):
        slots = allocate(11, True, sched.bands, sched.bands, (21 * 60, 23 * 60), 30, 12 * 60, random.Random(seed))
        assert len(slots) == 12
        minutes = sorted(s.minute for s in slots)
        assert minutes[0] >= 12 * 60 and minutes[-1] <= 25 * 60
        assert all(b - a >= 30 - 1e-6 for a, b in zip(minutes, minutes[1:]))
        summary = [s for s in slots if s.kind == "summary"]
        assert len(summary) == 1 and 21 * 60 <= summary[0].minute <= 23 * 60
        late += sum(1 for s in slots if s.minute >= 21 * 60)
        total += len(slots)
    # 21時以降(4時間)の1時間あたりの件数が、それ以前(9時間)より多い
    share = late / total
    assert (share / 4) / ((1 - share) / 9) > 1.3


def test_allocation_respects_earliest(tmp_path):
    sched = make_settings(tmp_path).config.schedule
    slots = allocate(5, True, sched.bands, sched.bands, (21 * 60, 23 * 60), 30, 22 * 60 + 30, random.Random(1))
    assert all(s.minute >= 22 * 60 + 30 for s in slots)
    assert len(slots) == 6


# ------------------------------------------------------------------ 検知
def test_detection_rules():
    assert detect("https://x.com/home", [], ["https://client-api.arkoselabs.com/fc"]).kind == "captcha"
    assert detect("https://x.com/account/access", []).kind == "locked"
    assert detect("https://x.com/i/flow/login", []).kind == "logged_out"
    assert detect("https://x.com/home", ["本人確認のため電話番号を入力してください"]).kind == "verification"
    assert detect("https://x.com/home", ["This request looks like it might be automated."]).kind == "automation_warning"
    d = detect("https://x.com/compose/post", ["You are over the daily limit for sending posts."])
    assert d.kind == "rate_limited" and d.freeze
    e = detect("https://x.com/home", ["問題が発生しました。もう一度お試しください"])
    assert e.kind == "post_error" and not e.halt
    assert detect("https://x.com/home", ["ポストを送信しました。"]) is None


def test_detection_ignores_own_text():
    own = "スパムではない普通の告知文"
    assert detect("https://x.com/compose/post", [own], own_text=own) is None


def test_reply_only_to_own_posts(tmp_path):
    from fanza_poster.post.base import ReplyFailed
    from fanza_poster.post.x_browser import XBrowserPoster

    poster = XBrowserPoster(make_settings(tmp_path).config.post, tmp_path / "p", tmp_path / "s")
    poster.handle = "myaccount"
    poster._assert_own("https://x.com/myaccount/status/123")
    with pytest.raises(ReplyFailed):
        poster._assert_own("https://x.com/someone_else/status/123")
