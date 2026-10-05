"""产品实体的 SQLite 仓储层。

每个实体一张表:少量可查询列 + 一个 payload(完整 Pydantic JSON)。
既能按 workspace / status / 时间过滤,又不必为每次模型演进写迁移。
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH
from .models import Asset, Channel, ComposeRun, Post, Topic, Workspace

log = logging.getLogger("yominpost.db")

# 当前 schema 版本(每次结构变更 +1,并在 _MIGRATIONS 追加迁移函数)
SCHEMA_VERSION = 2

_BASE_DDL = """
CREATE TABLE IF NOT EXISTS workspaces(
  id TEXT PRIMARY KEY, name TEXT, slug TEXT, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, workspace_id TEXT, platform TEXT, status TEXT,
  created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY, workspace_id TEXT, kind TEXT, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS topics(
  id TEXT PRIMARY KEY, workspace_id TEXT, status TEXT, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS posts(
  id TEXT PRIMARY KEY, workspace_id TEXT, status TEXT,
  scheduled_at TEXT, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS compose_runs(
  id TEXT PRIMARY KEY, workspace_id TEXT, status TEXT, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS oauth_states(
  state TEXT PRIMARY KEY, workspace_id TEXT, platform TEXT,
  verifier TEXT, instance TEXT, redirect_uri TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_channels_ws ON channels(workspace_id);
CREATE INDEX IF NOT EXISTS ix_assets_ws ON assets(workspace_id);
CREATE INDEX IF NOT EXISTS ix_topics_ws ON topics(workspace_id);
CREATE INDEX IF NOT EXISTS ix_posts_ws ON posts(workspace_id);
CREATE INDEX IF NOT EXISTS ix_posts_status ON posts(workspace_id, status);
CREATE INDEX IF NOT EXISTS ix_compose_ws ON compose_runs(workspace_id);
"""


def _mig_1_due_index(c: sqlite3.Connection) -> None:
    """加速调度器扫描到期定时帖。"""
    c.execute("CREATE INDEX IF NOT EXISTS ix_posts_due ON posts(status, scheduled_at)")


def _mig_2_oauth_created_index(c: sqlite3.Connection) -> None:
    c.execute("CREATE INDEX IF NOT EXISTS ix_oauth_created ON oauth_states(created_at)")


# (version, fn):按顺序对 user_version < version 的库执行
_MIGRATIONS = [(1, _mig_1_due_index), (2, _mig_2_oauth_created_index)]

_INITED: set[str] = set()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """建表 + 跑迁移。每个 db_path 每进程只执行一次(省去每次调用重复 DDL)。"""
    key = str(db_path or DB_PATH)
    if key in _INITED:
        return
    with _conn(db_path) as c:
        c.executescript(_BASE_DDL)
        cur = c.execute("PRAGMA user_version").fetchone()[0]
        for ver, fn in _MIGRATIONS:
            if cur < ver:
                fn(c)
                c.execute(f"PRAGMA user_version={ver}")
                log.info("db migrated to version %d", ver)
    _INITED.add(key)


def _load(model, rows):
    """把 payload 行反序列化为模型;单行损坏只跳过并告警,不让整个列表接口崩。"""
    out = []
    for r in rows:
        try:
            out.append(model.model_validate_json(r["payload"]))
        except Exception as e:  # noqa: BLE001
            log.warning("skip invalid %s row: %s", model.__name__, e)
    return out


def get_setting(key: str, default: str | None = None, db_path=None) -> str | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(key: str, value: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))


def set_secret_setting(key: str, value: str, db_path=None) -> None:
    """加密后写入(供 API key 等敏感设置,和 OAuth 令牌一样不落明文)。"""
    from .crypto import encrypt
    set_setting(key, encrypt(value or ""), db_path=db_path)


def get_secret_setting(key: str, default: str = "", db_path=None) -> str:
    """读取加密设置;向后兼容历史明文值(解不开就按明文返回)。"""
    from .crypto import try_decrypt
    raw = get_setting(key, None, db_path=db_path)
    if raw is None:
        return default
    ok, val = try_decrypt(raw)
    return val if ok else raw


def save_oauth_state(state: str, workspace_id: str, platform: str, verifier: str,
                     instance: str, redirect_uri: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO oauth_states VALUES(?,?,?,?,?,?,?)",
                  (state, workspace_id, platform, verifier, instance, redirect_uri, now_iso()))


OAUTH_STATE_TTL_SEC = 600  # 授权 state 有效期 10 分钟


def pop_oauth_state(state: str, db_path=None) -> dict | None:
    """取出并删除 state;过期(>10min)视为无效。"""
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT * FROM oauth_states WHERE state=?", (state,)).fetchone()
        if r:
            c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not r:
        return None
    d = dict(r)
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(d["created_at"])).total_seconds()
        if age > OAUTH_STATE_TTL_SEC:
            return None
    except Exception:  # noqa: BLE001
        pass
    return d


def purge_oauth_states(db_path=None) -> int:
    """清理过期的授权 state(调度器周期调用)。"""
    init_db(db_path)
    cutoff = (datetime.now(timezone.utc)).timestamp() - OAUTH_STATE_TTL_SEC
    with _conn(db_path) as c:
        rows = c.execute("SELECT state, created_at FROM oauth_states").fetchall()
        stale = []
        for r in rows:
            try:
                if datetime.fromisoformat(r["created_at"]).timestamp() < cutoff:
                    stale.append(r["state"])
            except Exception:  # noqa: BLE001
                stale.append(r["state"])
        for s in stale:
            c.execute("DELETE FROM oauth_states WHERE state=?", (s,))
    return len(stale)


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #
def create_workspace(name: str, brand_name: str = "", db_path=None) -> Workspace:
    init_db(db_path)
    slug = "".join(ch for ch in name.lower().replace(" ", "-") if ch.isalnum() or ch == "-")
    ws = Workspace(id=new_id("ws"), name=name, slug=slug or "workspace",
                   brand_name=brand_name or name, created_at=now_iso())
    with _conn(db_path) as c:
        c.execute("INSERT INTO workspaces VALUES(?,?,?,?,?)",
                  (ws.id, ws.name, ws.slug, ws.created_at, ws.model_dump_json()))
    return ws


def list_workspaces(db_path=None) -> list[Workspace]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute("SELECT payload FROM workspaces ORDER BY created_at").fetchall()
    return _load(Workspace, rows)


def get_workspace(ws_id: str, db_path=None) -> Workspace | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    return Workspace.model_validate_json(r["payload"]) if r else None


# --------------------------------------------------------------------------- #
# Channel
# --------------------------------------------------------------------------- #
def save_channel(ch: Channel, db_path=None) -> Channel:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO channels VALUES(?,?,?,?,?,?)",
            (ch.id, ch.workspace_id, ch.platform, ch.status.value,
             ch.connected_at or now_iso(), ch.model_dump_json()),
        )
    return ch


def get_channel(ch_id: str, db_path=None) -> Channel | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM channels WHERE id=?", (ch_id,)).fetchone()
    return Channel.model_validate_json(r["payload"]) if r else None


def list_channels(workspace_id: str, db_path=None) -> list[Channel]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT payload FROM channels WHERE workspace_id=? ORDER BY created_at",
            (workspace_id,),
        ).fetchall()
    return _load(Channel, rows)


def delete_channel(ch_id: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("DELETE FROM channels WHERE id=?", (ch_id,))


# --------------------------------------------------------------------------- #
# Asset
# --------------------------------------------------------------------------- #
def save_asset(a: Asset, db_path=None) -> Asset:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?)",
                  (a.id, a.workspace_id, a.kind, a.created_at or now_iso(), a.model_dump_json()))
    return a


def list_assets(workspace_id: str, db_path=None) -> list[Asset]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT payload FROM assets WHERE workspace_id=? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return _load(Asset, rows)


def get_asset(asset_id: str, db_path=None) -> Asset | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM assets WHERE id=?", (asset_id,)).fetchone()
    return Asset.model_validate_json(r["payload"]) if r else None


def delete_asset(asset_id: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("DELETE FROM assets WHERE id=?", (asset_id,))


# --------------------------------------------------------------------------- #
# Topic
# --------------------------------------------------------------------------- #
def save_topic(t: Topic, db_path=None) -> Topic:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO topics VALUES(?,?,?,?,?)",
                  (t.id, t.workspace_id, t.status.value, t.created_at or now_iso(),
                   t.model_dump_json()))
    return t


def list_topics(workspace_id: str, db_path=None) -> list[Topic]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT payload FROM topics WHERE workspace_id=? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
    return _load(Topic, rows)


def get_topic(topic_id: str, db_path=None) -> Topic | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM topics WHERE id=?", (topic_id,)).fetchone()
    return Topic.model_validate_json(r["payload"]) if r else None


def delete_topic(topic_id: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("DELETE FROM topics WHERE id=?", (topic_id,))


# --------------------------------------------------------------------------- #
# Post
# --------------------------------------------------------------------------- #
def save_post(p: Post, db_path=None) -> Post:
    init_db(db_path)
    p.updated_at = now_iso()
    if not p.created_at:
        p.created_at = p.updated_at
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?,?)",
                  (p.id, p.workspace_id, p.status.value, p.scheduled_at,
                   p.created_at, p.model_dump_json()))
    return p


def get_post(post_id: str, db_path=None) -> Post | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM posts WHERE id=?", (post_id,)).fetchone()
    return Post.model_validate_json(r["payload"]) if r else None


def list_posts(workspace_id: str, status: str | None = None, db_path=None) -> list[Post]:
    init_db(db_path)
    q = "SELECT payload FROM posts WHERE workspace_id=?"
    args: list = [workspace_id]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY COALESCE(NULLIF(scheduled_at,''), created_at) DESC"
    with _conn(db_path) as c:
        rows = c.execute(q, args).fetchall()
    return _load(Post, rows)


def due_scheduled_posts(workspace_id: str | None, before_iso: str, db_path=None) -> list[Post]:
    """返回到期(scheduled_at <= before)的待发帖。"""
    init_db(db_path)
    # scheduled_at LIKE '____-__-__T%' 兜底:只认规范 ISO 形状的排期,挡住历史脏数据
    # (如 "07/15/2026 10:00")被字符串比较误判成到期而错误提前发布。
    q = ("SELECT payload FROM posts WHERE status='scheduled' "
         "AND scheduled_at!='' AND scheduled_at LIKE '____-__-__T%' "
         "AND scheduled_at<=?")
    args: list = [before_iso]
    if workspace_id:
        q += " AND workspace_id=?"
        args.append(workspace_id)
    with _conn(db_path) as c:
        rows = c.execute(q, args).fetchall()
    return _load(Post, rows)


def delete_post(post_id: str, db_path=None) -> None:
    init_db(db_path)
    with _conn(db_path) as c:
        c.execute("DELETE FROM posts WHERE id=?", (post_id,))


def claim_post_for_publish(post_id: str, db_path=None) -> bool:
    """手动发布的原子认领:仅当当前不是 publishing/posted 时置为 publishing。
    返回是否认领成功。防止双击/并发重复进入发布。

    认领时同步刷新 payload.updated_at,否则一篇很久未编辑的草稿一旦进入 publishing,
    就会立刻被 recover_stuck_publishing(以 updated_at < 5min 为准)误判成卡死 →
    标 failed → 诱导重发,造成重复投递。"""
    init_db(db_path)
    with _conn(db_path) as c:
        cur = c.execute(
            "UPDATE posts SET status='publishing', "
            "payload=json_set(payload,'$.status','publishing','$.updated_at',?) "
            "WHERE id=? AND status NOT IN ('publishing','posted')",
            (now_iso(), post_id))
        return cur.rowcount == 1


def claim_scheduled_post(post_id: str, db_path=None) -> bool:
    """原子认领:仅当仍是 scheduled 时置为 publishing。返回是否认领成功。
    防止多个调度 tick / 并发点击导致同一帖被发多次。

    同步刷新 updated_at(见 claim_post_for_publish),避免定时帖一进 publishing
    就被 recovery 误判卡死。"""
    init_db(db_path)
    with _conn(db_path) as c:
        cur = c.execute(
            "UPDATE posts SET status='publishing', "
            "payload=json_set(payload,'$.status','publishing','$.updated_at',?) "
            "WHERE id=? AND status='scheduled'",
            (now_iso(), post_id))
        return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# ComposeRun(生成历史,可在 UI 恢复)
# --------------------------------------------------------------------------- #
def save_compose_run(run: ComposeRun, db_path=None) -> ComposeRun:
    init_db(db_path)
    if not run.created_at:
        run.created_at = now_iso()
    with _conn(db_path) as c:
        c.execute("INSERT OR REPLACE INTO compose_runs VALUES(?,?,?,?,?)",
                  (run.id, run.workspace_id, run.status, run.created_at, run.model_dump_json()))
    return run


def list_compose_runs(workspace_id: str, limit: int = 20, db_path=None) -> list[ComposeRun]:
    init_db(db_path)
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT payload FROM compose_runs WHERE workspace_id=? "
            "ORDER BY created_at DESC LIMIT ?", (workspace_id, limit)).fetchall()
    return _load(ComposeRun, rows)


def get_compose_run(run_id: str, db_path=None) -> ComposeRun | None:
    init_db(db_path)
    with _conn(db_path) as c:
        r = c.execute("SELECT payload FROM compose_runs WHERE id=?", (run_id,)).fetchone()
    return ComposeRun.model_validate_json(r["payload"]) if r else None


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def backup_to(dest: str | Path, db_path=None) -> str:
    """一致性备份(sqlite backup API,允许并发写入时安全拷贝)。返回目标路径。"""
    init_db(db_path)
    dest = str(dest)
    src = sqlite3.connect(str(db_path or DB_PATH))
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest
