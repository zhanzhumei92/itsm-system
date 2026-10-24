"""
设备管理接口（模块1、模块5：设备接入、协议参数配置、采集任务启停、测试连接）
===============================================================================
权限策略：
  - 查询类：管理员 + 查看员
  - 增删改、启停、测试连接：仅管理员、仅管理员
    （测试连接会真实登录设备，属于有副作用的操作，查看员不应触发）
===============================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, get_current_user, require_role
from app.core.response import PageData, Resp, build_page, resp_ok
from app.models.user import SysUser
from app.schemas.device import (
    DeviceCreate,
    DeviceDetailOut,
    DeviceOut,
    DeviceSimpleOut,
    DeviceUpdate,
    TestConnectionResult,
)
from app.schemas.metric import DeviceMetricsOut, MetricSeriesOut
from app.schemas.system import CollectLogOut
from app.schemas.common import IdsRequest, OpResult
from app.services import device_service, log_service, metric_service

router = APIRouter(prefix="/devices", tags=["设备管理"])


@router.get("/protocols", response_model=Resp, summary="支持的采集协议列表")
def list_protocols(user: SysUser = Depends(get_current_user)):
    """
    返回当前平台已实现采集器的协议。

    前端"新增设备"表单据此渲染协议下拉框，
    未实现的协议不会出现在选项中，避免用户配置了却采不到数据。
    """
    from app.collectors import CollectorFactory

    supported = CollectorFactory.available_protocols()
    labels = {
        "snmp": "SNMP（交换机/服务器通用指标）",
        "ssh": "SSH（华为/华三/信创主机低频信息）",
        "winrm": "WinRM（Windows Server）",
        "icmp": "ICMP 长 Ping（在线状态/延迟/丢包）",
    }
    return resp_ok([{"value": p, "label": labels.get(p, p)} for p in supported])


@router.get("", response_model=Resp[PageData[DeviceOut]], summary="设备列表")
def list_devices(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    keyword: Optional[str] = Query(default=None, description="名称/IP/位置模糊搜索"),
    protocol: Optional[str] = Query(default=None),
    device_type: Optional[str] = Query(default=None),
    online_status: Optional[str] = Query(default=None, description="online/offline/unknown"),
    enabled: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    rows, total = device_service.list_devices(
        db, page, page_size, keyword, protocol, device_type, online_status, enabled
    )
    items = [DeviceOut.model_validate(row) for row in rows]
    return resp_ok(build_page(items, total, page, page_size))


@router.get("/simple", response_model=Resp[List[DeviceSimpleOut]], summary="设备精简列表")
def list_devices_simple(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    全部设备精简信息（供告警规则、指标查询页面的设备下拉框使用）。

    最多返回 2000 台：超过此规模应改用搜索接口，
    否则一次响应几 MB 会明显拖慢页面。
    """
    rows, _ = device_service.list_devices(db, page=1, page_size=2000)
    return resp_ok([DeviceSimpleOut.model_validate(row) for row in rows])


@router.get("/{device_id}", response_model=Resp[DeviceDetailOut], summary="设备详情")
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """详情含脱敏后的协议凭据（密钥类字段只返回"是否已配置"）"""
    return resp_ok(device_service.get_device_detail(db, device_id))


