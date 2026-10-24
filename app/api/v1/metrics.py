"""
指标接口（模块5：指标管理 —— 指标字典维护、实时指标批量查询、历史曲线）
===============================================================================
说明：设备维度的指标查询放在 devices.py（/devices/{id}/metrics），
本模块负责"跨设备的指标查询"与"指标字典维护"。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, get_current_user, require_role
from app.core.response import Resp, resp_ok
from app.models.user import SysUser
from app.schemas.metric import (
    MetricCatalogCreate,
    MetricCatalogOut,
    MetricLatestOut,
    MetricSeriesOut,
)
from app.services import metric_service

router = APIRouter(prefix="/metrics", tags=["指标管理"])


# ============================================================================
# 指标字典
# ============================================================================

@router.get("/catalog", response_model=Resp[List[MetricCatalogOut]], summary="指标字典列表")
def list_catalog(
    enabled_only: bool = Query(default=False, description="仅返回已启用的指标"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    指标字典（前端指标选择器、告警规则配置页的数据源）。

    新增采集指标只需在此登记一行元数据（中文名/单位/分类），无需改表结构。
    """
    rows = metric_service.list_catalog(db, enabled_only)
    return resp_ok([MetricCatalogOut.model_validate(row) for row in rows])


@router.post("/catalog", response_model=Resp[MetricCatalogOut], summary="新增自定义指标（仅管理员）")
def create_catalog(
    payload: MetricCatalogCreate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    登记自定义指标元数据。

    注意：这里只登记"元数据"，真正采集到该指标还需要在采集器解析器中
    产出对应的 metric_key（元数据可随时加，解析逻辑需发版）。
    """
    item = metric_service.create_catalog(db, payload)
    return resp_ok(MetricCatalogOut.model_validate(item), msg="指标已登记")


@router.delete("/catalog/{catalog_id}", response_model=Resp, summary="删除自定义指标（仅管理员）")
def delete_catalog(
    catalog_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """内置指标不可删除（大屏与告警规则已引用），如需隐藏请停用"""
    metric_service.delete_catalog(db, catalog_id)
    return resp_ok(msg="指标已删除")


# ============================================================================
# 实时指标（大屏与设备列表用）
# ============================================================================

@router.get("/latest", response_model=Resp[List[MetricLatestOut]], summary="批量取最新指标")
def get_latest(
    device_ids: List[int] = Query(..., description="设备 ID 列表，可重复传参"),
    metric_keys: Optional[List[str]] = Query(default=None, description="指标键列表，不传则取全部"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    批量取多台设备的最新指标。

    大屏与设备列表页都用这一接口，一次请求拿全所需数据，
    避免前端对 200 台设备发起 200 次请求。
    """
    latest_map = metric_service.get_latest_map(db, device_ids, metric_keys)
    catalog = metric_service._load_catalog_map(db)

    items: List[MetricLatestOut] = []
    for (_device_id, metric_key), row in latest_map.items():
        out = MetricLatestOut.model_validate(row)
        base = metric_service.base_metric_key(metric_key)
        meta = catalog.get(base)
        out.metric_name = meta.metric_name if meta else base
        out.category = meta.category if meta else None
        items.append(out)

    # 按设备 + 指标名排序，保证前端展示顺序稳定
    items.sort(key=lambda x: (x.device_id, x.metric_name or ""))
    return resp_ok(items)


# ============================================================================
# 历史曲线
# ============================================================================

@router.get("/history", response_model=Resp[MetricSeriesOut], summary="查询历史曲线")
def get_history(
    device_id: int = Query(..., description="设备 ID"),
    metric_key: str = Query(..., description="指标键"),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    limit: int = Query(default=500, ge=10, le=5000),
    agg: Optional[str] = Query(default=None, description="raw/5min/hour，不传自动选择"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    return resp_ok(metric_service.get_history(db, device_id, metric_key, start, end, limit, agg))


@router.get("/history/multi", response_model=Resp[List[MetricSeriesOut]], summary="查询多条历史曲线")
def get_multi_history(
    device_id: int = Query(..., description="设备 ID"),
    metric_keys: List[str] = Query(..., description="指标键列表"),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    limit: int = Query(default=300, ge=10, le=2000),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    一次取多条曲线（设备详情页同时画 CPU / 内存 / 磁盘 趋势）。

    比前端并发发起 3 个请求更省连接，也保证三条曲线的时间轴一致。
    """
    rows = metric_service.get_multi_history(db, device_id, metric_keys, start, end, limit)
    return resp_ok(rows)
