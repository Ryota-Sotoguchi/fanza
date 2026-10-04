"""ドライラン: 投稿せず、手動投稿用のテキストファイルに書き出す。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import KIND_SINGLE, Slot
from ..timeutil import parse_iso

if TYPE_CHECKING:
    from ..app import App

RULE = "-" * 60


def _time_label(slot: Slot) -> str:
    at = parse_iso(slot.scheduled_at)
    label = at.strftime("%H:%M")
    if at.date().isoformat() != slot.ops_date:
        label += "(翌日)"
    return label


def render_slot(app: "App", slot: Slot, index: int, total: int) -> str:
    from ..patterns import DIM_GENRE, DIM_SUMMARY, DIM_TONE, Patterns
    from ..postlog import kind_label

    patterns = Patterns(app.cfg, app.db)
    if slot.kind == KIND_SINGLE:
        detail = f"トーン: {patterns.name(DIM_TONE, slot.style)} / ジャンル: {patterns.name(DIM_GENRE, slot.genre)}"
    else:
        detail = f"形式: {patterns.name(DIM_SUMMARY, slot.style)}"
    lines = [
        RULE,
        f"[{index}/{total}] 予定 {_time_label(slot)}  {kind_label(slot)}  パターン {slot.pattern_id}",
        f"({detail} / アフィリエイトID: {slot.affiliate_id})",
        f"商品ID: {', '.join(slot.content_ids)}",
    ]
    for url in slot.image_urls:
        lines.append(f"画像: {url}")
    lines += [
        "",
        "▼本文",
        slot.text,
        "",
        "▼リプライ(本文を投稿したあと、自分の投稿に返信する)",
        slot.reply_text,
        "",
    ]
    return "\n".join(lines)


def write_manual_file(app: "App", ops_date: date, heading: str, slots: list[Slot]) -> Path:
    path = app.settings.output_dir / "manual" / f"{ops_date.isoformat()}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"FANZA セール投稿 下書き {ops_date.isoformat()}({heading})",
        "",
        "手動で投稿する場合は、予定時刻ごとに「本文」を投稿し、その投稿に「リプライ」の内容を返信してください。",
        "画像を添付する場合は「画像」のURLの画像(DMMのAPIが返したもの)だけを使ってください。",
        "本文の価格・割引率は作成時点のものです。投稿前にセールが続いているか確認してください。",
        "",
    ]
    body = [render_slot(app, s, i + 1, len(slots)) for i, s in enumerate(slots)]
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8-sig")
    return path


def append_dry_run(app: "App", slot: Slot) -> Path:
    """実行時にドライラン指定された枠を output/dryrun/日付.txt に追記する。"""
    path = app.settings.output_dir / "dryrun" / f"{slot.ops_date}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(render_slot(app, slot, slot.seq, slot.seq) + "\n")
    return path
