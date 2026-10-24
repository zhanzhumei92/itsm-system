"""
告警服务（模块3：告警规则管理、告警中心、人工确认、统计）
===============================================================================
分层说明（与 app/alarm/ 的分工，避免职责重叠）：

| 模块                             | 职责                                                  |
|----------------------------------|-------------------------------------------------------|
| app/services/alert_service.py    | 规则 CRUD、事件查询、人工确认、统计（**业务数据操作**）|
| app/alarm/evaluator.py（第5步）  | 连续 N 次判定、滞回恢复（**判定逻辑**）                |
| app/alarm/converger.py（第5步）  | 指纹收敛、静默抑制（**限流逻辑**）                     |
| app/alarm/notifier.py（第5步）   | 邮件/企微/钉钉/微信推送（**送达逻辑**）                |

本模块提供 create_event() / recover_event() 两个底层方法供评估器调用，
保证"告警事件"的新增与恢复只有一处实现，避免两套逻辑写出不一致的记录。
===============================================================================
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundException, StateInvalidException
from app.core.logger import system_logger
from app.models.alert import (
    ALERT_LEVEL_TEXT,
    EVENT_STATUS_ACKED,
    EVENT_STATUS_CLOSED,
    EVENT_STATUS_FIRING,
    EVENT_STATUS_RECOVERED,
    AlertEvent,
    AlertNotifyRecord,
    AlertRule,
    AlertRuleState,
    metric_unit,
)
from app.models.device import MonDevice
from app.models.log import ALARM_ACTION_ACK, ALARM_ACTION_CLOSE, ALARM_ACTION_RECOVER, ALARM_ACTION_TRIGGER, AlarmLog
from app.models.metric import MetricCatalog
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

# 通知记录状态
NOTIFY_PENDING = "pending"
NOTIFY_SUCCESS = "success"
NOTIFY_FAIL = "fail"

# 规则运行状态（alert_rule_state.status）。
# 定义在这里而不是散落字面量：评估器（app/alarm/evaluator.py）按这三态决定
# "该判断触发还是该判断恢复"，任一处写错都会让告警忽有忽无。
RULE_STATE_NORMAL = "normal"
RULE_STATE_FIRING = EVENT_STATUS_FIRING
RULE_STATE_ACKED = EVENT_STATUS_ACKED

# 说明：指标单位的唯一来源是 app/models/alert.py 的 METRIC_UNITS + metric_unit()。
# Service（本模块）、Schema（app/schemas/alert.py）、通知渠道（notifier）三处共用同一份定义，
# 并由 scripts/static_audit.py 的检查 6 守住"这份表与 sql/init_data.sql 的 metric_catalog 一致"。
# 上面已 import metric_unit，因此外部也可写 `alert_service.metric_unit(...)`
# （app/alarm/evaluator.py 就是这么用的），无需再各自从模型层导入。
# 注：本模块**不再**转发 METRIC_UNITS / PORT_KEY_SEPARATOR —— 没有任何调用方，
# 转发只会让"单位表到底有几个入口"变得说不清。


def build_fingerprint(device_id: int, rule_id: int) -> str:
    """
    生成告警指纹。

    作用：同一设备 + 同一规则的反复触发归并为一条告警事件（收敛的基础）。
    用 md5 而非直接拼接字符串，是为了让 fingerprint 列保持定长 32 字符，
    索引效率稳定且便于将来按指纹做分区。
    """
    return hashlib.md5(f"{device_id}:{rule_id}".encode("utf-8")).hexdigest()


# ============================================================================
# 一、告警规则 CRUD
# ============================================================================

def list_rules(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    device_id: Optional[int] = None,
    level: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Tuple[List[AlertRuleOut], int]:
    """分页查询规则，并补充设备名、指标中文名与运行状态"""
    stmt = select(AlertRule)
    count_stmt = select(func.count(AlertRule.id))

    conditions = []
    if keyword:
        conditions.append(AlertRule.name.like(f"%{keyword}%"))
    if device_id is not None:
        conditions.append(AlertRule.device_id == device_id)
    if level:
        conditions.append(AlertRule.level == level)
    if enabled is not None:
        conditions.append(AlertRule.enabled.is_(enabled))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rules = db.execute(
        stmt.order_by(AlertRule.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    items = _attach_rule_meta(db, list(rules))
    return items, total


def _attach_rule_meta(db: Session, rules: List[AlertRule]) -> List[AlertRuleOut]:
    """
    给规则补充展示字段：
      - device_name：关联设备名，全局规则显示"全部设备"
      - metric_name：指标中文名
      - firing_device_count：该规则当前触发中的设备数（运维一眼看出影响面）
    """
    device_ids = [r.device_id for r in rules if r.device_id]
    device_map: Dict[int, str] = {}
    if device_ids:
        for device in db.execute(select(MonDevice).where(MonDevice.id.in_(device_ids))).scalars().all():
            device_map[device.id] = device.name

    metric_map = {
        row.metric_key: row.metric_name
        for row in db.execute(select(MetricCatalog)).scalars().all()
    }

    rule_ids = [r.id for r in rules]
    firing_map: Dict[int, int] = {}
    if rule_ids:
        rows = db.execute(
            select(AlertEvent.rule_id, func.count(AlertEvent.id))
            .where(
                AlertEvent.rule_id.in_(rule_ids),
                AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED]),
            )
            .group_by(AlertEvent.rule_id)
        ).all()
        firing_map = {row[0]: row[1] for row in rows}

    items: List[AlertRuleOut] = []
    for rule in rules:
        out = AlertRuleOut.model_validate({
            **{c.name: getattr(rule, c.name) for c in rule.__table__.columns},
            "notify_channels": rule.channel_list,
        })
        out.device_name = device_map.get(rule.device_id) if rule.device_id else "全部设备"
        out.metric_name = metric_map.get(rule.metric_key, rule.metric_key)
        out.firing_device_count = firing_map.get(rule.id, 0)
        items.append(out)
    return items


def get_rule(db: Session, rule_id: int) -> AlertRule:
    """按 ID 取规则"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise NotFoundException("告警规则不存在")
    return rule


