"""
告警评估器：规则匹配 + 连续 N 次触发 + 滞回 M 次恢复
===============================================================================
**调用契约（第 4 步的 collect_job.py 已按此调用，不可改）**

    from app.alarm import evaluator
    stats = evaluator.evaluate_devices(db, device_ids)
    stats["fired"]      # 本轮新增告警数
    stats["recovered"]  # 本轮恢复数

调用时机由 collect_job 的"汇总任务"保证：本批采集的 future 全部结束后才调用，
因此这里读到的一定是本轮刚写入的 metric_latest，且同一台设备同轮只评估一次。

**核心：一个设备 × 一条规则 = 一个独立的状态机**

    正常 ──连续 duration_n 次超标──► 告警中(firing)
      ▲                                  │
      │                                  ├── 人工确认 ──► 已确认(acked)
      └──连续 recovery_n 次正常──────────┘

三条不能含糊的规则（含糊就会出现"告警忽有忽无"，运维无法信任）：

1. **触发后清零 breach_count**。
   若不清理，第 4、5、6 次采集都会满足 `breach_count >= duration_n`，
   于是每轮都"触发"一次 —— 事件表里 occur_count 疯涨，推送被反复发出。
   本实现里触发后进入 firing 状态，该状态下**只判断恢复、不再判断触发**，
   这是"同一次故障只报一次"的落点。

2. **已确认(acked)状态下持续超标不重新触发**。
   acked 的语义是"我已知晓、正在处理"。若还继续报，运维刚点完确认
   又收到一条同样的告警 —— 这正是"确认"按钮失去意义的原因。
   只有"恢复正常 → 再次超标"才会产生新事件。
   （`alert_service.create_event` 里有"已确认后再次触发回到 firing"的逻辑，
   那是给**跨故障周期**用的，与本条不冲突。）

3. **取不到指标值时不评估，既不记超标也不记正常**。
   这是最容易写错的一处：设备离线时 `ping_latency` 自然取不到值，
   若把"取不到"当成"未超标"，就会在设备离线的同时把延迟告警**误恢复**，
   值班人员看到的是"设备离线了、延迟告警恢复了"，完全不合逻辑。
   同理也不能当成超标（会刷出一堆 0 值告警）。
   正确做法是**跳过这一轮**，让计数保持原样 —— 状态机"停在原地"。

**为什么把设备在线状态当指标处理而不是单独逻辑**
   规则表里已有一条 `online_status eq 0` 的内置规则，
   把"离线"统一成指标（online=1 / offline=0 / unknown=不评估），
   那么"连续 N 次采集失败才判离线"这件事就由采集层
   （`device_service.update_device_status` 的 offline_threshold）负责，
   告警层只管"状态变了要报"。两层各管一件事，不会互相打架。

**unknown 状态刻意不评估**：新导入的设备在第一次采集成功前是 unknown，
若把 unknown 当离线，导入 200 台设备会瞬间刷出 200 条"设备离线"告警。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alarm import notifier
from app.core.logger import alarm_logger
from app.models.alert import (
    EVENT_STATUS_ACKED,
    EVENT_STATUS_FIRING,
    METRIC_KEY_OFFLINE,
    AlertEvent,
    AlertRule,
    AlertRuleState,
)
from app.models.device import MonDevice
from app.models.metric import MetricCatalog, MetricLatest
from app.services import alert_service, metric_service

# ---------------------------------------------------------------------------
# 比较符实现表
#
# 用表驱动而不是 if/elif 链：新增比较符只需加一行，
# 且"未知比较符"能被统一识别出来（`OPERATORS.get(op)` 为 None），
# 而不是静默地落到某个 else 分支按错误语义比较。
# ---------------------------------------------------------------------------
OPERATORS: Dict[str, Any] = {
    "gt": lambda value, threshold: value > threshold,
    "lt": lambda value, threshold: value < threshold,
    "ge": lambda value, threshold: value >= threshold,
    "le": lambda value, threshold: value <= threshold,
    "eq": lambda value, threshold: value == threshold,
    "ne": lambda value, threshold: value != threshold,
}

# 规则状态取值（与 alert_rule_state.status 的约定一致）
STATE_NORMAL = "normal"
STATE_FIRING = EVENT_STATUS_FIRING
STATE_ACKED = EVENT_STATUS_ACKED

# 在线状态 → 数值（供 online_status 类规则比较）
ONLINE_VALUE = {"online": 1.0, "offline": 0.0}


# ============================================================================
# 主入口
# ============================================================================

def evaluate_devices(
    db: Session, device_ids: Optional[Sequence[int]] = None
) -> Dict[str, int]:
    """
    批量评估告警。

    :param device_ids: 要评估的设备 ID；传 None 表示全部已启用设备
                       （手动"立即评估"与单测使用）
    :return: 统计摘要；`fired` / `recovered` 是本函数对外的稳定契约，
             其余字段供日志与排障使用。

    **单台设备/单条规则出错不影响其它**：最内层 try/except 逐条兜住，
    出错只累加 errors 计数并回滚该条的事务，继续处理下一条。
    一条坏规则（比如阈值是 NULL）不该让全平台告警停摆。
    """
    stats: Dict[str, int] = {
        "devices": 0, "rules": 0, "pairs": 0,
        "fired": 0, "recovered": 0, "no_value": 0, "no_operator": 0, "errors": 0,
    }

    rules = _load_rules(db)
    if not rules:
        return stats
    stats["rules"] = len(rules)

    devices = _load_devices(db, device_ids)
    if not devices:
        return stats
    stats["devices"] = len(devices)

    device_ids_list = [d.id for d in devices]
    latest_map = metric_service.get_latest_map(db, device_ids_list)
    catalog = _load_catalog(db)
    now_ts = datetime.now()

    for device in devices:
        rules_for_device = _rules_for(device, rules, stats)
        for rule in rules_for_device:
            stats["pairs"] += 1
            try:
                _evaluate_one(db, device, rule, latest_map, catalog, now_ts, stats)
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                alarm_logger.exception(
                    "告警评估失败 | 设备=%s(%s) | 规则=%s | %s",
                    device.name, device.ip, rule.name, exc,
                )
                # 必须回滚：SQLAlchemy 会话一旦处于失败事务状态，
                # 后续所有语句都会报 "PendingRollbackError"，让剩余设备全部评估失败
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        alarm_logger.exception("告警评估提交失败：%s", exc)
        db.rollback()

    return stats


# ============================================================================
# 单条规则评估（状态机）
# ============================================================================

def _evaluate_one(
    db: Session,
    device: MonDevice,
    rule: AlertRule,
    latest_map: Dict[Tuple[int, str], MetricLatest],
    catalog: Dict[str, MetricCatalog],
    now_ts: datetime,
    stats: Dict[str, int],
) -> None:
    """评估「一台设备 + 一条规则」"""
    compare = OPERATORS.get(str(rule.operator or "").lower().strip())
    if compare is None:
        # 未知比较符（历史脏数据 / 手工改库）。**不报错、不告警**，只计数，
        # 否则一条坏规则会每分钟刷一条错误日志
        stats["no_operator"] += 1
        return

    value, resolved_key = _resolve_value(device, rule, latest_map)
    if value is None:
        # 见模块 docstring 第 3 条：拿不到值就跳过，不碰计数器
        stats["no_value"] += 1
        return

    threshold = float(rule.threshold or 0)
    breached = bool(compare(value, threshold))
    duration_n = max(int(rule.duration_n or 1), 1)
    recovery_n = max(int(rule.recovery_n or 1), 1)

    state = alert_service.get_or_create_rule_state(db, rule.id, device.id)
    state.last_value = value
    state.last_eval_time = now_ts

    if state.status in (STATE_FIRING, STATE_ACKED):
        # ---- 已在告警中：只判断"是否恢复"，绝不重新触发 ----
        _handle_recovering(db, device, rule, state, breached, value,
                           recovery_n, now_ts, stats)
    else:
        # ---- 正常态：判断"是否触发" ----
        _handle_breaching(db, device, rule, state, breached, duration_n,
                          value, resolved_key, catalog, now_ts, stats)


def _handle_breaching(
    db: Session,
    device: MonDevice,
    rule: AlertRule,
    state: AlertRuleState,
    breached: bool,
    duration_n: int,
    value: float,
    resolved_key: str,
    catalog: Dict[str, MetricCatalog],
    now_ts: datetime,
    stats: Dict[str, int],
) -> None:
    """正常态下的计数与触发判定"""
    if not breached:
        # 正常：清零超标计数，把正常计数往上顶（封顶在 recovery_n，
        # 避免长期正常时数字无限增长 —— 前端会显示"连续正常 100000 次"这种噪音）
        state.breach_count = 0
        state.normal_count = min((state.normal_count or 0) + 1, max(int(rule.recovery_n or 1), 1))
        return

    state.breach_count = (state.breach_count or 0) + 1
    state.normal_count = 0
    if state.breach_count < duration_n:
        return

    # ---- 达到触发条件 ----
    metric_name = _metric_display_name(resolved_key, catalog)
    event = alert_service.create_event(
        db, rule, device, resolved_key, value, metric_name=metric_name, now_ts=now_ts,
    )
    state.status = STATE_FIRING
    state.current_event_id = event.id
    state.last_fire_time = now_ts
    # **触发后清零**：配合上面的"firing 状态只判断恢复"，
    # 保证同一次故障只产生一次触发与一次推送（见模块 docstring 第 1 条）
    state.breach_count = 0
    state.normal_count = 0
    stats["fired"] += 1

    alarm_logger.info(
        "触发告警 | %s | 设备=%s(%s) | %s=%s%s | %s %s | 连续 %s 次超标 | 策略：%s",
        event.event_no, device.name, device.ip,
        metric_name, value, alert_service.metric_unit(resolved_key),
        rule.operator_text, rule.threshold, duration_n, rule.name,
    )

    # 推送（同步部分只落库，实际发送在线程池里做）
    notifier.dispatch(db, event, rule, device, is_recovery=False)


def _handle_recovering(
    db: Session,
    device: MonDevice,
    rule: AlertRule,
    state: AlertRuleState,
    breached: bool,
    value: float,
    recovery_n: int,
    now_ts: datetime,
    stats: Dict[str, int],
) -> None:
    """告警中/已确认状态下的恢复判定"""
    if breached:
        # 仍在超标：清零正常计数（滞回的核心 —— 恢复必须"连续" M 次正常）
        state.normal_count = 0
        return

    state.normal_count = (state.normal_count or 0) + 1
    state.breach_count = 0
    if state.normal_count < recovery_n:
        return

    # ---- 达到恢复条件 ----
    event = _find_active_event(db, state, device.id, rule.id)
    unit = alert_service.metric_unit(event.metric_key if event else rule.metric_key)
    if event is not None:
        # 先记下恢复前的信息用于日志：recover_event 会就地改状态
        event_title = event.title
        alert_service.recover_event(db, event)
        stats["recovered"] += 1
        alarm_logger.info(
            "告警恢复 | %s | 设备=%s(%s) | 当前值=%s%s | 连续 %s 次正常",
            event.event_no or event.id, device.name, device.ip, value, unit, recovery_n,
        )
        # 恢复通知同样走推送（且在收敛器里不受窗口限流限制）
        notifier.dispatch(db, event, rule, device, is_recovery=True)
    else:
        # 状态显示"告警中"但找不到活跃事件：可能是历史脏数据，或事件被人工删除。
        # 自愈：把状态重置为正常，避免该规则永久卡在"告警中"而不再触发任何新告警
        alarm_logger.warning(
            "规则状态异常自愈 | 设备=%s(%s) | 规则=%s | 状态=%s 但无活跃事件，已重置为正常",
            device.name, device.ip, rule.name, state.status,
        )
        event_title = rule.name

    state.status = STATE_NORMAL
    state.breach_count = 0
    state.normal_count = 0
    state.current_event_id = None
    alarm_logger.debug("告警关闭循环 | %s | %s", event_title, now_ts)


# ============================================================================
# 取值
# ============================================================================

def _resolve_value(
    device: MonDevice,
    rule: AlertRule,
    latest_map: Dict[Tuple[int, str], MetricLatest],
) -> Tuple[Optional[float], str]:
    """
    取规则要比较的数值。

    :return: (数值, 实际命中的指标键)。数值为 None 表示本轮无法评估。

    三种取值方式，按优先级：
      1. **在线状态**：用 device.online_status（设备表字段最权威），
         因为 metric_latest 里的 online_status 可能滞后一轮
         （先写指标、再回写设备状态，顺序上差一拍）。
      2. **精确键命中**：规则直接写了完整键，如 `ping_latency`、
         或针对具体端口的 `if_in_rate:GigabitEthernet0/0/1`。
      3. **基名命中**：规则写基名（如 `if_in_rate`），
         匹配该设备上所有 `if_in_rate:*` 端口，再按比较方向择一：
           - 大于/大于等于  → 取**最大**值（"任一端口的速率超标"）
           - 小于/小于等于  → 取**最小**值（"任一端口的剩余空间不足"）
         取极值而不是平均值：单个端口跑满就是真问题，
         平均到 48 个端口上会被完全抹平（这是网络告警最经典的漏报原因）。
    """
    key = str(rule.metric_key or "").strip()
    if not key:
        return None, key

    # ---- 1. 在线状态 ----
    if key == METRIC_KEY_OFFLINE:
        status = (device.online_status or "").lower()
        value = ONLINE_VALUE.get(status)
        if value is None:
            # unknown：从未成功采集过（新导入设备），不评估 ——
            # 否则批量导入 200 台设备会瞬间刷出 200 条"设备离线"
            return None, key
        return value, key

    # ---- 2. 精确键 ----
    row = latest_map.get((device.id, key))
    if row is not None and row.metric_value is not None:
        return float(row.metric_value), key

    # ---- 3. 基名匹配端口类指标 ----
    prefix = f"{key}{metric_service.PORT_KEY_SEPARATOR}"
    candidates = [
        (metric_key[len(prefix):], latest.metric_value)
        for (device_id, metric_key), latest in latest_map.items()
        if device_id == device.id
        and metric_key.startswith(prefix)
        and latest.metric_value is not None
    ]
    if not candidates:
        return None, key

    operator = str(rule.operator or "").lower().strip()
    pick_max = operator in ("gt", "ge")
    port_name, value = (max if pick_max else min)(candidates, key=lambda item: item[1])
    return float(value), f"{key}{metric_service.PORT_KEY_SEPARATOR}{port_name}"


def _metric_display_name(resolved_key: str, catalog: Dict[str, MetricCatalog]) -> str:
    """
    指标中文名。端口类指标带上端口名，让运维一眼看出是哪个口。

    例：if_in_rate:GigabitEthernet0/0/1 → "端口入向速率(GE0/0/1)"
    """
    base = metric_service.base_metric_key(resolved_key)
    catalog_item = catalog.get(base)
    name = catalog_item.metric_name if catalog_item else base
    if resolved_key != base:
        port = resolved_key.split(metric_service.PORT_KEY_SEPARATOR, 1)[1]
        return f"{name}({port})"
    return name


# ============================================================================
# 数据装载与规则筛选
# ============================================================================

def _load_rules(db: Session) -> List[AlertRule]:
    """加载全部已启用规则（一次查全表，规则表通常只有几十行）"""
    return list(db.execute(
        select(AlertRule).where(AlertRule.enabled.is_(True)).order_by(AlertRule.id.asc())
    ).scalars().all())


def _load_devices(db: Session, device_ids: Optional[Sequence[int]]) -> List[MonDevice]:
    """加载待评估设备"""
    stmt = select(MonDevice)
    if device_ids is None:
        stmt = stmt.where(MonDevice.enabled.is_(True))
    else:
        ids = sorted({int(i) for i in device_ids if i is not None})
        if not ids:
            return []
        stmt = stmt.where(MonDevice.id.in_(ids))
    return list(db.execute(stmt).scalars().all())


def _load_catalog(db: Session) -> Dict[str, MetricCatalog]:
    """指标字典（几十行，一次取全表比按需查更省）"""
    return {
        row.metric_key: row
        for row in db.execute(select(MetricCatalog)).scalars().all()
    }


def _rules_for(
    device: MonDevice, rules: Sequence[AlertRule], stats: Dict[str, int]
) -> List[AlertRule]:
    """
    筛出对某台设备生效的规则。

    **优先级语义（设备级优先于全局级，按指标键分组）**

      对同一个 metric_key：只要该设备有专属规则，就**不再**用全局规则。
      这样"给核心交换机单独放宽 CPU 阈值"才能真的生效；
      若全局规则也一起生效，运维会同时收到"85% 告警"和"95% 不告警"，
      完全无法解释自己的配置产生了什么行为。

      而同一个 metric_key 上的**多条全局规则**是允许的（都生效）：
      "CPU > 80 一般告警 + CPU > 95 紧急告警"是常见且合理的配置。
    """
    by_metric_all: List[AlertRule] = []
    by_metric_device: List[AlertRule] = []
    for rule in rules:
        if rule.device_id and rule.device_id != device.id:
            continue                                  # 属于别的设备
        if rule.device_id == device.id:
            by_metric_device.append(rule)
        else:
            by_metric_all.append(rule)

    if not by_metric_device:
        return by_metric_all

    device_metrics = {r.metric_key for r in by_metric_device}
    # 设备专属规则覆盖了这些指标键，全局规则中对应的条目要剔除
    return [r for r in by_metric_all if r.metric_key not in device_metrics] + by_metric_device


def _find_active_event(
    db: Session, state: AlertRuleState, device_id: int, rule_id: int
) -> Optional[AlertEvent]:
    """
    找出该状态行对应的活跃告警事件。

    先按 state.current_event_id 精确取；取不到时**按指纹兜底查一次**。
    为什么需要兜底：老数据的 current_event_id 可能为空
    （早期版本未写该字段，或事件被人工清理），
    没有兜底就会出现"指标已恢复正常，但规则状态永远停在告警中"，
    该规则从此再也不会产生任何新告警 —— 属于最危险的一类静默失效。
    """
    if state.current_event_id:
        event = db.get(AlertEvent, state.current_event_id)
        if event is not None and event.status in (EVENT_STATUS_FIRING, EVENT_STATUS_ACKED):
            return event

    fingerprint = alert_service.build_fingerprint(device_id, rule_id)
    return db.execute(
        select(AlertEvent)
        .where(
            AlertEvent.fingerprint == fingerprint,
            AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED]),
        )
        .order_by(AlertEvent.id.desc())
        .limit(1)
    ).scalars().first()


__all__ = ["evaluate_devices", "OPERATORS", "STATE_NORMAL", "STATE_FIRING", "STATE_ACKED"]
