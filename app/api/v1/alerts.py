"""
告警接口（模块3：告警规则配置、告警中心、手动确认、通知记录）
===============================================================================
路由顺序注意事项（FastAPI 路由按定义顺序匹配）：
  字面量路径（如 /events/batch-ack）**必须**定义在参数化路径（/events/{event_id}）之前。
  否则 "batch-ack" 会被当作 event_id 去转 int，直接返回 422 参数错误。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, ROLE_VIEWER, get_current_user, require_role
from app.core.response import PageData, Resp, build_page, resp_ok
from app.models.user import SysUser
from app.schemas.alert import (
    AlertAckRequest,
    AlertEventOut,
    AlertNotifyRecordOut,
    AlertOverviewOut,
    AlertRuleCreate,
    AlertRuleOut,
    AlertRuleUpdate,
    AlarmLogOut,
)
from app.schemas.common import IdsRequest, OpResult
from app.services import alert_service

router = APIRouter(prefix="/alerts", tags=["告警管理"])


# ============================================================================
# 告警规则
# ============================================================================

@router.get("/rules", response_model=Resp[PageData[AlertRuleOut]], summary="告警规则列表")
def list_rules(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: Optional[str] = Query(default=None),
    device_id: Optional[int] = Query(default=None, description="按设备筛选"),
    level: Optional[str] = Query(default=None, description="critical/major/minor/info"),
    enabled: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    规则列表。

    返回结果附带 firing_device_count（该规则当前触发中的设备数），
    运维一眼就能看出"哪条规则最容易报"，便于反过来调阈值。
    """
    items, total = alert_service.list_rules(db, page, page_size, keyword, device_id, level, enabled)
    return resp_ok(build_page(items, total, page, page_size))


@router.get("/rules/{rule_id}", response_model=Resp[AlertRuleOut], summary="规则详情")
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    rule = alert_service.get_rule(db, rule_id)
    return resp_ok(alert_service._attach_rule_meta(db, [rule])[0])


