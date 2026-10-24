"""资产 Schema（对应模块2：IT资产管理、导入导出、与监控设备绑定）"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import Field, computed_field, field_validator

from app.schemas.common import BaseSchema, OptionalDateTimeStr

VALID_ASSET_STATUS = {"in_use", "idle", "repair", "scrapped"}


class _AssetInputValidators:
    """资产**入参**校验器（AssetCreate / AssetUpdate 共用，None 放行以支持部分更新）"""

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_ASSET_STATUS:
            raise ValueError(f"资产状态只能是 {'/'.join(sorted(VALID_ASSET_STATUS))}")
        return value


class AssetBase(BaseSchema):
    """资产公共字段（字段定义与需求给定的台账字段一一对应）"""

    asset_code: str = Field(..., min_length=1, max_length=64, description="资产编号（唯一）")
    name: str = Field(..., min_length=1, max_length=128, description="设备名称")
    # 资产类型：与设备共用同一套取值（switch/router/firewall/server/...），
    # 前端下拉框直接复用 GET /api/v1/devices/types，不必再维护一份资产类型字典。
    device_type: Optional[str] = Field(default=None, max_length=32, description="资产类型，取值见 /api/v1/devices/types")
    vendor: Optional[str] = Field(default=None, max_length=64, description="厂商")
    model: Optional[str] = Field(default=None, max_length=64, description="型号")
    serial_no: Optional[str] = Field(default=None, max_length=128, description="序列号")
    ip: Optional[str] = Field(default=None, max_length=64, description="管理 IP")
    mac: Optional[str] = Field(default=None, max_length=64, description="MAC 地址")
    room_location: Optional[str] = Field(default=None, max_length=128, description="机房位置")
    rack_position: Optional[str] = Field(default=None, max_length=64, description="机柜/机位")
    online_at: Optional[datetime] = Field(default=None, description="上架时间")
    warranty_start: Optional[datetime] = Field(default=None, description="维保起始")
    warranty_end: Optional[datetime] = Field(default=None, description="维保到期时间；用于首页维保预警")
    owner: Optional[str] = Field(default=None, max_length=64, description="责任人")
    owner_phone: Optional[str] = Field(default=None, max_length=32, description="责任人电话")
    supplier: Optional[str] = Field(default=None, max_length=128, description="供应商")
    purchase_at: Optional[datetime] = Field(default=None, description="采购日期")
    purchase_price: Optional[float] = Field(default=None, ge=0, description="采购金额（元）")
    status: str = Field(default="in_use", description="in_use/idle/repair/scrapped")
    remark: Optional[str] = Field(default=None, description="备注")


class AssetCreate(_AssetInputValidators, AssetBase):
    """新增资产"""

    device_id: Optional[int] = Field(default=None, description="可同时绑定监控设备")


class AssetUpdate(_AssetInputValidators, BaseSchema):
    """编辑资产（全字段可选）」

    补上 status 校验的原因：此前 AssetUpdate 不带任何校验，
    可以把任意字符串写进 status（如中文"在用"），而 AssetOut 又以枚举校验
    读取该字段，最终导致"改错一个字段 → 整个资产列表 500"。
    输入校验与输出解耦后仍需在此拦住非法值——写入口是最后一道防线。
    """

    asset_code: Optional[str] = Field(default=None, max_length=64, description="资产编号，全平台唯一")
    name: Optional[str] = Field(default=None, max_length=128, description="资产名称")
    device_type: Optional[str] = Field(default=None, description="资产类型，取值见 /api/v1/assets/types")
    vendor: Optional[str] = Field(default=None, max_length=64, description="厂商")
    model: Optional[str] = Field(default=None, max_length=64, description="型号")
    serial_no: Optional[str] = Field(default=None, max_length=128, description="序列号（SN）")
    ip: Optional[str] = Field(default=None, max_length=64, description="管理 IP")
    mac: Optional[str] = Field(default=None, max_length=64, description="MAC 地址")
    room_location: Optional[str] = Field(default=None, max_length=128, description="所在机房/位置")
    rack_position: Optional[str] = Field(default=None, max_length=64, description="机柜与 U 位")
    online_at: Optional[datetime] = Field(default=None, description="上线日期")
    warranty_start: Optional[datetime] = Field(default=None, description="维保开始日期")
    warranty_end: Optional[datetime] = Field(default=None, description="维保到期日期；用于首页维保预警")
    owner: Optional[str] = Field(default=None, max_length=64, description="责任人")
    owner_phone: Optional[str] = Field(default=None, max_length=32, description="责任人电话")
    supplier: Optional[str] = Field(default=None, max_length=128, description="供应商")
    purchase_at: Optional[datetime] = Field(default=None, description="采购日期")
    purchase_price: Optional[float] = Field(default=None, ge=0, description="采购金额（元）")
    status: Optional[str] = Field(
        default=None,
        # ⚠ 枚举取值以 AssetBase.status 为准（in_use/idle/repair/scrapped），不在此处重新列举。
        # 写这段时这里曾误写成 "scrap"，与基类的 "scrapped" 不一致 —— 典型的"同一件事两处定义"：
        # 两处都是自由字符串、漂移时不报错，失效点却在"改了对的那一处之后"才出现。
        description="资产状态，取值同 AssetBase：in_use/idle/repair/scrapped",
    )
    device_id: Optional[int] = Field(default=None, description="绑定的监控设备 ID；解绑时传 null")
    remark: Optional[str] = Field(default=None, description="备注")

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in VALID_ASSET_STATUS:
            raise ValueError(f"资产状态只能是 {'/'.join(sorted(VALID_ASSET_STATUS))}")
        return value


class AssetOut(BaseSchema):
    """
    资产输出（含绑定设备的实时状态摘要）。

    ⚠ 刻意**不继承** AssetBase，原因同 DeviceOut：
    AssetBase 上的 status 枚举校验器会随继承作用到输出路径。
    配合"编辑接口可写入非法值"的旧行为，会形成完整故障链：
        PUT status="在用"（当时未校验 → 入库）→ GET 列表校验失败 → 500
    即"改错一个字段，整个资产列表打不开"。输出侧只做字段映射与兜底，
    不做业务校验；未知状态原样透出，让前端能显示而不是白屏。

    同时把 4 个时间字段改用 OptionalDateTimeStr，
    与项目"统一输出 YYYY-MM-DD HH:MM:SS"的约定保持一致
    （继承 AssetBase 时它们是裸 datetime，会输出 ISO 8601 格式）。
    """

    id: int = Field(description="资产 ID")
    asset_code: str = Field(default="", description="资产编号（唯一）")
    name: str = Field(default="", description="设备名称")
    device_type: Optional[str] = Field(default=None, description="资产类型，取值见 /api/v1/devices/types")
    vendor: Optional[str] = Field(default=None, description="厂商")
    model: Optional[str] = Field(default=None, description="型号")
    serial_no: Optional[str] = Field(default=None, description="序列号")
    ip: Optional[str] = Field(default=None, description="管理 IP")
    mac: Optional[str] = Field(default=None, description="MAC 地址")
    room_location: Optional[str] = Field(default=None, description="机房位置")
    rack_position: Optional[str] = Field(default=None, description="机柜/机位")
    online_at: OptionalDateTimeStr = Field(default=None, description="上架时间")
    warranty_start: OptionalDateTimeStr = Field(default=None, description="维保起始")
    warranty_end: OptionalDateTimeStr = Field(default=None, description="维保到期时间；用于首页维保预警")
    owner: Optional[str] = Field(default=None, description="责任人")
    owner_phone: Optional[str] = Field(default=None, description="责任人电话")
    supplier: Optional[str] = Field(default=None, description="供应商")
    purchase_at: OptionalDateTimeStr = Field(default=None, description="采购日期")
    purchase_price: Optional[float] = Field(default=None, description="采购金额（元）")
    status: Optional[str] = Field(default="in_use", description="in_use/idle/repair/scrapped")
    remark: Optional[str] = Field(default=None, description="备注")

    device_id: Optional[int] = Field(default=None, description="已绑定的监控设备 ID；未绑定为 null")
    created_at: OptionalDateTimeStr = Field(default=None, description="创建时间")
    updated_at: OptionalDateTimeStr = Field(default=None, description="最后修改时间")

    # 以下字段由 Service 联表补充（资产与实时监控数据关联展示）
    device_name: Optional[str] = Field(default=None, description="绑定设备名称")
    device_online_status: Optional[str] = Field(default=None, description="绑定设备在线状态")

    @computed_field(description="状态中文名")
    @property
    def status_text(self) -> str:
        return {
            "in_use": "在用", "idle": "闲置",
            "repair": "维修中", "scrapped": "已报废",
        }.get(self.status or "", self.status or "未知")

    @computed_field(description="是否已绑定监控设备")
    @property
    def is_monitored(self) -> bool:
        return self.device_id is not None

    @computed_field(description="距维保到期天数，负数表示已过期")
    @property
    def warranty_days_left(self) -> Optional[int]:
        """
        维保剩余天数。负数=已过期。
        大屏"维保到期预警"与资产列表的红色高亮都依赖此字段，
        在服务端算好可避免前端各页面算法不一致。
        """
        if not self.warranty_end:
            return None
        return (self.warranty_end - datetime.now()).days


class AssetBindRequest(BaseSchema):
    """资产与监控设备绑定/解绑"""

    device_id: Optional[int] = Field(default=None, description="传 null 表示解绑")


class AssetImportResult(BaseSchema):
    """Excel 导入结果"""

    total: int = Field(default=0, description="Excel 中读取到的数据行数")
    created: int = Field(default=0, description="新增条数")
    updated: int = Field(default=0, description="按资产编号更新条数")
    failed: int = Field(default=0, description="失败条数")
    errors: List[str] = Field(default_factory=list, description="逐行错误说明（最多返回 50 条）")


class MaintainRemindOut(BaseSchema):
    """维保到期提醒输出（大屏容量视图用）"""

    id: int = Field(description="提醒记录 ID")
    asset_id: int = Field(description="资产 ID")
    asset_code: Optional[str] = Field(default=None, description="资产编号")
    asset_name: Optional[str] = Field(default=None, description="资产名称")
    warranty_end: OptionalDateTimeStr = Field(default=None, description="维保到期日期")
    remind_days: int = Field(default=30, description="距到期天数；负数表示已过期")
    remind_level: Optional[str] = Field(default=None, description="提醒级别")
    remind_status: str = Field(default="pending", description="提醒状态")
    owner: Optional[str] = Field(default=None, description="责任人（Service 补充）")
    owner_phone: Optional[str] = Field(default=None, description="责任人电话")

    @computed_field(description="提醒等级中文名")
    @property
    def remind_level_text(self) -> str:
        return {"expired": "已过期", "urgent": "30天内到期", "soon": "90天内到期"}.get(
            self.remind_level or "", "未知"
        )
