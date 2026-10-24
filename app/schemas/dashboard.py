"""
大屏数据契约（模块4：可视化大屏的出入参）
===============================================================================
**设计原则：前端只做渲染，不做业务判断**

大屏上出现的每一个中文标签（"在线"/"离线"/"紧急"/"交换机"）都由后端算好一起返回，
前端只负责把它放进对应的位置。这样做的原因：

  1. **标签唯一来源**：`ONLINE_TEXT` / `ALERT_LEVEL_TEXT` 等映射表在后端只有一份
     （见 `app/schemas/device.py` 与 `app/models/alert.py`），
     前端如果自己再写一份 JS 版本，就是"同一件事两处定义"——
     后端加了新的告警级别 `disaster`，前端 JS 里没有，大屏上就会显示一个空白标签，
     而且不报任何错。
  2. **大屏往往没人盯着看**：一个静默渲染错误的成本远高于多传几个字段的带宽成本。
     大屏 10 秒刷新一次、单次响应几十 KB，多带几个标签完全值得。

**注意：本文件的输出类不得继承任何带 `field_validator` 的基类。**
理由见 `scripts/static_audit.py` 的检查 2：Pydantic v2 的校验器在序列化**输出**时
也会执行，库里只要有一条枚举外数据，整个接口就 500 —— 而大屏的 500 是"整屏白掉"，
比列表页 500 严重得多。本项目所有 `*Out` 类一律独立声明字段、不做枚举强校验。
===============================================================================
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ============================================================================
# 一、顶部 KPI 与总览
# ============================================================================

class StatusCount(BaseModel):
    """一个状态格子的计数（键值对 + 中文标签 + 颜色，前端直接渲染）"""

    code: str = Field(default="", description="状态编码，如 online")
    text: str = Field(default="", description="中文标签，如 在线")
    count: int = Field(default=0, description="数量")
    color: str = Field(default="", description="建议颜色（主题色板统一由后端给）")


class KpiCard(BaseModel):
    """一张顶部指标卡"""

    key: str = Field(default="", description="标识，便于前端固定卡片顺序")
    label: str = Field(default="", description="卡片标题")
    value: float = Field(default=0, description="主数值")
    unit: str = Field(default="", description="单位")
    sub: str = Field(default="", description="副标题（补充说明，可空）")
    level: str = Field(default="normal", description="normal/warning/danger，用于卡片配色")
    color: str = Field(default="", description="建议颜色")


class DashboardOverviewOut(BaseModel):
    """大屏总览：顶部 KPI + 状态分布 + 数据新鲜度"""

    kpis: List[KpiCard] = Field(default_factory=list, description="顶部指标卡")

    device_total: int = Field(default=0, description="设备总数（含停用）")
    device_enabled: int = Field(default=0, description="启用采集的设备数（状态统计口径）")
    device_status: List[StatusCount] = Field(default_factory=list, description="启用设备的在线/离线/未知计数")

    alert_total: int = Field(default=0, description="历史告警总数")
    alert_active: int = Field(default=0, description="未恢复告警数（firing + acked）")
    alert_today: int = Field(default=0, description="今日新增告警数")
    alert_level: List[StatusCount] = Field(default_factory=list, description="未恢复告警的级别分布")

    asset_total: int = Field(default=0, description="资产总数")
    asset_maintain_warning: int = Field(default=0, description="维保即将到期资产数")

    collect_success_rate: float = Field(default=0, description="近 24 小时采集成功率（%）")
    collect_avg_ms: float = Field(default=0, description="近 24 小时平均采集耗时（毫秒）")
    collect_last_hour: int = Field(default=0, description="近 1 小时采集次数")

    server_time: str = Field(default="", description="服务器当前时间（大屏时钟基准）")
    data_fresh_at: Optional[str] = Field(default=None, description="最近一次采集完成时间，大屏判断数据是否新鲜")
    refresh_seconds: int = Field(default=10, description="建议刷新间隔（秒），由后端告知前端")


# ============================================================================
# 二、设备状态灯矩阵
# ============================================================================

class DeviceCellOut(BaseModel):
    """
    大屏中部的一台设备（一个"状态灯格子"）。

    刻意带上 type_text / status_text / protocol_text：
    这些标签的映射表在后端只有一份，前端不该复制。
    """

    id: int = Field(description="设备 ID")
    name: str = Field(default="", description="设备名称")
    ip: str = Field(default="", description="管理 IP")
    device_type: str = Field(default="", description="设备类型编码")
    type_text: str = Field(default="", description="设备类型中文")
    protocol: str = Field(default="", description="采集协议编码")
    protocol_text: str = Field(default="", description="采集协议中文")
    status: str = Field(default="unknown", description="online/offline/unknown/disabled")
    status_text: str = Field(default="", description="在线状态中文")
    status_color: str = Field(default="", description="状态灯颜色（颜色统一由后端给，前端零色值常量）")
    location: Optional[str] = Field(default=None, description="物理位置")
    cpu: Optional[float] = Field(default=None, description="CPU 使用率（%）")
    mem: Optional[float] = Field(default=None, description="内存使用率（%）")
    latency: Optional[float] = Field(default=None, description="Ping 延迟（ms）")
    last_collect_at: Optional[str] = Field(default=None, description="上次采集时间")


# ============================================================================
# 三、趋势
# ============================================================================

class TrendPointOut(BaseModel):
    """趋势图的一个时间点（按告警级别堆叠）"""

    time: str = Field(default="", description="时间标签，如 14:00")
    total: int = Field(default=0, description="该时间点新增告警总数")
    critical: int = Field(default=0, description="紧急级别告警数")
    major: int = Field(default=0, description="重要级别告警数")
    minor: int = Field(default=0, description="一般级别告警数")
    info: int = Field(default=0, description="信息级（含未知级别兜底）")
    recovered: int = Field(default=0, description="该时间点恢复数")


class AlertTrendOut(BaseModel):
    """告警趋势（大屏折线/柱状图数据源）"""

    hours: int = Field(default=24, description="统计窗口（小时）")
    points: List[TrendPointOut] = Field(default_factory=list, description="按小时分桶，从早到晚")
    peak: int = Field(default=0, description="单小时最大新增数，前端据此决定 Y 轴上限")
    total: int = Field(default=0, description="窗口内新增总数")
    recovered: int = Field(default=0, description="窗口内恢复总数")


# ============================================================================
# 四、排行榜与分布
# ============================================================================

class RankItemOut(BaseModel):
    """排行榜一项（某设备的某指标值）"""

    device_id: int = Field(description="设备 ID")
    device_name: str = Field(default="", description="设备名称")
    device_ip: str = Field(default="", description="设备 IP")
    metric_key: str = Field(default="", description="命中的指标键（可能是端口级，如 if_in_rate:XGE0/0/2）")
    metric_name: str = Field(default="", description="指标中文名")
    value: float = Field(default=0, description="指标值")
    unit: str = Field(default="", description="单位（唯一来源：models/alert.py 的 metric_unit）")
    level: str = Field(default="normal", description="normal/warning/danger，按阈值着色")
    color: str = Field(default="", description="建议颜色")


class RankOut(BaseModel):
    """一个排行榜（大屏右侧 TOP N）"""

    metric_key: str = Field(default="", description="请求的指标键")
    metric_name: str = Field(default="", description="指标中文名")
    unit: str = Field(default="", description="单位")
    items: List[RankItemOut] = Field(default_factory=list, description="按值倒序")
    empty_reason: str = Field(default="", description="无数据时的原因说明（大屏显示友好提示而不是空白）")


class DistributionOut(BaseModel):
    """分布类数据（饼图/环形图数据源）"""

    device_type: List[StatusCount] = Field(default_factory=list, description="设备类型分布")
    asset_status: List[StatusCount] = Field(default_factory=list, description="资产状态分布")
    alert_level: List[StatusCount] = Field(default_factory=list, description="历史告警级别分布")
    vendor: List[StatusCount] = Field(default_factory=list, description="厂商分布（信创适配情况一目了然）")
    colors: Dict[str, str] = Field(default_factory=dict, description="编码 → 颜色，统一色板")
