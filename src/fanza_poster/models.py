"""商品・投稿枠のデータ構造。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 商品の状態
P_PENDING = "pending"  # 待機
P_POSTED = "posted"  # 投稿済み
P_FAILED = "failed"  # 失敗
P_SKIPPED = "skipped"  # スキップ(セール終了・生成不可など)
P_DRAFTED = "drafted"  # 手動投稿用に出力済み(慣らし期間・ドライラン)

PRODUCT_STATUS_JA = {
    P_PENDING: "待機",
    P_POSTED: "投稿済み",
    P_FAILED: "失敗",
    P_SKIPPED: "スキップ",
    P_DRAFTED: "手動用出力済み",
}

# 投稿枠の状態
S_PLANNED = "planned"
S_DONE = "done"
S_SKIPPED = "skipped"
S_DRY_RUN = "dry_run"

# 投稿結果
R_POSTED = "posted"  # 本文・リプライとも成功
R_REPLY_FAILED = "reply_failed"  # 本文は投稿済み、リンクのリプライに失敗
R_UNKNOWN = "unknown"  # 投稿ボタンは押したが結果を確認できない
R_FAILED = "failed"  # 投稿前に失敗
R_HALTED = "halted"  # ログイン切れ・CAPTCHA・警告などを検知して停止
R_SKIPPED = "skipped"
R_DRY_RUN = "dry_run"

RESULT_JA = {
    R_POSTED: "投稿済み",
    R_REPLY_FAILED: "本文のみ投稿(リプライ失敗)",
    R_UNKNOWN: "結果不明",
    R_FAILED: "失敗",
    R_HALTED: "停止",
    R_SKIPPED: "スキップ",
    R_DRY_RUN: "ドライラン",
}

KIND_SINGLE = "single"
KIND_SUMMARY = "summary"


@dataclass
class Product:
    content_id: str
    floor: str
    title: str
    url: str = ""
    affiliate_url: str = ""
    image_url: str = ""
    genre: str = "OTH"
    genre_names: list[str] = field(default_factory=list)
    series: str = ""
    maker: str = ""
    label: str = ""
    actresses: list[str] = field(default_factory=list)
    price: int = 0
    list_price: int = 0
    discount_rate: int = 0
    price_from: bool = False
    release_date: str = ""
    sale_end: str = ""
    campaign_title: str = ""
    status: str = P_PENDING
    status_note: str = ""
    pattern_id: str = ""
    posted_at: str = ""
    first_seen_date: str = ""
    last_seen_date: str = ""

    def same_offer(self, other: "Product") -> bool:
        return (
            self.price == other.price
            and self.list_price == other.list_price
            and self.discount_rate == other.discount_rate
            and self.price_from == other.price_from
            and self.sale_end == other.sale_end
        )


@dataclass
class Slot:
    id: int | None
    ops_date: str
    seq: int
    scheduled_at: str
    kind: str
    band: str
    style: str
    genre: str = ""
    period: str = ""
    pattern_id: str = ""
    content_ids: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    text: str = ""
    reply_text: str = ""
    image_urls: list[str] = field(default_factory=list)
    affiliate_id: str = ""
    status: str = S_PLANNED
    result: str = ""
    counted: bool = False
    tweet_url: str = ""
    reply_url: str = ""
    error: str = ""
    executed_at: str = ""
    dry_run: bool = False

    @property
    def content_ids_json(self) -> str:
        return json.dumps(self.content_ids, ensure_ascii=False)
