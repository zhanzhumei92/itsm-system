"""资产模型（asset_info / asset_maintain_remind）—— 对应模块2：IT资产管理"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_type

# 资产状态枚举（集中定义，Service 与前端下拉框共用同一份取值）
ASSET_STATUS_IN_USE = "in_use"
ASSET_STATUS_IDLE = "idle"
ASSET_STATUS_REPAIR = "repair"
ASSET_STATUS_SCRAPPED = "scrapped"

ASSET_STATUS_TEXT = {
    ASSET_STATUS_IN_USE: "在用",
    ASSET_STATUS_IDLE: "闲置",
    ASSET_STATUS_REPAIR: "维修中",
    ASSET_STATUS_SCRAPPED: "已报废",
}


class AssetInfo(Base, TimestampMixin):
    """
    IT 资产台账。

    与监控设备的关系：`device_id` 指向 mon_device.id。
    绑定后，资产详情页即可展示该资产的实时指标与历史曲线
    （数据仍来自 metric_latest / metric_sample，不重复存储）。

    `asset_code` 唯一约束是 Excel 导入去重的依据：
    同编号的记录执行"更新"而非"新增"，避免重复导入产生垃圾数据。
    """

    __tablename__ = "asset_info"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    asset_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="资产编号，导入去重依据"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="设备名称")
    device_type: Mapped[Optional[str]] = mapped_column(String(32), comment="switch/server/firewall/pc/other")
    vendor: Mapped[Optional[str]] = mapped_column(String(64), comment="厂商")
    model: Mapped[Optional[str]] = mapped_column(String(64), comment="型号")
    serial_no: Mapped[Optional[str]] = mapped_column(String(128), comment="序列号")
    ip: Mapped[Optional[str]] = mapped_column(String(64), comment="IP 地址")
    mac: Mapped[Optional[str]] = mapped_column(String(64), comment="MAC 地址")
    room_location: Mapped[Optional[str]] = mapped_column(String(128), comment="机房位置")
    rack_position: Mapped[Optional[str]] = mapped_column(String(64), comment="机柜/机位")
    online_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="上架时间")
    warranty_start: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="维保起始")
    warranty_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime, comment="维保到期，大屏预警依据"
    )
    owner: Mapped[Optional[str]] = mapped_column(String(64), comment="责任人")
    owner_phone: Mapped[Optional[str]] = mapped_column(String(32), comment="责任人电话")
    supplier: Mapped[Optional[str]] = mapped_column(String(128), comment="供应商")
    purchase_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="采购日期")
    purchase_price: Mapped[Optional[float]] = mapped_column(Float, comment="采购金额")
    status: Mapped[Optional[str]] = mapped_column(
        String(16), default=ASSET_STATUS_IN_USE, comment="in_use/idle/repair/scrapped"
    )
    device_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="绑定的监控设备 ID")
    remark: Mapped[Optional[str]] = mapped_column(Text, comment="备注")

    @property
    def status_text(self) -> str:
        """状态中文名，前端表格直接展示"""
        return ASSET_STATUS_TEXT.get(self.status or "", self.status or "")

    @property
    def is_monitored(self) -> bool:
        """是否已绑定监控设备"""
        return self.device_id is not None


class AssetMaintainRemind(Base, TimestampMixin):
    """
    维保到期提醒。

    为什么单独建表而不是每次查询时动态算：
    1. 大屏"维保预警"卡片要求秒级返回，动态算 `warranty_end - now` 需要扫全表；
    2. 提醒有业务状态（待提醒/已提醒/已忽略），必须可持久化，
       否则运维点了"忽略"下次刷新又冒出来。
    由定时任务（每日 08:00）刷新本表内容。
    """

    __tablename__ = "asset_maintain_remind"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(pk_type(), nullable=False, comment="关联资产 ID")
    asset_code: Mapped[Optional[str]] = mapped_column(String(64), comment="资产编号（冗余，便于列表展示）")
    asset_name: Mapped[Optional[str]] = mapped_column(String(128), comment="资产名称（冗余）")
    warranty_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="维保到期时间")
    remind_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False, comment="提前提醒天数")
    remind_level: Mapped[Optional[str]] = mapped_column(
        String(16), comment="expired 已过期 / urgent 30天内 / soon 90天内"
    )
    remind_status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, comment="pending/sent/ignored"
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="提醒发送时间")
