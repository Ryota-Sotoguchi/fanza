"""config.yaml と .env の読み込み・検証。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 対象にできるフロア(service=digital 固定)。アニメ・同人・漫画は対象外。
ALLOWED_FLOORS = {"videoa": "ビデオ", "videoc": "素人"}
BLOCKED_FLOORS = {
    "anime": "アニメ",
    "doujin": "同人",
    "digital_doujin": "同人",
    "comic": "漫画",
    "ebook": "電子書籍(漫画)",
}

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_AFFILIATE_ID = re.compile(r"^(?P<base>.+)-(?P<num>\d{3})$")


class ConfigError(Exception):
    """設定ファイルや .env の内容に問題がある。"""


def hhmm_to_minutes(value: str) -> int:
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"時刻は HH:MM 形式で指定してください: {value!r}")
    hours, minutes = int(m.group(1)), int(m.group(2))
    if minutes >= 60 or hours > 29:
        raise ValueError(f"時刻が範囲外です(00:00〜29:59): {value!r}")
    return hours * 60 + minutes


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectConfig(_Model):
    floors: list[str] = Field(default_factory=lambda: ["videoa"])
    min_discount_rate: int = Field(50, ge=1, le=100)
    sorts: list[str] = Field(default_factory=lambda: ["rank", "date"])
    hits_per_page: int = Field(100, ge=1, le=100)
    scan_pages: int = Field(5, ge=1, le=50)
    max_items: int = Field(100, ge=1)
    request_interval_sec: float = Field(1.0, ge=0)
    candidate_max_age_days: int = Field(2, ge=0)

    @field_validator("floors")
    @classmethod
    def _check_floors(cls, floors: list[str]) -> list[str]:
        if not floors:
            raise ValueError("collect.floors を1つ以上指定してください")
        for floor in floors:
            if floor in BLOCKED_FLOORS:
                raise ValueError(f"{BLOCKED_FLOORS[floor]}({floor})は対象外です")
            if floor not in ALLOWED_FLOORS:
                allowed = ", ".join(ALLOWED_FLOORS)
                raise ValueError(f"未対応のフロアです: {floor}(指定できるのは {allowed})")
        return floors

    @field_validator("sorts")
    @classmethod
    def _check_sorts(cls, sorts: list[str]) -> list[str]:
        valid = {"rank", "date", "price", "-price", "review", "match"}
        bad = [s for s in sorts if s not in valid]
        if bad or not sorts:
            raise ValueError(f"collect.sorts に使えるのは {sorted(valid)} です: {bad}")
        return sorts


class ExcludeConfig(_Model):
    genres: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    content_ids: list[str] = Field(default_factory=list)
    makers: list[str] = Field(default_factory=list)


class GenreCategory(_Model):
    code: str
    name: str
    genres: list[str] = Field(default_factory=list)
    title_keywords: list[str] = Field(default_factory=list)
    has_series: bool = False

    @property
    def is_catch_all(self) -> bool:
        return not (self.genres or self.title_keywords or self.has_series)


class Band(_Model):
    code: str
    start: str
    end: str
    weight: float = Field(1.0, gt=0)

    @property
    def start_min(self) -> int:
        return hhmm_to_minutes(self.start)

    @property
    def end_min(self) -> int:
        return hhmm_to_minutes(self.end)

    @model_validator(mode="after")
    def _check_range(self) -> "Band":
        if self.end_min <= self.start_min:
            raise ValueError(f"時間帯 {self.code} の end は start より後にしてください")
        return self


class ScheduleConfig(_Model):
    weekly_posts: list[int] = Field(default_factory=lambda: [6, 8, 12])
    burn_in_daily_posts: int | None = Field(None, ge=0)
    daily_hard_limit: int = Field(12, ge=0)
    ops_day_start_hour: int = Field(5, ge=0, le=11)
    bands: list[Band]
    min_gap_minutes: int = Field(30, ge=0)
    late_skip_minutes: int = Field(60, ge=1)
    max_posts_per_tick: int = Field(1, ge=1)

    @field_validator("weekly_posts")
    @classmethod
    def _check_weekly(cls, value: list[int]) -> list[int]:
        if not value or any(v < 0 for v in value):
            raise ValueError("schedule.weekly_posts は0以上の数の並びで指定してください")
        return value

    @model_validator(mode="after")
    def _check_bands(self) -> "ScheduleConfig":
        if not self.bands:
            raise ValueError("schedule.bands を1つ以上指定してください")
        codes = [b.code for b in self.bands]
        if len(set(codes)) != len(codes):
            raise ValueError("schedule.bands の code が重複しています")
        ordered = sorted(self.bands, key=lambda b: b.start_min)
        for a, b in zip(ordered, ordered[1:]):
            if b.start_min < a.end_min:
                raise ValueError(f"時間帯 {a.code} と {b.code} が重なっています")
        return self

    @property
    def window_start_min(self) -> int:
        return min(b.start_min for b in self.bands)

    @property
    def window_end_min(self) -> int:
        return max(b.end_min for b in self.bands)


class SummaryConfig(_Model):
    enabled: bool = True
    top_n: int = Field(5, ge=1, le=7)
    min_items: int = Field(3, ge=1)
    window: list[str] = Field(default_factory=lambda: ["21:00", "23:00"])
    weekly_weekday: int = Field(6, ge=0, le=6)
    label_max_chars: int = Field(12, ge=4, le=30)
    verify_max_checks: int = Field(15, ge=1)
    formats: dict[str, str] = Field(default_factory=lambda: {"SA": "ランキング形式", "SB": "価格一覧形式"})

    @field_validator("window")
    @classmethod
    def _check_window(cls, value: list[str]) -> list[str]:
        if len(value) != 2 or hhmm_to_minutes(value[1]) <= hhmm_to_minutes(value[0]):
            raise ValueError('summary.window は ["21:00", "23:00"] のように開始と終了を指定してください')
        return value

    @field_validator("formats")
    @classmethod
    def _check_formats(cls, value: dict[str, str]) -> dict[str, str]:
        from .generate.summary import SUMMARY_FORMATS

        if not value:
            raise ValueError("summary.formats を1つ以上指定してください")
        unknown = [k for k in value if k not in SUMMARY_FORMATS]
        if unknown:
            raise ValueError(f"未対応のまとめ形式です: {unknown}(対応: {list(SUMMARY_FORMATS)})")
        return value

    @property
    def window_min(self) -> tuple[int, int]:
        return hhmm_to_minutes(self.window[0]), hhmm_to_minutes(self.window[1])


class Tone(_Model):
    name: str
    instruction: str


class GenerationConfig(_Model):
    model: str = "claude-haiku-4-5"
    max_tokens: int = Field(600, ge=64)
    target_length: int = Field(100, ge=20)
    min_length: int = Field(70, ge=10)
    max_length: int = Field(120, ge=20, le=135)
    max_retries: int = Field(2, ge=0, le=5)
    pr_suffix: str = "\n#PR"
    tones: dict[str, Tone]
    ng_words: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "GenerationConfig":
        if len(self.tones) == 0:
            raise ValueError("generation.tones を1つ以上指定してください")
        if not self.min_length <= self.target_length <= self.max_length:
            raise ValueError("generation の文字数は min_length ≦ target_length ≦ max_length にしてください")
        if not self.pr_suffix.strip().endswith("#PR"):
            raise ValueError("generation.pr_suffix は「#PR」で終わる必要があります")
        return self


class TrackingConfig(_Model):
    affiliate_ids: dict[str, int | str] = Field(default_factory=dict)


class PostConfig(_Model):
    browser_channel: str = "msedge"
    profile_dir: str = "profile"
    x_handle: str = ""
    attach_image: bool = True
    reply_template: str = "{url}\n#PR"
    summary_reply_line: str = "{rank}位 {url}"
    summary_reply_footer: str = "#PR"
    reply_delay_sec: tuple[float, float] = (20.0, 60.0)
    action_delay_sec: tuple[float, float] = (1.0, 3.0)
    timeout_sec: int = Field(60, ge=10)
    screenshot_on_error: bool = True

    @field_validator("reply_template")
    @classmethod
    def _check_reply(cls, value: str) -> str:
        if "{url}" not in value:
            raise ValueError("post.reply_template には {url} を含めてください")
        return value

    @field_validator("summary_reply_line")
    @classmethod
    def _check_summary_line(cls, value: str) -> str:
        if "{url}" not in value:
            raise ValueError("post.summary_reply_line には {url} を含めてください")
        return value


class SafetyConfig(_Model):
    max_consecutive_failures: int = Field(3, ge=1)
    verify_before_post: bool = True
    max_replacements: int = Field(3, ge=0)


class PathsConfig(_Model):
    data_dir: str = "data"
    log_dir: str = "logs"
    output_dir: str = "output"
    report_dir: str = "reports"


class AnalyticsConfig(_Model):
    clicks_by_id_csv: str = "data/clicks_by_id.csv"
    clicks_by_item_csv: str = "data/clicks_by_item.csv"
    review_weeks: int = Field(2, ge=1)
    review_min_posts: int = Field(10, ge=1)
    review_threshold_ratio: float = Field(0.8, gt=0, le=1)
    exit_week: int = Field(8, ge=2)
    exit_min_slope: float = 0.0


class AppConfig(_Model):
    start_date: date
    burn_in_days: int = Field(14, ge=0)
    dry_run: bool = False
    timezone: str = "Asia/Tokyo"
    collect: CollectConfig = Field(default_factory=CollectConfig)
    exclude: ExcludeConfig = Field(default_factory=ExcludeConfig)
    genre_categories: list[GenreCategory]
    schedule: ScheduleConfig
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    generation: GenerationConfig
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    post: PostConfig = Field(default_factory=PostConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def _check_cross(self) -> "AppConfig":
        if not self.genre_categories:
            raise ValueError("genre_categories を1つ以上指定してください")
        codes = [g.code for g in self.genre_categories]
        if len(set(codes)) != len(codes):
            raise ValueError("genre_categories の code が重複しています")
        if not self.genre_categories[-1].is_catch_all:
            raise ValueError("genre_categories の最後は条件なしの「その他」にしてください")

        styles = list(self.generation.tones) + list(self.summary.formats)
        if len(set(styles)) != len(styles):
            raise ValueError("トーンとまとめ形式のキーが重複しています")
        missing = [s for s in styles if s not in self.tracking.affiliate_ids]
        if missing:
            raise ValueError(f"tracking.affiliate_ids に次のスタイルのIDがありません: {missing}")
        extra = [s for s in self.tracking.affiliate_ids if s not in styles]
        if extra:
            raise ValueError(f"tracking.affiliate_ids に未使用のスタイルがあります: {extra}")
        for style, value in self.tracking.affiliate_ids.items():
            _validate_tracking_value(style, value)
        values = [str(v) for v in self.tracking.affiliate_ids.values()]
        if len(set(values)) != len(values):
            raise ValueError("tracking.affiliate_ids のIDが重複しています(スタイルごとに別のIDにしてください)")

        sched = self.schedule
        w0, w1 = self.summary.window_min
        if w0 < sched.window_start_min or w1 > sched.window_end_min:
            raise ValueError("summary.window は schedule.bands の範囲内にしてください")
        return self


def _validate_tracking_value(style: str, value: int | str) -> None:
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        num = int(value)
        if not 990 <= num <= 999:
            raise ValueError(f"tracking.affiliate_ids.{style}: 番号は 990〜999 で指定してください({value})")
        return
    m = _AFFILIATE_ID.match(str(value))
    if not m or not 990 <= int(m.group("num")) <= 999:
        raise ValueError(f"tracking.affiliate_ids.{style}: APIで使えるのは末尾990〜999のIDです({value})")


@dataclass
class Secrets:
    dmm_api_id: str = ""
    dmm_affiliate_id: str = ""
    anthropic_api_key: str = ""

    def missing(self) -> list[str]:
        names = []
        if not self.dmm_api_id:
            names.append("DMM_API_ID")
        if not self.dmm_affiliate_id:
            names.append("DMM_AFFILIATE_ID")
        if not self.anthropic_api_key:
            names.append("ANTHROPIC_API_KEY")
        return names

    def placeholders(self) -> list[str]:
        """.env.example の雛形の値のまま書き換えていない項目。"""
        names = []
        if "xxxx" in self.dmm_api_id:
            names.append("DMM_API_ID")
        if self.dmm_affiliate_id.startswith("yourname-"):
            names.append("DMM_AFFILIATE_ID")
        if "xxxx" in self.anthropic_api_key:
            names.append("ANTHROPIC_API_KEY")
        return names


@dataclass
class Settings:
    config: AppConfig
    root: Path
    config_path: Path
    secrets: Secrets = field(default_factory=Secrets)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.config.timezone)

    def path(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root / p

    @property
    def data_dir(self) -> Path:
        return self.path(self.config.paths.data_dir)

    @property
    def log_dir(self) -> Path:
        return self.path(self.config.paths.log_dir)

    @property
    def output_dir(self) -> Path:
        return self.path(self.config.paths.output_dir)

    @property
    def report_dir(self) -> Path:
        return self.path(self.config.paths.report_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fanza.db"

    @property
    def profile_dir(self) -> Path:
        return self.path(self.config.post.profile_dir)

    # ---- アフィリエイトID ----
    def collect_affiliate_id(self) -> str:
        af = self.secrets.dmm_affiliate_id
        m = _AFFILIATE_ID.match(af)
        if not m or not 990 <= int(m.group("num")) <= 999:
            raise ConfigError(
                f"DMM_AFFILIATE_ID は末尾が990〜999のAPI用IDにしてください(現在: {af or '未設定'})"
            )
        return af

    def affiliate_id_for(self, style: str) -> str:
        value = self.config.tracking.affiliate_ids[style]
        if isinstance(value, int) or str(value).isdigit():
            base = _AFFILIATE_ID.match(self.collect_affiliate_id()).group("base")  # type: ignore[union-attr]
            return f"{base}-{int(value)}"
        return str(value)

    def tracking_map(self) -> dict[str, str]:
        """アフィリエイトID → スタイル(TA/TB/TC/SA/SB)"""
        return {self.affiliate_id_for(style): style for style in self.config.tracking.affiliate_ids}

    def tracking_warnings(self) -> list[str]:
        warnings = []
        try:
            collect_id = self.collect_affiliate_id()
            mapping = self.tracking_map()
        except ConfigError as e:
            return [str(e)]
        if collect_id in mapping:
            warnings.append(
                f"収集用ID {collect_id} がスタイル {mapping[collect_id]} の計測用IDと同じです。別の番号にしてください。"
            )
        return warnings


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    cwd = Path.cwd() / "config.yaml"
    if cwd.exists():
        return cwd.resolve()
    # src/fanza_poster/config.py から見たプロジェクトルート
    return (Path(__file__).resolve().parents[2] / "config.yaml").resolve()


def load_secrets(root: Path) -> Secrets:
    env_path = root / ".env"
    values: dict[str, str | None] = {}
    if env_path.exists():
        values.update(dotenv_values(env_path, encoding="utf-8-sig"))
    for key in ("DMM_API_ID", "DMM_AFFILIATE_ID", "ANTHROPIC_API_KEY"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return Secrets(
        dmm_api_id=(values.get("DMM_API_ID") or "").strip(),
        dmm_affiliate_id=(values.get("DMM_AFFILIATE_ID") or "").strip(),
        anthropic_api_key=(values.get("ANTHROPIC_API_KEY") or "").strip(),
    )


def parse_config(data: dict) -> AppConfig:
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            lines.append(f"  - {loc or '(全体)'}: {err['msg']}")
        raise ConfigError("config.yaml の内容に問題があります:\n" + "\n".join(lines)) from None


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = find_config(str(config_path) if config_path else None)
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    with path.open(encoding="utf-8-sig") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("config.yaml の形式が正しくありません")
    config = parse_config(data)
    root = path.parent
    return Settings(config=config, root=root, config_path=path, secrets=load_secrets(root))