def create_rule(db: Session, payload: AlertRuleCreate) -> AlertRuleOut:
    """新增规则"""
    if payload.device_id:
        if db.get(MonDevice, payload.device_id) is None:
            raise NotFoundException(f"设备 {payload.device_id} 不存在")

    rule = AlertRule(
        **payload.model_dump(exclude={"notify_channels"}),
        notify_channels="[]",
    )
    rule.channel_list = payload.notify_channels

    db.add(rule)
    db.commit()
    db.refresh(rule)
    system_logger.info("新增告警规则 | %s | %s", rule.name, rule.metric_key)
    return _attach_rule_meta(db, [rule])[0]


def update_rule(db: Session, rule_id: int, payload: AlertRuleUpdate) -> AlertRuleOut:
    """
    编辑规则。

    重要：阈值或比较符变更时，**重置该规则的连续计数器**。
    理由：原来"CPU>85 连续3次"已经累计了 2 次超标，此时把阈值改成 70，
    若不重置，下一次采集达标就直接告警，运维会觉得"刚改完就报警很突兀"。
    重置后从 0 开始计数，行为可预期。
    """
    rule = get_rule(db, rule_id)
    data = payload.model_dump(exclude_unset=True)

    need_reset = False
    if "threshold" in data and data["threshold"] != rule.threshold:
        need_reset = True
    if "operator" in data and data["operator"] != rule.operator:
        need_reset = True
    if "metric_key" in data and data["metric_key"] != rule.metric_key:
        need_reset = True

    channels = data.pop("notify_channels", None)
    for field, value in data.items():
        setattr(rule, field, value)
    if channels is not None:
        rule.channel_list = channels

    if need_reset:
        db.execute(
            update(AlertRuleState)
            .where(AlertRuleState.rule_id == rule_id,
                   AlertRuleState.status == RULE_STATE_NORMAL)
            .values(breach_count=0, normal_count=0, updated_at=datetime.now())
        )

    db.commit()
    db.refresh(rule)
    return _attach_rule_meta(db, [rule])[0]


