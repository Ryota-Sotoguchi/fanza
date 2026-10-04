"""実行時の共通オブジェクト(設定・DB・各クライアント)。テストでは各 factory を差し替える。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .config import AppConfig, ConfigError, Settings
from .db import Database
from .notify import Notifier
from .timeutil import ops_date_of


@dataclass
class App:
    settings: Settings
    db: Database
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], datetime] | None = None
    dmm_factory: Callable[[], object] | None = None
    writer_factory: Callable[[], object] | None = None
    poster_factory: Callable[[], object] | None = None
    image_fetcher: Callable[[str, Path], Path] | None = None
    notifier: Notifier = field(default_factory=Notifier)
    sleeper: Callable[[float], None] = time.sleep
    _dmm: object | None = None
    _writer: object | None = None

    @classmethod
    def create(cls, settings: Settings) -> "App":
        return cls(settings=settings, db=Database(settings.db_path))

    @property
    def cfg(self) -> AppConfig:
        return self.settings.config

    @property
    def tz(self):
        return self.settings.tz

    def now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(self.tz)

    def ops_date(self, dt: datetime | None = None) -> date:
        return ops_date_of(dt or self.now(), self.cfg.schedule.ops_day_start_hour)

    def ng_words(self) -> list[str]:
        """生成文のチェックに使う語(露骨な表現 + 除外キーワード)。"""
        return list(dict.fromkeys([*self.cfg.generation.ng_words, *self.cfg.exclude.keywords]))

    # ---------------------------------------------------------- clients
    def dmm(self):
        if self._dmm is None:
            if self.dmm_factory is not None:
                self._dmm = self.dmm_factory()
            else:
                from .collect.dmm_client import DmmClient

                s = self.settings.secrets
                if not s.dmm_api_id:
                    raise ConfigError(".env に DMM_API_ID がありません")
                self._dmm = DmmClient(
                    s.dmm_api_id,
                    self.settings.collect_affiliate_id(),
                    interval_sec=self.cfg.collect.request_interval_sec,
                )
        return self._dmm

    def writer(self):
        if self._writer is None:
            if self.writer_factory is not None:
                self._writer = self.writer_factory()
            else:
                from .generate.writer import CopyWriter

                self._writer = CopyWriter(
                    self.cfg.generation, self.ng_words(), api_key=self.settings.secrets.anthropic_api_key
                )
        return self._writer

    def new_poster(self):
        if self.poster_factory is not None:
            return self.poster_factory()
        from .post.x_browser import XBrowserPoster

        return XBrowserPoster(
            self.cfg.post,
            self.settings.profile_dir,
            self.settings.log_dir / "screenshots",
            rng=self.rng,
            sleeper=self.sleeper,
        )

    def notify(self, title: str, message: str) -> None:
        self.notifier.notify(title, message)

    def sleep(self, seconds: float) -> None:
        self.sleeper(seconds)
