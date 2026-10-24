"""
大屏数据聚合服务（模块4：可视化大屏的数据供给）
===============================================================================
**本模块的定位**

大屏与后台列表的差别不在数据，而在**使用场景**：

  - 后台列表：一个人、按需翻页、看细节 → 可以慢，可以多请求几次
  - 可视化大屏：挂在墙上、7×24 自动刷新、10 秒一次、看一眼就要能判断"有没有事"
              → 必须**一次请求拿全**，且**任何一项数据缺失都不能让整屏白掉**

因此本模块只做三件事：

  1. **聚合**：把散在设备/指标/告警/资产/日志五个域的数据汇成"一屏所需的形状"，
     让大屏每 10 秒只发 1~2 个请求，而不是 8 个。
  2. **翻译**：把编码翻译成中文标签（"online" → "在线"、"critical" → "紧急"），
     并附上建议颜色。**标签的唯一来源始终是后端那几张映射表**，
     前端不做任何业务判断 —— 详见 `app/schemas/dashboard.py` 的说明。
  3. **兜底**：新装好的平台一条数据都没有，大屏不能显示成一片空白或 NaN，
     要显示"暂无数据"并说明原因。所以每个返回值都带 `empty_reason` 之类的字段。

**本模块是只读的**：不写任何业务数据、不产生副作用、不改设备状态。
大屏刷新频率高，任何写操作都会被放大 8640 倍（10 秒一次 × 一天）。

**关于时间分桶为什么在 Python 里做**

告警趋势要按小时分桶。用 SQL 分桶需要 `strftime('%Y-%m-%d %H:00')`（SQLite）
或 `DATE_FORMAT(..., '%Y-%m-%d %H:00')`（MySQL）—— **两套方言**。
本项目的一条硬约定是"SQLite ↔ MySQL 只改 DATABASE_URL 即可迁移"，
一旦写出方言函数就破坏了这个约定，而且这种破坏在 SQLite 环境上**测不出来**。

24 小时内的事件量在 200 台设备的规模下通常是几百到几千条，
一次性读回内存分桶的开销可以忽略。**用一点内存换掉一个方言分支，很划算。**
===============================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.alert import (
    ALERT_LEVEL_COLOR,
    ALERT_LEVEL_TEXT,
    EVENT_STATUS_ACKED,
    EVENT_STATUS_FIRING,
    AlertEvent,
    metric_unit,
)
from app.models.asset import ASSET_STATUS_TEXT, AssetInfo
from app.models.device import MonDevice
from app.models.log import CollectLog
from app.models.metric import MetricCatalog, MetricLatest
from app.schemas.dashboard import (
    AlertTrendOut,
    DashboardOverviewOut,
    DeviceCellOut,
    DistributionOut,
    KpiCard,
    RankItemOut,
    RankOut,
    StatusCount,
    TrendPointOut,
)
# 标签映射统一从契约层引入：大屏返回的每个中文标签都来自这里，
# 前端不复制任何一张映射表（理由见 app/schemas/dashboard.py 的模块说明）。
from app.schemas.device import DEVICE_TYPE_TEXT, ONLINE_TEXT, PROTOCOL_TEXT, VENDOR_TEXT

# ============================================================================
# 一、统一色板
# ============================================================================
# 大屏配色集中在这里定义，前端只按 key 取色，不自带任何颜色常量。
# 这样做的好处：客户说"红色太刺眼"，改一处即全屏生效（走 sys_config 时可进一步做成可配）。
#
# ⚠ 2026-09-22 随 UI 改版整体换过一轮：大屏由暗色改为亮色（视觉基线参考深信服官网），
#   原色值是"发光色"（#00D4A0 / #F5A623 / #FF4D4F / #3B9EFF），在深色背景上很跳，
#   放到白色卡片上却会褪色发灰。现在统一换成**白底可读版**：
#   色相不变（绿=正常、橙=警告、红=危险、蓝=信息），只压亮度、提饱和度。
LEVEL_COLOR: Dict[str, str] = {
    "normal": "#0FA97A",     # 正常 — 绿
    "warning": "#E8891C",    # 警告 — 橙
    "danger": "#E5484D",     # 危险 — 红
    "info": "#007ACC",       # 信息 — 蓝（与站点主色一致）
    "muted": "#8B9AB0",      # 灰色 — 无数据/停用
}

#: 设备在线状态 → 颜色
ONLINE_STATUS_COLOR: Dict[str, str] = {
    "online": LEVEL_COLOR["normal"],
    "offline": LEVEL_COLOR["danger"],
    "unknown": LEVEL_COLOR["muted"],
    "disabled": LEVEL_COLOR["info"],
}

#: 告警级别 → 颜色。直接复用模型层的 ALERT_LEVEL_COLOR，
#: 保证"大屏上的红色"和"告警列表里的红色"是同一个红。
ALERT_LEVEL_COLOR_DASHBOARD: Dict[str, str] = dict(ALERT_LEVEL_COLOR)

#: 设备类型 → 颜色（环形图用，按类型数量取，超出则循环）
#: 白底配色要点：相邻色块要够 distinguishable，且都不能太浅——
#: 浅色是白底环形图最常见的失败方式（浅黄描白边后几乎看不见）。
DEVICE_TYPE_COLOR: Sequence[str] = (
    "#007ACC", "#12B886", "#E8891C", "#E5484D", "#7A5CD6",
    "#0091B8", "#D95284", "#8B9AB0", "#2FA36B", "#C79A00",
)

#: 大屏默认刷新间隔（秒）。改为 5 会让 200 台设备的聚合查询变成明显负载，
#: 10 秒是"看起来是实时的"与"数据库无压力"之间的常用折中。
DEFAULT_REFRESH_SECONDS = 10

#: 维保到期预警阈值（天）。与 asset_service 的默认值一致，
#: 避免"大屏说 30 天、列表说 90 天"这种互相矛盾。
MAINTAIN_WARNING_DAYS = 30


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    """统一时间输出格式（前端 new Date() 无法可靠解析带微秒的字符串）"""
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _device_status_code(device: MonDevice) -> str:
    """
    设备的**大屏显示状态**（四态），与后台列表的三态不同。

    差别在于"停用"：停用采集的设备在后台仍是 online/offline/unknown 三态之一
    （它的 online_status 会停在停用前的那一刻），但大屏上必须显示成"停用"——
    否则运维看到一台"在线"的设备其实根本没在被采集，会误判为"监控正常"。
    """
    if not device.enabled:
        return "disabled"
    status = device.online_status or "unknown"
    return status if status in ONLINE_TEXT else "unknown"


def _status_text(code: str) -> str:
    return ONLINE_TEXT.get(code, "停用" if code == "disabled" else "未知")


def _count_statuses(devices: Sequence[MonDevice]) -> List[StatusCount]:
    """统计设备四态计数（顺序固定，前端不必排序）"""
    bucket: Dict[str, int] = {"online": 0, "offline": 0, "unknown": 0, "disabled": 0}
    for d in devices:
        bucket[_device_status_code(d)] += 1
    return [
        StatusCount(code=c, text=_status_text(c), count=bucket[c],
                    color=ONLINE_STATUS_COLOR[c])
        for c in ("online", "offline", "unknown", "disabled")
    ]


# ============================================================================
# 二、总览（顶部 KPI）
# ============================================================================

def kpi_summary(db: Session) -> DashboardOverviewOut:
    """
    大屏总览：顶部指标卡 + 状态分布 + 数据新鲜度。

    **状态统计口径说明（容易被质疑，所以在这里写清楚）**

    在线/离线/未知三态只统计 **enabled=True 的设备**，"停用"单独一张卡。
    理由：一台停用采集的设备，它的 `online_status` 会永远停在停用前的那个值 ——
    把停用设备算进"在线"，会让"在线 12/12"这种看起来很漂亮的数字掩盖
    "其实有 3 台早就没在采了"。**大屏最忌讳的就是给出一个乐观但错误的数字。**
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ---------------- 设备（一次查询，在内存里分状态，避免 4 次 COUNT） ----------------
    devices = db.execute(select(MonDevice)).scalars().all()
    status_counts = _count_statuses(devices)
    by_code = {s.code: s.count for s in status_counts}

    # ---------------- 告警 ----------------
    alert_total = db.execute(select(func.count(AlertEvent.id))).scalar_one() or 0
    active_filter = AlertEvent.status.in_([EVENT_STATUS_FIRING, EVENT_STATUS_ACKED])
    alert_active = db.execute(
        select(func.count(AlertEvent.id)).where(active_filter)
    ).scalar_one() or 0
    alert_today = db.execute(
        select(func.count(AlertEvent.id)).where(AlertEvent.first_occur_at >= today_start)
    ).scalar_one() or 0

    # 级别分布只统计"未恢复"的 —— 大屏要回答的是"现在有什么问题"，
    # 把三个月前已恢复的历史告警混进来，级别饼图会被历史数据压垮，看不出当前态势。
    level_rows = db.execute(
        select(AlertEvent.level, func.count(AlertEvent.id))
        .where(active_filter)
        .group_by(AlertEvent.level)
    ).all()
    level_map = {row[0]: row[1] for row in level_rows}
    alert_level = [
        StatusCount(code=code, text=text, count=level_map.get(code, 0),
                    color=ALERT_LEVEL_COLOR_DASHBOARD.get(code, LEVEL_COLOR["muted"]))
        for code, text in ALERT_LEVEL_TEXT.items()
    ]

    # ---------------- 资产 ----------------
    asset_total = db.execute(select(func.count(AssetInfo.id))).scalar_one() or 0
    warn_deadline = now + timedelta(days=MAINTAIN_WARNING_DAYS)
    asset_maintain_warning = db.execute(
        select(func.count(AssetInfo.id)).where(
            AssetInfo.warranty_end.is_not(None),
            AssetInfo.warranty_end >= now,
            AssetInfo.warranty_end <= warn_deadline,
        )
    ).scalar_one() or 0

    # ---------------- 采集健康度（近 24 小时，一次查询算三个数） ----------------
    since_24h = now - timedelta(hours=24)
    since_1h = now - timedelta(hours=1)
    total_24h, ok_24h, avg_ms = db.execute(
        select(
            func.count(CollectLog.id),
            func.sum(case((CollectLog.status == "success", 1), else_=0)),
            func.avg(CollectLog.duration_ms),
        ).where(CollectLog.collect_time >= since_24h)
    ).one()
    total_24h = int(total_24h or 0)
    ok_24h = int(ok_24h or 0)
    success_rate = round(ok_24h / total_24h * 100, 2) if total_24h else 0.0
    collect_last_hour = db.execute(
        select(func.count(CollectLog.id)).where(CollectLog.collect_time >= since_1h)
    ).scalar_one() or 0

    # 数据新鲜度：最近一次采集时间。大屏上放一个"数据更新于 xx:xx:xx"，
    # 一旦调度器挂了、页面还挂在那儿显示旧数字，运维第一眼就能发现。
    last_collect = db.execute(
        select(func.max(MonDevice.last_collect_at))
    ).scalar_one()

    online = by_code.get("online", 0)
    offline = by_code.get("offline", 0)
    enabled_total = len([d for d in devices if d.enabled])

    kpis: List[KpiCard] = [
        KpiCard(key="device_total", label="设备总数", value=len(devices),
                unit="台", sub=f"启用采集 {enabled_total} 台",
                level="normal", color=LEVEL_COLOR["info"]),
        KpiCard(key="device_online", label="在线设备", value=online, unit="台",
                sub=f"在线率 {round(online / enabled_total * 100, 1) if enabled_total else 0}%",
                level="normal", color=LEVEL_COLOR["normal"]),
        KpiCard(key="device_offline", label="离线设备", value=offline, unit="台",
                sub="需立即处理" if offline else "全部正常",
                level="danger" if offline else "normal",
                color=LEVEL_COLOR["danger"] if offline else LEVEL_COLOR["normal"]),
        KpiCard(key="alert_active", label="未恢复告警", value=alert_active, unit="条",
                sub=f"其中紧急 {level_map.get('critical', 0)} 条",
                level="danger" if level_map.get("critical", 0) else (
                    "warning" if alert_active else "normal"),
                color=LEVEL_COLOR["danger"] if alert_active else LEVEL_COLOR["normal"]),
        KpiCard(key="alert_today", label="今日新增告警", value=alert_today, unit="条",
                sub=f"历史累计 {alert_total} 条",
                level="warning" if alert_today else "normal",
                color=LEVEL_COLOR["warning"] if alert_today else LEVEL_COLOR["normal"]),
        KpiCard(key="collect_success", label="采集成功率", value=success_rate, unit="%",
                sub=f"近 24h {total_24h} 次，均耗时 {round(float(avg_ms or 0))} ms",
                level="danger" if total_24h and success_rate < 90 else (
                    "warning" if total_24h and success_rate < 99 else "normal"),
                color=LEVEL_COLOR["danger"] if total_24h and success_rate < 90 else (
                    LEVEL_COLOR["warning"] if total_24h and success_rate < 99
                    else LEVEL_COLOR["normal"])),
        KpiCard(key="asset_total", label="IT 资产", value=asset_total, unit="条",
                sub=f"维保 {MAINTAIN_WARNING_DAYS} 天内到期 {asset_maintain_warning} 条",
                level="warning" if asset_maintain_warning else "normal",
                color=LEVEL_COLOR["warning"] if asset_maintain_warning else LEVEL_COLOR["info"]),
    ]

    return DashboardOverviewOut(
        kpis=kpis,
        device_total=len(devices),
        device_enabled=enabled_total,
        device_status=status_counts,
        alert_total=alert_total,
        alert_active=alert_active,
        alert_today=alert_today,
        alert_level=alert_level,
        asset_total=asset_total,
        asset_maintain_warning=asset_maintain_warning,
        collect_success_rate=success_rate,
        collect_avg_ms=round(float(avg_ms or 0), 1),
        collect_last_hour=int(collect_last_hour),
        server_time=_fmt(now) or "",
        data_fresh_at=_fmt(last_collect),
        refresh_seconds=DEFAULT_REFRESH_SECONDS,
    )