def delete_rule(db: Session, rule_id: int) -> None:
    """
    删除规则（同时清理运行状态）。

    告警事件**保留**：删规则不等于历史没发生过，审计需要保留记录。
    """
    rule = get_rule(db, rule_id)
    db.execute(AlertRuleState.__table__.delete().where(AlertRuleState.rule_id == rule_id))
    db.delete(rule)
    db.commit()
    system_logger.warning("删除告警规则 | %s", rule.name)


def toggle_rule(db: Session, rule_id: int, enabled: bool) -> AlertRule:
    """启用/停用规则"""
    rule = get_rule(db, rule_id)
    rule.enabled = enabled
    db.commit()
    db.refresh(rule)
    return rule


def silence_rule(db: Session, rule_id: int, minutes: int) -> AlertRule:
    """
    设置静默期（设备维护窗口用）。

    静默期内规则仍会评估并更新状态，只是**不推送通知**，
    这样维护结束后若指标确实异常，能立刻收到告警而不是被吞掉。
    """
    rule = get_rule(db, rule_id)
    rule.silence_until = datetime.now() + timedelta(minutes=max(minutes, 1))
    db.commit()
    db.refresh(rule)
    return rule


# ============================================================================
# 二、规则状态（供评估器读写）
# ============================================================================

def get_rule_state(db: Session, rule_id: int, device_id: int) -> Optional[AlertRuleState]:
    """取规则在某设备上的运行状态"""
    return db.execute(
        select(AlertRuleState).where(
            AlertRuleState.rule_id == rule_id, AlertRuleState.device_id == device_id
        )
    ).scalar_one_or_none()


def get_or_create_rule_state(db: Session, rule_id: int, device_id: int) -> AlertRuleState:
    """
    取或创建规则状态。

    首次评估某设备时状态行不存在，此处自动创建；
    用 flush 而非 commit，让调用方（评估器）把状态更新与事件创建放在同一事务里，
    保证"计数增加"与"生成事件"要么都成功要么都失败。
    """
    state = get_rule_state(db, rule_id, device_id)
    if state is None:
        state = AlertRuleState(
            rule_id=rule_id, device_id=device_id, status=RULE_STATE_NORMAL
        )
        db.add(state)
        db.flush()
    return state


def _mark_rule_state_acked(db: Session, event: AlertEvent) -> None:
    """
    人工确认/关闭后，把规则运行状态同步为 acked。

    **为什么必须同步（即使"看起来"不改行为也一样要做）**

    评估器把状态分成两组判断：`normal` 走"是否触发"，`firing/acked` 走"是否恢复"。
    在补上这个同步之前，`alert_rule_state.status` 全平台只被写成 normal 和 firing，
    于是评估器里的 `STATE_ACKED` 分支是**永远不会被走到的死代码** ——
    "已确认后不再重复告警"这条保证只是**恰好**成立（借用了 firing 分支的行为），
    并没有被任何代码显式表达。

    风险在于：将来有人给 firing 分支加需求（例如"长时间未恢复就再次提醒值班"），
    这条保证会在无人察觉的情况下失效 —— 运维刚点完确认，又收到一条同样的告警，
    而这正是"确认"按钮失去意义的原因。

    同步之后 acked 是独立可达的状态，语义与代码一一对应；行为不变
    （acked 与 firing 一样，只在指标连续正常后动作）。
    """
    if not event.rule_id:
        # 规则被删除后事件仍保留（审计需要），此时没有状态行可同步
        return
    state = get_rule_state(db, event.rule_id, event.device_id)
    if state is not None and state.status != RULE_STATE_NORMAL:
        state.status = RULE_STATE_ACKED


# ============================================================================
# 三、告警事件：创建 / 恢复（供评估器调用）
# ============================================================================

