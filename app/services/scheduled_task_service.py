"""
自定义定时任务 Service（sys_scheduled_task 的增删改查 + 调度热更新）
===============================================================================
为什么 CRUD 放在 Service 而不是 API 里直接写：
  "保存一条任务"不是单纯的 insert——还要同步调度器（注册/摘除 job）、
  处理重名冲突、把执行记录格式化成出参。这些编排逻辑属于业务层，
  API 层只做参数接收与响应包装（保持平台 分层 约定）。

热更新约定：
  任何改变任务定义/启用状态的写操作，落库成功后立即调用调度器的
  _apply_custom_job / remove_custom_job，让变更"保存即生效"，
  不需要重启平台（与 sys_config 的生效方式一致）。
===============================================================================
"""
from __future__ import annotations

from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.models import SysScheduledTask
from app.schemas.system import ScheduledTaskIn
from app.scheduler.custom_tasks import get_action
from app.scheduler.scheduler import get_scheduler_manager
from app.core.logger import system_logger


class ScheduledTaskError(Exception):
    """任务管理业务错误（重名、不存在等），API 层转 400/404"""


def _manager():
    return get_scheduler_manager()


def _to_out(task: SysScheduledTask) -> Dict[str, Any]:
    """ORM 行 → 出参 dict（执行记录格式化、下次执行时间从调度器查）"""
    action = get_action(task.action)
    return {
        "id": task.id,
        "name": task.name,
        "action": task.action,
        "action_label": action.label if action else task.action,
        "trigger_type": task.trigger_type,
        "interval_seconds": task.interval_seconds if task.trigger_type == "interval" else None,
        "cron_expr": task.cron_expr if task.trigger_type == "cron" else None,
        "enabled": task.enabled,
        "remark": task.remark,
        "last_run_at": task.last_run_at.strftime("%Y-%m-%d %H:%M:%S") if task.last_run_at else None,
        "last_run_ok": task.last_run_ok,
        "last_run_message": task.last_run_message,
        "next_run_at": _manager().custom_task_next_run(task.id) if task.enabled else None,
    }


def list_tasks(db: Session) -> List[Dict[str, Any]]:
    """全部自定义任务（含停用的，管理视角要看全量）"""
    rows = db.query(SysScheduledTask).order_by(SysScheduledTask.id).all()
    return [_to_out(t) for t in rows]


def create_task(db: Session, payload: ScheduledTaskIn, operator: str = "") -> Dict[str, Any]:
    """新建任务：查重 → 落库 → 注册到调度器（保存即生效）"""
    dup = db.query(SysScheduledTask).filter(SysScheduledTask.name == payload.name).first()
    if dup is not None:
        raise ScheduledTaskError(f"任务名称「{payload.name}」已存在")
    task = SysScheduledTask(
        name=payload.name,
        action=payload.action,
        trigger_type=payload.trigger_type,
        interval_seconds=payload.interval_seconds,
        cron_expr=payload.cron_expr.strip() if payload.cron_expr else None,
        enabled=payload.enabled,
        remark=payload.remark,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _manager()._apply_custom_job(task.id)  # noqa: SLF001 —— Service 与调度器同属平台内部组件
    system_logger.info("创建自定义定时任务 %s(#%s) by %s", task.name, task.id, operator)
    return _to_out(task)


def update_task(db: Session, task_id: int, payload: ScheduledTaskIn, operator: str = "") -> Dict[str, Any]:
    """编辑任务：全量更新定义并重新注册 job"""
    task = db.get(SysScheduledTask, task_id)
    if task is None:
        raise ScheduledTaskError(f"任务不存在（id={task_id}）")
    dup = db.query(SysScheduledTask).filter(
        SysScheduledTask.name == payload.name, SysScheduledTask.id != task_id).first()
    if dup is not None:
        raise ScheduledTaskError(f"任务名称「{payload.name}」已存在")
    task.name = payload.name
    task.action = payload.action
    task.trigger_type = payload.trigger_type
    task.interval_seconds = payload.interval_seconds
    task.cron_expr = payload.cron_expr.strip() if payload.cron_expr else None
    task.enabled = payload.enabled
    task.remark = payload.remark
    db.commit()
    db.refresh(task)
    # enabled 或定义都可能变了：统一走"应用"入口，禁用状态会自动摘除 job
    _manager()._apply_custom_job(task.id)
    system_logger.info("修改自定义定时任务 %s(#%s) by %s", task.name, task.id, operator)
    return _to_out(task)


def toggle_task(db: Session, task_id: int, enabled: bool, operator: str = "") -> Dict[str, Any]:
    """启用/停用（停用=保留定义、摘除 job；启用=重新注册）"""
    task = db.get(SysScheduledTask, task_id)
    if task is None:
        raise ScheduledTaskError(f"任务不存在（id={task_id}）")
    task.enabled = enabled
    db.commit()
    _manager()._apply_custom_job(task.id)
    system_logger.info("%s自定义定时任务 %s(#%s) by %s",
                       "启用" if enabled else "停用", task.name, task.id, operator)
    return _to_out(task)


def delete_task(db: Session, task_id: int, operator: str = "") -> Dict[str, Any]:
    """删除任务（定义与 job 一起移除；执行历史随行删除）"""
    task = db.get(SysScheduledTask, task_id)
    if task is None:
        raise ScheduledTaskError(f"任务不存在（id={task_id}）")
    name = task.name
    db.delete(task)
    db.commit()
    _manager().remove_custom_job(task_id)
    system_logger.info("删除自定义定时任务 %s(#%s) by %s", name, task_id, operator)
    return {"deleted": True, "id": task_id}


def run_task_now(db: Session, task_id: int, operator: str = "") -> Dict[str, Any]:
    """
    立即执行（手动触发）。停用的任务也允许手动跑一次——排障时"先手动验证、
    再决定是否启用"是标准排障路径。
    """
    task = db.get(SysScheduledTask, task_id)
    if task is None:
        raise ScheduledTaskError(f"任务不存在（id={task_id}）")
    ok = _manager().trigger_custom_task(task_id)
    return {"triggered": ok, "task_name": task.name}
