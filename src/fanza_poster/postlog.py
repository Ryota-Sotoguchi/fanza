"""投稿ごとの記録(data/posts_log.csv)。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .csvlog import POST_LOG_HEADER, append_rows
from .models import KIND_SINGLE, RESULT_JA, Slot

if TYPE_CHECKING:
    from .app import App

POST_LOG_FILE = "posts_log.csv"


def kind_label(slot: Slot) -> str:
    if slot.kind == KIND_SINGLE:
        return "個別"
    return "まとめ(週間)" if slot.period == "W" else "まとめ(日次)"


def log_slot(app: "App", slot: Slot) -> None:
    if slot.kind == KIND_SINGLE:
        genre = slot.genre
    else:
        genres = []
        for cid in slot.content_ids:
            p = app.db.get_product(cid)
            genres.append(p.genre if p else "")
        genre = ";".join(genres)
    row = [
        app.now().replace(microsecond=0).isoformat(),
        slot.ops_date,
        slot.scheduled_at,
        kind_label(slot),
        ";".join(slot.content_ids),
        genre,
        slot.pattern_id,
        slot.style,
        slot.band,
        slot.affiliate_id,
        slot.text,
        slot.reply_text,
        RESULT_JA.get(slot.result, slot.result),
        slot.tweet_url,
        slot.error,
        "はい" if slot.dry_run else "いいえ",
    ]
    append_rows(app.settings.data_dir / POST_LOG_FILE, POST_LOG_HEADER, [row])
