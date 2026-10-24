"""
告警收敛器：指纹窗口限流 + 静默期抑制
===============================================================================
**为什么需要这一层（需求原文："防止消息轰炸"）**

没有收敛的告警系统会这样：一台交换机链路抖动，30 秒内"离线→恢复"来回 20 次，
运维手机上就是 40 条推送。几次之后，所有人都会把告警群设为免打扰，
真正的大故障也就没人看了 —— **告警系统的第一死因不是漏报，是被静音。**

**三层防护的分工（不要混在一起做，否则规则冲突时无法解释行为）**

| 层次     | 实现位置        | 解决的问题                                   |
|----------|-----------------|----------------------------------------------|
| 抖动抑制 | `evaluator.py`  | 连续 N 次超标才报（单次毛刺不报）             |
| 滞回恢复 | `evaluator.py`  | 连续 M 次正常才恢复（阈值边缘不来回刷）       |
| **总量限流** | **本文件**  | 窗口内最多推 N 条（真的反复发生时也不刷屏）  |
| 计划内抑制 | **本文件**  | 维护窗口静默（我知道我要停机，别报警）       |

**限流的计数口径：按「告警事件数」而不是「推送条数」**

`converge_max=3` 的语义是"窗口内最多推送 3 条告警"，
不是"最多发 3 封邮件"。所以统计时用 `COUNT(DISTINCT event_id)`：
一条告警同时发邮件+企微算 **1 条**，若按记录数算就会算成 2 条，
配了 3 个渠道的客户会发现限额莫名其妙地被用掉三分之二。

**计数为什么直接查 alert_notify_record 而不另建计数表**
  1. 表结构在第 2 步已确认，不想为限流再加一张表；
  2. 推送记录本身就是"发过什么"的流水，用它反推次数是精确的；
  3. **天然持久化**：平台重启（升级、断电、服务重启）后计数不会归零。
     若用内存字典，重启瞬间会把所有在途告警当成"窗口内还没推过"重新推一遍 ——
     这正是运维最反感的"一重启就刷屏"。
  4. 只统计 status in (success, pending) 的记录：失败的推送（渠道没配好）
     不该占用限额，否则 email 没配好会把 wecom 的额度吃掉。

**恢复通知不受收敛限制**
"故障恢复了"是运维最需要知道的消息，被限量压掉会造成误判。
它只受静默期约束（维护窗口内确实不需要知道）。
===============================================================================
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alert import AlertEvent, AlertNotifyRecord, AlertRule
from app.models.log import ALARM_ACTION_CONVERGE, ALARM_ACTION_SUPPRESS
from app.services.alert_service import NOTIFY_PENDING, NOTIFY_SUCCESS


@dataclass
class ConvergeDecision:
    """
    收敛判定结果。

    :param allowed: 是否允许本次推送
    :param reason: 被抑制的原因（会写进告警时间线，必须是人话）
    :param action: 被抑制时对应的告警日志动作（converge / suppress）
    :param window_count: 窗口内已推送的告警事件数（用于写日志与排障）
    """

    allowed: bool
    reason: str = ""
    action: str = ""
    window_count: int = 0


# 允许推送的空判定（不要在调用处到处构造，统一出口便于将来加字段）
ALLOW = ConvergeDecision(allowed=True)


# ============================================================================
# 一、窗口内已推送的告警事件数
# ============================================================================

def count_recent_notified_events(db: Session, fingerprint: str, window_seconds: int) -> int:
    """
    统计某指纹在最近 window_seconds 秒内**已推送过的告警事件数**。

    用 COUNT(DISTINCT event_id)：一条告警发多个渠道只算一条。
    """
    if window_seconds <= 0 or not fingerprint:
        return 0

    since = datetime.now() - timedelta(seconds=window_seconds)
    stmt = (
        select(func.count(func.distinct(AlertNotifyRecord.event_id)))
        # join 而不是子查询：alert_notify_record 与 alert_event 都有索引，
        # 这条语句在 SQLite / MySQL 上生成的 SQL 完全一致（方言无关）
        .join(AlertEvent, AlertEvent.id == AlertNotifyRecord.event_id)
        .where(
            AlertEvent.fingerprint == fingerprint,
            AlertNotifyRecord.created_at >= since,
            AlertNotifyRecord.status.in_([NOTIFY_SUCCESS, NOTIFY_PENDING]),
        )
    )
    return int(db.execute(stmt).scalar_one() or 0)


# ============================================================================
# 二、收敛判定
# ============================================================================

def decide(
    db: Session,
    *,
    rule: AlertRule,
    event: AlertEvent,
    is_recovery: bool = False,
) -> ConvergeDecision:
    """
    判定本条告警是否应该推送。

    :param is_recovery: 是否为"恢复通知"。恢复通知跳过窗口限流（只受静默期约束）

    调用时机：**在创建推送记录之前**。若先建记录再判断，会出现
    "创建了 pending 记录但决定不发"的悬空记录，被统计口径计入限额。

    **为什么 rule / event 强制关键字传参（`*` 之后）**

    这两个参数类型不同但都是 ORM 对象，写反了 Python 不会报任何错：
    `rule.is_silenced()` 会作用在 AlertEvent 上抛 AttributeError，
    而 dispatch() 的最外层 try/except 会把它吞掉并只记一条日志 ——
    表现是"平台一切正常，但永远没人收到告警"，属于最难查的一类故障。
    强制关键字传入后，写反会在第一次调用时立刻 TypeError。
    """
    # ---- 1. 静默期：仍评估、仍记录事件，只是不推送 ----
    if rule.is_silenced():
        until = rule.silence_until.strftime("%Y-%m-%d %H:%M") if rule.silence_until else "未知"
        return ConvergeDecision(
            allowed=False,
            action=ALARM_ACTION_SUPPRESS,
            reason=f"规则「{rule.name}」处于静默期（至 {until}），本条仅记录不推送",
        )

    # ---- 2. 恢复通知不受窗口限流 ----
    if is_recovery:
        return ALLOW

    # ---- 3. 收敛窗口限流 ----
    window = int(rule.converge_window or 0)
    cap = int(rule.converge_max or 0)
    if window <= 0 or cap <= 0:
        # converge_window=0 表示该规则不做收敛（前端有对应的开关说明）
        return ALLOW

    sent = count_recent_notified_events(db, event.fingerprint, window)
    if sent >= cap:
        return ConvergeDecision(
            allowed=False,
            action=ALARM_ACTION_CONVERGE,
            window_count=sent,
            reason=(
                f"{window} 秒内该告警已推送 {sent} 次（规则上限 {cap} 次），"
                f"本条合并计数不单独推送"
            ),
        )

    return ConvergeDecision(allowed=True, window_count=sent)


def describe_rule(rule: AlertRule) -> str:
    """
    生成规则限流策略的中文描述（写进告警时间线，让运维一眼看懂为什么消息变少了）。

    现场最常被问到的问题是"为什么这次只收到一条告警"，
    把策略原文写在触发记录里，比让实施人员解释快得多。
    """
    parts = [f"连续 {max(int(rule.duration_n or 1), 1)} 次超标触发",
             f"连续 {max(int(rule.recovery_n or 1), 1)} 次正常恢复"]
    window = int(rule.converge_window or 0)
    cap = int(rule.converge_max or 0)
    if window > 0 and cap > 0:
        parts.append(f"{window} 秒内最多推送 {cap} 条")
    else:
        parts.append("不做窗口收敛")
    if rule.is_silenced():
        until: Optional[datetime] = rule.silence_until
        parts.append(f"静默期至 {until.strftime('%Y-%m-%d %H:%M') if until else '未知'}")
    return "；".join(parts)


__all__ = [
    "ConvergeDecision",
    "ALLOW",
    "decide",
    "count_recent_notified_events",
    "describe_rule",
]