def create_event(
    db: Session,
    rule: AlertRule,
    device: MonDevice,
    metric_key: str,
    metric_value: Optional[float],
    metric_name: Optional[str] = None,
    now_ts: Optional[datetime] = None,
) -> AlertEvent:
    """
    创建告警事件。

    收敛关键：**该指纹已存在未恢复事件时，不新增记录，只累加 occur_count**。
    这是"同一设备短时间多条同类告警合并"的落库实现。
    """
    now_ts = now_ts or datetime.now()
    fingerprint = build_fingerprint(device.id, rule.id)

    existing = db.execute(
        select(AlertEvent).where(
            AlertEvent.fingerprint == fingerprint,
            AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED]),
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.occur_count = (existing.occur_count or 0) + 1
        existing.last_occur_at = now_ts
        existing.metric_value = metric_value
        existing.status = EVENT_STATUS_FIRING    # 已确认后再次触发，重新回到"告警中"
        existing.ack_at = None
        existing.ack_user = None
        db.flush()
        return existing

    title = _build_title(device, rule, metric_name)
    content = _build_content(device, rule, metric_name, metric_value, now_ts)

    event = AlertEvent(
        event_no=_build_event_no(now_ts),
        rule_id=rule.id,
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip,
        metric_key=metric_key,
        metric_name=metric_name or metric_key,
        metric_value=metric_value,
        threshold=rule.threshold,
        operator=rule.operator,
        level=rule.level,
        title=title,
        content=content,
        fingerprint=fingerprint,
        status=EVENT_STATUS_FIRING,
        occur_count=1,
        first_occur_at=now_ts,
        last_occur_at=now_ts,
    )
    db.add(event)
    db.flush()   # 取到 event.id，供规则状态与后续通知记录引用

    db.add(AlarmLog(
        event_id=event.id,
        device_id=device.id,
        action=ALARM_ACTION_TRIGGER,
        level=rule.level,
        message=f"触发告警：{title}（当前值 {metric_value}，阈值 {rule.operator} {rule.threshold}）",
    ))
    return event


def recover_event(
    db: Session, event: AlertEvent, operator: str = "", reason: str = ""
) -> AlertEvent:
    """
    恢复告警事件。

    状态从 firing/acked → recovered，并记录恢复时间。
    恢复后该指纹不再有活跃事件，下次超标会重新生成一条新事件
    —— 这样告警列表能清晰呈现"故障发生 → 恢复"的完整周期。
    """
    now_ts = datetime.now()
    event.status = EVENT_STATUS_RECOVERED
    event.recover_at = now_ts
    db.add(AlarmLog(
        event_id=event.id,
        device_id=event.device_id,
        action=ALARM_ACTION_RECOVER,
        level=event.level,
        message=reason or f"告警恢复：{event.title}",
        operator=operator,
    ))
    db.flush()
    return event


def _build_title(device: MonDevice, rule: AlertRule, metric_name: Optional[str]) -> str:
    """告警标题：【紧急】核心交换机01 CPU使用率过高"""
    level_text = ALERT_LEVEL_TEXT.get(rule.level, rule.level)
    return f"【{level_text}】{device.name} {rule.name}"


def _build_content(
    device: MonDevice,
    rule: AlertRule,
    metric_name: Optional[str],
    metric_value: Optional[float],
    now_ts: datetime,
) -> str:
    """
    告警正文（多行文本，可直接用于邮件/企微/钉钉消息体）。

    刻意把运维排查最需要的字段全部带上（设备、IP、位置、指标、当前值、阈值、时间），
    让值班人员不需要登录平台就能判断严重程度——这是告警有效性的关键。
    """
    # 单位统一从 metric_unit() 取（全平台唯一来源，避免四处各写一套判断）
    unit = metric_unit(rule.metric_key)

    value_text = "无法获取" if metric_value is None else f"{metric_value:g}{unit}"
    threshold_text = f"{rule.threshold:g}{unit}"

    return "\n".join([
        f"设备名称：{device.name}",
        f"设备IP：{device.ip}",
        f"设备类型：{device.device_type}",
        f"位置：{device.location or '未填写'}",
        f"监控指标：{metric_name or rule.metric_key}",
        f"当前值：{value_text}",
        f"告警阈值：{rule.operator_text} {threshold_text}",
        f"连续次数：{rule.duration_n} 次采集超标",
        f"告警时间：{now_ts.strftime('%Y-%m-%d %H:%M:%S')}",
    ])


