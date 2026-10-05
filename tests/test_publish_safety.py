"""从 unified 分支挑回的 4 个安全修复的回归测试。"""
import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from yominpost import db, services  # noqa: E402
from yominpost.models import (Channel, ChannelStatus, Post, PostContent,  # noqa: E402
                              PostStatus, PublishTarget)
from yominpost.web.app import app  # noqa: E402


def _ws():
    return db.create_workspace("Safety T", brand_name="T")


# --- Fix1:编辑 PATCH content 强类型,不因 dict 退化导致 _post_out 500 --- #
def test_patch_content_does_not_500_and_preserves_type():
    with TestClient(app) as c:
        ws = _ws()
        p = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"hook": "h", "body": "b"}}).json()
        r = c.patch(f"/api/posts/{p['id']}",
                    json={"content": {"hook": "H2", "body": "B2", "cta": "C2", "hashtags": ["x"]}})
        assert r.status_code == 200                     # 不再 500
        d = r.json()
        assert d["content"]["hook"] == "H2" and d["content"]["hashtags"] == ["x"]
        assert d["text"]                                 # as_text 正常(content 是 PostContent 非 dict)


# --- Fix2:recover 不把在途帖自动打回重发,标 failed;已发 target 保留 --- #
def test_recover_marks_failed_not_scheduled_and_keeps_posted_target():
    import json
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.publishing,
             scheduled_at=db.now_iso(),
             targets=[PublishTarget(channel_id="c1", platform="x", status="posted",
                                    external_id="EXT1", permalink="http://x/1"),
                      PublishTarget(channel_id="c2", platform="tiktok", status="pending")])
    db.save_post(p)
    with db._conn() as conn:
        row = conn.execute("SELECT payload FROM posts WHERE id=?", (p.id,)).fetchone()
        d = json.loads(row["payload"]); d["updated_at"] = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE posts SET payload=? WHERE id=?", (json.dumps(d), p.id))
    n = services.recover_stuck_publishing()
    assert n >= 1
    got = db.get_post(p.id)
    assert got.status == PostStatus.failed              # 不是 scheduled(否则调度器会重发)
    posted = [t for t in got.targets if t.status == "posted"]
    assert posted and posted[0].external_id == "EXT1"   # 已发 target 保留,重试时会跳过


# --- Fix3:平台删除「临时失败」→ 保留本地记录,不无条件删 --- #
def _post_with_connected_target(ws, platform="facebook", ext="EXT9"):
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform=platform,
                 display_name="acct", status=ChannelStatus.connected)
    db.save_channel(ch)
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.posted,
             content=PostContent(body="hi"),
             targets=[PublishTarget(channel_id=ch.id, platform=platform,
                                    status="posted", external_id=ext)])
    db.save_post(p)
    return ch, p


def _patch_delete(monkeypatch, result):
    from yominpost.connectors.base import Connector
    monkeypatch.setattr(Connector, "delete", lambda self, ch, ext: result)


def test_delete_preserves_local_on_temporary_remote_failure(monkeypatch):
    from yominpost.connectors.base import PublishResult
    ws = _ws()
    _, p = _post_with_connected_target(ws)
    # 临时失败:ok=False 且 unsupported=False(网络/权限,内容可能还在平台)
    _patch_delete(monkeypatch, PublishResult(ok=False, error="rate limited"))
    res = services.delete_post(p)
    assert res["ok"] is False and res["deleted_local"] is False
    still = db.get_post(p.id)
    assert still is not None                             # 本地记录仍在
    assert (still.meta or {}).get("delete_failed") is True


def test_delete_removes_local_when_platform_unsupported(monkeypatch):
    """IG/YT 结构上不支持删除 → 不能永久卡住本地帖,应删本地。"""
    from yominpost.connectors.base import PublishResult
    ws = _ws()
    _, p = _post_with_connected_target(ws, platform="instagram")
    _patch_delete(monkeypatch, PublishResult(ok=False, unsupported=True,
                                             error="IG 不支持删除已发布内容"))
    res = services.delete_post(p)
    assert res["ok"] is True and res["deleted_local"] is True
    assert db.get_post(p.id) is None


def test_delete_missing_channel_is_non_blocking(monkeypatch):
    """posted target 指向已删渠道 → 无法重试远端删除,不应卡住本地删除。"""
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.posted,
             content=PostContent(body="hi"),
             targets=[PublishTarget(channel_id="ghost", platform="facebook",
                                    status="posted", external_id="EXT9")])
    db.save_post(p)
    res = services.delete_post(p)
    assert res["ok"] is True and res["deleted_local"] is True
    assert db.get_post(p.id) is None


def test_delete_force_removes_local_despite_temp_failure(monkeypatch):
    """force=True:临时失败也强制只删本地(前端「仅删本地」入口)。"""
    from yominpost.connectors.base import PublishResult
    ws = _ws()
    _, p = _post_with_connected_target(ws)
    _patch_delete(monkeypatch, PublishResult(ok=False, error="rate limited"))
    res = services.delete_post(p, force=True)
    assert res["ok"] is True and res["deleted_local"] is True
    assert db.get_post(p.id) is None


