"""ログイン切れ・CAPTCHA・本人確認・警告/制限の検知(画面のURLと表示文言から判定)。

検知した場合は突破を試みず、全処理を停止する。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..textutil import norm

# 停止 + 件数の引き上げ凍結(警告・制限とみなすもの)
FREEZE_KINDS = {
    "captcha",
    "verification",
    "locked",
    "suspended",
    "restricted",
    "rate_limited",
    "automation_warning",
}
# 停止のみ
HALT_ONLY_KINDS = {"logged_out", "wrong_account"}

KIND_JA = {
    "captcha": "CAPTCHA",
    "verification": "本人確認",
    "locked": "アカウントのロック",
    "suspended": "アカウントの凍結",
    "restricted": "機能の制限",
    "rate_limited": "投稿数の上限・レート制限",
    "automation_warning": "自動化・スパムの警告",
    "logged_out": "ログイン切れ",
    "wrong_account": "想定と違うアカウント",
    "consecutive_failures": "連続失敗",
    "post_error": "投稿エラー",
}

# URLに含まれていたら判定するもの(順に評価)
URL_RULES: list[tuple[str, str]] = [
    ("arkose", "captcha"),
    ("captcha", "captcha"),
    ("/account/access", "locked"),
    ("login_challenge", "verification"),
    ("login_verification", "verification"),
    ("/account/verify", "verification"),
    ("/i/flow/login", "logged_out"),
    ("/i/flow/signup", "logged_out"),
    ("x.com/login", "logged_out"),
    ("twitter.com/login", "logged_out"),
    ("/logout", "logged_out"),
]

# 画面の文言(アラート・ダイアログ・トーストなど)で判定するもの(順に評価)
TEXT_RULES: list[tuple[str, list[str]]] = [
    ("captcha", ["captcha", "ロボットではない", "人間であることを確認", "認証パズル", "verify you are human",
                 "are you a robot", "prove you're human", "パズルを解"]),
    ("suspended", ["凍結されています", "アカウントは凍結", "account is suspended", "account has been suspended",
                   "your account is suspended"]),
    ("locked", ["ロックされています", "アカウントがロック", "account has been locked", "account is locked",
                "一時的にロック"]),
    ("verification", ["本人確認", "verify your identity", "confirm your identity", "確認コード", "verification code",
                      "電話番号を確認", "メールアドレスを確認", "confirm your phone", "confirm your email",
                      "アカウントを確認"]),
    ("automation_warning", ["自動化されて", "自動化された", "automated", "スパム", "spam", "不審なアクティビティ",
                            "不審な動作", "unusual activity", "suspicious activity", "suspicious"]),
    ("rate_limited", ["上限に達し", "制限を超え", "daily limit", "over the daily", "rate limit", "too many requests",
                      "limit exceeded"]),
    ("restricted", ["制限されています", "一時的に制限", "機能が制限", "利用制限", "temporarily restricted",
                    "has been restricted", "is restricted", "limited some of your account"]),
    ("post_error", ["問題が発生しました", "エラーが発生", "something went wrong", "try again", "もう一度お試し",
                    "送信できませんでした", "ポストできませんでした", "could not be sent", "already said that",
                    "同じ内容", "重複"]),
]


@dataclass
class Detection:
    kind: str
    detail: str

    @property
    def halt(self) -> bool:
        return self.kind in FREEZE_KINDS or self.kind in HALT_ONLY_KINDS

    @property
    def freeze(self) -> bool:
        return self.kind in FREEZE_KINDS

    @property
    def label(self) -> str:
        return KIND_JA.get(self.kind, self.kind)


def classify(url: str, texts: list[str], frame_urls: list[str] | None = None, own_text: str = "") -> Detection | None:
    """画面の状態を判定する。own_text(自分が入力した本文)は判定から除く。"""
    for frame in frame_urls or []:
        f = norm(frame)
        if "arkoselabs" in f or "funcaptcha" in f or "captcha" in f:
            return Detection("captcha", f"CAPTCHAの埋め込みを検出: {frame[:120]}")
    u = norm(url)
    for pattern, kind in URL_RULES:
        if pattern in u:
            return Detection(kind, f"URL: {url}")
    own = norm(own_text)
    joined = "\n".join(norm(t).replace(own, " ") if own else norm(t) for t in texts if t)
    if not joined.strip():
        return None
    for kind, words in TEXT_RULES:
        for w in words:
            if norm(w) in joined:
                snippet = _snippet(joined, norm(w))
                return Detection(kind, f"表示文言「{snippet}」")
    return None


def _snippet(text: str, word: str, width: int = 40) -> str:
    i = text.find(word)
    start = max(0, i - width // 2)
    return text[start : i + len(word) + width // 2].replace("\n", " ").strip()
