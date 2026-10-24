"""
调度器管理（APScheduler）
===============================================================================
本平台有三类定时任务：

| 任务            | 触发方式              | 频率      | 说明                                   |
|-----------------|----------------------|-----------|----------------------------------------|
| 采集轮询 tick   | IntervalTrigger       | 每 10 秒  | 扫描"到点且已启用"的设备，投递线程池     |
| 指标与日志清理  | CronTrigger           | 每日 03:30| 降采样 + 删超期日志（低峰期执行）        |
| 维保到期提醒    | CronTrigger           | 每日 08:00| 刷新提醒表，上班后运维即可看到          |

三个关键参数的含义（都踩过坑，务必保留）：
  - coalesce=True ：进程重启期间错过的多次执行合并为一次。
                    否则重启后补偿执行会瞬间跑 N 次采集，把设备打爆。
  - max_instances=1：同一任务不允许并发实例，避免慢任务堆积。
  - misfire_grace_time=60：允许延迟 60 秒内补跑，超过则丢弃
                    （采集是周期性任务，错过一轮直接等下一轮，补跑没有意义）。

采集 tick 为什么是"每 10 秒扫一次"而不是"每台设备一个 job"：
  200 台设备就是 200 个 APScheduler job，调度开销与内存占用都不划算，
  且新增/停用设备时需要频繁增删 job。改成"固定 tick + 数据库筛选到点设备"，
  设备增减对调度器完全透明，实现也更简单可靠。
===============================================================================
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logger import silence_noisy_loggers, system_logger

# 采集轮询间隔（秒）：这是"检查哪些设备到点了"的频率，
# 与设备自身的 collect_interval 无关。取 10 秒是为了让最小采集周期 30 秒的设备
# 也能被准时触发（最多延迟 10 秒）。
COLLECT_TICK_SECONDS = 10

JOB_ID_COLLECT = "collect_tick"
JOB_ID_CLEANUP = "cleanup_data"
JOB_ID_REMIND = "refresh_maintain_remind"


class SchedulerManager:
    """
    调度器管理器（进程级单例）。

    为什么用 BackgroundScheduler：
    它以守护线程方式运行，与 FastAPI 的 uvicorn 主进程共存，
    不需要额外的进程管理（如 systemd 单独起一个 worker），
    符合"Windows/Linux 一键部署"的要求。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._started_at: Optional[datetime] = None

    # ---------------- 生命周期 ----------------

    def start(self) -> bool:
        """
        启动调度器并注册全部任务。

        :return: 是否本次真正启动（已在运行则返回 False，保证幂等）
        """
        with self._lock:
            if self._scheduler is not None and self._scheduler.running:
                system_logger.info("调度器已在运行，跳过重复启动")
                return False

            silence_noisy_loggers()   # 抑制 APScheduler 自身的 INFO 刷屏

            self._scheduler = BackgroundScheduler(
                jobstores={"default": MemoryJobStore()},
                # 调度器自身线程池只需跑"投递任务"这种轻量工作，5 个线程足够；
                # 真正的采集并发由 app.tasks.executor 的采集线程池承担
                executors={"default": APSThreadPoolExecutor(max_workers=5)},
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": 60,
                },
                timezone="Asia/Shanghai",
            )

            self._register_jobs()
            self._load_custom_tasks()
            self._scheduler.start()
            self._started_at = datetime.now()
            system_logger.info(
                "调度器已启动，共注册 %s 个任务：%s",
                len(self._scheduler.get_jobs()),
                [job.id for job in self._scheduler.get_jobs()],
            )
            return True

    def shutdown(self, wait: bool = False) -> None:
        """关闭调度器（平台退出时调用）"""
        with self._lock:
            if self._scheduler is not None and self._scheduler.running:
                self._scheduler.shutdown(wait=wait)
                system_logger.info("调度器已关闭")
            self._scheduler = None
            self._started_at = None

    # ---------------- 任务注册 ----------------

    def _register_jobs(self) -> None:
        """注册全部内置任务"""
        assert self._scheduler is not None

        # ---- 1. 采集轮询（核心）----
        # 采用容错导入：采集任务模块在后续交付步骤中落地，
        # 缺失时平台仍可正常启动（只是不采集），便于分阶段部署与排障。
        try:
            from app.scheduler.jobs.collect_job import run_collect_job

            self._scheduler.add_job(
                run_collect_job,
                trigger=IntervalTrigger(seconds=COLLECT_TICK_SECONDS),
                id=JOB_ID_COLLECT,
                name="设备采集轮询",
                replace_existing=True,
            )
        except ImportError as exc:
            system_logger.warning("采集任务模块未加载，采集功能暂不可用：%s", exc)

        # ---- 2. 数据清理（每日 03:30 低峰期）----
        from app.scheduler.jobs.cleanup_job import run_cleanup_job

        self._scheduler.add_job(
            run_cleanup_job,
            trigger=CronTrigger(hour=3, minute=30),
            id=JOB_ID_CLEANUP,
            name="指标降采样与日志清理",
            replace_existing=True,
        )

        # ---- 3. 维保到期提醒（每日 08:00）----
        from app.scheduler.jobs.remind_job import run_refresh_remind_job

        self._scheduler.add_job(
            run_refresh_remind_job,
            trigger=CronTrigger(hour=8, minute=0),
            id=JOB_ID_REMIND,
            name="资产维保到期提醒刷新",
            replace_existing=True,
        )

    # ---------------- 手动触发 ----------------

    # ========================================================================
    # 用户自定义定时任务（sys_scheduled_task 表驱动，界面可增删改）
    # ========================================================================

    CUSTOM_JOB_PREFIX = "custom_"

    def _build_trigger(self, task) -> Any:
        """
        根据任务行构造 APScheduler 触发器。

        校验放这里而不是 API 层的原因：cron 表达式的最终解释权在 APScheduler，
        这里用 from_crontab 实际构造一次，非法表达式在"保存并生效"这一步就报错，
        而不是等到下次调度才无声失败。
        """
        if task.trigger_type == "cron":
            expr = (task.cron_expr or "").strip()
            if not expr:
                raise ValueError("trigger_type=cron 时必须填写 cron 表达式")
            return CronTrigger.from_crontab(expr, timezone="Asia/Shanghai")
        # interval：最小间隔由 Schema 层约束，这里只做兜底
        seconds = task.interval_seconds or 0
        if seconds < 10:
            raise ValueError("固定间隔不能小于 10 秒")
        return IntervalTrigger(seconds=seconds, timezone="Asia/Shanghai")

    def _load_custom_tasks(self) -> None:
        """调度器启动时加载全部已启用的自定义任务（建表失败/库不可用时降级不阻塞启动）"""
        assert self._scheduler is not None
        try:
            from app.core.database import session_scope
            from app.models import SysScheduledTask
            with session_scope() as db:
                tasks = db.query(SysScheduledTask).filter(SysScheduledTask.enabled == True).all()  # noqa: E712
                rows = [(t.id, t.name) for t in tasks]
        except Exception as exc:  # noqa: BLE001 —— 首次部署表可能尚未创建
            system_logger.warning("自定义定时任务加载失败（不影响平台启动）：%s", exc)
            return
        for task_id, _name in rows:
            self._apply_custom_job(task_id)

    def _apply_custom_job(self, task_id: int) -> None:
        """
        把一条自定义任务同步为调度器 job（存在则覆盖，禁用则移除）。

        CRUD 之后调用它即可"热更新"，无需重启平台；
        job_id 用 custom_{id} 前缀与内置任务隔离，永不冲突。
        """
        if self._scheduler is None or not self._scheduler.running:
            return
        job_id = f"{self.CUSTOM_JOB_PREFIX}{task_id}"
        with self._lock:
            try:
                from app.core.database import session_scope
                from app.models import SysScheduledTask
                with session_scope() as db:
                    task = db.get(SysScheduledTask, task_id)
                    if task is None or not task.enabled:
                        # 任务被删/停用：把已注册的 job 摘掉
                        existing = self._scheduler.get_job(job_id)
                        if existing is not None:
                            existing.remove()
                            system_logger.info("自定义定时任务已从调度器移除：%s", job_id)
                        return
                    from app.scheduler.custom_tasks import run_task_by_id
                    trigger = self._build_trigger(task)
                    self._scheduler.add_job(
                        run_task_by_id,
                        args=[task_id],
                        trigger=trigger,
                        id=job_id,
                        name=f"自定义：{task.name}",
                        replace_existing=True,
                    )
                system_logger.info("自定义定时任务已注册：%s", job_id)
            except Exception as exc:  # noqa: BLE001 —— 配置错误不能拖垮调度器
                system_logger.error("自定义定时任务 %s 注册失败：%s", task_id, exc)

    def remove_custom_job(self, task_id: int) -> None:
        """删除任务时移除对应 job（幂等：job 不存在也不报错）"""
        if self._scheduler is None or not self._scheduler.running:
            return
        job_id = f"{self.CUSTOM_JOB_PREFIX}{task_id}"
        with self._lock:
            job = self._scheduler.get_job(job_id)
            if job is not None:
                job.remove()
                system_logger.info("自定义定时任务已删除：%s", job_id)

    def refresh_custom_jobs(self) -> None:
        """全量重载自定义任务（任务表发生批量变化时使用）"""
        if self._scheduler is None or not self._scheduler.running:
            return
        # 先摘掉全部旧 job 再重新加载，保证"删掉的不再跑、改过的用新定义"
        with self._lock:
            for job in list(self._scheduler.get_jobs()):
                if str(job.id).startswith(self.CUSTOM_JOB_PREFIX):
                    job.remove()
        self._load_custom_tasks()

    def trigger_custom_task(self, task_id: int) -> bool:
        """
        立即执行一条自定义任务（"立即执行"按钮）。

        与内置任务不同：自定义任务可能处于"已停用"状态（调度器里没有 job），
        所以这里不走 next_run_time 调度，而是直接起一个守护线程跑一次——
        这样"停用的任务也能手动验证一次"，排障时很关键。
        执行记录（last_run_*）由 run_task_by_id 内部统一回写，两条路径行为一致。
        """
        from app.scheduler.custom_tasks import run_task_by_id
        from app.core.logger import system_logger as _log

        if self._scheduler is None or not self._scheduler.running:
            _log.warning("调度器未运行，自定义任务 %s 立即执行请求被拒绝", task_id)
            return False
        threading.Thread(
            target=run_task_by_id, args=(task_id, "manual"),
            name=f"manual-task-{task_id}", daemon=True,
        ).start()
        _log.info("手动触发自定义定时任务 %s", task_id)
        return True

    def custom_task_next_run(self, task_id: int) -> Optional[str]:
        """查询自定义任务在调度器中的下次执行时间（未启用/调度器未运行返回 None）"""
        if self._scheduler is None or not self._scheduler.running:
            return None
        job = self._scheduler.get_job(f"{self.CUSTOM_JOB_PREFIX}{task_id}")
        if job is None or getattr(job, "next_run_time", None) is None:
            return None
        return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")

    def trigger_job(self, job_id: str) -> bool:
        """
        立即手动执行某任务（运维排障用："我改完配置，想立刻跑一次清理"）。

        实现方式：把 next_run_time 设为当前时间，由 APScheduler 调度执行。
        """
        with self._lock:
            if self._scheduler is None or not self._scheduler.running:
                return False
            job = self._scheduler.get_job(job_id)
            if job is None:
                return False
            job.modify(next_run_time=datetime.now())
            system_logger.info("手动触发任务 %s", job_id)
            return True

    # ---------------- 状态查询 ----------------

    @property
    def running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    @property
    def started_at(self) -> Optional[datetime]:
        return self._started_at

    def list_jobs(self) -> List[Dict[str, Any]]:
        """
        任务列表（系统配置页"定时任务"面板展示）。

        返回字段与前端表格列一一对应：
          job_id / name / description / interval_seconds / cron_expr /
          next_run_at / is_custom
        其中 is_custom=True 的行来自 sys_scheduled_task 表，前端据此渲染
        编辑/启停/删除按钮（内置任务只能立即执行）。
        """
        if self._scheduler is None:
            return []
        jobs = []

        # ---- 内置任务：从 APScheduler 实例读取（周期写死在代码里） ----
        builtin_desc = {
            JOB_ID_COLLECT: f"每 {COLLECT_TICK_SECONDS} 秒扫描到点设备并投递采集",
            JOB_ID_CLEANUP: "每日 03:30 低峰期清理指标明细与过期日志",
            JOB_ID_REMIND: "每日 08:00 刷新资产维保到期提醒",
        }
        for job in self._scheduler.get_jobs():
            jid = str(job.id)
            if jid.startswith(self.CUSTOM_JOB_PREFIX):
                continue  # 自定义任务在下面统一从库里读，避免两处定义
            next_run = getattr(job, "next_run_time", None)
            jobs.append({
                "job_id": jid,
                "name": job.name,
                "description": builtin_desc.get(jid, ""),
                "interval_seconds": COLLECT_TICK_SECONDS if jid == JOB_ID_COLLECT else None,
                "cron_expr": None,
                "next_run_at": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None,
                "is_custom": False,
            })

        # ---- 自定义任务：以数据库为准（定义/备注/周期都在库里），下次执行时间查调度器 ----
        try:
            from app.core.database import session_scope
            from app.models import SysScheduledTask
            with session_scope() as db:
                rows = db.query(SysScheduledTask).order_by(SysScheduledTask.id).all()
                for t in rows:
                    jobs.append({
                        "job_id": f"{self.CUSTOM_JOB_PREFIX}{t.id}",
                        "name": t.name,
                        "description": t.remark or "",
                        "interval_seconds": t.interval_seconds if t.trigger_type == "interval" else None,
                        "cron_expr": t.cron_expr if t.trigger_type == "cron" else None,
                        "next_run_at": self.custom_task_next_run(t.id) if t.enabled else None,
                        "is_custom": True,
                        "enabled": t.enabled,
                        "task_id": t.id,
                        "last_run_at": t.last_run_at.strftime("%Y-%m-%d %H:%M:%S") if t.last_run_at else None,
                        "last_run_ok": t.last_run_ok,
                        "last_run_message": t.last_run_message,
                        "action": t.action,
                    })
        except Exception as exc:  # noqa: BLE001 —— 表未创建等场景不阻塞状态展示
            system_logger.warning("读取自定义定时任务列表失败：%s", exc)
        return jobs

    def status(self) -> Dict[str, Any]:
        """调度器整体状态"""
        return {
            "running": self.running,
            "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S") if self._started_at else None,
            "job_count": len(self.list_jobs()),
            "collect_tick_seconds": COLLECT_TICK_SECONDS,
        }


# 进程级单例
_manager: Optional[SchedulerManager] = None
_manager_lock = threading.Lock()


def get_scheduler_manager() -> SchedulerManager:
    """获取调度器管理器单例"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = SchedulerManager()
    return _manager


def start_scheduler() -> bool:
    """
    启动调度器（由 main.py 的生命周期钩子调用）。

    受配置 SCHEDULER_ENABLED 控制：
    多实例部署或只想调试 API 时可关闭，避免多个进程重复采集同一批设备。
    """
    if not settings.SCHEDULER_ENABLED:
        system_logger.warning("SCHEDULER_ENABLED=false，采集调度器未启动")
        return False
    return get_scheduler_manager().start()


def stop_scheduler() -> None:
    """停止调度器"""
    get_scheduler_manager().shutdown(wait=True)


def get_scheduler_status() -> Dict[str, Any]:
    """调度器状态（供 API 返回）"""
    return get_scheduler_manager().status()