@router.post("", response_model=Resp[DeviceDetailOut], summary="新增设备（仅管理员）")
def create_device(
    payload: DeviceCreate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    新增监控设备（设备 + 协议凭据一次提交）。

    凭据中的口令会在服务层用 Fernet 加密后入库，数据库中不存在明文口令。
    """
    result = device_service.create_device(db, payload, operator=operator.username)
    return resp_ok(result, msg="设备接入成功")


@router.put("/{device_id}", response_model=Resp[DeviceDetailOut], summary="编辑设备（仅管理员）")
def update_device(
    device_id: int,
    payload: DeviceUpdate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    编辑设备与凭据。

    凭据中的敏感字段（口令、团体字）留空表示"保持不变"，
    这样前端在无法回显口令的前提下也能安全地修改其它参数。
    """
    result = device_service.update_device(db, device_id, payload, operator=operator.username)
    return resp_ok(result, msg="设备信息已更新")


@router.delete("/{device_id}", response_model=Resp, summary="删除设备（仅管理员）")
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    删除设备。

    会清理凭据、最新指标与资产绑定关系；
    历史采样与日志保留，供事后审计（详见 device_service.delete_device 说明）。

    返回 data 为各类关联数据的清理计数，前端删除成功后据此展示
    "清理凭据 X 条…"的明细提示（service 层返回 None/缺失时前端需自行兜底）。
    """
    counts = device_service.delete_device(db, device_id, operator=operator.username)
    return resp_ok(counts or {}, msg="设备已删除")


@router.post("/{device_id}/toggle", response_model=Resp[DeviceOut], summary="启用/停用采集任务")
def toggle_device(
    device_id: int,
    enabled: bool = Query(..., description="true 启用 / false 停用"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    启用或停用该设备的采集任务（需求：支持启用/停用采集任务）。

    停用后调度器立即跳过该设备，历史数据与告警记录不受影响。
    """
    device = device_service.toggle_device(db, device_id, enabled, operator=operator.username)
    return resp_ok(DeviceOut.model_validate(device), msg="已启用采集" if enabled else "已停用采集")


@router.post("/{device_id}/test", response_model=Resp[TestConnectionResult], summary="测试连接")
def test_connection(
    device_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    测试设备连通性与协议可用性。

    走的是正式采集器（同一套代码路径），因此"测试通过"等价于"采集能成功"。
    测试结果**不写入指标表**，避免污染真实监控曲线。
    """
    result = device_service.test_connection(db, device_id, save_metrics=False)
    return resp_ok(result, msg=result.message)


@router.post("/batch-toggle", response_model=Resp[OpResult], summary="批量启停采集任务")
def batch_toggle(
    payload: IdsRequest,
    enabled: bool = Query(..., description="true 启用 / false 停用"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    批量启停（批量维护时使用）。

    单台失败不中断整批：逐台 try/except，失败原因汇总返回，
    避免"第 3 台设备 ID 不存在"导致后面 50 台都没处理。
    """
    result = OpResult()
    for device_id in payload.ids:
        try:
            device_service.toggle_device(db, device_id, enabled, operator=operator.username)
            result.success += 1
        except Exception as exc:
            result.failed += 1
            result.messages.append(f"设备 {device_id}：{exc}")
    return resp_ok(result, msg=f"成功 {result.success} 台，失败 {result.failed} 台")


# ============================================================================
# 设备维度的指标与日志（模块5：指标管理 —— 单台设备历史记录）
# ============================================================================

@router.get("/{device_id}/metrics", response_model=Resp[DeviceMetricsOut], summary="设备实时指标")
def get_device_metrics(
    device_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """设备当前全部指标最新值（设备详情页"实时指标"卡片）"""
    return resp_ok(metric_service.get_device_metrics(db, device_id))


@router.get("/{device_id}/history", response_model=Resp[MetricSeriesOut], summary="设备指标历史曲线")
def get_device_history(
    device_id: int,
    metric_key: str = Query(..., description="指标键，如 cpu_usage"),
    start: Optional[datetime] = Query(default=None, description="起始时间；与 hours 二选一，同时给出则以本参数为准"),
    end: Optional[datetime] = Query(default=None, description="结束时间；不传则取当前时间"),
    hours: Optional[int] = Query(
        default=None, ge=1, le=8760,
        description="最近 N 小时（1-8760，即一年）；与告警趋势等接口统一使用“最近 N 小时”这一种时间窗口写法",
    ),
    limit: int = Query(default=500, ge=10, le=5000),
    agg: Optional[str] = Query(default=None, description="raw/5min/hour，不传则自动选择"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    历史曲线数据。

    跨度大时自动降采样（≤6 小时原始点、≤4 天 5 分钟均值、更长取小时均值），
    避免一次返回十万个点把浏览器卡死。

    ⚠ **为什么补了 `hours` 这个参数**
    原先本接口只有 start/end，而调用方绝大多数需求是"看最近 N 小时"，
    每次都要自己算发送资料再拼进 URL。更隐蔽的问题是：
    **FastAPI 会静默忽略未声明的 query 参数** —— 于是有人写了 `?hours=2`，
    服务器当没看见、按默认 6 小时窗口返回，既不报错也不符合预期。

    这次就是这样踩到的：回归脚本里写了 `hours=2`，实际根本没生效，
    只是因为演示数据恰好在 6 小时窗口内，连续多轮都"通过"了。
    """
    # hours 与 start/end 的优先级：**显式给了 start 就以 start 为准**，
    # hours 只有在 start 为空时才生效。这样既兼容"看某个固定区间"，
    # 又让最常见的"看最近 N 小时"不必再让调用方自己算时间戳。
    if hours is not None and start is None:
        anchor = end or datetime.now()
        start = anchor - timedelta(hours=hours)
        # end 未给时也要显式带上：否则 get_history 会以 now() 为准，
        # 与这里的 anchor 差几毫秒，虽然不影响结果，但会让排查时多一个变量。
        end = anchor

    return resp_ok(metric_service.get_history(db, device_id, metric_key, start, end, limit, agg))


@router.get("/{device_id}/collect-logs", response_model=Resp[PageData[CollectLogOut]], summary="设备采集日志")
def get_device_collect_logs(
    device_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    status: Optional[str] = Query(default=None, description="success/fail/timeout"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """某台设备的采集记录（排查"为什么这台采不到数据"）"""
    rows, total = log_service.list_collect_logs(
        db, page=page, page_size=page_size, device_id=device_id, status=status
    )
    return resp_ok(build_page(rows, total, page, page_size))


@router.get("/{device_id}/collect-stat", response_model=Resp, summary="设备采集健康度")
def get_device_collect_stat(
    device_id: int,
    hours: int = Query(default=24, ge=1, le=720),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """近 N 小时采集成功率与平均耗时（设备详情页顶部指标卡）"""
    return resp_ok(log_service.device_collect_stat(db, device_id, hours))
