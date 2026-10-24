"""
大屏接口（模块4：可视化大屏数据供给）
===============================================================================
**权限设计**

本模块所有接口都需要登录（`get_current_user`），与其它业务接口一致。理由：

  1. 大屏展示的是**完整的资产与设备清单**（IP、型号、在线状态、告警内容）——
     这是一份攻击者非常想要的资产测绘报告，不能匿名可读。
  2. 实践中的做法是给大屏单独开一个 **viewer 只读账号**登录一次，
     token 有效期由 `ACCESS_TOKEN_EXPIRE_MINUTES` 控制（默认 12 小时）——
     正好覆盖一个班次，交接班时顺带刷新一次登录。
  3. 前端页面（`web/dashboard.html`）会处理 401：弹出登录浮层而不是白屏，
     并明确提示"登录已过期"，避免运维误以为是大屏坏了。

**刷新节奏（前后端约定）**

  | 接口 | 内容 | 建议刷新间隔 | 理由 |
  | --- | --- | --- | --- |
  | `/dashboard/bundle` | 整屏（总览+设备+趋势+分布） | 首屏一次 | 避免"拼图式"逐块出现 |
  | `/dashboard/overview` | 顶部 KPI + 状态分布 | 10 秒 | 变化最快，要"看起来是实时的" |
  | `/dashboard/devices` | 设备状态灯矩阵 | 30 秒 | 设备状态变化远慢于指标 |
  | `/dashboard/alert-trend` | 告警趋势 | 60 秒 | 按小时分桶，10 秒刷毫无意义 |
  | `/dashboard/rank` | TOP N 排行榜 | 30 秒 | 与设备矩阵同步即可 |
  | `/dashboard/distribution` | 分布类环形图 | 300 秒 | 几乎不变（设备类型/厂商是台账属性）|

  上面的间隔由前端在页面里用一个刷新器统一调度，后端不做限制；
  这里写清楚是为了让后续调优有依据，而不是"拍脑袋改成 5 秒"。
===============================================================================
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import Resp, resp_ok
from app.models.user import SysUser
from app.schemas.dashboard import (
    AlertTrendOut,
    DashboardOverviewOut,
    DeviceCellOut,
    DistributionOut,
    RankOut,
)
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["可视化大屏"])


@router.get("/overview", response_model=Resp[DashboardOverviewOut], summary="大屏总览（顶部 KPI）")
def overview(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    大屏顶部指标卡与状态分布。单次响应约 2 KB，可 10 秒刷新。

    返回的每张卡片都已带好 `level` 与 `color`，前端不做任何阈值判断 ——
    否则"离线设备该显示红色还是橙色"这件事就会有两处定义。
    """
    return resp_ok(dashboard_service.kpi_summary(db))


@router.get("/devices", response_model=Resp[List[DeviceCellOut]], summary="大屏设备状态灯矩阵")
def devices(
    limit: int = Query(default=200, ge=1, le=1000, description="最多返回多少台设备"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    设备状态灯矩阵（大屏中部）。

    返回的 `status` 是**四态**：online / offline / unknown / **disabled**。
    注意 disabled 只在显示层存在 —— 停用采集的设备在库里仍是三态之一，
    若大屏直接把库里的 online_status 画出来，一台"已停用采集"的设备会显示成"在线"。
    """
    return resp_ok(dashboard_service.device_grid(db, limit=limit))


@router.get("/alert-trend", response_model=Resp[AlertTrendOut], summary="告警趋势（按小时分桶）")
def alert_trend(
    hours: int = Query(default=24, ge=1, le=336, description="统计窗口（小时），上限 14 天"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    近 N 小时告警趋势。

    分桶在 Python 里做而不是用 SQL 的 `strftime` / `DATE_FORMAT`：
    后者是两种数据库方言，会破坏"SQLite ↔ MySQL 只改 DATABASE_URL"这条约定，
    而且这种破坏在 SQLite 上测不出来。详见 `dashboard_service` 的模块说明。
    """
    return resp_ok(dashboard_service.alert_trend(db, hours=hours))


@router.get("/rank", response_model=Resp[RankOut], summary="指标排行榜（TOP N）")
def rank(
    metric_key: str = Query(default="cpu_usage", description="指标键，如 cpu_usage / ping_latency"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    某指标的值最高设备排行（大屏右侧 TOP N）。

    端口类指标（如 `if_in_rate`）一台设备只占一行，取该设备**所有端口的最大值**，
    并在 `metric_key` 中返回具体端口名。判断规则集中在本接口的实现里，
    避免前后端各有一套"多端口怎么合并"的口径。
    """
    return resp_ok(dashboard_service.metric_rank(db, metric_key=metric_key, limit=limit))


@router.get("/distribution", response_model=Resp[DistributionOut], summary="分布统计（环形图）")
def distribution(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    设备类型 / 资产状态 / 告警级别 / 厂商 分布。

    几乎不变，前端 5 分钟刷一次即可 —— 它是台账属性，不是实时指标。
    """
    return resp_ok(dashboard_service.distribution(db))


@router.get("/bundle", summary="整屏打包（首屏一次拿全）")
def bundle(
    hours: int = Query(default=24, ge=1, le=336, description="趋势窗口（小时）"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    整屏数据打包：`{overview, devices, trend, distribution}`。

    大屏首屏只发这一个请求，好处是**要么整屏可用、要么整屏报同一个错**，
    不会出现"三块有数据、一块空白"这种让人无法判断是"真没数据"还是"接口挂了"的状态。
    """
    return resp_ok(dashboard_service.dashboard_bundle(db, hours=hours))
