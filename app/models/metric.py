"""指标模型（metric_catalog / metric_latest / metric_sample）—— 冷热分离设计"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, now, pk_type


class MetricCatalog(Base, TimestampMixin):
    """
    指标字典。

    扩展性设计核心：**新增采集指标不需要改表结构**
    —— 只需在采集器的解析器里产出新的 metric_key，
    并在本表登记一行（中文名、单位、类型），大屏与告警规则即可直接使用。

    metric_type 语义：
      - gauge   瞬时值（CPU%、内存%、温度）
      - counter 累计值（端口字节数 Octets），速率需两次采样做差
      - state   状态值（端口 up/down、在线状态），存 str_value
    """

    __tablename__ = "metric_catalog"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    metric_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="指标键")
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="中文名")
    unit: Mapped[Optional[str]] = mapped_column(String(16), comment="单位 %/Mbps/ms/℃/KB")
    metric_type: Mapped[Optional[str]] = mapped_column(String(16), default="gauge", comment="gauge/counter/state")
    value_type: Mapped[Optional[str]] = mapped_column(String(16), default="float", comment="float/int/string")
    protocol: Mapped[Optional[str]] = mapped_column(String(16), comment="snmp/ssh/winrm/icmp/any")
    category: Mapped[Optional[str]] = mapped_column(
        String(32), comment="cpu/memory/disk/interface/ping/system"
    )
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="内置指标不可删")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[Optional[int]] = mapped_column(Integer, default=0, comment="展示排序")
    remark: Mapped[Optional[str]] = mapped_column(String(255))


class MetricLatest(Base):
    """
    最新指标表（热表）。

    唯一键 (device_id, metric_key) 支撑 UPSERT：
      - SQLite：INSERT ... ON CONFLICT(device_id, metric_key) DO UPDATE
      - MySQL ：INSERT ... ON DUPLICATE KEY UPDATE
    由 MetricService.ingest() 统一写入（内部按方言选择语法）。

    表体积恒定 = 设备数 × 指标数（200 台 × 20 = 4000 行），
    大屏所有"当前值"类统计都读这张表，保证秒级响应。

    ⚠ **(device_id, metric_key) 的唯一索引是本表设计的生命线，必须显式声明**：
      - metric_service._upsert_latest() 的 ON CONFLICT / ON DUPLICATE KEY 依赖它；
      - 一旦缺失，SQLite 直接报 "ON CONFLICT clause does not match any PRIMARY KEY
        or UNIQUE constraint"，而 MySQL 更危险 —— 它**不报错**，退化为普通 INSERT，
        于是每轮采集都会新增一批行，"行数恒定" 的保证被静默打破，
        直到大屏变慢、磁盘写满才被发现。
    索引名与 sql/schema_sqlite.sql 中的 uk_latest_device_metric 严格保持一致，
    保证 "ORM 建表（create_all）" 与 "SQL 脚本建表" 两条路径产出完全相同的结构。
    """

    __tablename__ = "metric_latest"
    __table_args__ = (
        Index("uk_latest_device_metric", "device_id", "metric_key", unique=True),
        Index("idx_latest_metric", "metric_key"),
    )

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="设备 ID")
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="指标键")
    metric_value: Mapped[Optional[float]] = mapped_column(Float, comment="数值型指标值")
    str_value: Mapped[Optional[str]] = mapped_column(String(255), comment="字符串型指标值，如 up/down")
    unit: Mapped[Optional[str]] = mapped_column(String(16), comment="单位（冗余，免联表）")
    collect_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="采集时间")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=now, onupdate=now)


class MetricSample(Base):
    """
    历史指标明细表（冷表）。

    写入策略：只 INSERT，永不 UPDATE，靠定时任务降采样清理：
      ≤7 天 保留原始 → 7~30 天聚合为 5 分钟 → 30~90 天聚合为 1 小时 → >90 天 删除

    为什么不做成"实时聚合写"：聚合会引入读-改-写竞态，
    60 并发采集下会产生锁冲突；批量定时聚合只在低峰期跑一次，简单且可靠。
    """

    __tablename__ = "metric_sample"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="设备 ID")
    metric_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="指标键")
    metric_value: Mapped[Optional[float]] = mapped_column(Float, comment="指标值")
    str_value: Mapped[Optional[str]] = mapped_column(String(255), comment="字符串型值")
    unit: Mapped[Optional[str]] = mapped_column(String(16))
    collect_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="采集时间")
