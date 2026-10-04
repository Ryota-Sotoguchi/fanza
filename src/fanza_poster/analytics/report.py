"""週次集計: スタイル(アフィリエイトID)別・ジャンル別・時間帯別のクリック率と、撤退判定。

クリック率は「1投稿あたりのクリック数」(クリック数 ÷ 投稿数)で表す。
表示回数(インプレッション)は取得しないため、厳密なCTRの代わりにこの指標で比較する。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import ConfigError
from ..csvlog import write_rows
from ..models import KIND_SINGLE, R_POSTED, Slot
from ..patterns import DIM_BAND, DIM_GENRE, DIM_JA, DIM_SUMMARY, DIM_TONE, Patterns
from ..post.detect import KIND_JA
from ..schedule.phase import week_of, week_range
from .clicks import ClickRow, ensure_templates, load_clicks

if TYPE_CHECKING:
    from ..app import App

DIM_PATTERN = "pattern"


@dataclass
class Bucket:
    posts: int = 0
    clicks: int = 0
    conversions: int = 0

    @property
    def cpp(self) -> float | None:
        return self.clicks / self.posts if self.posts else None

    @property
    def cvr(self) -> float | None:
        return self.conversions / self.clicks if self.clicks else None


def fmt_num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def slope(points: list[tuple[float, float]]) -> float | None:
    """最小二乗法による傾き。"""
    if len(points) < 2:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / den


class Dataset:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg
        self.patterns = Patterns(self.cfg, app.db)
        self.posts: list[Slot] = [s for s in app.db.all_slots(live_only=True) if s.result == R_POSTED]
        a = self.cfg.analytics
        self.by_id_path = app.settings.path(a.clicks_by_id_csv)
        self.by_item_path = app.settings.path(a.clicks_by_item_csv)
        ensure_templates(self.by_id_path, self.by_item_path)
        self.by_id: list[ClickRow] = load_clicks(self.by_id_path, "affiliate_id")
        self.by_item: list[ClickRow] = load_clicks(self.by_item_path, "content_id")
        try:
            self.tracking = app.settings.tracking_map()
        except ConfigError:
            self.tracking = {}

    def week_of_slot(self, s: Slot) -> int:
        return week_of(date.fromisoformat(s.ops_date), self.cfg)

    def week_of_day(self, d: date) -> int:
        return week_of(d, self.cfg)

    # ------------------------------------------------ スタイル(ID)別
    def style_buckets(self, weeks: set[int]) -> dict[str, Bucket]:
        buckets: dict[str, Bucket] = defaultdict(Bucket)
        for s in self.posts:
            if self.week_of_slot(s) in weeks:
                buckets[s.style].posts += 1
        for r in self.by_id:
            style = self.tracking.get(r.key)
            if style and self.week_of_day(r.day) in weeks:
                buckets[style].clicks += r.clicks
                buckets[style].conversions += r.conversions
        return buckets

    def unmapped_ids(self, weeks: set[int]) -> dict[str, int]:
        result: dict[str, int] = defaultdict(int)
        for r in self.by_id:
            if r.key not in self.tracking and self.week_of_day(r.day) in weeks:
                result[r.key] += r.clicks
        return dict(result)

    # ------------------------------------------- 商品別(ジャンル・時間帯など)
    @property
    def has_item_data(self) -> bool:
        return bool(self.by_item)

    def item_buckets(self, weeks: set[int]) -> tuple[dict[str, dict[str, Bucket]], int]:
        """個別投稿だけを対象に、商品別クリックをその投稿のジャンル・時間帯・トーンに割り当てる。"""
        dims = (DIM_GENRE, DIM_BAND, DIM_TONE, DIM_PATTERN)
        buckets: dict[str, dict[str, Bucket]] = {d: defaultdict(Bucket) for d in dims}
        singles = [s for s in self.posts if s.kind == KIND_SINGLE]
        by_cid: dict[str, Slot] = {}
        for s in singles:
            by_cid[s.content_ids[0]] = s
            if self.week_of_slot(s) in weeks:
                for dim, code in self._codes(s):
                    buckets[dim][code].posts += 1
        unattributed = 0
        for r in self.by_item:
            if self.week_of_day(r.day) not in weeks:
                continue
            s = by_cid.get(r.key)
            if s is None:
                unattributed += r.clicks
                continue
            for dim, code in self._codes(s):
                buckets[dim][code].clicks += r.clicks
                buckets[dim][code].conversions += r.conversions
        return buckets, unattributed

    @staticmethod
    def _codes(s: Slot) -> list[tuple[str, str]]:
        return [(DIM_GENRE, s.genre), (DIM_BAND, s.band), (DIM_TONE, s.style), (DIM_PATTERN, s.pattern_id)]

    # ------------------------------------------------------- 週次推移
    def weekly_series(self, upto: int) -> list[tuple[int, Bucket]]:
        series = {w: Bucket() for w in range(1, upto + 1)}
        for s in self.posts:
            w = self.week_of_slot(s)
            if w in series:
                series[w].posts += 1
        for r in self.by_id:
            w = self.week_of_day(r.day)
            if w in series and r.key in self.tracking:
                series[w].clicks += r.clicks
                series[w].conversions += r.conversions
        return sorted(series.items())


def default_week(app: "App") -> int:
    """最後に終わった週。まだ1週目の途中なら1。"""
    current = week_of(app.ops_date(), app.cfg)
    if current <= 0:
        raise ValueError("自動投稿はまだ始まっていません(慣らし期間中は週次集計の対象外です)")
    return max(1, current - 1)


@dataclass
class ExitJudgement:
    status: str
    message: str
    slope: float | None


def judge_exit(ds: Dataset, week: int) -> ExitJudgement:
    a = ds.cfg.analytics
    series = ds.weekly_series(week)
    points = [(float(w), b.cpp) for w, b in series if b.cpp is not None]
    s = slope(points)
    if week < a.exit_week:
        return ExitJudgement("判定前", f"第{a.exit_week}週の集計で判定します(現時点の傾き: {fmt_num(s, 3)})", s)
    if s is None:
        return ExitJudgement("判定不可", "クリックデータが不足しているため判定できません。clicks_by_id.csv を入力してください", s)
    if s <= a.exit_min_slope:
        return ExitJudgement(
            "停止を提案",
            f"第1〜{week}週の1投稿あたりクリック数に上昇傾向がありません(傾き {s:.3f})。撤退基準により運用の停止を提案します。",
            s,
        )
    return ExitJudgement("継続", f"1投稿あたりクリック数は上昇傾向です(傾き {s:.3f})", s)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def build_report(app: "App", week: int | None = None) -> tuple[int, str, list[list]]:
    from .review import find_candidates

    ds = Dataset(app)
    week = week or default_week(app)
    start, end = week_range(week, app.cfg)
    weeks = {week}
    pat = ds.patterns
    md: list[str] = [f"# 週次レポート 第{week}週({start.isoformat()}〜{end.isoformat()})", ""]
    csv_rows: list[list] = []
    md.append("クリック率は「1投稿あたりのクリック数」です(クリック数 ÷ リンク付きで投稿できた件数)。")
    md.append("")

    # 全体
    total = Bucket()
    for b in ds.style_buckets(weeks).values():
        total.posts += b.posts
        total.clicks += b.clicks
        total.conversions += b.conversions
    md += ["## 概要", ""]
    md += _table(
        ["投稿数", "クリック数", "成果数", "1投稿あたりクリック", "成約率"],
        [[str(total.posts), str(total.clicks), str(total.conversions), fmt_num(total.cpp), fmt_pct(total.cvr)]],
    )
    md.append("")

    # スタイル別(アフィリエイトID別)
    md += ["## 文面スタイル別(アフィリエイトID別のクリック数)", ""]
    styles = ds.style_buckets(weeks)
    rows = []
    for dim in (DIM_TONE, DIM_SUMMARY):
        for code in pat.codes(dim):
            b = styles.get(code, Bucket())
            try:
                af = app.settings.affiliate_id_for(code)
            except ConfigError:
                af = str(app.cfg.tracking.affiliate_ids.get(code, ""))
            state = "有効" if code in pat.enabled(dim) else "無効"
            rows.append([
                DIM_JA[dim], code, pat.name(dim, code), af, str(b.posts), str(b.clicks), str(b.conversions),
                fmt_num(b.cpp), fmt_pct(b.cvr), state,
            ])
            csv_rows.append([week, DIM_JA[dim], code, pat.name(dim, code), b.posts, b.clicks, b.conversions,
                             fmt_num(b.cpp, 4), fmt_pct(b.cvr)])
    md += _table(["区分", "コード", "名前", "アフィリエイトID", "投稿数", "クリック", "成果", "1投稿あたり", "成約率", "状態"], rows)
    unmapped = ds.unmapped_ids(weeks)
    if unmapped:
        md += ["", "設定にないアフィリエイトIDのクリック(集計対象外): "
               + ", ".join(f"{k}: {v}" for k, v in sorted(unmapped.items()))]
    if not ds.by_id:
        md += ["", f"※ {ds.by_id_path.name} にデータがありません。DMMのレポートからIDごとのクリック数を入力してください。"]
    md.append("")

    # 商品別データがある場合のジャンル・時間帯・パターン別
    md += ["## ジャンル別・時間帯別・パターン別(個別投稿のみ)", ""]
    if ds.has_item_data:
        buckets, unattributed = ds.item_buckets(weeks)
        for dim, title in ((DIM_GENRE, "ジャンル"), (DIM_BAND, "時間帯"), (DIM_TONE, "トーン(商品別データ)"),
                           (DIM_PATTERN, "パターンID")):
            md += [f"### {title}", ""]
            rows = []
            for code, b in sorted(buckets[dim].items(), key=lambda kv: -(kv[1].cpp or 0)):
                name = pat.name(dim, code) if dim != DIM_PATTERN else code
                rows.append([code, name, str(b.posts), str(b.clicks), str(b.conversions), fmt_num(b.cpp), fmt_pct(b.cvr)])
                csv_rows.append([week, title, code, name, b.posts, b.clicks, b.conversions, fmt_num(b.cpp, 4),
                                 fmt_pct(b.cvr)])
            md += _table(["コード", "名前", "投稿数", "クリック", "成果", "1投稿あたり", "成約率"], rows) if rows else ["(データなし)"]
            md.append("")
        if unattributed:
            md += [f"個別投稿に対応しない商品のクリック(まとめ投稿経由など): {unattributed}", ""]
    else:
        md += [f"{ds.by_item_path.name} にデータがないため、ジャンル別・時間帯別は集計していません。",
               "DMMのレポートで商品別のクリック数が分かる場合は入力してください。", ""]

    # 週次推移
    md += ["## 週次推移(全体)", ""]
    series = ds.weekly_series(week)
    md += _table(
        ["週", "投稿数", "クリック数", "成果数", "1投稿あたりクリック"],
        [[str(w), str(b.posts), str(b.clicks), str(b.conversions), fmt_num(b.cpp)] for w, b in series],
    )
    md.append("")

    # 撤退判定
    j = judge_exit(ds, week)
    md += ["## 撤退判定", "", f"**{j.status}**: {j.message}", ""]
    halts = [h for h in app.db.halts() if h["freeze"]]
    if halts:
        md.append("警告・制限を検知して停止した記録(撤退基準: 即停止):")
        for h in halts:
            resumed = f"、再開 {h['resumed_at']}" if h["resumed_at"] else "、停止中"
            md.append(f"- {h['at']} {KIND_JA.get(h['kind'], h['kind'])}: {h['reason']}{resumed}")
        md.append("")

    # 無効化候補
    candidates, notes = find_candidates(ds, week)
    md += ["## 無効化候補(`scripts\\fp.bat review` で承認すると無効化されます)", ""]
    if candidates:
        md += _table(
            ["区分", "コード", "名前", "投稿数", "1投稿あたり", "区分の平均"],
            [[DIM_JA[c.dim], c.code, c.name, str(c.posts), fmt_num(c.cpp), fmt_num(c.mean_cpp)] for c in candidates],
        )
    else:
        md.append("候補はありません。")
    for n in notes:
        md.append(f"- {n}")
    md.append("")

    disabled = pat.disabled()
    if disabled:
        md += ["## 現在無効にしているパターン", ""]
        md += [f"- {DIM_JA.get(d, d)} {c}({pat.name(d, c)}) {note} {at}" for d, c, note, at in disabled]
        md.append("")
    return week, "\n".join(md), csv_rows


def write_report(app: "App", week: int | None = None) -> tuple[Path, Path, str]:
    week, md, rows = build_report(app, week)
    out = app.settings.report_dir
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"week_{week:02d}.md"
    csv_path = out / f"week_{week:02d}.csv"
    md_path.write_text(md + "\n", encoding="utf-8")
    write_rows(csv_path, ["週", "区分", "コード", "名前", "投稿数", "クリック数", "成果数", "1投稿あたりクリック", "成約率"], rows)
    return md_path, csv_path, md
