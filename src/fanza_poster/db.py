"""SQLite のキュー(商品・投稿枠・状態・パターン有効/無効)。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .models import (
    P_PENDING,
    P_SKIPPED,
    S_PLANNED,
    Product,
    Slot,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    content_id      TEXT PRIMARY KEY,
    floor           TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT,
    affiliate_url   TEXT,
    image_url       TEXT,
    genre           TEXT NOT NULL,
    genre_names     TEXT NOT NULL DEFAULT '[]',
    series          TEXT,
    maker           TEXT,
    label           TEXT,
    actresses       TEXT NOT NULL DEFAULT '[]',
    price           INTEGER,
    list_price      INTEGER,
    discount_rate   INTEGER,
    price_from      INTEGER NOT NULL DEFAULT 0,
    release_date    TEXT,
    sale_end        TEXT,
    campaign_title  TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    status_note     TEXT,
    pattern_id      TEXT,
    posted_at       TEXT,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status, last_seen_date);

CREATE TABLE IF NOT EXISTS collections (
    ops_date   TEXT PRIMARY KEY,
    attempts   INTEGER NOT NULL DEFAULT 0,
    success    INTEGER NOT NULL DEFAULT 0,
    fetched    INTEGER,
    kept       INTEGER,
    excluded   INTEGER,
    error      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    ops_date     TEXT PRIMARY KEY,
    phase        TEXT NOT NULL,
    week         INTEGER NOT NULL,
    target_posts INTEGER NOT NULL,
    dry_run      INTEGER NOT NULL,
    note         TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ops_date     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    kind         TEXT NOT NULL,
    period       TEXT,
    band         TEXT,
    style        TEXT,
    genre        TEXT,
    pattern_id   TEXT,
    content_ids  TEXT NOT NULL DEFAULT '[]',
    labels       TEXT NOT NULL DEFAULT '{}',
    text         TEXT,
    reply_text   TEXT,
    image_urls   TEXT NOT NULL DEFAULT '[]',
    affiliate_id TEXT,
    status       TEXT NOT NULL DEFAULT 'planned',
    result       TEXT,
    counted      INTEGER NOT NULL DEFAULT 0,
    tweet_url    TEXT,
    reply_url    TEXT,
    error        TEXT,
    executed_at  TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_slots_date ON slots(ops_date);
CREATE INDEX IF NOT EXISTS idx_slots_status ON slots(status, scheduled_at);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pattern_status (
    dimension  TEXT NOT NULL,
    code       TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    note       TEXT,
    updated_at TEXT,
    PRIMARY KEY (dimension, code)
);

CREATE TABLE IF NOT EXISTS halt_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    freeze     INTEGER NOT NULL,
    reason     TEXT,
    detail     TEXT,
    resumed_at TEXT
);
"""

_PRODUCT_COLS = [
    "content_id", "floor", "title", "url", "affiliate_url", "image_url", "genre", "genre_names",
    "series", "maker", "label", "actresses", "price", "list_price", "discount_rate", "price_from",
    "release_date", "sale_end", "campaign_title", "status", "status_note", "pattern_id", "posted_at",
    "first_seen_date", "last_seen_date",
]