# --- Fix4:mock 渠道发布标 sandbox,不给假永久链接 --- #
def test_mock_publish_is_sandbox_no_fake_permalink():
    ws = _ws()
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform="tiktok",
                 display_name="demo", status=ChannelStatus.connected)
    ch = services.get_connector("tiktok").connect(ch, {"handle": "demo"})
    db.save_channel(ch)
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.draft,
             content=PostContent(body="hello world"),
             targets=[PublishTarget(channel_id=ch.id, platform="tiktok")])
    db.save_post(p)
    out = services.publish_post(p)
    t = out.targets[0]
    assert t.status == "posted" and t.sandbox is True
    assert t.permalink == ""                             # 不给误导性的"真"链接


# --- A1:PATCH 排期/存草稿要同步 status(否则排期不自动发 / 幽灵定时帖)--- #
def test_patch_schedule_derives_scheduled_status():
    """编辑 draft 点排期:写 scheduled_at 的同时 status 必须变 scheduled,否则调度器永不发。"""
    with TestClient(app) as c:
        ws = _ws()
        p = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"body": "b"}}).json()
        assert p["status"] == "draft"
        r = c.patch(f"/api/posts/{p['id']}",
                    json={"scheduled_at": "2099-01-01T00:00:00+00:00"}).json()
        assert r["status"] == "scheduled"                # 不再停留 draft


def test_patch_clear_schedule_derives_draft_status():
    """编辑 scheduled 点存草稿(清空 scheduled_at):status 必须回 draft,不留幽灵定时帖。"""
    with TestClient(app) as c:
        ws = _ws()
        p = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"body": "b"},
                         "scheduled_at": "2099-01-01T00:00:00+00:00"}).json()
        assert p["status"] == "scheduled"
        r = c.patch(f"/api/posts/{p['id']}", json={"scheduled_at": ""}).json()
        assert r["status"] == "draft"


# --- 复审 #8:PATCH 现在能更新 qa(重生成后草稿的 QA 分与内容同步)--- #
def test_patch_updates_qa_score():
    with TestClient(app) as c:
        ws = _ws()
        p = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"body": "v1"}, "qa": {"overall": 0.5}}).json()
        r = c.patch(f"/api/posts/{p['id']}",
                    json={"content": {"body": "v2"}, "qa": {"overall": 0.9}}).json()
        assert r["qa"]["overall"] == 0.9          # 不再停留在旧分 0.5


def _posted_post(ws):
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.posted,
             content=PostContent(body="hi"), posted_at=db.now_iso(),
             targets=[PublishTarget(channel_id="c1", platform="x",
                                    status="posted", external_id="E1")])
    db.save_post(p)
    return p


# --- ultrareview 必修 #2:posted 帖 PATCH 一律 409(否则画布重写链可静默改写已发内容)--- #
def test_patch_posted_post_rejected_409():
    ws = _ws()
    p = _posted_post(ws)
    with TestClient(app) as c:
        r = c.patch(f"/api/posts/{p.id}", json={"scheduled_at": ""})
        assert r.status_code == 409
        r2 = c.patch(f"/api/posts/{p.id}", json={"content": {"body": "rewritten"}})
        assert r2.status_code == 409
    still = db.get_post(p.id)
    assert still.status == PostStatus.posted and still.content.body == "hi"   # 原样未动


# --- ultrareview 必修 #1:publishing 帖 DELETE 一律 409(发布线程 save_post 会复活已删帖)--- #
def test_delete_publishing_post_returns_409():
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.publishing,
             content=PostContent(body="hi"),
             targets=[PublishTarget(channel_id="c1", platform="x", status="pending")])
    db.save_post(p)
    with TestClient(app) as c:
        r = c.delete(f"/api/posts/{p.id}")
        assert r.status_code == 409
        r2 = c.delete(f"/api/posts/{p.id}?force=1")     # force 也不豁免
        assert r2.status_code == 409
    assert db.get_post(p.id) is not None


# --- ultrareview 必修 #3:failed 帖显式排期 → 转 scheduled 且进入到期队列(不再幽灵)--- #
def test_patch_schedule_on_failed_post_becomes_scheduled_and_due():
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.failed,
             content=PostContent(body="hi"),
             targets=[PublishTarget(channel_id="c1", platform="x", status="failed", error="boom")])
    db.save_post(p)
    with TestClient(app) as c:
        r = c.patch(f"/api/posts/{p.id}",
                    json={"scheduled_at": "2000-01-01T00:00:00+00:00"}).json()   # 过去时刻=立即到期
        assert r["status"] == "scheduled"
    due = [x.id for x in db.due_scheduled_posts(ws.id, db.now_iso())]
    assert p.id in due                                   # 调度器能看到它了