def _build_event_no(now_ts: datetime) -> str:
    """
    事件编号：AL + 年月日时分秒 + 微秒后 3 位。

    加上微秒是为了避免同一秒内多台设备同时告警时编号重复
    （200 台设备轮询集中在同一分钟内，这个场景很常见）。
    """
    return f"AL{now_ts.strftime('%Y%m%d%H%M%S')}{now_ts.microsecond // 1000:03d}"


# ============================================================================
# 四、告警事件查询与确认
# ============================================================================

def list_events(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    level: Optional[str] = None,
    status: Optional[str] = None,
    device_id: Optional[int] = None,
    keyword: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    only_unhandled: bool = False,
) -> Tuple[List[AlertEventOut], int]:
    """分页查询告警事件（告警中心主列表）"""
    stmt = select(AlertEvent)
    count_stmt = select(func.count(AlertEvent.id))

    conditions = []
    if level:
        conditions.append(AlertEvent.level == level)
    if status:
        conditions.append(AlertEvent.status == status)
    if device_id:
        conditions.append(AlertEvent.device_id == device_id)
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            AlertEvent.device_name.like(pattern),
            AlertEvent.device_ip.like(pattern),
            AlertEvent.title.like(pattern),
        ))
    if start:
        conditions.append(AlertEvent.last_occur_at >= start)
    if end:
        conditions.append(AlertEvent.last_occur_at <= end)
    if only_unhandled:
        conditions.append(AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED]))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(AlertEvent.last_occur_at.desc(), AlertEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars().all()

    # 补充规则名（一条 SQL 批量取，避免逐条查库）
    rule_ids = [e.rule_id for e in rows if e.rule_id]
    rule_map: Dict[int, str] = {}
    if rule_ids:
        for rule in db.execute(select(AlertRule).where(AlertRule.id.in_(rule_ids))).scalars().all():
            rule_map[rule.id] = rule.name

    items: List[AlertEventOut] = []
    for event in rows:
        out = AlertEventOut.model_validate(event)
        out.rule_name = rule_map.get(event.rule_id or 0)
        items.append(out)
    return items, total


def get_event(db: Session, event_id: int) -> AlertEvent:
    """按 ID 取告警事件"""
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise NotFoundException("告警记录不存在")
    return event


def get_event_detail(db: Session, event_id: int) -> AlertEventOut:
    """告警详情（含规则名）"""
    event = get_event(db, event_id)
    out = AlertEventOut.model_validate(event)
    if event.rule_id:
        rule = db.get(AlertRule, event.rule_id)
        out.rule_name = rule.name if rule else None
    return out


def ack_event(
    db: Session, event_id: int, payload: AlertAckRequest, username: str
) -> AlertEventOut:
    """
    手动标记已确认（需求：手动标记已确认）。

    语义区分：
      - acked：已知晓，正在处理。设备恢复前不再重复推送，但事件仍算"未处理"
      - closed：人工处理完毕直接关闭（如确认是误报、或已换硬件）
    关闭后该指纹再次超标会产生新事件，不会被历史状态吃掉。
    """
    event = get_event(db, event_id)
    if event.status in (EVENT_STATUS_RECOVERED, EVENT_STATUS_CLOSED) and not payload.close:
        raise StateInvalidException("该告警已恢复或已关闭，无需再次确认")

    now_ts = datetime.now()
    event.ack_at = now_ts
    event.ack_user = username
    event.ack_remark = payload.remark
    event.status = EVENT_STATUS_CLOSED if payload.close else EVENT_STATUS_ACKED
    _mark_rule_state_acked(db, event)

    db.add(AlarmLog(
        event_id=event.id,
        device_id=event.device_id,
        action=ALARM_ACTION_CLOSE if payload.close else ALARM_ACTION_ACK,
        level=event.level,
        message=(f"手动关闭：{payload.remark or '无备注'}"
                 if payload.close else f"确认告警：{payload.remark or '无备注'}"),
        operator=username,
    ))
    db.commit()
    db.refresh(event)
    return get_event_detail(db, event_id)


def batch_ack_events(db: Session, event_ids: Sequence[int], username: str, remark: str = "") -> int:
    """批量确认（值班交接时一次处理多条）"""
    count = 0
    for event_id in event_ids:
        event = db.get(AlertEvent, event_id)
        if event is None or event.status in (EVENT_STATUS_RECOVERED, EVENT_STATUS_CLOSED):
            continue
        event.ack_at = datetime.now()
        event.ack_user = username
        event.ack_remark = remark
        event.status = EVENT_STATUS_ACKED
        _mark_rule_state_acked(db, event)
        db.add(AlarmLog(
            event_id=event.id, device_id=event.device_id, action=ALARM_ACTION_ACK,
            level=event.level, message=f"批量确认：{remark or '无备注'}", operator=username,
        ))
        count += 1
    db.commit()
    return count


def get_event_timeline(db: Session, event_id: int) -> List[AlarmLogOut]:
    """告警处理时间线（触发 → 通知 → 确认 → 恢复 全过程）"""
    rows = db.execute(
        select(AlarmLog).where(AlarmLog.event_id == event_id).order_by(AlarmLog.created_at.asc())
    ).scalars().all()
    return [AlarmLogOut.model_validate(row) for row in rows]


def list_notify_records(db: Session, event_id: int) -> List[AlertNotifyRecordOut]:
    """某事件的各渠道通知结果"""
    rows = db.execute(
        select(AlertNotifyRecord)
        .where(AlertNotifyRecord.event_id == event_id)
        .order_by(AlertNotifyRecord.id.asc())
    ).scalars().all()
    return [AlertNotifyRecordOut.model_validate(row) for row in rows]


# ============================================================================
# 五、统计（大屏与告警中心共用）
# ============================================================================

def get_overview(db: Session) -> AlertOverviewOut:
    """
    告警总览统计。

    实现说明：用整表聚合而非逐条遍历。
    活跃事件的判定口径：status in (firing, acked) —— 与 alert_event.is_active 属性一致，
    保证大屏数字与列表筛选结果对得上（这是运维最容易质疑的地方）。
    """
    total = db.execute(select(func.count(AlertEvent.id))).scalar_one() or 0

    active = db.execute(
        select(func.count(AlertEvent.id)).where(
            AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED])
        )
    ).scalar_one() or 0

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today = db.execute(
        select(func.count(AlertEvent.id)).where(AlertEvent.last_occur_at >= today_start)
    ).scalar_one() or 0

    level_rows = db.execute(
        select(AlertEvent.level, func.count(AlertEvent.id)).group_by(AlertEvent.level)
    ).all()
    status_rows = db.execute(
        select(AlertEvent.status, func.count(AlertEvent.id)).group_by(AlertEvent.status)
    ).all()

    return AlertOverviewOut(
        total=total,
        active=active,
        today=today,
        by_level={row[0]: row[1] for row in level_rows},
        by_status={row[0]: row[1] for row in status_rows},
    )


def get_latest_events(db: Session, limit: int = 20) -> List[AlertEventOut]:
    """
    最新告警（大屏底部滚动字幕数据源）。

    只取活跃事件：已恢复的告警放在滚动条上会让现场领导误判系统仍在故障。
    """
    rows = db.execute(
        select(AlertEvent)
        .where(AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED]))
        .order_by(AlertEvent.last_occur_at.desc())
        .limit(limit)
    ).scalars().all()
    return [AlertEventOut.model_validate(row) for row in rows]
