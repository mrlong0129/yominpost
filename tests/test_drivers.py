"""AI 驱动(Claude Code / Codex)接线与回退安全测试。

不真实调用 CLI —— 打桩 _invoke 验证:成功解析走 CLI 结果;失败/非创意任务回退模板。
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from yominpost.providers.cli_provider import ClaudeCodeProvider, CodexProvider
from yominpost.providers.template_provider import TemplateProvider

warnings.filterwarnings("ignore")


def _ctx():
    return {
        "brand": {"brand_name": "YominPost", "vocabulary": ["增长"], "audience": ["创业者"],
                  "sample_hooks": ["hook"], "taboos": []},
        "topic": {"title": "测试选题", "angle": "清单式"},
        "format": "text", "platform": "x", "critique": None,
    }


def test_cli_provider_uses_cli_result_on_success(monkeypatch):
    prov = ClaudeCodeProvider(model="haiku")
    monkeypatch.setattr(prov, "_invoke",
                        lambda p: '{"hook":"真实钩子","body":"真实正文","cta":"关注","hashtags":["#x"]}')
    out = prov.run("copy", _ctx())
    assert out["hook"] == "真实钩子"
    assert out["char_count"] > 0  # 自动补齐


def test_cli_provider_falls_back_on_error(monkeypatch):
    prov = ClaudeCodeProvider()

    def boom(prompt):
        raise RuntimeError("cli missing")

    monkeypatch.setattr(prov, "_invoke", boom)
    out = prov.run("copy", _ctx())
    # 回退到模板,依然拿到结构合法的结果
    assert "hook" in out and out["hook"]


def test_cli_provider_delegates_non_cli_tasks(monkeypatch):
    prov = CodexProvider()

    def should_not_call(prompt):
        raise AssertionError("brand_dna 不应走 CLI")

    monkeypatch.setattr(prov, "_invoke", should_not_call)
    out = prov.run("brand_dna", {"brand_name": "X", "sources": []})  # 非创意任务 → 模板
    assert out["brand_name"] == "X"


def test_provider_name_reported():
    assert ClaudeCodeProvider().name == "claude_code"
    assert CodexProvider().name == "codex"
    assert TemplateProvider().name == "template"


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def test_settings_endpoints(client):
    s = client.get("/api/settings").json()
    assert "provider" in s and "available" in s
    assert s["available"]["template"] is True
    # 切到 template 保证可测试且即时
    r = client.post("/api/settings", json={"provider": "template"}).json()
    assert r["provider"] == "template"


def test_settings_test_driver_template(client):
    client.post("/api/settings", json={"provider": "template"})
    r = client.post("/api/settings/test").json()
    assert r["driver"] == "template"
    assert r["hook"]
