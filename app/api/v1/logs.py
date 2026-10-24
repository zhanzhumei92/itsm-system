"""
日志查询接口（非功能需求：系统日志、采集日志、告警日志落地保存、可查询）
===============================================================================
性能约束：
  日志表是唯一可能到百万级的表，所有查询都强制时间窗（默认最近 24 小时），
  并且必须分页。这样即便现场运行一年、日志几百万行，页面也不会卡住。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import PageData, Resp, build_page, resp_ok
from app.models.user import SysUser
# AlarmLogOut 属于告警域 Schema，从 schemas.alert 导入（保持域边界清晰）
from app.schemas.alert import AlarmLogOut
from app.schemas.system import (
    CollectLogOut,
    CollectStatOut,
    SysLogOut,
)
from app.services import log_service

router = APIRouter(prefix="/logs", tags=["日志查询"])


@router.get("/system", response_model=Resp[PageData[SysLogOut]], summary="系统日志")
def list_sys_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    level: Optional[str] = Query(default=None, description="INFO/WARNING/ERROR"),
    module: Optional[str] = Query(default=None, description="来源模块，如 auth/device"),
    keyword: Optional[str] = Query(default=None),
    start: Optional[datetime] = Query(default=None, description="默认最近 24 小时"),
    end: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """系统日志（登录记录、操作记录、异常信息）"""
    rows, total = log_service.list_sys_logs(db, page, page_size, level, module, keyword, start, end)
    return resp_ok(build_page(rows, total, page, page_size))


@router.get("/collect", response_model=Resp[PageData[CollectLogOut]], summary="采集日志")
def list_collect_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    device_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None, description="success/fail/timeout"),
    protocol: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="设备名/IP/错误信息"),
    start: Optional[datetime] = Query(default=None, description="默认最近 24 小时"),
    end: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    采集日志（含耗时与错误原因）。

    排查"某台设备为什么采不到数据"的入口：
    先按 status=fail 过滤，再看 error_msg 定位是网络、凭据还是协议问题。
    """
    rows, total = log_service.list_collect_logs(
        db, page, page_size, device_id, status, protocol, keyword, start, end
    )
    return resp_ok(build_page(rows, total, page, page_size))


@router.get("/collect-stat", response_model=Resp[CollectStatOut], summary="采集健康度统计")
def collect_stat(
    hours: int = Query(default=24, ge=1, le=720, description="统计最近 N 小时"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """整体采集成功率与平均耗时（采集日志页顶部指标卡）"""
    return resp_ok(log_service.collect_stat(db, hours))


@router.get("/alarm", response_model=Resp[PageData[AlarmLogOut]], summary="告警处理日志")
def list_alarm_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    device_id: Optional[int] = Query(default=None),
    action: Optional[str] = Query(default=None, description="trigger/recover/ack/notify/close"),
    start: Optional[datetime] = Query(default=None, description="默认最近 24 小时"),
    end: Optional[datetime] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """告警全链路日志（触发、恢复、确认、通知、收敛）"""
    rows, total = log_service.list_alarm_logs(db, page, page_size, device_id, action, start, end)
    return resp_ok(build_page(rows, total, page, page_size))