# ============================================================================
# 三、设备状态灯矩阵
# ============================================================================

def device_grid(
    db: Session, limit: int = 200, metrics: Sequence[str] = ("cpu_usage", "mem_usage", "ping_latency")
) -> List[DeviceCellOut]:
    """
    大屏中部的设备状态灯矩阵。

    **为什么用两次查询而不是联表**

    一次查设备、一次批量查这些设备的 CPU/内存/延迟指标（`(device_id, metric_key)` 命中
    `metric_latest` 的唯一索引），再在内存里拼起来。若写成设备 LEFT JOIN 指标三次，
    200 台设备会产生 200×3 行中间结果，SQLite 上比两次简单查询慢得多。
    这是"用两次简单查询换掉一次复杂联表"的常见取舍。
    """
    devices = db.execute(
        select(MonDevice).order_by(MonDevice.id).limit(max(limit, 1))
    ).scalars().all()
    if not devices:
        return []

    device_ids = [d.id for d in devices]
    metric_keys = list(metrics)
    rows = db.execute(
        select(MetricLatest.device_id, MetricLatest.metric_key, MetricLatest.metric_value)
        .where(MetricLatest.device_id.in_(device_ids), MetricLatest.metric_key.in_(metric_keys))
    ).all()
    metric_map: Dict[Tuple[int, str], Optional[float]] = {
        (r[0], r[1]): r[2] for r in rows
    }

    cells: List[DeviceCellOut] = []
    for d in devices:
        code = _device_status_code(d)
        cells.append(DeviceCellOut(
            id=d.id,
            name=d.name or "",
            ip=d.ip or "",
            device_type=d.device_type or "",
            type_text=DEVICE_TYPE_TEXT.get(d.device_type or "", d.device_type or "未知"),
            protocol=d.protocol or "",
            protocol_text=PROTOCOL_TEXT.get(d.protocol or "", d.protocol or "未知"),
            status=code,
            status_text=_status_text(code),
            status_color=ONLINE_STATUS_COLOR[code],
            location=d.location,
            cpu=metric_map.get((d.id, "cpu_usage")),
            mem=metric_map.get((d.id, "mem_usage")),
            latency=metric_map.get((d.id, "ping_latency")),
            last_collect_at=_fmt(d.last_collect_at),
        ))
    return cells


