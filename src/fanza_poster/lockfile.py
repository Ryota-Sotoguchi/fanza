"""多重起動の防止(タスクスケジューラの起動と手動実行が重ならないように)。"""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockBusy(Exception):
    pass


class RunLock:
    def __init__(self, path: Path, stale_minutes: int = 45):
        self.path = path
        self.stale_seconds = stale_minutes * 60
        self._held = False

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_seconds:
                    # 異常終了で残ったロックは消して取り直す
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockBusy(f"別の処理が実行中です({self.path})") from None
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._held = True
            return self
        raise LockBusy(f"ロックを取得できませんでした({self.path})")

    def __exit__(self, *exc) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False