def _now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ products
    @staticmethod
    def _row_to_product(row: sqlite3.Row) -> Product:
        d = dict(row)
        return Product(
            content_id=d["content_id"],
            floor=d["floor"],
            title=d["title"],
            url=d["url"] or "",
            affiliate_url=d["affiliate_url"] or "",
            image_url=d["image_url"] or "",
            genre=d["genre"],
            genre_names=json.loads(d["genre_names"] or "[]"),
            series=d["series"] or "",
            maker=d["maker"] or "",
            label=d["label"] or "",
            actresses=json.loads(d["actresses"] or "[]"),
            price=d["price"] or 0,
            list_price=d["list_price"] or 0,
            discount_rate=d["discount_rate"] or 0,
            price_from=bool(d["price_from"]),
            release_date=d["release_date"] or "",
            sale_end=d["sale_end"] or "",
            campaign_title=d["campaign_title"] or "",
            status=d["status"],
            status_note=d["status_note"] or "",
            pattern_id=d["pattern_id"] or "",
            posted_at=d["posted_at"] or "",
            first_seen_date=d["first_seen_date"] or "",
            last_seen_date=d["last_seen_date"] or "",
        )

    def upsert_product(self, p: Product, seen_date: date) -> None:
        """収集した商品を保存する。状態(投稿済みなど)は保持し、価格などの情報だけ更新する。"""
        existing = self.get_product(p.content_id)
        seen = seen_date.isoformat()
        if existing is None:
            values = {
                **{c: getattr(p, c) for c in _PRODUCT_COLS},
                "genre_names": json.dumps(p.genre_names, ensure_ascii=False),
                "actresses": json.dumps(p.actresses, ensure_ascii=False),
                "price_from": int(p.price_from),
                "status": P_PENDING,
                "status_note": "",
                "pattern_id": "",
                "posted_at": "",
                "first_seen_date": seen,
                "last_seen_date": seen,
            }
            cols = ", ".join(values) + ", updated_at"
            marks = ", ".join("?" for _ in values) + ", ?"
            self.conn.execute(
                f"INSERT INTO products ({cols}) VALUES ({marks})", [*values.values(), _now_iso()]
            )
        else:
            status, note = existing.status, existing.status_note
            # セール終了でスキップした商品が再びセール対象になったら待機に戻す
            if status == P_SKIPPED and note in ("sale_ended", "below_min_discount"):
                status, note = P_PENDING, ""
            self.conn.execute(
                """UPDATE products SET floor=?, title=?, url=?, affiliate_url=?, image_url=?, genre=?,
                   genre_names=?, series=?, maker=?, label=?, actresses=?, price=?, list_price=?,
                   discount_rate=?, price_from=?, release_date=?, sale_end=?, campaign_title=?,
                   status=?, status_note=?, last_seen_date=?, updated_at=? WHERE content_id=?""",
                (
                    p.floor, p.title, p.url, p.affiliate_url, p.image_url, p.genre,
                    json.dumps(p.genre_names, ensure_ascii=False), p.series, p.maker, p.label,
                    json.dumps(p.actresses, ensure_ascii=False), p.price, p.list_price,
                    p.discount_rate, int(p.price_from), p.release_date, p.sale_end, p.campaign_title,
                    status, note, max(seen, existing.last_seen_date or seen), _now_iso(), p.content_id,
                ),
            )
        self.conn.commit()

    def update_offer(self, p: Product) -> None:
        """投稿直前の再確認で得た価格情報を反映する。"""
        self.conn.execute(
            """UPDATE products SET price=?, list_price=?, discount_rate=?, price_from=?, sale_end=?,
               campaign_title=?, affiliate_url=?, image_url=?, updated_at=? WHERE content_id=?""",
            (p.price, p.list_price, p.discount_rate, int(p.price_from), p.sale_end, p.campaign_title,
             p.affiliate_url, p.image_url, _now_iso(), p.content_id),
        )
        self.conn.commit()

    def get_product(self, content_id: str) -> Product | None:
        row = self.conn.execute("SELECT * FROM products WHERE content_id=?", (content_id,)).fetchone()
        return self._row_to_product(row) if row else None

    def set_product_status(
        self, content_id: str, status: str, note: str = "", pattern_id: str | None = None,
        posted_at: str | None = None,
    ) -> None:
        sets = ["status=?", "status_note=?", "updated_at=?"]
        args: list = [status, note, _now_iso()]
        if pattern_id is not None:
            sets.append("pattern_id=?")
            args.append(pattern_id)
        if posted_at is not None:
            sets.append("posted_at=?")
            args.append(posted_at)
        args.append(content_id)
        self.conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE content_id=?", args)
        self.conn.commit()

    def candidates(
        self, genre: str, min_seen: date, min_discount: int, exclude_ids: Iterable[str], limit: int = 20
    ) -> list[Product]:
        """個別投稿の候補(待機中・最近確認できた・割引率が下限以上)。割引率の高い順。"""
        rows = self.conn.execute(
            """SELECT * FROM products WHERE status=? AND genre=? AND last_seen_date>=? AND discount_rate>=?
               ORDER BY discount_rate DESC, last_seen_date DESC, price ASC, content_id""",
            (P_PENDING, genre, min_seen.isoformat(), min_discount),
        ).fetchall()
        excluded = set(exclude_ids)
        result = [self._row_to_product(r) for r in rows if r["content_id"] not in excluded]
        return result[:limit]

    def ranking(
        self, start: date, end: date, min_discount: int, genres: Iterable[str], exclude_ids: Iterable[str] = ()
    ) -> list[Product]:
        """まとめ投稿用のランキング(期間内に確認できた商品を割引率の高い順に)。"""
        genre_list = list(genres)
        if not genre_list:
            return []
        marks = ",".join("?" for _ in genre_list)
        rows = self.conn.execute(
            f"""SELECT * FROM products WHERE last_seen_date>=? AND last_seen_date<=? AND discount_rate>=?
                AND genre IN ({marks}) AND status NOT IN ('skipped', 'failed')
                ORDER BY discount_rate DESC, price ASC, content_id""",
            (start.isoformat(), end.isoformat(), min_discount, *genre_list),
        ).fetchall()
        excluded = set(exclude_ids)
        return [self._row_to_product(r) for r in rows if r["content_id"] not in excluded]

    def product_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM products GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --------------------------------------------------------------- collections
    def collection(self, ops_date: date) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM collections WHERE ops_date=?", (ops_date.isoformat(),)).fetchone()

    def record_collection(
        self, ops_date: date, success: bool, fetched: int = 0, kept: int = 0, excluded: int = 0, error: str = ""
    ) -> None:
        row = self.collection(ops_date)
        attempts = (row["attempts"] if row else 0) + 1
        self.conn.execute(
            """INSERT INTO collections (ops_date, attempts, success, fetched, kept, excluded, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(ops_date) DO UPDATE SET attempts=excluded.attempts, success=excluded.success,
               fetched=excluded.fetched, kept=excluded.kept, excluded=excluded.excluded,
               error=excluded.error, updated_at=excluded.updated_at""",
            (ops_date.isoformat(), attempts, int(success), fetched, kept, excluded, error, _now_iso()),
        )
        self.conn.commit()

    # --------------------------------------------------------------------- plans
    def get_plan(self, ops_date: date) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM plans WHERE ops_date=?", (ops_date.isoformat(),)).fetchone()

    def insert_plan(
        self, ops_date: date, phase: str, week: int, target: int, dry_run: bool, note: str, slots: list[Slot]
    ) -> list[Slot]:
        with self.conn:
            self.conn.execute(
                "INSERT INTO plans (ops_date, phase, week, target_posts, dry_run, note, created_at) VALUES (?,?,?,?,?,?,?)",
                (ops_date.isoformat(), phase, week, target, int(dry_run), note, _now_iso()),
            )
            for s in slots:
                s.id = self._insert_slot(s)
        return slots

    # --------------------------------------------------------------------- slots
    def _insert_slot(self, s: Slot) -> int:
        cur = self.conn.execute(
            """INSERT INTO slots (ops_date, seq, scheduled_at, kind, period, band, style, genre, pattern_id,
               content_ids, labels, text, reply_text, image_urls, affiliate_id, status, result, counted,
               tweet_url, reply_url, error, executed_at, dry_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                s.ops_date, s.seq, s.scheduled_at, s.kind, s.period, s.band, s.style, s.genre, s.pattern_id,
                s.content_ids_json, json.dumps(s.labels, ensure_ascii=False), s.text, s.reply_text,
                json.dumps(s.image_urls, ensure_ascii=False), s.affiliate_id, s.status, s.result,
                int(s.counted), s.tweet_url, s.reply_url, s.error, s.executed_at, int(s.dry_run),
            ),
        )
        return int(cur.lastrowid)

    def update_slot(self, s: Slot) -> None:
        self.conn.execute(
            """UPDATE slots SET scheduled_at=?, kind=?, period=?, band=?, style=?, genre=?, pattern_id=?,
               content_ids=?, labels=?, text=?, reply_text=?, image_urls=?, affiliate_id=?, status=?, result=?,
               counted=?, tweet_url=?, reply_url=?, error=?, executed_at=?, dry_run=? WHERE id=?""",
            (
                s.scheduled_at, s.kind, s.period, s.band, s.style, s.genre, s.pattern_id, s.content_ids_json,
                json.dumps(s.labels, ensure_ascii=False), s.text, s.reply_text,
                json.dumps(s.image_urls, ensure_ascii=False), s.affiliate_id, s.status, s.result,
                int(s.counted), s.tweet_url, s.reply_url, s.error, s.executed_at, int(s.dry_run), s.id,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_slot(row: sqlite3.Row) -> Slot:
        d = dict(row)
        return Slot(
            id=d["id"],
            ops_date=d["ops_date"],
            seq=d["seq"],
            scheduled_at=d["scheduled_at"],
            kind=d["kind"],
            band=d["band"] or "",
            style=d["style"] or "",
            genre=d["genre"] or "",
            period=d["period"] or "",
            pattern_id=d["pattern_id"] or "",
            content_ids=json.loads(d["content_ids"] or "[]"),
            labels=json.loads(d["labels"] or "{}"),
            text=d["text"] or "",
            reply_text=d["reply_text"] or "",
            image_urls=json.loads(d["image_urls"] or "[]"),
            affiliate_id=d["affiliate_id"] or "",
            status=d["status"],
            result=d["result"] or "",
            counted=bool(d["counted"]),
            tweet_url=d["tweet_url"] or "",
            reply_url=d["reply_url"] or "",
            error=d["error"] or "",
            executed_at=d["executed_at"] or "",
            dry_run=bool(d["dry_run"]),
        )

    def slots_for(self, ops_date: date) -> list[Slot]:
        rows = self.conn.execute(
            "SELECT * FROM slots WHERE ops_date=? ORDER BY scheduled_at, seq", (ops_date.isoformat(),)
        ).fetchall()
        return [self._row_to_slot(r) for r in rows]

    def planned_slots(self) -> list[Slot]:
        rows = self.conn.execute(
            "SELECT * FROM slots WHERE status=? ORDER BY scheduled_at, seq", (S_PLANNED,)
        ).fetchall()
        return [self._row_to_slot(r) for r in rows]

    def all_slots(self, live_only: bool = False) -> list[Slot]:
        sql = "SELECT * FROM slots"
        if live_only:
            sql += " WHERE dry_run=0"
        rows = self.conn.execute(sql + " ORDER BY scheduled_at, seq").fetchall()
        return [self._row_to_slot(r) for r in rows]

    def reserved_content_ids(self) -> set[str]:
        """未処理の投稿枠に割り当て済みの商品ID。"""
        ids: set[str] = set()
        for s in self.planned_slots():
            ids.update(s.content_ids)
        return ids

    def counted_posts(self, ops_date: date) -> int:
        """日次上限の計算に使う投稿数(実際に投稿ボタンを押したもの)。"""
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM slots WHERE ops_date=? AND counted=1 AND dry_run=0", (ops_date.isoformat(),)
        ).fetchone()
        return int(row["n"])

    def usage_counts(self, column: str, since: date, kind: str | None = None) -> dict[str, int]:
        """直近のスタイル・ジャンルの使用回数(偏りなく割り当てるため)。"""
        if column not in ("style", "genre", "band"):
            raise ValueError(column)
        sql = f"SELECT {column} c, COUNT(*) n FROM slots WHERE ops_date>=? AND status!='skipped'"
        args: list = [since.isoformat()]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        rows = self.conn.execute(sql + f" GROUP BY {column}", args).fetchall()
        return {r["c"]: r["n"] for r in rows if r["c"]}

    # --------------------------------------------------------------------- state
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self.conn.execute("DELETE FROM state WHERE key=?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self.conn.commit()

    # ------------------------------------------------------------ pattern status
    def pattern_overrides(self) -> dict[tuple[str, str], sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM pattern_status").fetchall()
        return {(r["dimension"], r["code"]): r for r in rows}

    def set_pattern_enabled(self, dimension: str, code: str, enabled: bool, note: str = "") -> None:
        self.conn.execute(
            """INSERT INTO pattern_status (dimension, code, enabled, note, updated_at) VALUES (?,?,?,?,?)
               ON CONFLICT(dimension, code) DO UPDATE SET enabled=excluded.enabled, note=excluded.note,
               updated_at=excluded.updated_at""",
            (dimension, code, int(enabled), note, _now_iso()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ halt log
    def add_halt(self, kind: str, freeze: bool, reason: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO halt_log (at, kind, freeze, reason, detail) VALUES (?,?,?,?,?)",
            (_now_iso(), kind, int(freeze), reason, detail),
        )
        self.conn.commit()

    def mark_halts_resumed(self) -> None:
        self.conn.execute("UPDATE halt_log SET resumed_at=? WHERE resumed_at IS NULL", (_now_iso(),))
        self.conn.commit()

    def halts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM halt_log ORDER BY id").fetchall()