# ============================================================================
# 四、告警趋势
# ============================================================================

def alert_trend(db: Session, hours: int = 24) -> AlertTrendOut:
    """
    近 N 小时告警趋势，按小时分桶（分桶在 Python 里做，理由见模块 docstring）。

    "新增"取 `first_occur_at`、"恢复"取 `recover_at`，而不是 `created_at`：
    一条告警事件是"同一故障的多次触发归并"，`created_at` 只在首次创建时写一次，
    用它会把"同一晚反复抖了 5 次"压缩成一个点，看不出抖动；
    而 `first_occur_at` 与它几乎等价、语义更清楚，且与恢复时间配对读起来更自然。
    """
    hours = max(1, min(hours, 24 * 14))          # 上限 14 天，防止大屏参数被人为放大拖垮库
    now = datetime.now()
    # 桶对齐到整点：now=17:23、hours=24 → 从昨天 18:00 开始，共 24 个整点桶 + 当前这个桶
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours - 1)

    buckets: Dict[datetime, Dict[str, int]] = {}
    order: List[datetime] = []
    for i in range(hours):
        slot = start + timedelta(hours=i)
        order.append(slot)
        buckets[slot] = {"total": 0, "critical": 0, "major": 0, "minor": 0, "info": 0,
                         "recovered": 0}

    def _slot(dt: Optional[datetime]) -> Optional[datetime]:
        """把时间归到所属整点桶；不在窗口内返回 None"""
        if dt is None:
            return None
        slot = dt.replace(minute=0, second=0, microsecond=0)
        return slot if slot in buckets else None

    created_rows = db.execute(
        select(AlertEvent.first_occur_at, AlertEvent.level)
        .where(AlertEvent.first_occur_at.is_not(None), AlertEvent.first_occur_at >= start)
    ).all()
    for created_at, level in created_rows:
        slot = _slot(created_at)
        if slot is None:
            continue
        b = buckets[slot]
        b["total"] += 1
        # 未知级别兜底进 info：大屏宁可少一个颜色，也不能出现"总数 ≠ 各段之和"
        b[level if level in ("critical", "major", "minor") else "info"] += 1

    recover_rows = db.execute(
        select(AlertEvent.recover_at)
        .where(AlertEvent.recover_at.is_not(None), AlertEvent.recover_at >= start)
    ).all()
    for (recover_at,) in recover_rows:
        slot = _slot(recover_at)
        if slot is not None:
            buckets[slot]["recovered"] += 1

    points = [
        TrendPointOut(time=slot.strftime("%H:00"), **buckets[slot])
        for slot in order
    ]
    total = sum(p.total for p in points)
    return AlertTrendOut(
        hours=hours,
        points=points,
        peak=max((p.total for p in points), default=0),
        total=total,
        recovered=sum(p.recovered for p in points),
    )


