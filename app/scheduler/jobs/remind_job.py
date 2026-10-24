"""
定时任务：资产维保到期提醒刷新（每日 08:00）
===============================================================================
为什么放 08:00：
  运维上班第一时间打开平台就能看到"本月有 3 台设备维保到期"，
  而不是等到下午任务跑完才看到——提醒类任务的价值在于"及时被看到"。

为什么不实时计算：
  大屏容量视图要求秒级响应，实时算需要扫全表并做日期比较；
  每日刷新一次写入 asset_maintain_remind 表后，查询变成对几十行的小表读取。
===============================================================================
"""
from __future__ import annotations

from app.core.database import session_scope
from app.core.logger import system_logger
from app.services import asset_service, log_service

# 提前提醒天数（覆盖"已过期"与"90 天内到期"两种情况，
# 与 asset_service.refresh_maintain_remind 的分级规则保持一致）
REMIND_DAYS = 90


def run_refresh_remind_job() -> dict:
    """
    刷新维保提醒。

    :return: {"count": 生成/更新的提醒条数}
    """
    try:
        with session_scope() as db:
            count = asset_service.refresh_maintain_remind(db, remind_days=REMIND_DAYS)
            log_service.write_sys_log(
                db=db, level="INFO", module="scheduler", action="refresh_remind",
                message=f"维保到期提醒刷新完成，共 {count} 条",
            )
        system_logger.info("维保提醒刷新完成，共 %s 条", count)
        return {"count": count}

    except Exception as exc:
        system_logger.exception("维保提醒刷新失败：%s", exc)
        try:
            with session_scope() as db:
                log_service.write_sys_log(
                    db=db, level="ERROR", module="scheduler", action="refresh_remind",
                    message=f"维保提醒刷新失败：{exc}",
                )
        except Exception:  # pragma: no cover
            pass
        return {"error": str(exc)}
