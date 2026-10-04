"""DMMアフィリエイトAPI(ItemList)のクライアントと、レスポンスの解析。"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from ..models import Product

log = logging.getLogger(__name__)

API_URL = "https://api.dmm.com/affiliate/v3/ItemList"
SITE = "FANZA"
SERVICE = "digital"  # FANZA動画。アニメ・同人・漫画のサービスは使わない


class DmmApiError(Exception):
    pass


class DmmClient:
    def __init__(
        self,
        api_id: str,
        affiliate_id: str,
        interval_sec: float = 1.0,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        self.api_id = api_id
        self.affiliate_id = affiliate_id
        self.interval_sec = interval_sec
        self.timeout = timeout
        self.session = session or requests.Session()
        self._last_call = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.interval_sec:
            time.sleep(self.interval_sec - elapsed)
        self._last_call = time.monotonic()

    def item_list(
        self,
        floor: str,
        *,
        sort: str | None = None,
        hits: int = 100,
        offset: int = 1,
        cid: str | None = None,
        affiliate_id: str | None = None,
    ) -> list[dict]:
        params = {
            "api_id": self.api_id,
            "affiliate_id": affiliate_id or self.affiliate_id,
            "site": SITE,
            "service": SERVICE,
            "floor": floor,
            "hits": hits,
            "offset": offset,
            "output": "json",
        }
        if sort:
            params["sort"] = sort
        if cid:
            params["cid"] = cid
        last_error: Exception | None = None
        for attempt in range(3):
            self._wait()
            try:
                resp = self.session.get(API_URL, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                last_error = e
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                last_error = DmmApiError(f"HTTP {resp.status_code}")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise DmmApiError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            result = data.get("result") or {}
            status = str(result.get("status", "200"))
            if status != "200":
                raise DmmApiError(f"APIエラー status={status}: {result.get('message') or result.get('errors')}")
            items = result.get("items") or []
            return items if isinstance(items, list) else [items]
        raise DmmApiError(f"API呼び出しに失敗しました: {last_error}")

    def find_item(self, floor: str, cid: str, affiliate_id: str | None = None) -> dict | None:
        """商品IDで1件取得する(投稿直前の価格再確認用)。"""
        items = self.item_list(floor, cid=cid, hits=1, affiliate_id=affiliate_id)
        for item in items:
            if item.get("content_id") == cid:
                return item
        return None


# ---------------------------------------------------------------------- 解析
_YEN_RE = re.compile(r"\d[\d,]*")


def parse_yen(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _YEN_RE.search(str(value))
    if not m:
        return None
    return int(m.group(0).replace(",", ""))


@dataclass
class Pricing:
    price: int
    list_price: int
    discount_rate: int
    price_from: bool


def discount_of(price: int, list_price: int) -> int:
    """割引率(%)。誇張しないよう小数点以下は切り捨てる。"""
    if list_price <= 0 or price >= list_price:
        return 0
    return int(math.floor((1 - price / list_price) * 100 + 1e-9))


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_pricing(prices: dict | None) -> Pricing | None:
    """配信形態ごとの価格から、最も安い配信のセール価格・通常価格・割引率を求める。"""
    if not prices:
        return None
    deliveries = _as_list((prices.get("deliveries") or {}).get("delivery"))
    pairs: list[tuple[int, int]] = []
    sale_prices: set[int] = set()
    for d in deliveries:
        p = parse_yen(d.get("price"))
        lp = parse_yen(d.get("list_price"))
        if p is not None:
            sale_prices.add(p)
        if p is not None and lp:
            pairs.append((p, lp))
    if not pairs:
        p = parse_yen(prices.get("price"))
        lp = parse_yen(prices.get("list_price"))
        if p is None:
            return None
        pairs.append((p, lp or p))
        sale_prices.add(p)
    price, list_price = min(pairs, key=lambda x: (x[0], -discount_of(x[0], x[1])))
    price_from = len(sale_prices) > 1 or "~" in str(prices.get("price", ""))
    return Pricing(price=price, list_price=list_price, discount_rate=discount_of(price, list_price), price_from=price_from)


def _names(items) -> list[str]:
    names: list[str] = []
    for it in _as_list(items):
        name = (it or {}).get("name") if isinstance(it, dict) else None
        if name and name not in names:
            names.append(str(name))
    return names


def _date_part(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip()[:10]


def _datetime_part(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip()[:16]


def parse_item(raw: dict, floor: str) -> Product | None:
    """ItemList の1件を Product に変換する。価格が取れない商品は None。"""
    content_id = raw.get("content_id")
    title = raw.get("title")
    if not content_id or not title:
        return None
    pricing = extract_pricing(raw.get("prices"))
    if pricing is None:
        return None
    info = raw.get("iteminfo") or {}
    images = raw.get("imageURL") or {}
    campaigns = [c for c in _as_list(raw.get("campaign")) if isinstance(c, dict)]
    campaign = min(campaigns, key=lambda c: str(c.get("date_end") or "9999")) if campaigns else {}
    series = _names(info.get("series"))
    makers = _names(info.get("maker"))
    labels = _names(info.get("label"))
    return Product(
        content_id=str(content_id),
        floor=floor,
        title=str(title),
        url=str(raw.get("URL") or ""),
        affiliate_url=str(raw.get("affiliateURL") or ""),
        image_url=str(images.get("large") or images.get("list") or images.get("small") or ""),
        genre_names=_names(info.get("genre")),
        series=series[0] if series else "",
        maker=makers[0] if makers else "",
        label=labels[0] if labels else "",
        actresses=_names(info.get("actress")),
        price=pricing.price,
        list_price=pricing.list_price,
        discount_rate=pricing.discount_rate,
        price_from=pricing.price_from,
        release_date=_date_part(raw.get("date")),
        sale_end=_datetime_part(campaign.get("date_end")),
        campaign_title=str(campaign.get("title") or ""),
    )


_AF_ID_RE = re.compile(r"(?<=[?&]af_id=)[^&#]*")


def with_affiliate_id(url: str, affiliate_id: str) -> str:
    """アフィリエイトURLの af_id を計測用のIDに置き換える(他の部分はそのまま)。"""
    if not url:
        return url
    if not _AF_ID_RE.search(url):
        log.warning("アフィリエイトURLに af_id が見つかりません: %s", url)
        return url
    return _AF_ID_RE.sub(quote(affiliate_id, safe="-_."), url, count=1)


def affiliate_id_in(url: str) -> str:
    for k, v in parse_qsl(urlsplit(url).query):
        if k == "af_id":
            return v
    return ""