# ============================================================================
# 五、排行榜
# ============================================================================

#: 排行榜的着色阈值：超过 danger 值标红，超过 warning 值标黄
#: （百分比类指标通用；延迟类由 `_rank_level` 单独判断）
_RANK_LEVELS: Dict[str, Tuple[float, float]] = {
    "cpu_usage": (70.0, 85.0),
    "mem_usage": (70.0, 85.0),
    "disk_usage": (75.0, 90.0),
    "ping_latency": (80.0, 150.0),
    "ping_loss": (1.0, 5.0),
    "if_in_rate": (70.0, 90.0),
    "if_out_rate": (70.0, 90.0),
}


def _rank_level(metric_key: str, value: float) -> Tuple[str, str]:
    """按阈值给出着色级别（阈值集中在本表，前端不写任何阈值）"""
    base = metric_key.split(":", 1)[0]
    warn, danger = _RANK_LEVELS.get(base, (0.0, 0.0))
    if warn <= 0:
        return "normal", LEVEL_COLOR["info"]
    if value >= danger:
        return "danger", LEVEL_COLOR["danger"]
    if value >= warn:
        return "warning", LEVEL_COLOR["warning"]
    return "normal", LEVEL_COLOR["normal"]


def metric_rank(db: Session, metric_key: str, limit: int = 10) -> RankOut:
    """
    某指标的设备排行榜（大屏右侧 TOP N）。

    **端口类指标的口径（必须说明，否则数字会被质疑）**

    像 `if_in_rate`（端口入向速率）这样的指标，一台交换机会有几十个端口，
    在 `metric_latest` 里就是几十行（键名形如 `if_in_rate:XGE0/0/2`）。
    排行榜一台设备只能占一行，所以取该设备**所有端口里的最大值**，
    并在返回的 `metric_key` 里带上具体端口名 ——
    运维看到"核心交换机 入向 820Mbps（XGE0/0/2）"才有意义，
    看到"核心交换机 入向 820Mbps"只会问"哪个口？"。
    """
    limit = max(1, min(limit, 50))
    base_key = metric_key.split(":", 1)[0]
    catalog_name = db.execute(
        select(MetricCatalog.metric_name).where(MetricCatalog.metric_key == base_key)
    ).scalar_one_or_none()
    unit = metric_unit(base_key)

    rows = db.execute(
        select(
            MetricLatest.device_id, MetricLatest.metric_key, MetricLatest.metric_value,
            MonDevice.name, MonDevice.ip,
        )
        .join(MonDevice, MonDevice.id == MetricLatest.device_id)
        .where(
            MetricLatest.metric_value.is_not(None),
            or_(MetricLatest.metric_key == base_key,
                MetricLatest.metric_key.like(f"{base_key}:%")),
        )
    ).all()

    # 每台设备只留最大值（端口类指标取最忙的那个端口）
    best: Dict[int, Tuple[str, float, str, str]] = {}
    for device_id, key, value, name, ip in rows:
        if value is None:
            continue
        cur = best.get(device_id)
        if cur is None or value > cur[1]:
            best[device_id] = (key, float(value), name or "", ip or "")

    items: List[RankItemOut] = []
    for device_id, (key, value, name, ip) in best.items():
        level, color = _rank_level(key, value)
        items.append(RankItemOut(
            device_id=device_id, device_name=name, device_ip=ip,
            metric_key=key, metric_name=catalog_name or base_key,
            value=round(value, 2), unit=unit, level=level, color=color,
        ))
    items.sort(key=lambda x: x.value, reverse=True)

    empty_reason = ""
    if not rows:
        empty_reason = f"暂无「{catalog_name or base_key}」采集数据（设备可能未启用该指标或尚未完成首次采集）"

    return RankOut(
        metric_key=base_key, metric_name=catalog_name or base_key, unit=unit,
        items=items[:limit], empty_reason=empty_reason,
    )


