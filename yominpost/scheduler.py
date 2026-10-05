"""后台调度:进程内 asyncio 循环,周期性发布到期定时帖 + 维护性任务。

- 每 TICK_SEC 秒:services.run_due_posts()(原子认领,发布到期帖)
- 每 PURGE_EVERY 次 tick:清理过期 oauth state
- 每 BACKUP_EVERY 次 tick:一致性备份(保留最近 N 份)

设计为「无外部依赖、随 app 生命周期起停」。同一进程只跑一个循环。
发布走线程池(connector 是同步阻塞 I/O),避免卡住事件循环。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, services
from .config import DATA_DIR

log = logging.getLogger("yominpost.scheduler")

TICK_SEC = 30                 # 发布检查间隔
PURGE_EVERY = 20              # 每 ~10 分钟清一次 oauth state
RECOVER_EVERY = 20            # 每 ~10 分钟回收卡死的 publishing 帖(运行时崩溃也能自愈)
BACKUP_INTERVAL_SEC = 86400   # 备份间隔(按墙钟,非 tick 计数——重启也不漏备份)
BACKUP_CHECK_EVERY = 20       # 每 ~10 分钟检查一次是否到备份点
BACKUP_KEEP = 7               # 保留最近 7 份
_BACKUP_SETTING = "last_backup_at"   # 上次备份时刻(持久化,跨重启)

def _int_env(name: str, default: int, lo: int = 0, hi: int = 23) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# ---- 渠道健康守护(主动续期+探针;失败标记→全局通知) ----
HEALTH_INTERVAL_SEC = 6 * 3600
HEALTH_CHECK_EVERY = 20            # 每 ~10 分钟看一次是否到 6h 点(仅令牌续期+熔断探针)
_HEALTH_SETTING = "last_channel_health_at"

# ---- 渠道数据同步(账号状态画像 + 内容事实):一天一次主动更新 ----
DATA_SYNC_HOUR = _int_env("YOMINPOST_SYNC_HOUR", 7)   # 每日同步时刻(YOMINPOST_TZ 或本机时区),坏值回退 7
DATA_SYNC_CHECK_EVERY = 20         # 每 ~10 分钟看一次是否到当日同步点
_DATA_SYNC_SETTING = "channel_data_sync_day"          # "YYYY-MM-DD"(沪日),当天只跑一次


def _data_sync_due(now_sh: datetime) -> bool:
    return (now_sh.hour >= DATA_SYNC_HOUR
            and db.get_setting(_DATA_SYNC_SETTING) != now_sh.date().isoformat())


def _backup_once() -> str | None:
    """一致性备份到 DATA_DIR/backups/yominpost-YYYYmmddHHMMSS.db,并轮转。"""
    bdir = Path(DATA_DIR) / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    dest = bdir / f"yominpost-{stamp}.db"
    try:
        db.backup_to(dest)
    except Exception:  # noqa: BLE001
        log.exception("backup failed")
        return None
    # 轮转:仅保留最近 BACKUP_KEEP 份
    backups = sorted(bdir.glob("yominpost-*.db"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(dest)


def _backup_due() -> bool:
    """按持久化的 last_backup_at 判断是否到备份点(墙钟,跨重启有效)。"""
    last = db.get_setting(_BACKUP_SETTING)
    if not last:
        return True                # 从未备份过 → 立刻备一次
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        return age >= BACKUP_INTERVAL_SEC
    except Exception:  # noqa: BLE001
        return True


def _health_due() -> bool:
    last = db.get_setting(_HEALTH_SETTING)
    if not last:
        return True
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= HEALTH_INTERVAL_SEC
    except Exception:  # noqa: BLE001
        return True


def _operator_tz():
    """Operator time zone for the daily sync point: YOMINPOST_TZ (IANA name) or the system local zone."""
    name = os.environ.get("YOMINPOST_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            log.warning("invalid YOMINPOST_TZ=%r, falling back to local time", name)
    return datetime.now().astimezone().tzinfo


async def _loop(stop: asyncio.Event) -> None:
    # 启动即回收崩溃遗留的 publishing 帖 + 卡在 running 的 compose run
    try:
        n = await asyncio.to_thread(services.recover_stuck_publishing)
        if n:
            log.info("recovered %d stuck publishing posts", n)
    except Exception:  # noqa: BLE001
        log.exception("recover_stuck_publishing failed")
    try:
        n = await asyncio.to_thread(services.recover_stuck_compose_runs)
        if n:
            log.info("recovered %d stuck compose runs", n)
    except Exception:  # noqa: BLE001
        log.exception("recover_stuck_compose_runs failed")

    tick = 0
    while not stop.is_set():
        tick += 1
        try:
            published = await asyncio.to_thread(services.run_due_posts)
            if published:
                log.info("scheduler published %d post(s)", len(published))
        except Exception:  # noqa: BLE001
            log.exception("run_due_posts failed")

        if tick % PURGE_EVERY == 0:
            try:
                await asyncio.to_thread(db.purge_oauth_states)
            except Exception:  # noqa: BLE001
                log.exception("purge_oauth_states failed")

        if tick % RECOVER_EVERY == 0:
            # 运行时(非启动)也可能有帖卡在 publishing(单次发布线程崩溃/超时)。
            # 只回收 >5min 无更新的(recover 内含 older_than_sec 保护),不误伤在途帖。
            try:
                n = await asyncio.to_thread(services.recover_stuck_publishing)
                if n:
                    log.info("periodic recover: %d stuck publishing posts", n)
            except Exception:  # noqa: BLE001
                log.exception("periodic recover_stuck_publishing failed")

        if tick % BACKUP_CHECK_EVERY == 0 and await asyncio.to_thread(_backup_due):
            path = await asyncio.to_thread(_backup_once)
            if path:
                db.set_setting(_BACKUP_SETTING, datetime.now(timezone.utc).isoformat(timespec="seconds"))
                log.info("db backup -> %s", path)

        if tick % HEALTH_CHECK_EVERY == 0 and await asyncio.to_thread(_health_due):
            # 6h:仅令牌续期 + 熔断探针(安全基建,非用户面「数据同步」)
            db.set_setting(_HEALTH_SETTING, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            try:
                await asyncio.to_thread(services.check_channel_health)
            except Exception:  # noqa: BLE001
                log.exception("channel health check failed")

        if tick % DATA_SYNC_CHECK_EVERY == 0:
            # 每日一次:账号状态画像 + 内容事实,统一同步(与手动刷新按钮同一入口)
            now_sh = datetime.now(_operator_tz())
            if await asyncio.to_thread(_data_sync_due, now_sh):
                db.set_setting(_DATA_SYNC_SETTING, now_sh.date().isoformat())
                try:
                    await asyncio.to_thread(services.daily_channel_sync)
                except Exception:  # noqa: BLE001
                    log.exception("daily channel sync failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SEC)
        except asyncio.TimeoutError:
            pass


def start(app) -> None:
    """在 lifespan 里调用:后台起循环,句柄挂到 app.state。"""
    stop = asyncio.Event()
    app.state._sched_stop = stop
    app.state._sched_task = asyncio.create_task(_loop(stop))
    log.info("scheduler started (tick=%ss)", TICK_SEC)


async def stop(app) -> None:
    """在 lifespan 关闭时调用:优雅停止循环。"""
    ev = getattr(app.state, "_sched_stop", None)
    task = getattr(app.state, "_sched_task", None)
    if ev:
        ev.set()
    if task:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    tri = getattr(_loop, "_triage_task", None)
    if tri and not tri.done():
        tri.cancel()
    log.info("scheduler stopped")
