"""系统配置模型（sys_config）—— 对应模块5：统一配置告警密钥与全局采集参数"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, now, pk_type

# 配置分组（前端"系统配置"页面按分组渲染为不同 Tab）
GROUP_MAIL = "mail"
GROUP_WECOM = "wecom"
GROUP_DINGTALK = "dingtalk"
GROUP_WECHAT = "wechat"
GROUP_COLLECT = "collect"
GROUP_SYSTEM = "system"

GROUP_TEXT = {
    GROUP_MAIL: "邮件告警",
    GROUP_WECOM: "企业微信机器人",
    GROUP_DINGTALK: "钉钉机器人",
    GROUP_WECHAT: "微信推送",
    GROUP_COLLECT: "采集与保留策略",
    GROUP_SYSTEM: "平台信息",
}


class SysConfig(Base):
    """
    系统配置项（键值对 + 分组 + 类型 + 是否敏感）。

    为什么用"键值对表"而不是"固定列宽表"：
    告警渠道将来可能新增（飞书、短信、语音），若用固定列就得每次改表结构；
    键值对表 + config_group，新增渠道只是新增几行数据。

    is_secret=1 的项（SMTP 口令、机器人 Webhook、CorpSecret）：
      - 入库时用 Fernet 加密（见 ConfigService.set_value）
      - API 返回时脱敏为 ***，前端只展示"已配置/未配置"
    """

    __tablename__ = "sys_config"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="配置键")
    config_value: Mapped[Optional[str]] = mapped_column(Text, comment="配置值（敏感项为密文）")
    config_group: Mapped[str] = mapped_column(String(32), nullable=False, comment="配置分组")
    value_type: Mapped[str] = mapped_column(
        String(16), default="string", nullable=False, comment="string/int/bool/json"
    )
    is_secret: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="True 则接口返回脱敏"
    )
    description: Mapped[Optional[str]] = mapped_column(String(255), comment="配置说明")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, onupdate=now)


# 自定义定时任务的触发类型（与 APScheduler 的 IntervalTrigger/CronTrigger 对应）
TRIGGER_INTERVAL = "interval"
TRIGGER_CRON = "cron"
TRIGGER_TYPE_TEXT = {
    TRIGGER_INTERVAL: "固定间隔",
    TRIGGER_CRON: "Cron 表达式",
}


class SysScheduledTask(Base):
    """
    用户自定义定时任务（系统配置页"定时任务"面板创建/管理）。

    设计要点：
      - 任务"做什么"由 action 决定，只能从后端 ACTION_REGISTRY 里选
        （见 app/scheduler/custom_tasks.py）。不让用户填任意函数名/脚本路径，
        否则等同于开放服务器执行权限——本平台的自定义任务定位是"运维维护动作"。
      - 周期支持两种：固定间隔秒数（interval_seconds）或 5 段 cron 表达式
        （cron_expr，Asia/Shanghai 时区），二选一，由 trigger_type 区分。
      - last_run_* 三列记录最近一次执行结果：调度任务"跑没跑、成没成"必须
        在界面上可见，否则"任务看着启用了实际一直失败"又是一个无声故障。
      - 任务定义持久化在库里，调度器启动时加载、增删改后热更新，
        不依赖代码重启（与"改配置不重启"的平台约定一致）。
    """

    __tablename__ = "sys_scheduled_task"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="任务名称，唯一")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作标识，取值见 ACTION_REGISTRY")
    trigger_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TRIGGER_INTERVAL, comment="interval=固定间隔 / cron=表达式")
    interval_seconds: Mapped[Optional[int]] = mapped_column(comment="间隔秒数（trigger_type=interval 时必填，最小 10）")
    cron_expr: Mapped[Optional[str]] = mapped_column(
        String(64), comment="5 段 cron 表达式（trigger_type=cron 时必填），如 30 3 * * *")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用调度")
    remark: Mapped[Optional[str]] = mapped_column(String(255), comment="备注")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最近一次执行时间（含手动触发）")
    last_run_ok: Mapped[Optional[bool]] = mapped_column(Boolean, comment="最近一次执行是否成功")
    last_run_message: Mapped[Optional[str]] = mapped_column(Text, comment="最近一次执行结果摘要/错误信息")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, onupdate=now)
