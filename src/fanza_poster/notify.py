"""Windows のトースト通知。Windows 以外ではログに残すだけ。"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

APP_ID = "FANZA自動投稿ツール"


class Notifier:
    def notify(self, title: str, message: str) -> bool:
        log.warning("[通知] %s: %s", title, message)
        if sys.platform != "win32":
            return False
        try:
            from winotify import Notification, audio

            toast = Notification(app_id=APP_ID, title=title, msg=message[:250], duration="long")
            toast.set_audio(audio.Default, loop=False)
            toast.show()
            return True
        except Exception:  # noqa: BLE001 - 通知の失敗で処理を止めない
            log.exception("Windows通知の表示に失敗しました")
            return False
