"""
日志服务（系统日志 / 采集日志的查询、写入与清理）
===============================================================================
设计要点：

1. **日志写入失败绝不影响主流程**
   采集成功但日志写库失败时，业务上应视为"采集成功"。
   故 write_xxx_log() 系列内部捕获异常并只打印到文件日志，
   不向上抛错——日志是辅助功能，不能成为业务失败的原因。

2. **查询必须带时间范围兜底**
   日志表是唯一可能到百万级的表，若前端不传时间范围，
   默认只查最近 24 小时，防止 "SELECT * FROM collect_log ORDER BY id DESC"
   在数据量上来后把数据库拖死。

3. **清理与指标清理分开执行**
   日志保留期（默认 30 天）与指标保留期（7/30/90 天）策略不同，
   由各自的定时任务负责，互不干扰。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.logger import system_logger
from app.models.log import AlarmLog, CollectLog, SysLog
# AlarmLogOut 定义在 schemas/alert.py（与告警域 Schema 放在一起，便于统一维护）
from app.schemas.alert import AlarmLogOut
from app.schemas.system import (
    CollectLogOut,
    CollectStatOut,
    SysLogOut,
)

# 查询默认时间窗（小时）：不传时间条件时生效
DEFAULT_QUERY_HOURS = 24


# ============================================================================
# 一、写入（全部为"失败不影响主流程"的安全写入）
# ============================================================================

def write_sys_log(
    db: Session,
    level: str,
    module: str,
    action: str,
    message: str,
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    ip: Optional[str] = None,
    commit: bool = False,
) -> None:
    """
    写入系统日志。

    :param commit: 是否由本函数提交事务。
        在业务事务中调用时传 False，让日志随业务一起提交（避免日志比业务先落库，
        出现"日志显示成功了但业务回滚了"的误导性记录）。
    """
    try:
        db.add(SysLog(
            level=level, module=module, action=action, message=message,
            user_id=user_id, username=username, ip=ip,
        ))
        if commit:
            db.commit()
    except Exception as exc:  # pragma: no cover
        system_logger.warning("系统日志写入失败（不影响主流程）：%s", exc)


def write_collect_log(
    db: Session,
    device_id: int,
    device_name: str,
    device_ip: str,
    protocol: str,
    status: str,
    duration_ms: int,
    metric_count: int = 0,
    error_msg: Optional[str] = None,
) -> None:
    """
    写入采集日志（由调度器的采集任务调用）。

    该方法本身就是独立事务：采集任务与 Web 请求不共享 Session，
    写入失败也不应影响其它设备的采集。
    """
    try:
        db.add(CollectLog(
            device_id=device_id,
            device_name=device_name,
            device_ip=device_ip,
            protocol=protocol,
            status=status,
            duration_ms=duration_ms,
            metric_count=metric_count,
            error_msg=(error_msg or "")[:1000] or None,   # 截断超长堆栈，避免撑爆字段
            collect_time=datetime.now(),
        ))
        db.commit()
    except Exception as exc:  # pragma: no cover
        system_logger.warning("采集日志写入失败 | 设备=%s | %s", device_ip, exc)


def write_alarm_log(
    db: Session,
    event_id: Optional[int],
    device_id: Optional[int],
    action: str,
    message: str,
    level: Optional[str] = None,
    channel: Optional[str] = None,
    operator: Optional[str] = None,
    commit: bool = True,
) -> None:
    """写入告警日志（触发/恢复/确认/通知留痕）"""
    try:
        db.add(AlarmLog(
            event_id=event_id, device_id=device_id, action=action,
            channel=channel, level=level, message=message, operator=operator,
        ))
        if commit:
            db.commit()
    except Exception as exc:  # pragma: no cover
        system_logger.warning("告警日志写入失败：%s", exc)


# ============================================================================
# 二、查询
# ============================================================================

def _time_window(start: Optional[datetime], end: Optional[datetime]) -> Tuple[datetime, datetime]:
    """统一时间窗口：未传起始时间时默认最近 24 小时"""
    end_ts = end or datetime.now()
    start_ts = start or (end_ts - timedelta(hours=DEFAULT_QUERY_HOURS))
    return start_ts, end_ts


def list_sys_logs(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    level: Optional[str] = None,
    module: Optional[str] = None,
    keyword: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Tuple[List[SysLogOut], int]:
    """分页查询系统日志"""
    start_ts, end_ts = _time_window(start, end)

    stmt = select(SysLog)
    count_stmt = select(func.count(SysLog.id))
    conditions = [SysLog.created_at >= start_ts, SysLog.created_at <= end_ts]

    if level:
        conditions.append(SysLog.level == level)
    if module:
        conditions.append(SysLog.module == module)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(SysLog.message.like(pattern), SysLog.username.like(pattern)))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(SysLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return [SysLogOut.model_validate(row) for row in rows], total


def list_collect_logs(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    device_id: Optional[int] = None,
    status: Optional[str] = None,
    protocol: Optional[str] = None,
    keyword: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Tuple[List[CollectLogOut], int]:
    """分页查询采集日志（排查"某台设备为什么采集失败"的主入口）"""
    start_ts, end_ts = _time_window(start, end)

    stmt = select(CollectLog)
    count_stmt = select(func.count(CollectLog.id))
    conditions = [CollectLog.collect_time >= start_ts, CollectLog.collect_time <= end_ts]

    if device_id:
        conditions.append(CollectLog.device_id == device_id)
    if status:
        conditions.append(CollectLog.status == status)
    if protocol:
        conditions.append(CollectLog.protocol == protocol)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            CollectLog.device_name.like(pattern),
            CollectLog.device_ip.like(pattern),
            CollectLog.error_msg.like(pattern),
        ))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(CollectLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return [CollectLogOut.model_validate(row) for row in rows], total


def list_alarm_logs(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    device_id: Optional[int] = None,
    action: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Tuple[List[AlarmLogOut], int]:
    """分页查询告警处理日志"""
    start_ts, end_ts = _time_window(start, end)

    stmt = select(AlarmLog)
    count_stmt = select(func.count(AlarmLog.id))
    conditions = [AlarmLog.created_at >= start_ts, AlarmLog.created_at <= end_ts]

    if device_id:
        conditions.append(AlarmLog.device_id == device_id)
    if action:
        conditions.append(AlarmLog.action == action)

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(AlarmLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return [AlarmLogOut.model_validate(row) for row in rows], total


def collect_stat(db: Session, hours: int = 24) -> CollectStatOut:
    """
    采集健康度统计（采集日志页顶部指标卡）。

    一条聚合 SQL 完成，避免把日志全量取出在内存里统计。
    """
    from sqlalchemy import case

    since = datetime.now() - timedelta(hours=hours)
    row = db.execute(
        select(
            func.count(CollectLog.id),
            func.sum(case((CollectLog.status == "success", 1), else_=0)),
            func.sum(case((CollectLog.status == "fail", 1), else_=0)),
            func.sum(case((CollectLog.status == "timeout", 1), else_=0)),
            func.avg(CollectLog.duration_ms),
        ).where(CollectLog.collect_time >= since)
    ).one()

    total = int(row[0] or 0)
    success = int(row[1] or 0)
    return CollectStatOut(
        total=total,
        success=success,
        fail=int(row[2] or 0),
        timeout=int(row[3] or 0),
        success_rate=round(success * 100.0 / total, 2) if total else 0.0,
        avg_duration_ms=int(row[4] or 0),
    )


def device_collect_stat(db: Session, device_id: int, hours: int = 24) -> Dict[str, Any]:
    """单台设备的采集健康度（设备详情页展示"近 24 小时采集成功率"）"""
    from sqlalchemy import case

    since = datetime.now() - timedelta(hours=hours)
    row = db.execute(
        select(
            func.count(CollectLog.id),
            func.sum(case((CollectLog.status == "success", 1), else_=0)),
            func.avg(CollectLog.duration_ms),
        ).where(CollectLog.device_id == device_id, CollectLog.collect_time >= since)
    ).one()

    total = int(row[0] or 0)
    success = int(row[1] or 0)
    return {
        "device_id": device_id,
        "total": total,
        "success": success,
        "success_rate": round(success * 100.0 / total, 2) if total else 0.0,
        "avg_duration_ms": int(row[2] or 0),
    }


# ============================================================================
# 三、清理
# ============================================================================

def cleanup_logs(
    db: Session, keep_days: int = 30, collect_keep_days: Optional[int] = None
) -> Dict[str, int]:
    """
    清理超期日志（由定时任务每日执行）。

    保留三类日志的最近 keep_days 天数据。
    注意：只删日志表，**不删告警事件表**——告警历史是审计资料，需要长期留存。

    :param collect_keep_days: 采集日志的独立保留天数。**为什么需要单独设置**：
        collect_log 是"每台设备每轮采集一行"，写入量是另外两张日志表的成百上千倍
        （200 台设备 × 60 秒周期 ≈ 每天 28.8 万行）。
        它与 sys_log/alarm_log 的审计价值不同 —— 采集日志主要用于"最近这段时间
        这台设备为什么没数据"的排障，超过一周的明细已无查询价值，
        而成功率的长期趋势由 metric_sample 与告警记录间接反映。
        因此默认取 keep_days 的一半（且不少于 3 天），把库体积控制在一个量级内。
        传 None 表示自动推导。
    """
    if collect_keep_days is None:
        collect_keep_days = max(3, keep_days // 2)

    cutoff = datetime.now() - timedelta(days=max(keep_days, 1))
    collect_cutoff = datetime.now() - timedelta(days=max(collect_keep_days, 1))
    result: Dict[str, int] = {}

    result["sys_log"] = db.execute(delete(SysLog).where(SysLog.created_at < cutoff)).rowcount or 0
    result["collect_log"] = db.execute(
        delete(CollectLog).where(CollectLog.collect_time < collect_cutoff)
    ).rowcount or 0
    result["alarm_log"] = db.execute(
        delete(AlarmLog).where(AlarmLog.created_at < cutoff)
    ).rowcount or 0

    db.commit()
    system_logger.info(
        "日志清理完成（系统/告警保留 %s 天，采集日志保留 %s 天）：%s",
        keep_days, collect_keep_days, result,
    )
    return result
