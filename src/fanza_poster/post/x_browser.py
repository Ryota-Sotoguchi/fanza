"""Playwright による X への投稿。

- ログイン状態を保存したブラウザプロファイル(永続コンテキスト)を headed で使う。
- ログインは自動化しない(login.bat で手動ログインしてもらう)。
- ログイン切れ・CAPTCHA・本人確認・警告/制限を検知したら、突破を試みず DetectionError を出す。
- 操作するのは「投稿」と「自分の投稿へのリプライ」だけ。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PWTimeout

from ..config import PostConfig
from ..textutil import squash_ws
from .base import DetectionError, PostFailed, ReplyFailed
from .detect import Detection, classify

log = logging.getLogger(__name__)

BASE_URL = "https://x.com"
HOME_URL = BASE_URL + "/home"
LOGIN_URL = BASE_URL + "/i/flow/login"
COMPOSE_URL = BASE_URL + "/compose/post"

SEL_TEXTAREA = '[data-testid="tweetTextarea_0"]'
SEL_FILE_INPUT = 'input[data-testid="fileInput"]'
SEL_POST_BUTTON = '[data-testid="tweetButton"]'
SEL_INLINE_BUTTON = '[data-testid="tweetButtonInline"]'
SEL_TOAST = '[data-testid="toast"]'
SEL_LOGGED_IN = '[data-testid="SideNav_NewTweet_Button"], a[data-testid="AppTabBar_Profile_Link"]'
SEL_PROFILE_LINK = 'a[data-testid="AppTabBar_Profile_Link"]'
SEL_ATTACHMENTS = '[data-testid="attachments"]'
SEL_ARTICLE = 'article[data-testid="tweet"]'
SEL_NOTICES = [
    '[role="alert"]',
    '[data-testid="toast"]',
    '[role="dialog"]',
    '[data-testid="sheetDialog"]',
    '[data-testid="confirmationSheetDialog"]',
]
# 画面全体の文言を確認するページ(タイムライン上の他人の投稿で誤検知しないよう限定する)
SPECIAL_PAGES = ("/account/", "/i/flow/", "/login", "/logout", "challenge")


class XBrowserPoster:
    def __init__(
        self,
        cfg: PostConfig,
        profile_dir: Path,
        screenshot_dir: Path,
        rng: random.Random | None = None,
        sleeper=time.sleep,
    ):
        self.cfg = cfg
        self.profile_dir = profile_dir
        self.screenshot_dir = screenshot_dir
        self.rng = rng or random.Random()
        self.sleep = sleeper
        self.handle = ""
        self._pw = None
        self._ctx = None
        self.page = None

    # ------------------------------------------------------------ ブラウザ
    def __enter__(self) -> "XBrowserPoster":
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs: dict = {
            "user_data_dir": str(self.profile_dir),
            "headless": False,
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "viewport": {"width": 1280, "height": 900},
        }
        channel = (self.cfg.browser_channel or "").strip()
        if channel and channel != "chromium":
            kwargs["channel"] = channel
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception:
            self._pw.stop()
            raise
        self._ctx.set_default_timeout(self.cfg.timeout_sec * 1000)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        finally:
            if self._pw is not None:
                self._pw.stop()

    # ------------------------------------------------------------ 補助
    def _pause(self, lo: float | None = None, hi: float | None = None) -> None:
        a, b = self.cfg.action_delay_sec
        self.sleep(self.rng.uniform(lo if lo is not None else a, hi if hi is not None else b))

    def _abs(self, href: str) -> str:
        if href.startswith("http"):
            return href
        return BASE_URL + href

    def _shot(self, name: str) -> None:
        if not self.cfg.screenshot_on_error or self.page is None:
            return
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"
            self.page.screenshot(path=str(path), full_page=False)
            log.info("画面を保存しました: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("スクリーンショットの保存に失敗しました")

    def _scan(self, own_text: str = "") -> Detection | None:
        page = self.page
        url = page.url
        texts: list[str] = []
        for sel in SEL_NOTICES:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    try:
                        texts.append(loc.nth(i).inner_text(timeout=1000))
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass
        if any(s in url.lower() for s in SPECIAL_PAGES):
            try:
                texts.append(page.locator("body").inner_text(timeout=3000))
            except Exception:  # noqa: BLE001
                pass
        frames = [f.url for f in page.frames]
        return classify(url, texts, frames, own_text)

    def _guard(self, stage: str, own_text: str = "", submitted: bool = False) -> Detection | None:
        d = self._scan(own_text)
        if d is not None and d.halt:
            self._shot(d.kind)
            raise DetectionError(d.kind, f"{stage}: {d.detail}", submitted)
        return d

    def _text_matches(self, locator, text: str) -> bool:
        try:
            actual = locator.inner_text(timeout=3000)
        except Exception:  # noqa: BLE001
            return False
        return squash_ws(actual).replace("​", "") == squash_ws(text)

    def _input_lines(self, text: str, typing: bool) -> None:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                if typing:
                    self.page.keyboard.type(line, delay=self.rng.randint(20, 60))
                else:
                    self.page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                self.page.keyboard.press("Enter")
            self._pause(0.1, 0.4)

    def _type_into(self, locator, text: str) -> None:
        locator.click()
        self._pause(0.3, 0.8)
        self._input_lines(text, typing=False)
        if not self._text_matches(locator, text):
            locator.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
            self._pause(0.3, 0.6)
            self._input_lines(text, typing=True)
            if not self._text_matches(locator, text):
                self._shot("input_mismatch")
                raise PostFailed("本文を正しく入力できませんでした")
        # 末尾のハッシュタグ候補を閉じるために空白を1つ入れる(投稿時に末尾の空白は除かれる)
        self.page.keyboard.insert_text(" ")

    def _wait_enabled(self, locator, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                if (
                    locator.is_visible()
                    and locator.is_enabled()
                    and locator.get_attribute("aria-disabled") != "true"
                ):
                    return True
            except Exception:  # noqa: BLE001
                pass
            self.sleep(0.5)
        return False

    def _toast_status_url(self) -> str:
        try:
            link = self.page.locator(SEL_TOAST).locator('a[href*="/status/"]')
            if link.count() == 0:
                return ""
            return self._abs(link.first.get_attribute("href") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------ 操作
    def open_for_login(self) -> None:
        """手動ログイン用にログイン画面を開く(入力は人が行う)。"""
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")

    def check_session(self) -> str:
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        self._pause()
        self._guard("ホーム画面")
        try:
            self.page.locator(SEL_LOGGED_IN).first.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            self._guard("ホーム画面")
            self._shot("logged_out")
            raise DetectionError("logged_out", "ログイン状態を確認できません(login.bat でログインし直してください)")
        href = self.page.locator(SEL_PROFILE_LINK).first.get_attribute("href") or ""
        handle = href.strip("/").split("/")[0]
        expected = self.cfg.x_handle.strip().lstrip("@")
        if expected and handle.lower() != expected.lower():
            raise DetectionError("wrong_account", f"ログイン中のアカウント @{handle} が設定 @{expected} と違います")
        self.handle = handle
        return handle

    def post(self, text: str, image_paths: list[Path]) -> str:
        if not self.handle:
            self.check_session()
        page = self.page
        page.goto(COMPOSE_URL, wait_until="domcontentloaded")
        self._pause()
        self._guard("投稿画面")
        box = page.locator(SEL_TEXTAREA).first
        try:
            box.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            self._guard("投稿画面")
            self._shot("compose")
            raise PostFailed("投稿欄が表示されませんでした")
        self._type_into(box, text)

        if image_paths:
            page.locator(SEL_FILE_INPUT).first.set_input_files([str(p) for p in image_paths])
            try:
                page.locator(SEL_ATTACHMENTS).first.wait_for(state="visible", timeout=30000)
            except PWTimeout:
                self._guard("画像添付", text)
                self._shot("attach")
                raise PostFailed("画像を添付できませんでした")

        button = page.locator(SEL_POST_BUTTON).first
        if not self._wait_enabled(button, 90):
            self._guard("投稿直前", text)
            self._shot("post_button")
            raise PostFailed("投稿ボタンが押せる状態になりませんでした")
        self._pause()
        self._guard("投稿直前", text)
        button.click()
        return self._await_post_result(text)

    def _await_post_result(self, text: str) -> str:
        page = self.page
        deadline = time.monotonic() + 30
        closed_at: float | None = None
        while time.monotonic() < deadline:
            self.sleep(0.7)
            d = self._scan(own_text=text)
            if d is not None and d.halt:
                self._shot(d.kind)
                raise DetectionError(d.kind, f"投稿直後: {d.detail}", submitted=True)
            if d is not None and d.kind == "post_error":
                self._shot("post_error")
                raise PostFailed(f"投稿エラー: {d.detail}", submitted=True)
            url = self._toast_status_url()
            if url:
                return url
            if "/compose/" not in page.url:
                closed_at = closed_at or time.monotonic()
                if time.monotonic() - closed_at > 4:
                    break
        url = self._find_own_post_url(text)
        if url:
            return url
        self._shot("post_unknown")
        raise PostFailed("投稿したかどうか確認できませんでした(プロフィールで確認してください)", submitted=True)

    def _find_own_post_url(self, text: str) -> str:
        page = self.page
        page.goto(f"{BASE_URL}/{self.handle}", wait_until="domcontentloaded")
        self._pause()
        self._guard("プロフィール", submitted=True)
        snippet = squash_ws(text)[:20]
        try:
            page.locator(SEL_ARTICLE).first.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            return ""
        articles = page.locator(SEL_ARTICLE)
        for i in range(min(articles.count(), 6)):
            art = articles.nth(i)
            try:
                body = squash_ws(art.inner_text(timeout=2000))
            except Exception:  # noqa: BLE001
                continue
            if snippet and snippet in body:
                href = art.locator("a:has(time)").first.get_attribute("href") or ""
                if "/status/" in href:
                    return self._abs(href)
        return ""

    def _assert_own(self, status_url: str) -> None:
        parts = urlsplit(status_url).path.strip("/").split("/")
        if len(parts) < 3 or parts[1] != "status" or parts[0].lower() != self.handle.lower():
            raise ReplyFailed(f"自分の投稿以外にはリプライしません: {status_url}")

    def reply_to_own(self, status_url: str, text: str) -> str:
        if not self.handle:
            self.check_session()
        self._assert_own(status_url)
        page = self.page
        page.goto(status_url, wait_until="domcontentloaded")
        self._pause()
        self._guard("自分の投稿", submitted=True)
        box = page.locator(SEL_TEXTAREA).first
        try:
            box.wait_for(state="visible", timeout=20000)
        except PWTimeout:
            self._guard("自分の投稿", submitted=True)
            self._shot("reply_box")
            raise ReplyFailed("返信欄が表示されませんでした")
        try:
            self._type_into(box, text)
        except PostFailed as e:
            raise ReplyFailed(e.detail) from e
        button = page.locator(SEL_INLINE_BUTTON).first
        if not self._wait_enabled(button, 30):
            self._guard("返信直前", text, submitted=True)
            self._shot("reply_button")
            raise ReplyFailed("返信ボタンが押せる状態になりませんでした")
        self._pause()
        self._guard("返信直前", text, submitted=True)
        button.click()

        deadline = time.monotonic() + 20
        cleared_at: float | None = None
        while time.monotonic() < deadline:
            self.sleep(0.7)
            d = self._scan(own_text=text)
            if d is not None and d.halt:
                self._shot(d.kind)
                raise DetectionError(d.kind, f"返信直後: {d.detail}", submitted=True)
            if d is not None and d.kind == "post_error":
                self._shot("reply_error")
                raise ReplyFailed(f"返信エラー: {d.detail}")
            url = self._toast_status_url()
            if url:
                return url
            try:
                empty = squash_ws(box.inner_text(timeout=1000)).replace("​", "") == ""
            except Exception:  # noqa: BLE001
                empty = False
            if empty:
                cleared_at = cleared_at or time.monotonic()
                if time.monotonic() - cleared_at > 3:
                    return ""
        self._shot("reply_unknown")
        raise ReplyFailed("返信できたか確認できませんでした")
