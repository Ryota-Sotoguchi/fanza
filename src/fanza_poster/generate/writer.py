"""Anthropic API による紹介文・まとめ投稿用ラベルの生成。"""

from __future__ import annotations

import json
import logging
import re

import anthropic

from ..config import GenerationConfig
from .validate import Facts, date_ja, datetime_ja, validate_label, validate_single, yen

log = logging.getLogger(__name__)


class GenerationError(Exception):
    """文面を生成できなかった(その商品はスキップする)。"""


class GenerationAPIError(Exception):
    """API自体の問題(認証・通信など)。商品は消費せず、次回の起動でやり直す。"""


SYSTEM_PROMPT = """\
あなたは、成人向け動画配信サービスのセール情報を、X(旧Twitter)で短く告知する担当者です。
以下のルールを必ず守り、告知文の本文だけを出力してください。

- 日本語で{target}字前後({min}〜{max}字)。改行は2回まで。
- 書いてよい事実は「商品データ」に書かれている内容だけです。価格・割引率・日付は商品データの値を算用数字でそのまま使ってください。計算した数字(差額など)や、データにない情報(評価、売上、ランキング順位、内容の詳細など)は書かないでください。
- 性的に露骨な表現、身体や行為の描写、扇情的な言い回しは使わないでください。
- 未成年を連想させる表現(学生・制服・幼さなど)は使わないでください。
- 作品タイトルはそのまま引用せず、作品の方向性が穏当に伝わる言い換えにしてください。
- URL、ハッシュタグ、絵文字、顔文字は入れないでください(「#PR」は後で付けます)。
- 「今だけ」「絶対」「必見」「急げ」など、事実で裏付けられない煽り表現は使わないでください。セール終了日時が商品データにある場合だけ、期限に触れてかまいません。
- 前置き・説明・カギ括弧での囲みは不要です。本文だけを出力してください。

文体: {tone}"""

