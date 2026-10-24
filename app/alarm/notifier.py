"""
告警通知分发器：构造消息 → 收敛判定 → 落库 → 异步多渠道发送
===============================================================================
**本模块是三件事的粘合层**（对应需求"实现邮件、企业微信、钉钉、微信推送"）

    evaluator（判定该告警）
        └──► notifier.dispatch()
                ├── build_message()   把 事件+规则+设备 翻成与渠道无关的 AlertMessage
                ├── converger.decide() 静默期 / 窗口限流判定
                ├── 落 AlertNotifyRecord（pending）+ AlarmLog
                └── 投递到**通知专用线程池** → channels.send()

**为什么不复用采集线程池（这是本模块最重要的一个决定）**

一个真实场景：机房断电恢复后，200 台设备同时离线。
若把推送任务丢进采集线程池（60 个线程），每条任务在失败重试时
要 `sleep` 等退避（最长 7 秒），400 条任务（200 事件 × 2 渠道）会瞬间占满
全部线程。后果是：

    采集线程被占满 → 所有设备本轮采不到 → 全部判离线 → 产生更多告警
    → 推送任务更多 → 线程更紧张 …… 雪崩。

所以通知用**独立的线程池**（`NOTIFY_POOL_SIZE` 个线程）。
采集线程池再忙，告警也能推出去；推送再积压，也不影响采集。
两个池互不争抢，这是"故障时系统仍可用"的关键。

**为什么先落库再异步发送，而不是发完再记**

若"先发送、成功后再写记录"，则平台在发送过程中崩溃/被重启时，
这条推送既没有记录、也没有发出，事后完全无从查证；
更糟的是收敛计数依赖推送记录，记录缺失会让限流失效、重新刷屏。

先落 `pending` 再发，任何时刻数据库里都能回答
"这条告警正在发给谁、发到哪一步了"。发送完成后原地改成 success/fail。

**重试策略**
  只对 `retryable=True` 的结果重试（网络超时、5xx、机器人限流）；
  "渠道没配好""密钥错"这类确定性失败立即结束，
  重试只是白白占用通知线程并拖慢其它渠道的送达。
===============================================================================
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.alarm import converger
from app.alarm.channels import AlertMessage, BaseChannel, ChannelResult, ChannelRegistry
from app.core.logger import alarm_logger
from app.models.alert import (
    EVENT_STATUS_RECOVERED,
    AlertEvent,
    AlertNotifyRecord,
    AlertRule,
)
from app.models.device import MonDevice
from app.models.log import ALARM_ACTION_NOTIFY
from app.services import config_service, log_service
from app.services.alert_service import (
    NOTIFY_FAIL,
    NOTIFY_PENDING,
    NOTIFY_SUCCESS,
    metric_unit,
)

# 通知专用线程池规模。
# 取值考虑：单条推送最长耗时 = 3 次尝试 × 超时 10 秒 + 退避 7 秒 ≈ 37 秒。
# 8 个线程意味着 8 条推送可以并行，配合收敛限流（同指纹窗口内最多 3 条）
# 已足够消化绝大多数告警风暴；再大反而会给企业微信/钉钉造成限流。
NOTIFY_POOL_SIZE = 8

# 失败重试次数（不含首次），以及每次重试前的退避秒数。
# 退避递增而不固定：企业微信/钉钉的限流往往是短时突发，
# 1 秒后重试仍会被拒，3 秒后成功率明显更高。
MAX_RETRY = 2
RETRY_BACKOFF = (2, 5)

# 进程级通知线程池（懒创建）
_pool_lock = threading.Lock()
_notify_pool: Optional[ThreadPoolExecutor] = None


def _get_pool() -> ThreadPoolExecutor:
    """获取通知线程池（首次调用创建，进程内单例）"""
    global _notify_pool
    if _notify_pool is None:
        with _pool_lock:
            if _notify_pool is None:
                _notify_pool = ThreadPoolExecutor(
                    max_workers=NOTIFY_POOL_SIZE, thread_name_prefix="notify"
                )
                alarm_logger.info("告警通知线程池已启动，并发数 %s", NOTIFY_POOL_SIZE)
    return _notify_pool


def shutdown(wait: bool = False) -> None:
    """关闭通知线程池（平台退出时调用；wait=True 表示等发完再退）"""
    global _notify_pool
    with _pool_lock:
        if _notify_pool is not None:
            _notify_pool.shutdown(wait=wait)
            _notify_pool = None
            alarm_logger.info("告警通知线程池已关闭")


# ============================================================================
# 一、消息构造
# ============================================================================

def build_message(
    event: AlertEvent,
    rule: Optional[AlertRule],
    device: Optional[MonDevice],
    platform_name: str = "亿网通IT运维监控管理平台",
    platform_url: str = "",
    is_recovery: Optional[bool] = None,
) -> AlertMessage:
    """
    把「事件 + 规则 + 设备」翻译成与渠道无关的消息体。

    :param is_recovery: 不传则按 event.status 推断。
        显式传入的原因是恢复通知的构造发生在 `recover_event()` **之前**，
        此时 event.status 还是 firing，靠 status 推断会得到"触发通知"的错误文案。

    所有"该带 % 还是 ms"的判断集中在 metric_unit()（与告警详情页共用同一张表），
    避免邮件里写 93.5%、企微里写 93.5ms 这种内容不一致的情况。
    """
    if is_recovery is None:
        is_recovery = event.status == EVENT_STATUS_RECOVERED

    unit = metric_unit(event.metric_key).strip()
    if event.metric_value is None:
        value_text = "无法获取"
    else:
        value_text = f"{event.metric_value:g}{unit}"

    threshold_text = ""
    if event.threshold is not None:
        operator_text = rule.operator_text if rule is not None else (event.operator or "")
        threshold_text = f"{operator_text} {event.threshold:g}{unit}".strip()

    return AlertMessage(
        event_id=event.id,
        event_no=event.event_no or f"EV{event.id}",
        title=event.title or "设备告警",
        device_name=event.device_name or (device.name if device else "未知设备"),
        device_ip=event.device_ip or (device.ip if device else "-"),
        device_type=device.device_type if device else "",
        location=(device.location or "") if device else "",
        metric_name=event.metric_name or event.metric_key or "指标",
        metric_value_text=value_text,
        threshold_text=threshold_text,
        level=event.level,
        occurred_at=event.recover_at if is_recovery and event.recover_at else (
            event.last_occur_at or datetime.now()
        ),
        is_recovery=bool(is_recovery),
        duration_text=_duration_text(event.first_occur_at, event.recover_at),
        occur_count=event.occur_count or 1,
        platform_name=platform_name,
        platform_url=platform_url,
        content=event.content or "",
    )


def _duration_text(start: Optional[datetime], end: Optional[datetime]) -> str:
    """
    告警持续时长（中文口语化写法：3秒 / 12分钟 / 2小时13分 / 1天4小时）。

    与 schemas/alert.py 的 `duration_text` 计算口径保持一致 ——
    邮件里写"已持续 2小时13分"、平台上显示"2小时13分"，
    两处不一致会让运维怀疑数据有问题。
    """
    if not start:
        return ""
    seconds = int(((end or datetime.now()) - start).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        return f"{seconds // 60}分钟"
    if seconds < 86400:
        return f"{seconds // 3600}小时{(seconds % 3600) // 60}分"
    return f"{seconds // 86400}天{(seconds % 86400) // 3600}小时"


# ============================================================================
# 二、分发入口（由 evaluator 调用）
# ============================================================================

def dispatch(
    db: Session,
    event: AlertEvent,
    rule: Optional[AlertRule],
    device: Optional[MonDevice] = None,
    is_recovery: bool = False,
) -> Dict[str, Any]:
    """
    分发一条告警到规则配置的所有渠道。

    :return: 摘要 {"queued": 投递条数, "channels": [...], "suppressed": bool, "reason": str}
        返回值只用于记日志与测试断言，不参与业务判断。

    **本函数不抛异常**：告警推送失败绝不能反向影响采集与评估流程
    （指标已经落库了，推送是"最后一公里"，它坏了不该让数据也丢）。
    """
    stats: Dict[str, Any] = {"queued": 0, "channels": [], "suppressed": False, "reason": ""}
    try:
        if rule is None:
            stats["reason"] = "告警无关联规则，跳过推送"
            return stats

        channel_keys = [str(c).strip().lower() for c in (rule.channel_list or []) if str(c).strip()]
        if not channel_keys:
            stats["reason"] = "该规则未配置通知渠道"
            return stats

        # ---- 1. 收敛判定（必须在创建推送记录之前，否则 pending 记录会占掉限额）----
        decision = converger.decide(db, rule=rule, event=event, is_recovery=is_recovery)
        if not decision.allowed:
            stats["suppressed"] = True
            stats["reason"] = decision.reason
            log_service.write_alarm_log(
                db, event.id, event.device_id, decision.action, decision.reason,
                level=event.level, commit=False,
            )
            db.commit()
            alarm_logger.info("告警推送被抑制 | 事件=%s | %s", event.event_no, decision.reason)
            return stats

        # ---- 2. 构造消息 ----
        platform = config_service.get_platform_info(db)
        message = build_message(
            event, rule, device,
            platform_name=platform.get("platform_name") or "亿网通IT运维监控管理平台",
            platform_url=platform.get("platform_url") or "",
            is_recovery=is_recovery,
        )

        # ---- 3. 逐渠道落 pending 记录 ----
        pending_records: List[Tuple[int, str]] = []
        for key in channel_keys:
            channel = ChannelRegistry.get(key)
            if channel is None:
                # 规则里写了平台不认识的渠道（历史配置、手工改库）
                log_service.write_alarm_log(
                    db, event.id, event.device_id, ALARM_ACTION_NOTIFY,
                    f"通知渠道「{key}」未注册，已跳过"
                    f"（当前支持：{'/'.join(ChannelRegistry.available()) or '无'}）",
                    level=event.level, channel=key, commit=False,
                )
                continue

            record = AlertNotifyRecord(
                event_id=event.id,
                channel=key,
                target="",
                status=NOTIFY_PENDING,
                retry_count=0,
            )
            db.add(record)
            db.flush()            # 取 record.id，供异步任务回写
            pending_records.append((record.id, key))
            stats["channels"].append(key)

        if not pending_records:
            db.commit()
            stats["reason"] = "无可用渠道"
            return stats

        # 先提交再投递：保证异步任务被调度时，记录一定已经在库里
        db.commit()
        log_service.write_alarm_log(
            db, event.id, event.device_id, ALARM_ACTION_NOTIFY,
            f"开始推送（{len(pending_records)} 个渠道：{'/'.join(stats['channels'])}）"
            f"｜策略：{converger.describe_rule(rule)}",
            level=event.level, commit=False,
        )
        db.commit()

        # ---- 4. 投递到通知专用线程池 ----
        pool = _get_pool()
        for record_id, key in pending_records:
            pool.submit(_deliver, record_id, key, message)
            stats["queued"] += 1

        return stats

    except Exception as exc:  # noqa: BLE001
        # 推送分发失败不影响采集/评估：指标已落库，告警事件也已创建，
        # 运维仍能在平台上看到告警，只是没收到外部推送
        alarm_logger.exception("告警分发异常 | 事件=%s | %s", getattr(event, "id", "?"), exc)
        stats["reason"] = f"{type(exc).__name__}: {exc}"
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return stats


# ============================================================================
# 三、异步投递（在通知线程池中执行）
# ============================================================================

def _deliver(record_id: int, channel_key: str, message: AlertMessage) -> None:
    """
    实际发送一条推送（在通知线程中执行）。

    **自己不持有 Session**：SQLAlchemy Session 非线程安全，
    必须用 session_scope() 取线程本地会话（与采集任务同一套纪律）。
    """
    from app.core.database import session_scope

    channel = ChannelRegistry.get(channel_key)
    if channel is None:      # pragma: no cover - dispatch 已过滤
        return

    try:
        with session_scope() as db:
            record = db.get(AlertNotifyRecord, record_id)
            if record is None:
                alarm_logger.warning("推送记录 %s 不存在，可能已被日志清理任务删除", record_id)
                return
            if record.status != NOTIFY_PENDING:
                # 已被处理（重发场景下的重复提交），不重复发送
                return

            cfg = config_service.get_channel_settings(db, channel.config_group)
            result, attempts = _send_with_retry(channel, message, cfg)

            record.status = NOTIFY_SUCCESS if result.success else NOTIFY_FAIL
            record.retry_count = max(attempts - 1, 0)      # 记录"重试次数"而非"尝试次数"
            record.target = (result.target or "")[:255]
            # response_msg 截断：渠道返回的错误体可能很长（HTML 错误页）
            record.response_msg = (result.message or "")[:1000]
            record.sent_at = datetime.now()

            log_service.write_alarm_log(
                db, record.event_id, None, ALARM_ACTION_NOTIFY,
                f"{channel.text}推送{'成功' if result.success else '失败'}"
                f"（尝试 {attempts} 次）：{result.message}",
                level=message.level, channel=channel_key, commit=False,
            )
            db.commit()

            if result.success:
                alarm_logger.info(
                    "告警推送成功 | 事件=%s | %s | %s",
                    message.event_no, channel.text, result.message,
                )
            else:
                # 失败记 WARNING 级别：这是需要运维关注的事（可能只是渠道没配好）
                alarm_logger.warning(
                    "告警推送失败 | 事件=%s | %s | 尝试 %s 次 | %s",
                    message.event_no, channel.text, attempts, result.message,
                )

    except Exception as exc:  # noqa: BLE001
        # 线程池任务绝不允许抛异常：未捕获异常会让 Future 处于异常态，
        # 且若将来换成进程池会直接导致任务静默丢失
        alarm_logger.exception("推送任务异常 | 记录=%s | 渠道=%s | %s",
                               record_id, channel_key, exc)
        _mark_failed(record_id, channel_key, f"{type(exc).__name__}: {exc}")


def _send_with_retry(
    channel: BaseChannel, message: AlertMessage, cfg: Dict[str, Any]
) -> Tuple[ChannelResult, int]:
    """
    带重试的发送。

    :return: (发送结果, 尝试次数)。尝试次数从 1 开始计数。
    """
    last: ChannelResult = ChannelResult(success=False, message="未执行")
    for attempt in range(MAX_RETRY + 1):
        last = channel.send(message, cfg)
        if last.success:
            return last, attempt + 1
        if not last.retryable:
            # 确定性失败（渠道未启用 / 地址非法 / 密钥错误）→ 立即结束
            return last, attempt + 1
        if attempt < MAX_RETRY:
            time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
    return last, MAX_RETRY + 1


def _mark_failed(record_id: int, channel_key: str, reason: str) -> None:
    """兜底：把记录标成失败（发送过程本身崩了时也要留下痕迹）"""
    from app.core.database import session_scope

    try:
        with session_scope() as db:
            record = db.get(AlertNotifyRecord, record_id)
            if record is not None and record.status == NOTIFY_PENDING:
                record.status = NOTIFY_FAIL
                record.response_msg = reason[:1000]
                record.sent_at = datetime.now()
    except Exception as exc:  # noqa: BLE001 - 兜底的兜底，不再往上抛
        alarm_logger.error("标记推送记录失败（记录=%s）：%s | 原始原因：%s",
                           record_id, exc, reason)


# ============================================================================
# 四、供其它模块使用的辅助
# ============================================================================

def resend_record(record_id: int) -> bool:
    """
    重发一条已失败的推送（供"补发"类运维操作复用）。

    实现：把记录重置为 pending 后走与首次发送完全相同的代码路径
    —— "补发"与"首发"用两套逻辑，是"补发能成、首发不成"这类怪问题的来源。
    """
    from app.core.database import session_scope

    with session_scope() as db:
        record = db.get(AlertNotifyRecord, record_id)
        if record is None:
            return False
        if record.status == NOTIFY_SUCCESS:
            return False         # 已成功的重发没有意义，直接拒绝
        event = db.get(AlertEvent, record.event_id)
        if event is None:
            return False

        rule = db.get(AlertRule, event.rule_id) if event.rule_id else None
        device = db.get(MonDevice, event.device_id)
        platform = config_service.get_platform_info(db)
        message = build_message(
            event, rule, device,
            platform_name=platform.get("platform_name") or "亿网通IT运维监控管理平台",
            platform_url=platform.get("platform_url") or "",
            is_recovery=event.status == EVENT_STATUS_RECOVERED,
        )
        record.status = NOTIFY_PENDING
        record.response_msg = "人工触发重发"
        db.commit()

    _get_pool().submit(_deliver, record_id, record.channel, message)
    return True


def notify_status_text(record: AlertNotifyRecord) -> str:
    """推送记录状态的中文描述（告警详情页时间线展示用）"""
    return {
        NOTIFY_PENDING: "发送中",
        NOTIFY_SUCCESS: "发送成功",
        NOTIFY_FAIL: "发送失败",
    }.get(record.status, record.status)


def available_channels() -> List[Dict[str, str]]:
    """当前已注册的通知渠道清单（透传给前端）"""
    return ChannelRegistry.describe()


__all__ = [
    "dispatch",
    "build_message",
    "resend_record",
    "shutdown",
    "available_channels",
    "notify_status_text",
    "NOTIFY_POOL_SIZE",
    "MAX_RETRY",
]
