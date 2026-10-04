from __future__ import annotations

from types import SimpleNamespace

import pytest

from fanza_poster.cli import main
from fanza_poster.generate.validate import Facts
from fanza_poster.generate.writer import CopyWriter, GenerationError

from .conftest import ROOT, make_settings

GOOD = ("山田花子さん出演のドラマ作品がセール中です。通常1,980円のところ495円、75%OFFで配信されています。"
        "発売日は2026年9月1日。セールは10月31日 23:59までです。")


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        stop, text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def _facts() -> Facts:
    return Facts(
        content_id="abc", title="穏やかな休日の物語", genre_names=["ドラマ"], actresses=["山田花子"], maker="メーカーA",
        series="", price=495, list_price=1980, discount_rate=75, price_from=False, release_date="2026-09-01",
        sale_end="2026-10-31 23:59", campaign_title="",
    )


def _writer(tmp_path, replies):
    cfg = make_settings(tmp_path).config.generation
    messages = FakeMessages(replies)
    w = CopyWriter(cfg, cfg.ng_words, client=SimpleNamespace(messages=messages))
    return w, messages


def test_writer_retries_with_feedback_then_succeeds(tmp_path):
    w, messages = _writer(tmp_path, [("end_turn", GOOD.replace("75%", "80%")), ("end_turn", GOOD)])
    text = w.write_single(_facts(), "TA")
    assert text.endswith("#PR")
    assert len(messages.requests) == 2
    second = messages.requests[1]
    assert second["model"] == "claude-haiku-4-5"
    assert second["messages"][-1]["role"] == "user" and "割引率" in second["messages"][-1]["content"]


def test_writer_refusal_skips_item(tmp_path):
    w, _ = _writer(tmp_path, [("refusal", "")])
    with pytest.raises(GenerationError):
        w.write_single(_facts(), "TB")


def test_writer_gives_up_after_retries(tmp_path):
    w, messages = _writer(tmp_path, [("end_turn", "短い")] * 3)
    with pytest.raises(GenerationError):
        w.write_single(_facts(), "TC")
    assert len(messages.requests) == 3  # 1回 + 再生成2回


def test_summary_labels_validated(tmp_path):
    payload = '{"labels": [{"id": "abc", "label": "山田花子出演のドラマ"}, {"id": "zzz", "label": "巨乳"}]}'
    w, messages = _writer(tmp_path, [("end_turn", payload)])
    facts2 = Facts(**{**_facts().__dict__, "content_id": "zzz"})
    labels = w.summary_labels([_facts(), facts2], 12)
    assert labels == {"abc": "山田花子出演のドラマ"}  # NG語のラベルは捨てて代替ラベルに回す
    assert "output_config" in messages.requests[0]


# ------------------------------------------------------------------ CLI
@pytest.fixture
def project(tmp_path):
    text = (ROOT / "config.yaml").read_text(encoding="utf-8").replace("start_date: 2026-10-06", "start_date: 2026-01-01")
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    (tmp_path / ".env").write_text("DMM_API_ID=x\nDMM_AFFILIATE_ID=test-990\nANTHROPIC_API_KEY=sk-x\n", encoding="utf-8")
    return tmp_path


def test_cli_selfcheck_offline(project, capsys):
    code = main(["--config", str(project / "config.yaml"), "selfcheck", "--skip-api", "--skip-browser"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "test-991" in out and "[OK] .env" in out


def test_cli_selfcheck_reports_missing_env(project, capsys, monkeypatch):
    for key in ("DMM_API_ID", "DMM_AFFILIATE_ID", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (project / ".env").write_text("", encoding="utf-8")
    code = main(["--config", str(project / "config.yaml"), "selfcheck", "--skip-api", "--skip-browser"])
    out = capsys.readouterr().out
    assert code == 1 and "未設定" in out


def test_cli_status_and_review_list(project, capsys):
    assert main(["--config", str(project / "config.yaml"), "status"]) == 0
    assert main(["--config", str(project / "config.yaml"), "review", "--list"]) == 0
    out = capsys.readouterr().out
    assert "運用日" in out and "トーン" in out


def test_cli_rejects_anime_floor(project, capsys):
    cfg = project / "config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("floors: [videoa]", "floors: [videoa, anime]"), encoding="utf-8")
    assert main(["--config", str(cfg), "status"]) == 1
    assert "アニメ" in capsys.readouterr().err
