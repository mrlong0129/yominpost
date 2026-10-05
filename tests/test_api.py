"""Web API 全链路测试:workspace / 渠道 / 生成 / 建帖 / 发布 / 定时 / 日历。"""
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _ws(client):
    return client.get("/api/workspaces").json()[0]


def test_seed_and_platforms(client):
    assert client.get("/api/health").json()["ok"] is True
    assert len(client.get("/api/platforms").json()) == 15  # demo 平台默认关
    ws = _ws(client)
    assert ws["name"]
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    assert any(c["status"] == "connected" for c in chans)


def test_generate_returns_all_slots(client):
    ws = _ws(client)
    r = client.post(f"/api/workspaces/{ws['id']}/generate",
                    json={"topic": "用一条 thread 讲清 5 个动作", "platform": "instagram",
                          "optimize": True}).json()
    assert r["content"]["hook"]
    assert r["format"] in ("image_text", "carousel")
    assert "scores" in r["qa"]
    assert r["router"]["rule_trace"]
    assert r["visual_url"].startswith("/artifacts/")  # 视觉平台会出海报


def test_full_post_flow(client):
    ws = _ws(client)
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    ich = next(c for c in chans if c["platform"] == "tiktok")
    g = client.post(f"/api/workspaces/{ws['id']}/generate",
                    json={"topic": "内容自动化实验", "platform": "tiktok"}).json()
    post = client.post(f"/api/workspaces/{ws['id']}/posts", json={
        "title": "实验复盘", "content": g["content"], "format": g["format"],
        "channel_ids": [ich["id"]], "qa": g["qa"],
    }).json()
    assert post["status"] == "draft"
    pub = client.post(f"/api/posts/{post['id']}/publish").json()
    assert pub["status"] == "posted"
    assert pub["targets"][0]["sandbox"] is True   # 模拟渠道:标 sandbox,不给假链接


def test_schedule_and_tick(client):
    ws = _ws(client)
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    ich = next(c for c in chans if c["platform"] == "tiktok")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    sp = client.post(f"/api/workspaces/{ws['id']}/posts", json={
        "title": "定时", "content": {"hook": "定时发布测试", "body": "正文", "cta": "关注"},
        "channel_ids": [ich["id"]], "scheduled_at": past,
    }).json()
    assert sp["status"] == "scheduled"
    ticked = client.post(f"/api/workspaces/{ws['id']}/tick").json()
    assert any(p["status"] == "posted" for p in ticked["published"])


def test_connect_disconnect(client):
    ws = _ws(client)
    ch = client.post(f"/api/workspaces/{ws['id']}/channels/connect",
                     json={"platform": "pinterest", "display_name": "Yomin"}).json()
    assert ch["status"] == "connected"
    off = client.post(f"/api/channels/{ch['id']}/disconnect").json()
    assert off["status"] == "disconnected"


def test_topics_crud(client):
    ws = _ws(client)
    t = client.post(f"/api/workspaces/{ws['id']}/topics",
                    json={"title": "新话题 X", "pillar": "方法论"}).json()
    assert t["title"] == "新话题 X"
    titles = [x["title"] for x in client.get(f"/api/workspaces/{ws['id']}/topics").json()]
    assert "新话题 X" in titles
    client.delete(f"/api/topics/{t['id']}")
    titles2 = [x["title"] for x in client.get(f"/api/workspaces/{ws['id']}/topics").json()]
    assert "新话题 X" not in titles2


def test_calendar_lists_scheduled_and_posted(client):
    ws = _ws(client)
    cal = client.get(f"/api/workspaces/{ws['id']}/calendar").json()
    assert isinstance(cal, list)


def test_delete_propagates_to_platform(client):
    """删帖时同步删除平台上已发布的真帖(mock 连接器模拟 ok)。"""
    ws = _ws(client)
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    tch = next(c for c in chans if c["platform"] == "tiktok")
    g = client.post(f"/api/workspaces/{ws['id']}/generate",
                    json={"topic": "删除对齐测试", "platform": "tiktok"}).json()
    post = client.post(f"/api/workspaces/{ws['id']}/posts", json={
        "title": "待删", "content": g["content"], "format": g["format"],
        "channel_ids": [tch["id"]]}).json()
    pub = client.post(f"/api/posts/{post['id']}/publish").json()
    assert pub["status"] == "posted"
    d = client.delete(f"/api/posts/{post['id']}").json()
    assert d["ok"] is True
    assert d["platform_deletes"] and d["platform_deletes"][0]["ok"] is True
    ids = [p["id"] for p in client.get(f"/api/workspaces/{ws['id']}/posts").json()]
    assert post["id"] not in ids


def test_canvas_autodraft_dedupe(client):
    """画布自动落盘幂等:重开/刷新画布丢了 c.postId,同 (渠道+素材) 的 dedupe_key
    命中现存草稿 → 就地更新,不新建重复草稿、不重复写素材;换图对账旧素材行;
    已排期帖不被降级覆盖;不同 key 各自独立。"""
    ws = _ws(client)
    ch = next(c for c in client.get(f"/api/workspaces/{ws['id']}/channels").json()
              if c["status"] == "connected")
    key = "cv:test-dedupe:abc123"
    img = "/artifacts/test_dedupe_img_v1.png"

    def drafts_with_key():
        return [p for p in client.get(f"/api/workspaces/{ws['id']}/posts").json()
                if p["status"] == "draft" and (p.get("meta") or {}).get("dedupe_key") == key]

    def assets_with(path):
        return [a for a in client.get(f"/api/workspaces/{ws['id']}/assets").json()
                if a.get("path") == path]

    body = {"title": "去重稿", "content": {"hook": "第一版", "body": "b"},
            "channel_ids": [ch["id"]], "media": [img], "dedupe_key": key}
    p1 = client.post(f"/api/workspaces/{ws['id']}/posts", json=body).json()
    assert p1["status"] == "draft"
    assert len(drafts_with_key()) == 1
    assert len(assets_with(img)) == 1

    # 重开画布再生成(内容不同,同素材同渠道同 key)→ 就地更新,不新建、不重复写素材
    p2 = client.post(f"/api/workspaces/{ws['id']}/posts",
                     json={**body, "content": {"hook": "第二版", "body": "b2"}}).json()
    assert p2["id"] == p1["id"]
    assert p2["content"]["hook"] == "第二版"
    assert len(drafts_with_key()) == 1
    assert len(assets_with(img)) == 1

    # 换图重生成 → 旧素材行被对账移除,新图登记,仍只一条
    img2 = "/artifacts/test_dedupe_img_v2.png"
    p3 = client.post(f"/api/workspaces/{ws['id']}/posts", json={**body, "media": [img2]}).json()
    assert p3["id"] == p1["id"]
    assert len(assets_with(img)) == 0
    assert len(assets_with(img2)) == 1

    # 该草稿被排期后,同 key 再来 → 不降级已排期帖,建新草稿
    sched = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    client.patch(f"/api/posts/{p1['id']}", json={"scheduled_at": sched})
    p4 = client.post(f"/api/workspaces/{ws['id']}/posts",
                     json={**body, "content": {"hook": "第三版", "body": "b3"}}).json()
    assert p4["id"] != p1["id"]
    assert p4["status"] == "draft"
    assert len(drafts_with_key()) == 1        # 只 p4 是 draft;p1 已 scheduled

    # 不同 key → 独立新草稿
    p5 = client.post(f"/api/workspaces/{ws['id']}/posts",
                     json={**body, "dedupe_key": "cv:test-dedupe:different"}).json()
    assert p5["id"] not in (p1["id"], p4["id"])