@router.post("/rules", response_model=Resp[AlertRuleOut], summary="新增规则（仅管理员）")
def create_rule(
    payload: AlertRuleCreate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    新增告警规则。

    device_id 留空表示全局规则（对全部设备生效）；
    某台设备需要特殊阈值时，再单独建一条绑定该设备的规则。
    """
    return resp_ok(alert_service.create_rule(db, payload), msg="规则已创建")


@router.put("/rules/{rule_id}", response_model=Resp[AlertRuleOut], summary="编辑规则（仅管理员）")
def update_rule(
    rule_id: int,
    payload: AlertRuleUpdate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    编辑规则。

    注意：修改阈值或比较符会自动重置该规则的连续计数器，
    避免"改完阈值立刻触发告警"造成现场误解（详见 alert_service 说明）。
    """
    return resp_ok(alert_service.update_rule(db, rule_id, payload), msg="规则已更新")


@router.delete("/rules/{rule_id}", response_model=Resp, summary="删除规则（仅管理员）")
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """删除规则会同时清理其运行状态；历史告警事件保留供审计"""
    alert_service.delete_rule(db, rule_id)
    return resp_ok(msg="规则已删除")


@router.post("/rules/{rule_id}/toggle", response_model=Resp[AlertRuleOut], summary="启停规则")
def toggle_rule(
    rule_id: int,
    enabled: bool = Query(..., description="true 启用 / false 停用"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    rule = alert_service.toggle_rule(db, rule_id, enabled)
    return resp_ok(alert_service._attach_rule_meta(db, [rule])[0],
                   msg="规则已启用" if enabled else "规则已停用")


@router.post("/rules/{rule_id}/silence", response_model=Resp[AlertRuleOut], summary="设置静默期")
def silence_rule(
    rule_id: int,
    minutes: int = Query(..., ge=1, le=10080, description="静默时长（分钟），最长 7 天"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    设置静默期（设备检修窗口用）。

    静默期内规则仍会评估并更新状态，只是不推送通知；
    检修结束后若指标确实异常，会立刻收到告警而不会被吞掉。
    """
    rule = alert_service.silence_rule(db, rule_id, minutes)
    return resp_ok(alert_service._attach_rule_meta(db, [rule])[0],
                   msg=f"已静默 {minutes} 分钟")


# ============================================================================
# 告警事件（告警中心）
# ============================================================================

@router.get("/overview", response_model=Resp[AlertOverviewOut], summary="告警总览统计")
def get_overview(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """告警总数 / 未处理数 / 今日新增 / 分级统计（大屏顶部卡片 + 告警中心头部）"""
    return resp_ok(alert_service.get_overview(db))


@router.get("/latest", response_model=Resp[List[AlertEventOut]], summary="最新告警（大屏滚动字幕）")
def get_latest(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    最新未恢复告警。

    只返回 firing / acked 状态：已恢复的告警留在滚动条上会让现场领导误判"还在故障"。
    """
    return resp_ok(alert_service.get_latest_events(db, limit))


@router.get("/events", response_model=Resp[PageData[AlertEventOut]], summary="告警事件列表")
def list_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    level: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None, description="firing/acked/recovered/closed"),
    device_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None, description="设备名/IP/标题模糊搜索"),
    start: Optional[datetime] = Query(default=None),
    end: Optional[datetime] = Query(default=None),
    only_unhandled: bool = Query(default=False, description="只看未处理（告警中+已确认）"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """告警中心主列表（告警列表、已处理/未处理筛选、告警历史）"""
    items, total = alert_service.list_events(
        db, page, page_size, level, status, device_id, keyword, start, end, only_unhandled
    )
    return resp_ok(build_page(items, total, page, page_size))


@router.post("/events/batch-ack", response_model=Resp[OpResult], summary="批量确认告警")
def batch_ack(
    payload: IdsRequest,
    remark: Optional[str] = Query(default=None, description="处理备注"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(require_role(ROLE_ADMIN, ROLE_VIEWER)),
):
    """
    批量确认（值班交接时一次处理多条）。

    允许查看员操作：一线值班人员需要能标记"已看到"，这是他们的日常工作。
    但查看员不能关闭事件（关闭属于管理动作，见 /events/{id}/ack 的 close 参数）。
    """
    count = alert_service.batch_ack_events(db, payload.ids, user.username, remark or "")
    return resp_ok(OpResult(success=count), msg=f"已确认 {count} 条告警")


@router.get("/events/{event_id}", response_model=Resp[AlertEventOut], summary="告警详情")
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    return resp_ok(alert_service.get_event_detail(db, event_id))


@router.post("/events/{event_id}/ack", response_model=Resp[AlertEventOut], summary="确认/关闭告警")
def ack_event(
    event_id: int,
    payload: AlertAckRequest,
    db: Session = Depends(get_db),
    user: SysUser = Depends(require_role(ROLE_ADMIN, ROLE_VIEWER)),
):
    """
    手动标记已确认（需求：手动标记已确认）。

    - close=false：标记为"已确认"，正在处理，设备恢复前不再重复推送
    - close=true ：直接关闭（误报、或已人工处理完毕）
    """
    result = alert_service.ack_event(db, event_id, payload, user.username)
    return resp_ok(result, msg="已关闭" if payload.close else "已确认")


@router.get("/events/{event_id}/timeline", response_model=Resp[List[AlarmLogOut]], summary="告警处理时间线")
def get_timeline(
    event_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """触发 → 通知 → 确认 → 恢复 全过程留痕（告警详情页时间线）"""
    return resp_ok(alert_service.get_event_timeline(db, event_id))


@router.get(
    "/events/{event_id}/notify-records",
    response_model=Resp[List[AlertNotifyRecordOut]],
    summary="告警通知记录",
)
def get_notify_records(
    event_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    各渠道推送结果（邮件成功 / 企微失败 等）。

    用途：出现"怎么没收到告警"时，先看这里定位是哪个渠道没送到，
    而不是盲目怀疑平台。
    """
    return resp_ok(alert_service.list_notify_records(db, event_id))
