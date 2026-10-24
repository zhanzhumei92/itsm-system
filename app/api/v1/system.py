"""
系统接口：平台信息、健康检查、运行状态
===============================================================================
用途：
  - /system/info    ：前端登录页标题、大屏页脚、刷新间隔（免登录可访问）
  - /system/health  ：部署后的存活探针（一键部署脚本据此判断启动成功）
  - /system/runtime ：线程池 + 调度器 + 采集器运行状态（运维排障用，仅管理员）
===============================================================================
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, require_role
from app.core.response import Resp, resp_ok
from app.models.alert import AlertEvent, AlertRule
from app.models.device import MonDevice
from app.models.user import SysUser
from app.schemas.system import PlatformInfoOut
from app.services import config_service

router = APIRouter(prefix="/system", tags=["系统信息"])


@router.get("/info", response_model=Resp[PlatformInfoOut], summary="平台基础信息（免登录）")
def get_platform_info(db: Session = Depends(get_db)):
    """
    平台基础信息。

    免登录可访问的原因：登录页需要显示平台名称，
    大屏页脚与刷新间隔也需要在未登录时（或令牌过期后）仍能获取。
    本接口不返回任何敏感数据。
    """
    info = config_service.get_platform_info(db)

    # 附带几个关键统计，便于登录页/大屏页脚展示与"系统是否在跑"的快速判断
    device_total = db.execute(select(func.count(MonDevice.id))).scalar_one() or 0
    device_online = db.execute(
        select(func.count(MonDevice.id)).where(MonDevice.online_status == "online")
    ).scalar_one() or 0
    alert_active = db.execute(
        select(func.count(AlertEvent.id)).where(AlertEvent.status.in_(["firing", "acked"]))
    ).scalar_one() or 0

    from app.scheduler.scheduler import get_scheduler_manager

    return resp_ok(PlatformInfoOut(
        platform_name=info["platform_name"],
        version=settings.APP_VERSION,
        company_name=info["company_name"],
        dashboard_refresh=info["dashboard_refresh"],
        scheduler_running=get_scheduler_manager().running,
        device_total=device_total,
        device_online=device_online,
        alert_active=alert_active,
    ))


@router.get("/health", summary="健康检查（免登录）")
def health_check(db: Session = Depends(get_db)):
    """
    健康检查（部署脚本与监控系统调用）。

    检查项：数据库连通性 + 调度器状态。
    数据库不通时返回 503 语义（status="down"），便于部署脚本判定启动失败。
    """
    from app.core.database import check_db_connection
    from app.scheduler.scheduler import get_scheduler_manager

    db_ok = check_db_connection()
    scheduler_running = get_scheduler_manager().running

    return resp_ok({
        "status": "up" if db_ok else "down",
        "database": "ok" if db_ok else "error",
        "scheduler": "running" if scheduler_running else "stopped",
        "version": settings.APP_VERSION,
    })


@router.get("/runtime", response_model=Resp, summary="运行状态（仅管理员）")
def get_runtime(
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    运行状态详情。

    包含：采集线程池（并发数/正在采集的设备数）、调度器任务、
    采集器已支持协议、以及各类数据的规模统计。
    这是"平台到底在不在干活"最直接的答案。
    """
    from app.collectors import CollectorFactory
    from app.scheduler.scheduler import get_scheduler_manager
    from app.tasks.executor import get_executor

    executor = get_executor()
    manager = get_scheduler_manager()

    return resp_ok({
        "version": settings.APP_VERSION,
        "database": "sqlite" if settings.is_sqlite else "mysql",
        "scheduler": {**manager.status(), "jobs": manager.list_jobs()},
        "executor": executor.stats(),
        "collectors": CollectorFactory.available_protocols(),
        "counts": {
            "device": db.execute(select(func.count(MonDevice.id))).scalar_one() or 0,
            "rule": db.execute(select(func.count(AlertRule.id))).scalar_one() or 0,
            "alert_event": db.execute(select(func.count(AlertEvent.id))).scalar_one() or 0,
        },
    })
