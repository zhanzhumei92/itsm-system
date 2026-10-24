"""
系统配置接口（模块5：统一配置邮件/企微/钉钉/微信密钥、全局采集参数、定时任务管理）
===============================================================================
安全约定：
  - 敏感配置项（SMTP 口令、机器人 Webhook、CorpSecret）接口一律返回 "***"，
    永不回传明文；前端保存时把未修改的敏感项原样提交为 "***" 表示"不修改"。
===============================================================================
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, get_current_user, require_role
from app.core.response import Resp, resp_fail, resp_ok
from app.models.user import SysUser
from app.schemas.system import (
    ConfigBatchUpdate,
    ConfigGroupOut,
    ConfigTestRequest,
    ConfigTestResult,
    ScheduledTaskIn,
)
from app.services import config_service

router = APIRouter(prefix="/configs", tags=["系统配置"])


@router.get("", response_model=Resp[List[ConfigGroupOut]], summary="获取全部配置（按分组）")
def list_configs(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    按分组返回全部配置（系统配置页 Tab 数据源）。

    敏感项已脱敏为 *** 或空字符串，前端据 is_configured 显示配置状态点。
    """
    return resp_ok(config_service.list_groups(db))


@router.get("/{group}", response_model=Resp[ConfigGroupOut], summary="获取指定分组配置")
def get_group(
    group: str,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    取单个分组（collect / mail / wecom / dingtalk / wechat / system）。
    分组不存在时返回空列表而非 404，便于前端统一渲染空状态。
    """
    return resp_ok(config_service.get_group(db, group))


@router.put("", response_model=Resp, summary="批量保存配置（仅管理员）")
def update_configs(
    payload: ConfigBatchUpdate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    批量保存配置（前端"保存"按钮一次提交一个分组）。

    保存后立即失效进程内配置缓存，新参数无需重启即可生效。
    """
    count = config_service.update_configs(db, payload.items, operator=operator.username)
    return resp_ok({"updated": count}, msg=f"已保存 {count} 项配置")


@router.post("/test", response_model=Resp[ConfigTestResult], summary="测试告警渠道连通性（仅管理员）")
def test_channel(
    payload: ConfigTestRequest,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    测试告警渠道是否能正常发送。

    为什么必须有这个功能：告警渠道配错（Webhook 少一段、SMTP 授权码过期）
    在真实故障发生前是完全静默的，等到出事才发现收不到告警就来不及了。
    运维配完必须点一次测试。
    """
    from app.alarm.notifier import ChannelFactory

    channel = ChannelFactory.get(payload.group)
    if channel is None:
        return resp_ok(ConfigTestResult(
            success=False, message=f"暂不支持的告警渠道：{payload.group}"
        ))

    result = channel.test(db)
    return resp_ok(ConfigTestResult(**result))


# ============================================================================
# 定时任务管理
# ============================================================================

@router.get("/scheduler/status", response_model=Resp, summary="调度器状态与任务列表")
def scheduler_status(user: SysUser = Depends(get_current_user)):
    """
    采集调度器运行状态（系统配置页"定时任务"面板）。

    运维最常问的是"采集是不是在跑"，此接口直接给出 running / 任务列表 / 下次执行时间。
    """
    from app.scheduler.scheduler import get_scheduler_manager

    manager = get_scheduler_manager()
    return resp_ok({
        **manager.status(),
        "jobs": manager.list_jobs(),
    })


@router.post("/scheduler/trigger", response_model=Resp, summary="立即执行定时任务（仅管理员）")
def trigger_job(
    job_id: str = Query(..., description="任务 ID：collect_tick / cleanup_data / refresh_maintain_remind"),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    立即手动执行某定时任务。

    典型场景：改完数据保留策略想立刻清理一次；新接入设备想立刻采集而不是等下一轮。
    """
    from app.scheduler.scheduler import get_scheduler_manager

    ok = get_scheduler_manager().trigger_job(job_id)
    if not ok:
        return resp_ok({"triggered": False}, msg=f"任务 {job_id} 不存在或调度器未启动")
    return resp_ok({"triggered": True}, msg=f"任务 {job_id} 已触发")


# ============================================================================
# 自定义定时任务管理（sys_scheduled_task 表驱动，界面可增删改，保存即生效）
# ============================================================================

@router.get("/scheduler/actions", response_model=Resp, summary="可用的定时任务动作目录")
def scheduler_actions(user: SysUser = Depends(get_current_user)):
    """
    返回当前平台支持的全部定时任务动作（"新建任务"弹窗下拉数据源）。

    为什么做成接口而不是前端写死：动作列表由后端注册器决定，
    将来新增动作（如 Webhook）前端无需任何改动。
    """
    from app.scheduler.custom_tasks import list_actions
    return resp_ok(list_actions())


@router.get("/scheduler/tasks", response_model=Resp, summary="自定义定时任务列表")
def scheduler_tasks(
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """全部自定义定时任务（含停用），带最近一次执行结果与下次执行时间。"""
    from app.services import scheduled_task_service
    return resp_ok(scheduled_task_service.list_tasks(db))


@router.post("/scheduler/tasks", response_model=Resp, summary="新建自定义定时任务（仅管理员）")
def create_scheduled_task(
    payload: ScheduledTaskIn,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """创建任务并立即注册到调度器（enabled=false 则只保存不调度）。"""
    from app.services import scheduled_task_service
    try:
        task = scheduled_task_service.create_task(db, payload, operator=operator.username)
    except scheduled_task_service.ScheduledTaskError as exc:
        return resp_fail(str(exc))
    return resp_ok(task, msg="任务已创建并生效")


@router.put("/scheduler/tasks/{task_id}", response_model=Resp, summary="编辑自定义定时任务（仅管理员）")
def update_scheduled_task(
    task_id: int,
    payload: ScheduledTaskIn,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """全量更新任务定义（名称/动作/周期/启用状态/备注），保存即重新调度。"""
    from app.services import scheduled_task_service
    try:
        task = scheduled_task_service.update_task(db, task_id, payload, operator=operator.username)
    except scheduled_task_service.ScheduledTaskError as exc:
        return resp_fail(str(exc))
    return resp_ok(task, msg="任务已更新并生效")


@router.post("/scheduler/tasks/{task_id}/toggle", response_model=Resp, summary="启用/停用自定义定时任务（仅管理员）")
def toggle_scheduled_task(
    task_id: int,
    enabled: bool = Query(..., description="true 启用 / false 停用"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """停用=保留定义并摘除调度；启用=重新注册。执行历史保留。"""
    from app.services import scheduled_task_service
    try:
        task = scheduled_task_service.toggle_task(db, task_id, enabled, operator=operator.username)
    except scheduled_task_service.ScheduledTaskError as exc:
        return resp_fail(str(exc))
    return resp_ok(task, msg="已启用" if enabled else "已停用")


@router.delete("/scheduler/tasks/{task_id}", response_model=Resp, summary="删除自定义定时任务（仅管理员）")
def delete_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """删除任务定义并从调度器摘除（最近执行记录随行删除）。"""
    from app.services import scheduled_task_service
    try:
        result = scheduled_task_service.delete_task(db, task_id, operator=operator.username)
    except scheduled_task_service.ScheduledTaskError as exc:
        return resp_fail(str(exc))
    return resp_ok(result, msg="任务已删除")


@router.post("/scheduler/tasks/{task_id}/run", response_model=Resp, summary="立即执行自定义定时任务（仅管理员）")
def run_scheduled_task(
    task_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    手动触发一次（异步执行，结果稍后通过列表的"最近执行"列查看）。

    停用状态的任务也允许手动执行——排障时"先手动跑一次验证，再决定是否启用"。
    """
    from app.services import scheduled_task_service
    try:
        result = scheduled_task_service.run_task_now(db, task_id, operator=operator.username)
    except scheduled_task_service.ScheduledTaskError as exc:
        return resp_fail(str(exc))
    if not result["triggered"]:
        return resp_ok(result, msg="调度器未运行（SCHEDULER_ENABLED=false），无法执行")
    return resp_ok(result, msg=f"任务「{result['task_name']}」已触发，执行结果稍后见最近执行列")
