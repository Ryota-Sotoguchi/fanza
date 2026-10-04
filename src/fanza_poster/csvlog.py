"""CSV の書き込み(Excelで文字化けしないよう UTF-8 BOM 付き)。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

POST_LOG_HEADER = [
    "記録時刻",
    "運用日",
    "予定時刻",
    "種別",
    "商品ID",
    "ジャンル",
    "パターンID",
    "スタイル",
    "時間帯",
    "アフィリエイトID",
    "投稿文",
    "リプライ文",
    "結果",
    "投稿URL",
    "エラー",
    "ドライラン",
]


def append_rows(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(header)
        for row in rows:
            writer.writerow(list(row))


def write_rows(path: Path, header: Sequence[str], rows: Iterable[Sequence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(list(row))


def read_rows(path: Path) -> list[dict[str, str]]:
    """手入力CSVを読む。UTF-8(BOMあり/なし)と Shift_JIS に対応。"""
    if not path.exists():
        return []
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path} の文字コードを判定できません(UTF-8 か Shift_JIS で保存してください)")
    reader = csv.DictReader(text.splitlines())
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
