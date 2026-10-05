"""数据层加固:原子认领 / compose 落库 / oauth TTL / 备份 / 迁移版本。"""
import warnings

warnings.filterwarnings("ignore")

from yominpost import db, services  # noqa: E402
from yominpost.models import ComposeRun, Post, PostStatus, PublishTarget  # noqa: E402


def _ws():
    return db.create_workspace("T", brand_name="T")


def test_claim_scheduled_post_is_atomic_once():
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.scheduled,
             scheduled_at=db.now_iso(),
             targets=[PublishTarget(channel_id="c", platform="x")])
    db.save_post(p)
    # 第一次认领成功,第二次失败(已非 scheduled)
    assert db.claim_scheduled_post(p.id) is True
    assert db.claim_scheduled_post(p.id) is False
    assert db.get_post(p.id).status == PostStatus.publishing


def test_compose_run_roundtrip():
    ws = _ws()
    run = ComposeRun(id=db.new_id("cmp"), workspace_id=ws.id, driver="template",
                     material_preview="hello", capabilities=["text"],
                     drafts=[{"platform": "x", "content": {"body": "hi"}}])
    db.save_compose_run(run)
    got = db.get_compose_run(run.id)
    assert got and got.material_preview == "hello"
    assert got.drafts[0]["platform"] == "x"
    listed = db.list_compose_runs(ws.id)
    assert any(r.id == run.id for r in listed)


def test_oauth_state_ttl_expires():
    ws = _ws()
    st = db.new_id("st")
    db.save_oauth_state(st, ws.id, "x", "verifier", "", "/")
    assert db.pop_oauth_state(st) is not None            # 新鲜 state 可取
    old = db.new_id("st")
    db.save_oauth_state(old, ws.id, "x", "verifier", "", "/")
    with db._conn() as c:                                # 人为置为过期
        c.execute("UPDATE oauth_states SET created_at=? WHERE state=?",
                  ("2000-01-01T00:00:00+00:00", old))
    assert db.pop_oauth_state(old) is None


def test_backup_to_creates_file(tmp_path):
    _ws()
    dest = tmp_path / "bk.db"
    out = db.backup_to(dest)
    assert dest.exists() and dest.stat().st_size > 0
    # 备份可独立打开且含 workspaces 表
    import sqlite3
    con = sqlite3.connect(out)
    n = con.execute("SELECT count(*) FROM workspaces").fetchone()[0]
    con.close()
    assert n >= 1


def test_schema_version_applied():
    db.init_db()
    with db._conn() as c:
        v = c.execute("PRAGMA user_version").fetchone()[0]
    assert v == db.SCHEMA_VERSION


def test_recover_stuck_publishing():
    import json
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.publishing,
             scheduled_at=db.now_iso(),
             targets=[PublishTarget(channel_id="c", platform="x")])
    db.save_post(p)  # save_post 会把 updated_at 刷成 now,手动改回很久以前
    with db._conn() as c:
        row = c.execute("SELECT payload FROM posts WHERE id=?", (p.id,)).fetchone()
        d = json.loads(row["payload"])
        d["updated_at"] = "2000-01-01T00:00:00+00:00"
        c.execute("UPDATE posts SET payload=? WHERE id=?", (json.dumps(d), p.id))
    n = services.recover_stuck_publishing()
    assert n >= 1
    assert db.get_post(p.id).status == PostStatus.failed  # 现为 failed(不再自动重发)
