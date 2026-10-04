"""手入力したDMMのクリック数・成果数CSVの読み込み。

clicks_by_id.csv   : 日付, アフィリエイトID, クリック数, 成果数  (必須。トーン・まとめ形式の比較に使う)
clicks_by_item.csv : 日付, 商品ID, クリック数, 成果数          (任意。ジャンル・時間帯の比較に使う)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..csvlog import read_rows, write_rows
from ..textutil import norm

BY_ID_HEADER = ["日付", "アフィリエイトID", "クリック数", "成果数"]
BY_ITEM_HEADER = ["日付", "商品ID", "クリック数", "成果数"]

ALIASES = {
    "date": ["date", "日付", "日"],
    "affiliate_id": ["affiliate_id", "アフィリエイトid", "affiliate id", "id"],
    "content_id": ["content_id", "商品id", "cid", "品番"],
    "clicks": ["clicks", "クリック数", "クリック"],
    "conversions": ["conversions", "成果数", "成果", "成約数"],
}


@dataclass
class ClickRow:
    day: date
    key: str
    clicks: int
    conversions: int


def _column(headers: list[str], name: str) -> str | None:
    wanted = {norm(a) for a in ALIASES[name]}
    for h in headers:
        if norm(h) in wanted:
            return h
    return None


def parse_date(value: str) -> date:
    m = re.match(r"^\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", value or "")
    if not m:
        raise ValueError(f"日付を読めません: {value!r}(2026-10-20 の形式で入力してください)")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_int(value: str) -> int:
    text = (value or "").replace(",", "").strip()
    return int(float(text)) if text else 0


def load_clicks(path: Path, key: str) -> list[ClickRow]:
    """key は 'affiliate_id' か 'content_id'。"""
    rows = read_rows(path)
    if not rows:
        return []
    headers = list(rows[0].keys())
    cols = {name: _column(headers, name) for name in ("date", key, "clicks", "conversions")}
    missing = [n for n in ("date", key, "clicks") if cols[n] is None]
    if missing:
        raise ValueError(f"{path.name} に必要な列がありません: {missing}(見出し: {headers})")
    result = []
    for i, row in enumerate(rows, start=2):
        if not any(row.values()):
            continue
        try:
            result.append(
                ClickRow(
                    day=parse_date(row[cols["date"]]),
                    key=row[cols[key]].strip(),
                    clicks=parse_int(row[cols["clicks"]]),
                    conversions=parse_int(row[cols["conversions"]]) if cols["conversions"] else 0,
                )
            )
        except ValueError as e:
            raise ValueError(f"{path.name} の{i}行目: {e}") from None
    return result


def ensure_templates(by_id: Path, by_item: Path) -> None:
    """入力用CSVがなければ見出しだけのファイルを作る。"""
    if not by_id.exists():
        write_rows(by_id, BY_ID_HEADER, [])
    if not by_item.exists():
        write_rows(by_item, BY_ITEM_HEADER, [])