LABEL_PROMPT = """\
成人向け動画配信サービスのセール作品を一覧で紹介するために、各作品の短いラベルを作ってください。

- 各ラベルは{max}字以内の日本語。出演者名や、作品の方向性を穏当に表す言葉にする。
- 性的に露骨な表現、身体や行為の描写、未成年を連想させる表現は使わない。
- 数字・URL・ハッシュタグ・絵文字は入れない。
- 作品タイトルをそのまま引用しない。
- すべての作品について、与えられた id ごとに1つずつ返す。

作品一覧:
{items}"""

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "label": {"type": "string"}},
                "required": ["id", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


def render_facts(f: Facts) -> str:
    lines = [
        f"- 作品タイトル(参考用・引用しない): {f.title}",
        f"- ジャンル: {'、'.join(f.genre_names) or '記載なし'}",
        f"- 出演: {'、'.join(f.actresses[:3]) or '記載なし'}",
        f"- メーカー: {f.maker or '記載なし'}",
        f"- 発売日: {date_ja(f.release_date) or '記載なし'}",
        f"- セール価格: {yen(f.price, f.price_from)}",
        f"- 通常価格: {yen(f.list_price)}",
        f"- 割引率: {f.discount_rate}%OFF",
        f"- セール終了: {datetime_ja(f.sale_end) or '記載なし'}",
    ]
    return "商品データ:\n" + "\n".join(lines)


def _text_of(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


class CopyWriter:
    def __init__(self, cfg: GenerationConfig, ng_words: list[str], api_key: str = "", client=None):
        self.cfg = cfg
        self.ng_words = ng_words
        if client is not None:
            self.client = client
        elif api_key:
            self.client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        else:
            self.client = anthropic.Anthropic(max_retries=3, timeout=60.0)

    # ------------------------------------------------------------------ API
    def _create(self, **kwargs):
        try:
            return self.client.messages.create(model=self.cfg.model, max_tokens=self.cfg.max_tokens, **kwargs)
        except anthropic.BadRequestError as e:
            raise GenerationAPIError(f"リクエストが不正です: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise GenerationAPIError("ANTHROPIC_API_KEY が正しくありません") from e
        except anthropic.PermissionDeniedError as e:
            raise GenerationAPIError(f"APIの権限がありません: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise GenerationAPIError(f"モデル {self.cfg.model} が見つかりません") from e
        except anthropic.RateLimitError as e:
            raise GenerationAPIError("APIのレート制限に達しました") from e
        except anthropic.APIStatusError as e:
            raise GenerationAPIError(f"APIエラー({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise GenerationAPIError("Anthropic API に接続できません") from e

    def check_model(self) -> str:
        """selfcheck用: APIキーとモデル名を確認する(文章は生成しない)。"""
        try:
            model = self.client.models.retrieve(self.cfg.model)
        except anthropic.AuthenticationError as e:
            raise GenerationAPIError("ANTHROPIC_API_KEY が正しくありません") from e
        except anthropic.NotFoundError as e:
            raise GenerationAPIError(f"モデル {self.cfg.model} が見つかりません") from e
        except anthropic.APIStatusError as e:
            raise GenerationAPIError(f"APIエラー({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise GenerationAPIError("Anthropic API に接続できません") from e
        return getattr(model, "display_name", "") or model.id

    # ------------------------------------------------------- 個別投稿の紹介文
    def write_single(self, facts: Facts, tone_code: str) -> str:
        """紹介文を生成し、検証を通った本文に #PR を付けて返す。"""
        tone = self.cfg.tones[tone_code]
        system = SYSTEM_PROMPT.format(
            target=self.cfg.target_length, min=self.cfg.min_length, max=self.cfg.max_length, tone=tone.instruction
        )
        messages: list[dict] = [{"role": "user", "content": render_facts(facts)}]
        errors: list[str] = []
        for attempt in range(1 + self.cfg.max_retries):
            resp = self._create(system=system, messages=messages)
            if resp.stop_reason == "refusal":
                raise GenerationError("生成AIが応答を拒否しました")
            text = _text_of(resp)
            if resp.stop_reason == "max_tokens":
                errors = ["出力が長すぎます"]
            else:
                result = validate_single(
                    text,
                    facts,
                    min_length=self.cfg.min_length,
                    max_length=self.cfg.max_length,
                    ng_words=self.ng_words,
                    pr_suffix=self.cfg.pr_suffix,
                )
                if result.ok:
                    return result.final_text
                errors = result.errors
            log.info("紹介文が検証に通りませんでした(%s, %d回目): %s", facts.content_id, attempt + 1, " / ".join(errors))
            messages = messages + [
                {"role": "assistant", "content": text or "(空)"},
                {
                    "role": "user",
                    "content": "次の理由で使えません。ルールを守って本文を書き直してください。\n"
                    + "\n".join(f"- {e}" for e in errors),
                },
            ]
        raise GenerationError("検証に通る紹介文を生成できませんでした: " + " / ".join(errors))

    # -------------------------------------------------- まとめ投稿用ラベル
    def summary_labels(self, items: list[Facts], max_chars: int) -> dict[str, str]:
        """作品ごとの短いラベル。検証に通らなかった作品は含めない(呼び出し側で代替する)。"""
        if not items:
            return {}
        listing = "\n".join(
            json.dumps(
                {
                    "id": f.content_id,
                    "title": f.title,
                    "genres": f.genre_names[:6],
                    "actresses": f.actresses[:2],
                    "maker": f.maker,
                },
                ensure_ascii=False,
            )
            for f in items
        )
        prompt = LABEL_PROMPT.format(max=max_chars, items=listing)
        try:
            resp = self.client.messages.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": LABEL_SCHEMA}},
            )
        except anthropic.BadRequestError:
            # 構造化出力が使えない場合は、JSONで答えるよう指示して取り直す
            json_prompt = prompt + '\n\n出力は次の形式のJSONのみ: {"labels": [{"id": "...", "label": "..."}]}'
            resp = self._create(messages=[{"role": "user", "content": json_prompt}])
        except anthropic.APIError:
            resp = self._create(messages=[{"role": "user", "content": prompt}],
                                output_config={"format": {"type": "json_schema", "schema": LABEL_SCHEMA}})
        if resp.stop_reason == "refusal":
            log.warning("ラベル生成が拒否されました。代替ラベルを使います")
            return {}
        text = _text_of(resp)
        m = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            log.warning("ラベルのJSONを読めませんでした。代替ラベルを使います")
            return {}
        labels: dict[str, str] = {}
        valid_ids = {f.content_id for f in items}
        for entry in data.get("labels", []):
            cid, label = str(entry.get("id", "")), str(entry.get("label", ""))
            if cid in valid_ids:
                checked = validate_label(label, max_chars, self.ng_words)
                if checked:
                    labels[cid] = checked
        return labels
