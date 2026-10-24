"""
日志模型（sys_log / collect_log / alarm_log）
===============================================================================
三类日志职责区分（对应非功能需求"日志落地保存，可查询"）：

| 表            | 记录内容                     | 写入方                    | 查询入口        |
|---------------|------------------------------|---------------------------|-----------------|
| sys_log       | 登录、增删改操作、系统异常   | API 层 / 异常处理器       | 系统日志页      |
| collect_log   | 每台设备每次采集结果与耗时   | 调度器 (CollectJob)       | 采集日志页      |
| alarm_log     | 告警触发/恢复/确认/通知动作  | 告警引擎 (AlarmService)   | 告警详情时间线  |

为什么同时写文件又写数据库：
文件是全量流水（含第三方库细节），数据库是结构化可检索记录（按设备/时间/状态筛选）。
两者互补，不是冗余——排查"某台设备为什么采集失败"用数据库定位，
深挖堆栈细节再看 collect.log 文件。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, now, pk_type

# 采集结果状态
COLLECT_SUCCESS = "success"
COLLECT_FAIL = "fail"
COLLECT_TIMEOUT = "timeout"

# 告警日志动作类型
ALARM_ACTION_TRIGGER = "trigger"       # 触发告警
ALARM_ACTION_RECOVER = "recover"       # 告警恢复
ALARM_ACTION_ACK = "ack"               # 人工确认
ALARM_ACTION_NOTIFY = "notify"         # 渠道推送
ALARM_ACTION_CONVERGE = "converge"     # 被收敛（未单独推送）
ALARM_ACTION_CLOSE = "close"           # 关闭事件
ALARM_ACTION_SUPPRESS = "suppress"     # 静默期内抑制推送

ALARM_ACTION_TEXT = {
    ALARM_ACTION_TRIGGER: "触发告警",
    ALARM_ACTION_RECOVER: "告警恢复",
    ALARM_ACTION_ACK: "人工确认",
    ALARM_ACTION_NOTIFY: "发送通知",
    ALARM_ACTION_CONVERGE: "告警收敛",
    ALARM_ACTION_CLOSE: "关闭事件",
    ALARM_ACTION_SUPPRESS: "静默抑制",
}


class SysLog(Base):
    """系统日志：用户操作与系统异常"""

    __tablename__ = "sys_log"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, comment="INFO/WARNING/ERROR")
    module: Mapped[Optional[str]] = mapped_column(String(64), comment="来源模块")
    action: Mapped[Optional[str]] = mapped_column(String(64), comment="动作，如 login/create_device")
    message: Mapped[Optional[str]] = mapped_column(Text, comment="日志内容")
    user_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="操作人 ID")
    username: Mapped[Optional[str]] = mapped_column(String(64), comment="操作人账号")
    ip: Mapped[Optional[str]] = mapped_column(String(64), comment="来源 IP")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, index=True)


class CollectLog(Base):
    """
    采集日志：每台设备每轮采集一条。

    用途：
      1. 排查"某台设备采集失败"的具体原因（error_msg）
      2. 统计设备采集成功率与平均耗时（大屏"采集健康度"）
      3. 识别慢设备（duration_ms 偏高，考虑改用 SSH 低频采集）
    """

    __tablename__ = "collect_log"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="设备 ID")
    device_name: Mapped[Optional[str]] = mapped_column(String(128), comment="设备名称（冗余）")
    device_ip: Mapped[Optional[str]] = mapped_column(String(64), comment="设备 IP（冗余）")
    protocol: Mapped[Optional[str]] = mapped_column(String(16), comment="采集协议")
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="success/fail/timeout")
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="采集耗时（毫秒）")
    error_msg: Mapped[Optional[str]] = mapped_column(Text, comment="失败原因")
    metric_count: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment="本轮指标条数")
    collect_time: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, comment="采集时间")


class AlarmLog(Base):
    """告警处理日志：构成告警详情页的"处理时间线" """

    __tablename__ = "alarm_log"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    event_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="告警事件 ID")
    device_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="设备 ID")
    action: Mapped[str] = mapped_column(String(16), nullable=False, comment="动作类型")
    channel: Mapped[Optional[str]] = mapped_column(String(16), comment="通知渠道")
    level: Mapped[Optional[str]] = mapped_column(String(16), comment="告警级别")
    message: Mapped[Optional[str]] = mapped_column(Text, comment="描述")
    operator: Mapped[Optional[str]] = mapped_column(String(64), comment="操作人（确认时）")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now)

    @property
    def action_text(self) -> str:
        return ALARM_ACTION_TEXT.get(self.action, self.action)