# ============================================================================
# 六、分布
# ============================================================================

def distribution(db: Session) -> DistributionOut:
    """
    设备类型 / 资产状态 / 告警级别 / 厂商 四类分布（环形图数据源）。

    全部用一条 `GROUP BY` 出结果，不做 N+1 查询。
    """
    def _rows_to_counts(
        rows: Sequence[Tuple[Optional[str], int]],
        text_map: Dict[str, str],
        color_map: Optional[Dict[str, str]] = None,
        palette: Optional[Sequence[str]] = None,
        lower_key: bool = False,
    ) -> List[StatusCount]:
        """
        GROUP BY 结果 → 带中文标签与颜色的计数列表。

        :param lower_key: 查表前是否把编码转小写。厂商列是自由字符串，
                          现场录入可能是 "Huawei" / "HUAWEI"，不做归一化就会漏掉映射
                          （表现为同一张环图里同时出现"华为"和"huawei"两个扇区）。
                          其余几列都是程序写入的固定小写编码，无需归一化。
        """
        out: List[StatusCount] = []
        for i, (code, count) in enumerate(rows):
            key = code or ""
            lookup = key.lower() if lower_key else key
            color = ""
            if color_map:
                color = color_map.get(lookup, LEVEL_COLOR["muted"])
            elif palette:
                color = palette[i % len(palette)]
            out.append(StatusCount(
                code=key,
                # 未登记的取值原样透出：原样透出是有用信息，"未知"会让人以为数据坏了
                text=text_map.get(lookup, key or "未知"),
                count=int(count or 0), color=color,
            ))
        return out

    device_type_rows = db.execute(
        select(MonDevice.device_type, func.count(MonDevice.id))
        .group_by(MonDevice.device_type)
        .order_by(func.count(MonDevice.id).desc())
    ).all()

    vendor_rows = db.execute(
        select(MonDevice.vendor, func.count(MonDevice.id))
        .group_by(MonDevice.vendor)
        .order_by(func.count(MonDevice.id).desc())
        .limit(10)
    ).all()

    asset_status_rows = db.execute(
        select(AssetInfo.status, func.count(AssetInfo.id))
        .group_by(AssetInfo.status)
        .order_by(func.count(AssetInfo.id).desc())
    ).all()

    alert_level_rows = db.execute(
        select(AlertEvent.level, func.count(AlertEvent.id))
        .group_by(AlertEvent.level)
    ).all()

    return DistributionOut(
        device_type=_rows_to_counts(device_type_rows, DEVICE_TYPE_TEXT, palette=DEVICE_TYPE_COLOR),
        asset_status=_rows_to_counts(asset_status_rows, ASSET_STATUS_TEXT),
        alert_level=_rows_to_counts(alert_level_rows, ALERT_LEVEL_TEXT,
                                    color_map=ALERT_LEVEL_COLOR_DASHBOARD),
        # 厂商用 VENDOR_TEXT 翻译成中文（"华为""深信服""麒麟"）——
        # "信创适配了哪些品牌"正是这张图要回答的问题，显示英文编码等于没回答。
        # 未登记的厂商由 _rows_to_counts 的 text_map.get(key, key) 原样透出。
        vendor=_rows_to_counts(vendor_rows, VENDOR_TEXT, palette=DEVICE_TYPE_COLOR,
                               lower_key=True),
        colors={
            **{f"online:{k}": v for k, v in ONLINE_STATUS_COLOR.items()},
            **{f"level:{k}": v for k, v in ALERT_LEVEL_COLOR_DASHBOARD.items()},
            **LEVEL_COLOR,
        },
    )


# ============================================================================
# 七、一键打包（大屏首屏一次请求拿全）
# ============================================================================

def dashboard_bundle(db: Session, hours: int = 24) -> Dict[str, Any]:
    """
    大屏"首屏/整屏"打包数据：一次请求返回总览 + 设备矩阵 + 趋势 + 分布。

    **为什么要有这个接口**

    大屏首次打开（或浏览器定时整页刷新）需要 4 类数据。分成 4 个请求会带来两个问题：
      1. 4 次往返意味着四块内容**先后出现**，墙上的屏幕上会看到"拼图"过程；
      2. 若其中某个请求失败，整屏半明半暗，值班人员无法判断是"真没数据"还是"接口挂了"。

    打包成一个请求后：**要么整屏可用，要么整屏报同一个错**，语义清晰。
    后续 10 秒的增量刷新则可以只打 `/overview`（数据量小、变化快），
    趋势与分布每 1 分钟刷一次即可 —— 这部分节奏由前端控制，后端只提供数据。
    """
    return {
        "overview": kpi_summary(db),
        "devices": device_grid(db),
        "trend": alert_trend(db, hours=hours),
        "distribution": distribution(db),
    }
