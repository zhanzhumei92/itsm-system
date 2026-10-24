"""
自定义定时任务：动作注册器 + 执行包装（对应系统配置页"定时任务"面板）
===============================================================================
定位与安全边界：
  用户在界面上"创建定时任务"，本质是选择【做什么】+【什么时候做】：
    - 做什么：只能从本文件的 ACTION_REGISTRY 里选（注册器模式，与采集器/
      告警渠道的扩展方式一致，新增动作只需 register 一行，不改旧代码）；
    - 什么时候做：固定间隔秒数或 5 段 cron 表达式（解析交给 APScheduler）。

  刻意**不提供**"执行任意脚本/SQL"类动作：定时任务以平台进程身份运行，
  一旦开放任意命令，任何一个拥有管理员账号的人就等于拿到了服务器 shell。
  当前交付范围（用户已确认）是内置维护动作，将来要加 Webhook 等再注册。

执行记录（为什么包装一层 run_task_by_id）：
  直接把动作函数塞给 APScheduler，"任务失败"只会躺在日志文件里，
  界面上看不到。包装器负责把 每次执行的 时间/成败/摘要 回写
  sys_scheduled_task.last_run_* 三列，并写一条 SysLog 留痕——
  "任务看着启用了实际一直失败"必须能在第一时间从界面发现。
===============================================================================
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional

from app.core.config import BASE_DIR, settings
from app.core.database import session_scope
from app.core.logger import system_logger
from app.models import SysLog, SysScheduledTask

# 自定义任务的最小间隔（秒）：与采集 tick 同数量级。
# 再小没有意义，还会让"用户填了个 1"这种误配置瞬间刷屏日志。
MIN_INTERVAL_SECONDS = 10

# SQLite 备份文件保留个数：超出后从旧到新删除，防止备份目录无限膨胀
BACKUP_KEEP_COUNT = 10


class ScheduledTaskAction:
    """一个可被定时调度的维护动作（key/名称/说明/执行函数）"""

    def __init__(self, key: str, label: str, description: str, func: Callable[[], Any]):
        self.key = key
        self.label = label
        self.description = description
        self.func = func


# 动作注册表（注册器模式：新增动作调用 register_action 即可，无需改动调度器）
ACTION_REGISTRY: Dict[str, ScheduledTaskAction] = {}


def register_action(key: str, label: str, description: str) -> Callable:
    """
    动作注册装饰器。

    用法：
        @register_action("cleanup_data", "数据清理", "指标降采样与日志清理")
        def _cleanup() -> dict: ...
    """
    def deco(func: Callable[[], Any]) -> Callable:
        if key in ACTION_REGISTRY:
            raise ValueError(f"定时任务动作重复注册：{key}")
        ACTION_REGISTRY[key] = ScheduledTaskAction(key, label, description, func)
        return func
    return deco


def get_action(key: str) -> Optional[ScheduledTaskAction]:
    return ACTION_REGISTRY.get(key)


def list_actions() -> List[Dict[str, str]]:
    """动作目录（前端"新建任务"弹窗的下拉数据源）"""
    return [
        {"action": a.key, "label": a.label, "description": a.description}
        for a in ACTION_REGISTRY.values()
    ]


# ============================================================================
# 内置维护动作（注册即生效）
# ============================================================================

@register_action("cleanup_data", "指标降采样与日志清理", "按保留期清理指标明细与过期日志（内置任务默认每日 03:30 执行）")
def _action_cleanup() -> dict:
    from app.scheduler.jobs.cleanup_job import run_cleanup_job
    return run_cleanup_job()


@register_action("refresh_maintain_remind", "资产维保到期提醒刷新", "重新扫描资产维保日期并刷新提醒列表（内置任务默认每日 08:00 执行）")
def _action_remind() -> dict:
    from app.scheduler.jobs.remind_job import run_refresh_remind_job
    return run_refresh_remind_job()


@register_action("collect_round", "立即执行一轮设备采集", "对当前所有已启用设备立即发起一轮采集（不等采集周期到点）")
def _action_collect() -> dict:
    from app.scheduler.jobs.collect_job import run_collect_job
    return run_collect_job()


@register_action("sqlite_backup", "SQLite 数据库备份", "将 SQLite 数据库在线备份到 data/backups/ 目录，自动保留最近 10 份（MySQL 部署时此动作自动跳过并提示）")
def _action_sqlite_backup() -> dict:
    """
    在线备份 SQLite 数据库。

    为什么用 sqlite3 的 backup API 而不是直接复制 .db 文件：
    直接 cp 可能把"正在写入途中"的页拷走，得到一个损坏的备份文件；
    backup API 会在页级加锁一致性拷贝，业务不中断、备份必定可用
    （部署手册中的 sqlite3 .backup 命令行方案与这里是同一原理）。
    """
    if not settings.is_sqlite:
        return {"skipped": True, "message": "当前数据库不是 SQLite，跳过备份"}

    db_path = settings.DATABASE_URL.replace("sqlite:///", "", 1)
    backup_dir = BASE_DIR / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"itsm_{time.strftime('%Y%m%d_%H%M%S')}.db"

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 清理旧备份：只留最近 BACKUP_KEEP_COUNT 份
    old = sorted(backup_dir.glob("itsm_*.db"))[:-BACKUP_KEEP_COUNT]
    removed = 0
    for f in old:
        f.unlink(missing_ok=True)
        removed += 1

    size_mb = target.stat().st_size / 1024 / 1024
    summary = {"backup_file": target.name, "size_mb": round(size_mb, 2), "removed_old": removed}
    system_logger.info("自定义定时任务执行 SQLite 备份：%s", summary)
    return summary


# ============================================================================
# 执行包装：回写执行记录 + 写系统日志
# ============================================================================

def run_task_by_id(task_id: int, operator: str = "scheduler") -> Dict[str, Any]:
    """
    执行一条自定义任务并记录结果（调度触发与"立即执行"按钮共用同一入口）。

    为什么每次执行都新开一个会话查任务行：
    任务定义可能刚被用户改过（改名/换动作），调度器内存里的 job 快照
    不一定是最新的；以数据库当前内容为准，避免"改了任务还跑旧逻辑"。

    :return: {ok, message} 供调用方（API/调度器）展示
    """
    with session_scope() as db:
        task = db.get(SysScheduledTask, task_id)
        if task is None:
            return {"ok": False, "message": f"任务不存在（id={task_id}）"}
        action = get_action(task.action)
        if action is None:
            ok, message = False, f"动作 {task.action} 未注册（可能已被移除）"
        else:
            started = time.time()
            try:
                result = action.func()
                elapsed = time.time() - started
                ok = True
                message = f"{action.label} 完成，耗时 {elapsed:.1f}s；结果：{result}"
            except Exception as exc:  # noqa: BLE001 —— 单次失败必须被记录且不影响调度器线程
                ok = False
                message = f"{action.label} 执行失败：{exc}"
                system_logger.exception("自定义定时任务 %s(%s) 执行失败", task.name, task.action)

        # 回写执行记录（同一会话内更新，保证与执行结果原子可见）
        task.last_run_at = __import__("datetime").datetime.now()
        task.last_run_ok = ok
        task.last_run_message = message[:2000]
        db.add(SysLog(
            level="INFO" if ok else "ERROR",
            module="scheduler",
            action="run_scheduled_task",
            message=(f"自定义定时任务「{task.name}」{'执行成功' if ok else '执行失败'}：{message}"),
            username=operator,
        ))

    if not ok:
        system_logger.warning("自定义定时任务 %s 执行失败：%s", task_id, message)
    return {"ok": ok, "message": message}
