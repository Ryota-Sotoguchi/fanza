from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from fanza_poster.app import App
from fanza_poster.config import Secrets, Settings, parse_config
from fanza_poster.db import Database
from fanza_poster.generate.validate import Facts, validate_single

ROOT = Path(__file__).resolve().parents[1]
JST = ZoneInfo("Asia/Tokyo")


def raw_item(
    cid: str,
    *,
    title: str = "穏やかな休日の物語",
    price: int = 500,
    list_price: int = 1980,
    genres: tuple[str, ...] = ("単体作品",),
    series: str | None = None,
    actresses: tuple[str, ...] = ("山田花子",),
    release: str = "2026-09-01 10:00:00",
    sale_end: str = "2026-10-31 23:59:59",
) -> dict:
    return {
        "content_id": cid,
        "title": title,
        "URL": f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={cid}/",
        "affiliateURL": (
            "https://al.dmm.co.jp/?lurl=https%3A%2F%2Fwww.dmm.co.jp%2Fdigital%2Fvideoa%2F-%2Fdetail%2F%3D%2Fcid%3D"
            f"{cid}%2F&af_id=test-990&ch=api"
        ),
        "imageURL": {
            "list": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pt.jpg",
            "small": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}ps.jpg",
            "large": f"https://pics.dmm.co.jp/digital/video/{cid}/{cid}pl.jpg",
        },
        "prices": {
            "price": f"{price}~",
            "list_price": f"{list_price}~",
            "deliveries": {
                "delivery": [
                    {"type": "stream", "price": str(price), "list_price": str(list_price)},
                    {"type": "download", "price": str(price + 200), "list_price": str(list_price + 200)},
                ]
            },
        },
        "date": release,
        "iteminfo": {
            "genre": [{"id": i, "name": g} for i, g in enumerate(genres)],
            "series": [{"id": 1, "name": series}] if series else [],
            "maker": [{"id": 1, "name": "メーカーA"}],
            "actress": [{"id": i, "name": a} for i, a in enumerate(actresses)],
        },
        "campaign": [{"date_begin": "2026-10-01 10:00:00", "date_end": sale_end, "title": "期間限定セール"}],
    }


def catalog(n: int = 30) -> list[dict]:
    genres = [("単体作品",), ("VR専用",), ("ドラマ",), ("単体作品", "ドラマ")]
    items = []
    for i in range(n):
        g = genres[i % len(genres)]
        items.append(
            raw_item(
                f"abc{i:05d}",
                price=300 + 10 * i,
                list_price=1980,
                genres=g,
                series="休日シリーズ" if i % 4 == 2 else None,
                actresses=(f"出演者{i}",),
            )
        )
    return items


class FakeDmm:
    def __init__(self, items: list[dict]):
        self.items = {it["content_id"]: it for it in items}
        self.calls: list[dict] = []

    def item_list(self, floor, *, sort=None, hits=100, offset=1, cid=None, affiliate_id=None):
        self.calls.append({"floor": floor, "sort": sort, "offset": offset, "cid": cid, "affiliate_id": affiliate_id})
        values = list(self.items.values())
        return values[offset - 1 : offset - 1 + hits]

    def find_item(self, floor, cid, affiliate_id=None):
        self.calls.append({"cid": cid, "affiliate_id": affiliate_id})
        item = self.items.get(cid)
        if item is None:
            return None
        item = dict(item)
        if affiliate_id:
            item["affiliateURL"] = item["affiliateURL"].replace("af_id=test-990", f"af_id={affiliate_id}")
        return item

    def end_sale(self, cid: str) -> None:
        item = self.items[cid]
        for d in item["prices"]["deliveries"]["delivery"]:
            d["price"] = d["list_price"]
        item["prices"]["price"] = item["prices"]["list_price"]


class FakeWriter:
    """検証を通る定型文を返す生成役(実際の API は呼ばない)。"""

    def __init__(self, cfg, ng_words):
        self.cfg = cfg
        self.ng_words = ng_words
        self.calls = 0

    def write_single(self, facts: Facts, tone: str) -> str:
        self.calls += 1
        body = (
            f"{facts.genre_category}作品のセール情報です。通常{facts.list_price:,}円のところ、"
            f"現在は{facts.price:,}円{'〜' if facts.price_from else ''}で配信中です。割引率は{facts.discount_rate}%です。"
            "気になっていた方はこの機会に作品ページで詳細をご確認ください。"
        )
        result = validate_single(
            body, facts, min_length=self.cfg.min_length, max_length=self.cfg.max_length,
            ng_words=self.ng_words, pr_suffix=self.cfg.pr_suffix,
        )
        assert result.ok, result.errors
        return result.final_text

    def summary_labels(self, items, max_chars):
        return {f.content_id: f"{f.actresses[0]}出演作"[:max_chars] for f in items if f.actresses}

    def check_model(self):
        return "fake"


class FakePoster:
    def __init__(self, log: list, mode: str = "ok", handle: str = "myaccount"):
        self.log = log
        self.mode = mode
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def check_session(self):
        from fanza_poster.post.base import DetectionError

        if self.mode in ("captcha", "logged_out"):
            raise DetectionError(self.mode, "テスト用の検知")
        return self.handle

    def post(self, text, image_paths):
        from fanza_poster.post.base import PostFailed

        if self.mode == "fail":
            raise PostFailed("テスト用の失敗")
        n = len([x for x in self.log if x[0] == "post"]) + 1
        self.log.append(("post", text, [str(p) for p in image_paths]))
        return f"https://x.com/{self.handle}/status/{1000 + n}"

    def reply_to_own(self, status_url, text):
        from fanza_poster.post.base import ReplyFailed

        if not status_url.startswith(f"https://x.com/{self.handle}/status/"):
            raise ReplyFailed("自分の投稿ではありません")
        if self.mode == "reply_fail":
            raise ReplyFailed("テスト用のリプライ失敗")
        self.log.append(("reply", status_url, text))
        return status_url + "9"


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def notify(self, title, message):
        self.sent.append((title, message))
        return True


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


def load_config_dict() -> dict:
    with (ROOT / "config.yaml").open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    data = load_config_dict()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    config = parse_config(data)
    return Settings(
        config=config,
        root=tmp_path,
        config_path=tmp_path / "config.yaml",
        secrets=Secrets(dmm_api_id="apiid", dmm_affiliate_id="test-990", anthropic_api_key="sk-test"),
    )


@pytest.fixture
def make_app(tmp_path):
    def _make(now: datetime, items: list[dict] | None = None, poster_mode: str = "ok", **overrides):
        settings = make_settings(tmp_path, **overrides)
        app = App(settings=settings, db=Database(settings.db_path), rng=random.Random(42))
        app.clock = Clock(now)
        app.fake_dmm = FakeDmm(items if items is not None else catalog())
        app.dmm_factory = lambda: app.fake_dmm
        app.fake_writer = FakeWriter(settings.config.generation, app.ng_words())
        app.writer_factory = lambda: app.fake_writer
        app.post_log = []
        app.poster_mode = poster_mode
        app.poster_factory = lambda: FakePoster(app.post_log, app.poster_mode)
        app.image_fetcher = lambda url, dest: dest
        app.notifier = FakeNotifier()
        app.sleeper = lambda s: None
        return app

    return _make
