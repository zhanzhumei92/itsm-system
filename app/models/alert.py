"""告警模型（alert_rule / alert_rule_state / alert_event / alert_notify_record）—— 对应模块3"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, now, pk_type

# ============================================================================
# 告警级别（需求：紧急、重要、一般、提示）
# 集中定义中文名与配色，后端接口与前端大屏共用，避免两边各写一套对不上
# ============================================================================
LEVEL_CRITICAL = "critical"
LEVEL_MAJOR = "major"
LEVEL_MINOR = "minor"
LEVEL_INFO = "info"

ALERT_LEVEL_TEXT = {
    LEVEL_CRITICAL: "紧急",
    LEVEL_MAJOR: "重要",
    LEVEL_MINOR: "一般",
    LEVEL_INFO: "提示",
}

# 告警级别配色（全平台唯一事实源）。
# ⚠ 2026-09-22：随 UI 改版（暗色 → 亮色）调整为**白底可读版**。
#   旧色值（#E24B4A / #EF9F27 / #BA7517 / #378ADD）是为深色大屏选的，
#   放到白底卡片上饱和度不够、会被看成"脏橙色/灰红色"，级别区分度下降。
#   换色的唯一原则是保持"紧急=红、重要=橙、一般=黄、提示=蓝"的心智不变。
#
# 修改这里时注意：**同一件事有两处定义** ——
#   app/schemas/alert.py 的 level_color 属性里有一份同样的字典，必须同步改。
ALERT_LEVEL_COLOR = {
    LEVEL_CRITICAL: "#D93A3C",   # 紧急 — 正红
    LEVEL_MAJOR: "#C2740C",      # 重要 — 深橙（原 #EF9F27 在白底上偏浅）
    LEVEL_MINOR: "#B9891B",      # 一般 — 深金
    LEVEL_INFO: "#007ACC",       # 提示 — 品牌蓝，与站点主色一致
}

# 告警事件状态流转：firing 告警中 → acked 已确认 → recovered 已恢复 / closed 已关闭
EVENT_STATUS_FIRING = "firing"
EVENT_STATUS_ACKED = "acked"
EVENT_STATUS_RECOVERED = "recovered"
EVENT_STATUS_CLOSED = "closed"

EVENT_STATUS_TEXT = {
    EVENT_STATUS_FIRING: "告警中",
    EVENT_STATUS_ACKED: "已确认",
    EVENT_STATUS_RECOVERED: "已恢复",
    EVENT_STATUS_CLOSED: "已关闭",
}

# 通知渠道标识（四个渠道全部实现）
CHANNEL_EMAIL = "email"
CHANNEL_WECOM = "wecom"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_WECHAT = "wechat"

CHANNEL_TEXT = {
    CHANNEL_EMAIL: "邮件",
    CHANNEL_WECOM: "企业微信",
    CHANNEL_DINGTALK: "钉钉",
    CHANNEL_WECHAT: "微信",
}

# 特殊指标键：设备离线（不走阈值比较，由在线状态直接判定）
METRIC_KEY_OFFLINE = "online_status"

# 端口类指标键的分隔符（与 metric_service.PORT_KEY_SEPARATOR 约定一致）
PORT_KEY_SEPARATOR = ":"

# ---------------------------------------------------------------------------
# 指标单位表 —— **全平台唯一来源**
#
# 为什么放在模型层而不是某个 Service：同一场告警的单位要在四个地方保持一致 ——
# 平台告警详情、邮件正文、企业微信 markdown、钉钉 markdown。
# 各处各写一套「if metric_key == "cpu_usage": unit = "%"」的结果，
# 就是邮件里写 93.5%、企微里写 93.5ms —— 值班人员会怀疑哪个数字是真的。
#
# 放在 models/alert.py 的原因：它与 ALERT_LEVEL_TEXT / CHANNEL_TEXT 同类，
# 都是"展示元数据"；且模型层被 Service 与 Schema 共同依赖，不会引入循环 import。
#
# ⚠ **本表必须与 sql/init_data.sql 的 metric_catalog.unit 逐项一致**：
#   这里少一项不报错，只是通知里那个指标少一个单位（"温度 78" 而不是 "78℃"），
#   属于最难被发现的一类缺陷。已由 scripts/static_audit.py 检查 6 双向守住。
#   键名必须是采集器真正产出的 metric_key，写错名字等价于没写
#   （本表最初就写了一个不存在的 "temperature"，而字典里真实的键是 device_temp）。
#
# 只登记**有单位**的指标：状态类（if_status / online_status / device_version ...）
# 本身没有单位，不登记即可，metric_unit() 返回空串是正确行为。
# ---------------------------------------------------------------------------
METRIC_UNITS = {
    "cpu_usage": "%",
    "mem_usage": "%",
    "mem_total": "MB",
    "mem_used": "MB",
    "disk_usage": "%",
    "disk_total": "GB",
    "disk_used": "GB",
    "disk_free": "GB",
    "if_in_rate": "Mbps",
    "if_out_rate": "Mbps",
    "if_in_octets": "Byte",
    "if_out_octets": "Byte",
    "if_bandwidth": "Mbps",
    "ping_latency": "ms",
    "ping_loss": "%",
    "ping_max": "ms",
    "ping_jitter": "ms",
    "device_uptime": "小时",
    "device_temp": "℃",
    "process_count": "个",
}


def metric_unit(metric_key: Optional[str]) -> str:
    """
    取指标单位（按**基名**匹配，端口类指标如 if_in_rate:Gi0/0/1 也能命中）。

    :return: 单位字符串；未登记的指标返回空串（而不是 None），
             便于调用方直接 f"{value}{unit}" 拼接而不必判空。
    """
    if not metric_key:
        return ""
    return METRIC_UNITS.get(str(metric_key).split(PORT_KEY_SEPARATOR, 1)[0], "")


class AlertRule(Base, TimestampMixin):
    """
    告警规则。

    设计要点（对应需求"避免瞬时抖动误报"与"防止消息轰炸"）：
      - duration_n ：连续 N 次采集都超标才真正触发（计数器在 AlertRuleState）
      - recovery_n ：连续 M 次正常才判定恢复（滞回，避免阈值边缘反复告警/恢复刷屏）
      - converge_window + converge_max：同一指纹在窗口内最多推送 N 条，其余合并计数
      - silence_until：维护窗口静默期，规则仍评估但不推送

    device_id 为 NULL 表示**全局规则**，对所有同类设备生效；
    某台设备需要特殊阈值时，再单独建一条绑定该设备的规则（优先级更高）。
    """

    __tablename__ = "alert_rule"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="规则名称")
    device_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="NULL=全局规则")
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="指标键，online_status 为离线")
    operator: Mapped[str] = mapped_column(String(8), default="gt", nullable=False, comment="gt/lt/ge/le/eq/ne")
    threshold: Mapped[float] = mapped_column(Float, nullable=False, comment="阈值")
    duration_n: Mapped[int] = mapped_column(Integer, default=3, nullable=False, comment="连续N次超标触发")
    recovery_n: Mapped[int] = mapped_column(Integer, default=3, nullable=False, comment="连续M次正常恢复")
    level: Mapped[str] = mapped_column(String(16), default="major", nullable=False, comment="紧急/重要/一般/提示")
    converge_window: Mapped[int] = mapped_column(Integer, default=300, nullable=False, comment="收敛窗口（秒）")
    converge_max: Mapped[int] = mapped_column(Integer, default=3, nullable=False, comment="窗口内最大推送数")
    notify_channels: Mapped[Optional[str]] = mapped_column(
        String(512), default="[]", comment='JSON 数组 ["email","wecom","dingtalk","wechat"]'
    )
    silence_until: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="静默截止时间")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remark: Mapped[Optional[str]] = mapped_column(String(255))

    # ---------------- JSON 字段读写封装 ----------------
    @property
    def channel_list(self) -> List[str]:
        """解析通知渠道列表（容错：脏数据不抛异常，退化为空列表）"""
        try:
            data = json.loads(self.notify_channels or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @channel_list.setter
    def channel_list(self, channels: List[str]) -> None:
        self.notify_channels = json.dumps(list(channels or []), ensure_ascii=False)

    @property
    def level_text(self) -> str:
        return ALERT_LEVEL_TEXT.get(self.level, self.level)

    @property
    def operator_text(self) -> str:
        """比较符中文描述，用于告警内容拼装与前端展示"""
        return {
            "gt": "大于", "lt": "小于", "ge": "大于等于",
            "le": "小于等于", "eq": "等于", "ne": "不等于",
        }.get(self.operator, self.operator)

    def is_silenced(self) -> bool:
        """是否处于静默期"""
        return bool(self.silence_until and self.silence_until > now())


class AlertRuleState(Base):
    """
    告警规则运行状态（连续计数器）。

    **为什么必须落库而不能只放内存**：
    平台重启（升级、断电、Windows 服务重启）后，若计数器丢失，
    下一轮采集会把所有"已在告警中"的设备重新当成首次触发，
    导致重启瞬间批量重复告警——这是运维最反感的现象。

    breach_count：连续超标次数，达到 rule.duration_n 即触发
    normal_count：连续正常次数，达到 rule.recovery_n 即恢复

    ⚠ **(rule_id, device_id) 唯一索引必须显式声明**：
    alert_service.get_rule_state() 用 scalar_one_or_none() 读取状态，
    一旦同一 (规则, 设备) 存在多行（唯一索引缺失 + 并发评估），
    该方法会抛 MultipleResultsFound 让整批告警评估中断。
    索引名与 sql/schema_sqlite.sql 的 uk_rule_state 保持一致。
    """

    __tablename__ = "alert_rule_state"
    __table_args__ = (
        Index("uk_rule_state", "rule_id", "device_id", unique=True),
    )

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="规则 ID")
    device_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="设备 ID")
    breach_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="连续超标次数")
    normal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="连续正常次数")
    status: Mapped[str] = mapped_column(
        String(16), default="normal", nullable=False, comment="normal/firing/acked/recovered"
    )
    last_value: Mapped[Optional[float]] = mapped_column(Float, comment="最近一次评估值")
    last_eval_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最近评估时间")
    last_fire_time: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最近触发时间")
    current_event_id: Mapped[Optional[int]] = mapped_column(
        pk_type(), comment="当前未关闭的告警事件 ID，用于恢复时定位事件"
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, onupdate=now)


class AlertEvent(Base, TimestampMixin):
    """
    告警事件（告警中心主表）。

    fingerprint = md5(device_id + rule_id)：同一设备同一规则的告警归为一条事件，
    反复触发只累加 occur_count 并刷新 last_occur_at，不产生新记录
    —— 这是"告警收敛"的落库基础，也是告警列表不被刷屏的关键。
    """

    __tablename__ = "alert_event"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    event_no: Mapped[Optional[str]] = mapped_column(String(64), comment="事件编号，便于口头沟通")
    rule_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="规则 ID")
    device_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="设备 ID")
    device_name: Mapped[Optional[str]] = mapped_column(String(128), comment="设备名称（冗余）")
    device_ip: Mapped[Optional[str]] = mapped_column(String(64), comment="设备 IP（冗余）")
    metric_key: Mapped[Optional[str]] = mapped_column(String(64), comment="指标键")
    metric_name: Mapped[Optional[str]] = mapped_column(String(64), comment="指标中文名（冗余）")
    metric_value: Mapped[Optional[float]] = mapped_column(Float, comment="触发时指标值")
    threshold: Mapped[Optional[float]] = mapped_column(Float, comment="触发时阈值")
    operator: Mapped[Optional[str]] = mapped_column(String(8), comment="比较符")
    level: Mapped[str] = mapped_column(String(16), nullable=False, comment="critical/major/minor/info")
    title: Mapped[Optional[str]] = mapped_column(String(255), comment="告警标题")
    content: Mapped[Optional[str]] = mapped_column(Text, comment="告警详情文本")
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, comment="md5(device_id+rule_id)")
    status: Mapped[str] = mapped_column(
        String(16), default=EVENT_STATUS_FIRING, nullable=False, comment="firing/acked/recovered/closed"
    )
    occur_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="累计触发次数")
    first_occur_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="首次发生时间")
    last_occur_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最近发生时间")
    recover_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="恢复时间")
    ack_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="人工确认时间")
    ack_user: Mapped[Optional[str]] = mapped_column(String(64), comment="确认人")
    ack_remark: Mapped[Optional[str]] = mapped_column(String(255), comment="确认备注")

    # ---------------- 展示辅助属性 ----------------
    @property
    def level_text(self) -> str:
        return ALERT_LEVEL_TEXT.get(self.level, self.level)

    @property
    def status_text(self) -> str:
        return EVENT_STATUS_TEXT.get(self.status, self.status)

    @property
    def level_color(self) -> str:
        return ALERT_LEVEL_COLOR.get(self.level, "#8B9AB0")

    @property
    def is_active(self) -> bool:
        """是否仍处于未恢复状态（大屏"未处理告警数"统计口径）"""
        return self.status in (EVENT_STATUS_FIRING, EVENT_STATUS_ACKED)


class AlertNotifyRecord(Base):
    """
    告警通知记录：一个事件 × 一个渠道 = 一条记录。

    为什么每渠道单独记：邮件成功但企微失败时，运维需要知道"哪条没送到"并能重发；
    合并成一条就丢失了这个粒度。retry_count 支撑"失败自动重试 2 次"。
    """

    __tablename__ = "alert_notify_record"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="告警事件 ID")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, comment="email/wecom/dingtalk/wechat")
    target: Mapped[Optional[str]] = mapped_column(String(255), comment="收件人 / 机器人标识")
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, comment="pending/success/fail"
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="重试次数")
    response_msg: Mapped[Optional[str]] = mapped_column(Text, comment="渠道返回信息 / 错误原因")
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="发送时间")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now)

    @property
    def channel_text(self) -> str:
        return CHANNEL_TEXT.get(self.channel, self.channel)
