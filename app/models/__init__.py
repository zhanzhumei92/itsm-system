"""
ORM 模型包
===============================================================================
重要：所有模型必须在此处显式导入并登记到 __all__。

原因：SQLAlchemy 的 Base.metadata 只认识"被 import 过的模型类"。
若某张表的模型没被导入，Base.metadata.create_all() 就不会为它建表，
表现为"启动正常但一查询就报 no such table"。

统一在此导出，任何模块只要 `from app.models import XxxModel` 就能拿到，
同时保证元数据完整。
===============================================================================
"""
from app.models.alert import (
    ALERT_LEVEL_COLOR,
    ALERT_LEVEL_TEXT,
    CHANNEL_TEXT,
    EVENT_STATUS_TEXT,
    AlertEvent,
    AlertNotifyRecord,
    AlertRule,
    AlertRuleState,
)
from app.models.asset import (
    ASSET_STATUS_TEXT,
    AssetInfo,
    AssetMaintainRemind,
)
from app.models.base import Base, TimestampMixin, now, pk_type
from app.models.config import (
    GROUP_TEXT,
    TRIGGER_CRON,
    TRIGGER_INTERVAL,
    TRIGGER_TYPE_TEXT,
    SysConfig,
    SysScheduledTask,
)
from app.models.device import MonDevice, MonDeviceCredential
from app.models.log import (
    ALARM_ACTION_TEXT,
    AlarmLog,
    CollectLog,
    SysLog,
)
from app.models.metric import MetricCatalog, MetricLatest, MetricSample
from app.models.user import SysUser

__all__ = [
    # 基类与工具
    "Base", "TimestampMixin", "now", "pk_type",
    # 系统域
    "SysUser", "SysConfig", "SysLog", "GROUP_TEXT",
    "SysScheduledTask", "TRIGGER_INTERVAL", "TRIGGER_CRON", "TRIGGER_TYPE_TEXT",
    # 设备域
    "MonDevice", "MonDeviceCredential",
    # 资产域
    "AssetInfo", "AssetMaintainRemind", "ASSET_STATUS_TEXT",
    # 指标域
    "MetricCatalog", "MetricLatest", "MetricSample",
    # 告警域
    "AlertRule", "AlertRuleState", "AlertEvent", "AlertNotifyRecord",
    "ALERT_LEVEL_TEXT", "ALERT_LEVEL_COLOR", "EVENT_STATUS_TEXT", "CHANNEL_TEXT",
    # 日志域
    "CollectLog", "AlarmLog", "ALARM_ACTION_TEXT",
]
