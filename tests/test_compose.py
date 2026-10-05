"""Compose 2.0:多模态素材解析 / 能力池 / 规划 Loop 测试(离线)。"""
import warnings

import pytest

from yominpost import capabilities, db, services
from yominpost.models import Channel, ChannelStatus, ComposeRequest

warnings.filterwarnings("ignore")


# --------------------------- 多模态素材解析 --------------------------- #
def test_parse_material_multimodal(monkeypatch):
    monkeypatch.setattr(services, "_fetch_page", lambda u: f"[标题] 测试页\n来自 {u} 的正文要点")
    mixed = ("这是我的想法,自动化真的省时间。 "
             "https://example.com/article "
             "https://cdn.example.com/poster.jpg "
             "https://cdn.example.com/demo.mp4")
    m = services.parse_material(mixed, notes="突出省时间")
    assert m["images"] == ["https://cdn.example.com/poster.jpg"]
    assert m["videos"] == ["https://cdn.example.com/demo.mp4"]
    assert m["pages"] == ["https://example.com/article"]
    assert "自动化真的省时间" in m["text"]
    assert "测试页" in m["combined"]
    assert "突出省时间" not in m["combined"]   # notes 是运营者指令,不得混进素材内容


def test_parse_material_plain_text():
    m = services.parse_material("纯文字素材,没有链接")
    assert m["images"] == [] and m["videos"] == [] and m["pages"] == []
    assert m["combined"] == "纯文字素材,没有链接"


# ------------------------------ 能力池 ------------------------------ #
def test_capabilities_pool_defaults():
    pool = capabilities.pool()
    ids = {c["id"] for c in pool}
    assert {"template", "claude_code", "codex", "svg_poster", "storyboard",
            "jimeng_image", "jimeng_video", "agy"} <= ids
    tmpl = next(c for c in pool if c["id"] == "template")
    assert tmpl["available"] is True and tmpl["enabled"] is True
    kinds = capabilities.usable_kinds()
    assert {"text", "image", "video"} <= kinds  # 内置三类始终可用


def test_capabilities_toggle_persists():
    capabilities.set_enabled("svg_poster", False)
    try:
        pool = {c["id"]: c for c in capabilities.pool()}
        assert pool["svg_poster"]["enabled"] is False
    finally:
        capabilities.set_enabled("svg_poster", True)


# ---------------------------- 规划 Loop ---------------------------- #
def _ws_channel(platform="x"):
    ws = services.seed_defaults()
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform=platform,
                 display_name=platform, status=ChannelStatus.connected)
    db.save_channel(ch)
    return ws, ch


def test_compose_loop_produces_plan_and_iterations():
    ws, ch = _ws_channel("x")
    r = services.compose(ws, ComposeRequest(
        source="我们实测:自动化发帖一周省 8 小时,互动提升 30%。",
        notes="分享真实数据", channel_ids=[ch.id], loop=True, max_iters=2))
    assert len(r["drafts"]) == 1
    d = r["drafts"][0]
    assert d["plan"]["format"]           # 规划存在
    assert d["plan"]["angle"]
    assert len(d["iterations"]) >= 1      # 至少一次评估
    assert d["content"]["hook"]
    assert "capabilities" in r


def test_compose_target_format_video_forced():
    ws, ch = _ws_channel("linkedin")     # 文本型渠道也可被强制成视频
    r = services.compose(ws, ComposeRequest(
        source="产品发布视频脚本素材", channel_ids=[ch.id],
        target_format="video", loop=False))
    d = r["drafts"][0]
    assert d["format"] == "short_video"
    assert d["video_srt"].endswith(".srt")   # 内置分镜能力被调度


def test_compose_video_falls_back_when_capability_disabled():
    ws, ch = _ws_channel("tiktok")
    capabilities.set_enabled("storyboard", False)
    capabilities.set_enabled("jimeng_video", False)
    try:
        r = services.compose(ws, ComposeRequest(
            source="视频素材", channel_ids=[ch.id], loop=False))
        d = r["drafts"][0]
        assert d["format"] != "short_video"   # 无视频能力 → 规划降级
        assert d["video_srt"] == ""
    finally:
        capabilities.set_enabled("storyboard", True)
        capabilities.set_enabled("jimeng_video", True)


def test_compose_uses_user_image_as_ext():
    ws, ch = _ws_channel("instagram")
    r = services.compose(ws, ComposeRequest(
        source="配图文案 https://cdn.example.com/pic.png",
        channel_ids=[ch.id], loop=False))
    d = r["drafts"][0]
    assert d["ext_image"] == "https://cdn.example.com/pic.png"
    assert d["visual_svg"] == ""             # 用户已给图,不再渲染海报


# ------------------------------ API ------------------------------ #
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def test_capabilities_api(client):
    pool = client.get("/api/capabilities").json()
    assert any(c["id"] == "template" for c in pool)
    r = client.post("/api/capabilities", json={"id": "agy", "enabled": False}).json()
    assert r["ok"] is True
    client.post("/api/capabilities", json={"id": "agy", "enabled": True})
