"""投稿処理のインターフェース。

実装するのは「投稿」と「自分の投稿へのリプライ」だけ。
いいね・フォロー・引用・他人へのリプライは実装しない(このインターフェースにも持たせない)。
後から X API 版に差し替える場合も、このインターフェースを実装する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class DetectionError(Exception):
    """ログイン切れ・CAPTCHA・本人確認・警告/制限などを検知した。全処理を停止する。"""

    def __init__(self, kind: str, detail: str, submitted: bool = False):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.submitted = submitted


class PostFailed(Exception):
    """投稿に失敗した。submitted=True は投稿ボタンを押した後(投稿された可能性がある)。"""

    def __init__(self, detail: str, submitted: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.submitted = submitted


class ReplyFailed(Exception):
    """本文の投稿後、リンクのリプライに失敗した。"""


class Poster(Protocol):
    def __enter__(self) -> "Poster": ...

    def __exit__(self, *exc) -> None: ...

    def check_session(self) -> str:
        """ログイン状態と警告の有無を確認し、アカウント名を返す。問題があれば DetectionError。"""
        ...

    def post(self, text: str, image_paths: list[Path]) -> str:
        """本文(と画像)を投稿し、投稿のURLを返す。"""
        ...

    def reply_to_own(self, status_url: str, text: str) -> str:
        """自分の投稿 status_url にリプライする。返り値はリプライのURL(分からなければ空)。"""
        ...
