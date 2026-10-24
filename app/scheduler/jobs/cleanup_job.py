"""
定时任务：指标降采样 + 日志清理（每日 03:30）
===============================================================================
为什么放凌晨 03:30：
  降采样涉及"删除 + 批量插入"，是本平台最重的写操作。
  放在业务低峰期执行，避免与白天的采集写、大屏读抢锁（SQLite 尤其敏感）。

为什么"指标清理"与"日志清理"合在一个任务：
  两者都是"按保留期删除历史数据"，同时执行能让磁盘空间的统计口径一致，
  也减少一次调度开销。但它们的保留期配置独立（指标 7/30/90 天，日志 30 天）。
===============================================================================
"""
from __future__ import annotations

from app.core.config import settings
from app.core.database import session_scope
from app.core.logger import system_logger
from app.services import config_service, log_service, metric_service


def run_cleanup_job() -> dict:
    """
    执行清理任务。

    :return: 清理结果摘要（写入系统日志，便于事后确认任务是否正常执行）
    """
    started = __import__("time").time()
    summary: dict = {}

    try:
        # 用 session_scope 保证异常时回滚并释放连接
        with session_scope() as db:
            # ---- 1. 指标降采样 ----
            retention = config_service.get_retention_settings(db)
            metric_result = metric_service.cleanup_samples(db, **retention)
            summary.update(metric_result)

            # ---- 2. 日志清理 ----
            log_result = log_service.cleanup_logs(db, keep_days=settings.LOG_KEEP_DAYS)
            summary["log_deleted"] = log_result

            # ---- 3. 记录本次执行 ----
            log_service.write_sys_log(
                db=db,
                level="INFO",
                module="scheduler",
                action="cleanup_job",
                message=f"数据清理完成：{summary}",
            )

        elapsed = int((__import__("time").time() - started) * 1000)
        summary["duration_ms"] = elapsed
        system_logger.info("清理任务执行完成，耗时 %sms：%s", elapsed, summary)
        return summary

    except Exception as exc:
        # 定时任务的异常必须自行捕获：未捕获异常会让 APScheduler 打印堆栈但不影响其它任务，
        # 我们主动记录到系统日志，便于在平台页面上看到"清理任务失败"
        system_logger.exception("清理任务执行失败：%s", exc)
        try:
            with session_scope() as db:
                log_service.write_sys_log(
                    db=db, level="ERROR", module="scheduler", action="cleanup_job",
                    message=f"数据清理失败：{exc}",
                )
        except Exception:  # pragma: no cover
            pass
        return {"error": str(exc)}
