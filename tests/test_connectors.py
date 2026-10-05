"""渠道连接器全链路(连接 → 校验 → 发布 → 断开)测试。"""
from yominpost.connectors import get_connector
from yominpost.models import Channel, ChannelStatus, PostContent


def test_connect_and_publish_lifecycle():
    conn = get_connector("tiktok")   # 模拟连接器全链路
    ch = Channel(id="ch1", workspace_id="ws1", platform="tiktok", display_name="DemoUser")
    ch = conn.connect(ch, {"handle": "DemoUser"})
    assert ch.status == ChannelStatus.connected
    assert ch.handle == "demouser"

    content = PostContent(hook="测试", body="内容", cta="关注")
    res = conn.publish(ch, content)
    assert res.ok
    assert res.sandbox and res.permalink == ""  # 模拟渠道不给假链接
    assert res.external_id

    ch = conn.disconnect(ch)
    assert ch.status == ChannelStatus.disconnected


def test_validate_rejects_overlength_and_disconnected():
    conn = get_connector("threads")     # threads 上限 500
    ch = Channel(id="ch2", workspace_id="ws1", platform="threads")
    # 未连接 → 校验失败
    errs = conn.validate(ch, PostContent(body="hi"))
    assert any("未连接" in e for e in errs)

    ch = conn.connect(ch)
    long = PostContent(body="字" * 600)  # 超过 threads 的 500
    errs = conn.validate(ch, long)
    assert any("超过上限" in e for e in errs)

    res = conn.publish(ch, long)
    assert not res.ok


def test_mock_platforms_publish_ok():
    """模拟平台(需 OAuth 的)用 MockConnector 打通全链路。"""
    from yominpost.models import PLATFORM_BY_ID
    for pid, meta in PLATFORM_BY_ID.items():
        if meta.real:
            continue
        c = get_connector(pid)
        ch = c.connect(Channel(id="c", workspace_id="w", platform=pid))
        res = c.publish(ch, PostContent(hook="hi", body="ok"))
        assert res.ok, f"{pid} 发布失败"


def test_real_connectors_reject_missing_credentials():
    """token/webhook 类真实连接器在缺凭据时置为 error(不发网络请求)。"""
    from yominpost.models import ChannelStatus, PLATFORM_BY_ID
    for pid, meta in PLATFORM_BY_ID.items():
        if meta.auth != "credentials":
            continue
        c = get_connector(pid)
        ch = c.connect(Channel(id="c", workspace_id="w", platform=pid), {})
        assert ch.status == ChannelStatus.error, f"{pid} 应因缺凭据而 error"
        assert ch.config.get("error")


def test_webhook_connector_publishes_to_local(monkeypatch):
    """Webhook 连接器:连接后真实 POST(用打桩的 httpx 验证请求体)。"""
    import httpx
    from yominpost.connectors.real import WebhookConnector

    sent = {}

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, timeout=None, **kw):
        sent["url"] = url
        sent["json"] = json
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    conn = WebhookConnector()
    ch = conn.connect(Channel(id="c", workspace_id="w", platform="webhook"),
                      {"url": "https://example.test/hook"})
    from yominpost.models import ChannelStatus
    assert ch.status == ChannelStatus.connected
    res = conn.publish(ch, PostContent(hook="标题", body="正文", cta="关注"))
    assert res.ok
    assert sent["url"] == "https://example.test/hook"
    assert "正文" in sent["json"]["text"]
