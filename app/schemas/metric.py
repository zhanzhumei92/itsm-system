"""指标 Schema（对应模块5：指标管理 —— 查看实时指标与历史曲线）"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import Field, computed_field

from app.schemas.common import BaseSchema, OptionalDateTimeStr


class MetricLatestOut(BaseSchema):
    """单台设备某指标的最新值"""

    device_id: int = Field(description="设备 ID")
    metric_key: str = Field(description="指标键")
    metric_value: Optional[float] = Field(default=None, description="数值型指标值")
    str_value: Optional[str] = Field(default=None, description="字符串型指标值")
    unit: Optional[str] = Field(default=None, description="单位")
    collect_time: OptionalDateTimeStr = Field(default=None, description="采集时间")

    # 由 Service 联表补充（避免前端再查一次指标字典）
    metric_name: Optional[str] = Field(default=None, description="指标中文名")
    category: Optional[str] = Field(default=None, description="cpu/memory/disk/interface/ping")

    @computed_field(description="展示值：字符串指标优先，否则用数值")
    @property
    def display_value(self) -> Optional[str]:
        if self.str_value not in (None, ""):
            return self.str_value
        if self.metric_value is None:
            return None
        # 百分比保留 1 位小数，其余保留 2 位，避免大屏出现 63.33333333 这种难读数字
        return f"{self.metric_value:.1f}" if self.unit == "%" else f"{self.metric_value:.2f}"


class MetricPointOut(BaseSchema):
    """曲线上的一个采样点"""

    time: str = Field(description="时间，格式 YYYY-MM-DD HH:MM:SS")
    value: Optional[float] = None


class MetricSeriesOut(BaseSchema):
    """一条历史曲线"""

    device_id: int = Field(description="设备 ID")
    device_name: Optional[str] = Field(default=None, description="设备名称")
    metric_key: str = Field(description="指标键")
    metric_name: Optional[str] = Field(default=None, description="指标中文名")
    unit: Optional[str] = Field(default=None, description="单位")
    count: int = Field(default=0, description="采样点数量")
    points: List[MetricPointOut] = Field(default_factory=list, description="数据点序列")


class DeviceMetricsOut(BaseSchema):
    """一台设备的全部最新指标（设备详情页"实时指标"卡片）"""

    device_id: int
    device_name: Optional[str] = None
    device_ip: Optional[str] = None
    online_status: str = "unknown"
    last_collect_at: OptionalDateTimeStr = None
    metrics: List[MetricLatestOut] = Field(default_factory=list)


class MetricHistoryQuery(BaseSchema):
    """历史指标查询参数"""

    device_id: int = Field(..., description="设备 ID")
    metric_key: str = Field(..., description="指标键")
    start: Optional[datetime] = Field(default=None, description="起始时间，默认 6 小时前")
    end: Optional[datetime] = Field(default=None, description="结束时间，默认当前")
    limit: int = Field(default=500, ge=10, le=5000, description="最大返回点数")
    agg: Optional[str] = Field(
        default=None, description="聚合粒度：raw 原始 / 5min 五分钟 / hour 小时；默认自动选择"
    )


class MetricCatalogOut(BaseSchema):
    """指标字典项"""

    id: int = Field(description="ID")
    metric_key: str = Field(description="指标键")
    metric_name: str = Field(description="指标中文名")
    unit: Optional[str] = Field(default=None, description="单位")
    metric_type: Optional[str] = Field(default=None, description="gauge/counter/state")
    value_type: Optional[str] = Field(default=None, description="float/int/string")
    protocol: Optional[str] = Field(default=None, description="适用协议")
    category: Optional[str] = Field(default=None, description="分类")
    is_builtin: bool = Field(default=True, description="是否内置（内置项不可删除）")
    enabled: bool = Field(default=True, description="是否启用")
    sort_order: Optional[int] = Field(default=0, description="排序号")
    remark: Optional[str] = Field(default=None, description="备注")


class MetricCatalogCreate(BaseSchema):
    """新增自定义指标（扩展性需求：不改表结构即可加指标）"""

    metric_key: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$",
                            description="指标键，仅小写字母数字下划线")
    metric_name: str = Field(..., min_length=1, max_length=64, description="指标中文名，用于界面展示")
    unit: Optional[str] = Field(default=None, max_length=16, description="单位，如 % / ms / MB/s / ℃")
    metric_type: str = Field(default="gauge", description="gauge/counter/state")
    value_type: str = Field(default="float", description="float/int/string")
    protocol: Optional[str] = Field(default=None, max_length=16, description="适用的采集协议；为空表示通用")
    category: Optional[str] = Field(default=None, max_length=32, description="分类：cpu/mem/disk/net/system")
    sort_order: int = Field(default=0, description="排序号，小的在前")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")
