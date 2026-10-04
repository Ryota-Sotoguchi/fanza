"""安全装置: 停止フラグ・連続失敗・件数引き上げの凍結。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .post.detect import KIND_JA

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger(__name__)

K_HALTED = "halted"
K_HALT_KIND = "halt_kind"
K_HALT_REASON = "halt_reason"
K_HALT_AT = "halt_at"
K_FAILURES = "consecutive_failures"
K_RAMP_FROZEN = "ramp_frozen_posts"
K_NOTIFIED_PREFIX = "notified:"

HALT_FILE = "HALTED.txt"


class Safety:
    def __init__(self, app: "App"):
        self.app = app
        self.db = app.db

    # ---------------------------------------------------------------- 停止
    def halted(self) -> dict | None:
        if self.db.get_state(K_HALTED) != "1":
            return None
        return {
            "kind": self.db.get_state(K_HALT_KIND, ""),
            "reason": self.db.get_state(K_HALT_REASON, ""),
            "at": self.db.get_state(K_HALT_AT, ""),
        }

    def halt(self, kind: str, reason: str, detail: str = "", freeze: bool = False, current_target: int | None = None) -> None:
        now = self.app.now().replace(microsecond=0).isoformat()
        label = KIND_JA.get(kind, kind)
        self.db.set_state(K_HALTED, "1")
        self.db.set_state(K_HALT_KIND, kind)
        self.db.set_state(K_HALT_REASON, reason)
        self.db.set_state(K_HALT_AT, now)
        if freeze and current_target is not None:
            frozen = self.ramp_frozen()
            value = current_target if frozen is None else min(frozen, current_target)
            self.db.set_state(K_RAMP_FROZEN, str(value))
        self.db.add_halt(kind, freeze, reason, detail)
        log.critical("停止しました [%s] %s %s", label, reason, detail)

        lines = [
            f"停止日時: {now}",
            f"種類: {label}",
            f"理由: {reason}",
            f"詳細: {detail}",
            "",
            "原因を確認・解消してから、次のコマンドで再開してください:",
            "  scripts\\fp.bat resume",
        ]
        if freeze:
            lines.insert(4, "警告・制限とみなしたため、1日の投稿件数の引き上げを停止しました。")
        path = self.app.settings.data_dir / HALT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.app.notify(
            f"自動投稿を停止しました({label})",
            f"{reason}。原因を確認し、fp.bat resume で再開してください。",
        )

    def resume(self) -> None:
        self.db.set_state(K_HALTED, None)
        self.db.set_state(K_HALT_KIND, None)
        self.db.set_state(K_HALT_REASON, None)
        self.db.set_state(K_HALT_AT, None)
        self.db.set_state(K_FAILURES, "0")
        self.db.mark_halts_resumed()
        path = self.app.settings.data_dir / HALT_FILE
        if path.exists():
            path.unlink()
        log.warning("停止を解除しました")

    # ------------------------------------------------------ 件数の引き上げ凍結
    def ramp_frozen(self) -> int | None:
        value = self.db.get_state(K_RAMP_FROZEN)
        return int(value) if value not in (None, "") else None

    def unfreeze_ramp(self) -> None:
        self.db.set_state(K_RAMP_FROZEN, None)

    # ------------------------------------------------------------ 連続失敗
    def consecutive_failures(self) -> int:
        return int(self.db.get_state(K_FAILURES, "0") or 0)

    def record_success(self) -> None:
        self.db.set_state(K_FAILURES, "0")

    def record_failure(self, reason: str) -> bool:
        """失敗を記録し、上限に達したら停止する。停止した場合 True。"""
        count = self.consecutive_failures() + 1
        self.db.set_state(K_FAILURES, str(count))
        limit = self.app.cfg.safety.max_consecutive_failures
        log.warning("投稿失敗 %d/%d 回連続: %s", count, limit, reason)
        if count >= limit:
            self.halt("consecutive_failures", f"投稿に{count}回連続で失敗しました", reason)
            return True
        return False

    # -------------------------------------------------------------- 通知
    def notify_once_per_day(self, key: str, title: str, message: str) -> None:
        """同じ種類のエラー通知は1日1回まで。"""
        today = self.app.now().date().isoformat()
        state_key = K_NOTIFIED_PREFIX + key
        if self.db.get_state(state_key) == today:
            return
        self.db.set_state(state_key, today)
        self.app.notify(title, message)


def halted_message(info: dict) -> str:
    label = KIND_JA.get(info.get("kind", ""), info.get("kind", ""))
    return f"停止中です({label}: {info.get('reason', '')} / {info.get('at', '')})。原因を確認して resume で再開してください。"
