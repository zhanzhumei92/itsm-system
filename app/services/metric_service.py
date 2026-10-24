"""
指标服务（冷热分离写入、历史曲线查询、降采样清理）
===============================================================================
一、指标键命名约定（重要）
   普通指标：  cpu_usage、mem_usage、ping_latency
   端口类指标：if_in_rate:GigabitEthernet0/0/1（基名 + 冒号 + 端口名）
   为什么这样设计：
   metric_latest 的唯一键是 (device_id, metric_key)，端口流量必须逐端口区分，
   否则一台 48 口交换机只能存一个端口速率。用"基名:端口"的写法，
   既能保证唯一性，又可以用前缀匹配（LIKE 'if_in_rate:%'）做大屏 TOP 排行，
   而指标字典里仍然只注册一条基名，不必为每个端口建字典项。

二、写入策略（性能关键）
   一次采集 = 1 次批量 UPSERT（热表） + 1 次批量 INSERT（冷表）
   绝不做"逐条查-改-写"：200 台 x 20 指标 x 每分钟，逐条操作会直接压垮 SQLite。

三、UPSERT 的方言差异
   SQLite 与 MySQL 的 upsert 语法不同，本模块集中封装 _upsert_latest()，
   调用方完全无感知 —— 这是"改 DATABASE_URL 即可迁移"的落点之一。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    BizException,
    DuplicateException,
    NotFoundException,
    OperationDeniedException,
)
from app.core.logger import system_logger
from app.models.alert import PORT_KEY_SEPARATOR
from app.models.device import MonDevice
from app.models.metric import MetricCatalog, MetricLatest, MetricSample
from app.schemas.metric import (
    DeviceMetricsOut,
    MetricCatalogCreate,
    MetricLatestOut,
    MetricPointOut,
    MetricSeriesOut,
)

# 端口类指标的分隔符（见模块 docstring 的命名约定）。
#
# ⚠ **唯一来源是 app/models/alert.py，上面那行 import 只是转发**：
# 早先本模块与 models/alert.py 各写了一个 ":" 字面量 —— 典型的"同一件事两处定义"。
# 两个值当时恰好相同，所以没出事；但告警模块按基名拆键（metric_unit / base_metric_key）
# 用的是**模型层**那份，指标模块拼端口键用的是**本模块**这份。
# 哪天有人只改一处：拼出来的键带新分隔符、拆键按旧分隔符拆，
# 单位查不到、规则匹配不上，告警里就会显示成"温度 78"这种没有单位的样子，
# 而且不报任何错。现在两处是同一个对象（identity 相同），改一处即全局生效。
#
# 方向说明：models 是下层、services 是上层，上层引用下层不违反分层约定，
# 反向依赖（models 去 import services）才是禁止的。

# 需要以字符串形式展示（不做数值平均）的指标类型
STATE_METRIC_TYPES = ("state",)


# ============================================================================
# 指标键工具
# ============================================================================

def make_port_metric_key(base_key: str, port_name: str) -> str:
    """拼接端口级指标键，如 make_port_metric_key('if_in_rate', 'Gi0/0/1')"""
    port = (port_name or "").strip()
    return f"{base_key}{PORT_KEY_SEPARATOR}{port}" if port else base_key


def base_metric_key(metric_key: str) -> str:
    """取指标键的基名（去掉端口后缀），用于查字典与规则匹配"""
    return metric_key.split(PORT_KEY_SEPARATOR, 1)[0]


# ============================================================================
# 写入：采集结果入库（热表 + 冷表）
# ============================================================================

def _normalize_metrics(metrics: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    归一化采集结果并去重。

    采集器可能对同一指标产出多条（例如 SNMP 多索引返回），
    这里按 metric_key 去重保留最后一条，避免 UPSERT 时同一批次内
    出现重复键导致 SQLite 报 "ON CONFLICT DO UPDATE command cannot affect row a second time"。
    """
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in metrics or []:
        key = item.get("metric_key")
        if not key:
            continue
        value = item.get("metric_value")
        str_value = item.get("str_value")

        # 数值字段做安全转换：第三方库可能返回字符串或 None
        if value is not None and not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError):
                str_value = str_value if str_value is not None else str(value)
                value = None

        dedup[key] = {
            "metric_key": key,
            "metric_value": value,
            "str_value": str_value,
            "unit": item.get("unit"),
        }
    return list(dedup.values())


def _upsert_latest(db: Session, rows: List[Dict[str, Any]]) -> None:
    """
    热表 UPSERT：存在则更新，不存在则插入。

    方言差异集中在此函数：
      SQLite → INSERT ... ON CONFLICT(device_id, metric_key) DO UPDATE SET ...
      MySQL  → INSERT ... ON DUPLICATE KEY UPDATE ...
    """
    if not rows:
        return

    if settings.is_sqlite:
        stmt = sqlite_insert(MetricLatest).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["device_id", "metric_key"],
            set_={
                "metric_value": stmt.excluded.metric_value,
                "str_value": stmt.excluded.str_value,
                "unit": stmt.excluded.unit,
                "collect_time": stmt.excluded.collect_time,
                "updated_at": stmt.excluded.updated_at,
            },
        )
    else:
        stmt = mysql_insert(MetricLatest).values(rows)
        stmt = stmt.on_duplicate_key_update(
            metric_value=stmt.inserted.metric_value,
            str_value=stmt.inserted.str_value,
            unit=stmt.inserted.unit,
            collect_time=stmt.inserted.collect_time,
            updated_at=stmt.inserted.updated_at,
        )
    db.execute(stmt)


def ingest(
    db: Session,
    device_id: int,
    metrics: Sequence[Dict[str, Any]],
    collect_time: Optional[datetime] = None,
    write_sample: bool = True,
) -> int:
    """
    采集结果入库（本平台数据入口，采集器与手动测试都走这里）。

    :param metrics: 归一化指标列表 [{"metric_key","metric_value","str_value","unit"}]
    :param collect_time: 采集时间，默认当前
    :param write_sample: 是否写历史明细表（手动测试时传 False，不污染曲线）
    :return: 实际写入的指标条数

    注意：本函数只负责"写"，**不触发告警评估**。
    告警评估由调度器在 ingest 之后单独调用 AlarmService.evaluate_batch()，
    这样指标写入与告警判断解耦，任一环节出错不互相影响。
    """
    rows = _normalize_metrics(metrics)
    if not rows:
        return 0

    ts = collect_time or datetime.now()
    now_ts = datetime.now()

    latest_rows = [
        {
            "device_id": device_id,
            "metric_key": item["metric_key"],
            "metric_value": item["metric_value"],
            "str_value": item["str_value"],
            "unit": item["unit"],
            "collect_time": ts,
            "created_at": now_ts,
            "updated_at": now_ts,
        }
        for item in rows
    ]
    _upsert_latest(db, latest_rows)

    if write_sample:
        # 冷表只 INSERT；用 bulk 方式减少 ORM 开销（一次采集可能上百条）
        db.bulk_insert_mappings(
            MetricSample,
            [
                {
                    "device_id": device_id,
                    "metric_key": item["metric_key"],
                    "metric_value": item["metric_value"],
                    "str_value": item["str_value"],
                    "unit": item["unit"],
                    "collect_time": ts,
                }
                for item in rows
            ],
        )

    db.commit()
    return len(rows)


# ============================================================================
# 查询：实时指标
# ============================================================================

def get_latest_map(
    db: Session, device_ids: Sequence[int], metric_keys: Optional[Sequence[str]] = None
) -> Dict[Tuple[int, str], MetricLatest]:
    """
    批量取最新指标，返回 {(device_id, metric_key): MetricLatest}。

    大屏聚合统计的**唯一数据来源**：一次性取出所需指标，
    在 Python 侧做统计，避免在数据库里做几十次聚合查询。
    """
    if not device_ids:
        return {}

    stmt = select(MetricLatest).where(MetricLatest.device_id.in_(list(device_ids)))
    if metric_keys:
        stmt = stmt.where(MetricLatest.metric_key.in_(list(metric_keys)))

    result: Dict[Tuple[int, str], MetricLatest] = {}
    for row in db.execute(stmt).scalars().all():
        result[(row.device_id, row.metric_key)] = row
    return result


def get_device_metrics(db: Session, device_id: int) -> DeviceMetricsOut:
    """
    单台设备的全部最新指标（设备详情页"实时指标"卡片）。

    返回结果按指标字典的 category + sort_order 排序，
    保证前端每次刷新顺序一致（避免数字位置乱跳）。
    """
    device = db.get(MonDevice, device_id)
    if device is None:
        raise NotFoundException("设备不存在")

    rows = db.execute(
        select(MetricLatest).where(MetricLatest.device_id == device_id)
    ).scalars().all()

    meta = _load_catalog_map(db)

    items: List[MetricLatestOut] = []
    for row in rows:
        out = MetricLatestOut.model_validate(row)
        catalog = meta.get(base_metric_key(row.metric_key))
        out.metric_name = catalog.metric_name if catalog else row.metric_key
        out.category = catalog.category if catalog else None
        items.append(out)

    category_order = {
        "cpu": 0, "memory": 1, "disk": 2, "interface": 3, "ping": 4, "system": 5,
    }
    items.sort(key=lambda x: (category_order.get(x.category or "", 9), x.metric_name or ""))

    return DeviceMetricsOut(
        device_id=device.id,
        device_name=device.name,
        device_ip=device.ip,
        online_status=device.online_status,
        last_collect_at=device.last_collect_at,
        metrics=items,
    )


def _load_catalog_map(db: Session) -> Dict[str, MetricCatalog]:
    """加载指标字典为 {metric_key: Catalog} 映射（一次性查全表，字典表只有几十行）"""
    return {
        row.metric_key: row
        for row in db.execute(select(MetricCatalog)).scalars().all()
    }


# ============================================================================
# 查询：历史曲线
# ============================================================================

def get_history(
    db: Session,
    device_id: int,
    metric_key: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 500,
    agg: Optional[str] = None,
) -> MetricSeriesOut:
    """
    查询历史曲线数据。

    自动降采样策略（避免一次返回十万个点把浏览器卡死）：
      - 时间跨度 ≤ 6 小时  → 原始点
      - ≤ 4 天            → 5 分钟均值（Python 侧聚合，保留桶内最后一个时间戳）
      - > 4 天            → 小时均值
    也可以由调用方用 agg 参数强制指定 raw/5min/hour。

    为什么在 Python 侧聚合而不写 SQL 的 GROUP BY：
    本函数按 device_id + metric_key 查询，已走复合索引，单次返回最多几千行，
    Python 聚合更简单且完全避开 SQLite/MySQL 的日期函数方言差异。
    """
    device = db.get(MonDevice, device_id)
    if device is None:
        raise NotFoundException("设备不存在")

    end = end or datetime.now()
    start = start or (end - timedelta(hours=6))
    if start > end:
        raise BizException("起始时间不能晚于结束时间")

    span_hours = (end - start).total_seconds() / 3600
    if agg is None:
        agg = "raw" if span_hours <= 6 else ("5min" if span_hours <= 96 else "hour")

    bucket_seconds = {"raw": 0, "5min": 300, "hour": 3600}.get(agg, 0)

    rows = db.execute(
        select(MetricSample)
        .where(
            MetricSample.device_id == device_id,
            MetricSample.metric_key == metric_key,
            MetricSample.collect_time >= start,
            MetricSample.collect_time <= end,
        )
        .order_by(MetricSample.collect_time.asc())
        .limit(limit * (4 if bucket_seconds else 1))
    ).scalars().all()

    points: List[MetricPointOut] = []
    if bucket_seconds == 0:
        for row in rows[-limit:]:
            points.append(MetricPointOut(
                time=row.collect_time.strftime("%Y-%m-%d %H:%M:%S"),
                value=row.metric_value,
            ))
    else:
        # 按时间桶聚合：桶内取平均值（字符串型指标取最后一个值）
        buckets: Dict[int, List[MetricSample]] = {}
        for row in rows:
            key = int(row.collect_time.timestamp()) // bucket_seconds
            buckets.setdefault(key, []).append(row)

        for key in sorted(buckets.keys()):
            group = buckets[key]
            numeric = [g.metric_value for g in group if g.metric_value is not None]
            avg = sum(numeric) / len(numeric) if numeric else None
            points.append(MetricPointOut(
                time=group[-1].collect_time.strftime("%Y-%m-%d %H:%M:%S"),
                value=avg,
            ))
        points = points[-limit:]

    meta = _load_catalog_map(db)
    catalog = meta.get(base_metric_key(metric_key))

    return MetricSeriesOut(
        device_id=device_id,
        device_name=device.name,
        metric_key=metric_key,
        metric_name=catalog.metric_name if catalog else metric_key,
        unit=catalog.unit if catalog else None,
        count=len(points),
        points=points,
    )


def get_multi_history(
    db: Session,
    device_id: int,
    metric_keys: Sequence[str],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 300,
) -> List[MetricSeriesOut]:
    """一次取多条曲线（设备详情页同时展示 CPU/内存/磁盘趋势）"""
    return [
        get_history(db, device_id, key, start=start, end=end, limit=limit)
        for key in metric_keys
    ]


# ============================================================================
# 指标字典管理
# ============================================================================

def list_catalog(db: Session, enabled_only: bool = False) -> List[MetricCatalog]:
    """指标字典列表（按 category + sort_order 排序）"""
    stmt = select(MetricCatalog)
    if enabled_only:
        stmt = stmt.where(MetricCatalog.enabled.is_(True))
    return list(db.execute(stmt.order_by(
        MetricCatalog.sort_order.asc(), MetricCatalog.id.asc()
    )).scalars().all())


def create_catalog(db: Session, payload: MetricCatalogCreate) -> MetricCatalog:
    """
    新增自定义指标（扩展性：不改进程、不改表结构即可加指标）。

    注意：这里只是"登记元数据"（中文名/单位/分类），
    真正采集到该指标还需要在采集器解析器中产出对应 metric_key。
    如此设计是因为两者的生命周期不同：元数据随时可加，解析逻辑需要发版。
    """
    exists = db.execute(
        select(MetricCatalog.id).where(MetricCatalog.metric_key == payload.metric_key)
    ).scalar_one_or_none()
    if exists:
        raise DuplicateException(f"指标键 {payload.metric_key} 已存在")

    item = MetricCatalog(**payload.model_dump(), is_builtin=False)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def delete_catalog(db: Session, catalog_id: int) -> None:
    """删除指标字典项（内置指标禁止删除，避免大屏/规则引用失效）"""
    item = db.get(MetricCatalog, catalog_id)
    if item is None:
        raise NotFoundException("指标不存在")
    if item.is_builtin:
        raise OperationDeniedException("内置指标不允许删除，如需隐藏请将其停用")
    db.delete(item)
    db.commit()


# ============================================================================
# 数据清理与降采样
# ============================================================================

def cleanup_samples(
    db: Session,
    keep_raw_days: int = 7,
    keep_5min_days: int = 30,
    keep_hour_days: int = 90,
) -> Dict[str, int]:
    """
    历史数据降采样清理（由定时任务每日低峰期执行）。

    处理顺序（必须从最老的数据开始，否则聚合会把已聚合数据再聚合一次）：
      1. 删除 > keep_hour_days 的数据
      2. [keep_5min_days, keep_hour_days] 区间：聚合为 1 小时粒度
      3. [keep_raw_days, keep_5min_days] 区间：聚合为 5 分钟粒度

    返回：{"deleted": n, "hour_aggregated": m, "five_min_aggregated": k}

    说明：字符串型指标（str_value）无法求平均，聚合时取 MAX（保留一个可读值），
    仅作趋势参考——需要精确的字符串历史请到原始数据保留期内查询。
    """
    now_ts = datetime.now()
    raw_cutoff = now_ts - timedelta(days=keep_raw_days)
    five_min_cutoff = now_ts - timedelta(days=keep_5min_days)
    hour_cutoff = now_ts - timedelta(days=keep_hour_days)
    result = {"deleted": 0, "hour_aggregated": 0, "five_min_aggregated": 0}

    # ---- 1. 删除超期数据 ----
    del_result = db.execute(
        delete(MetricSample).where(MetricSample.collect_time < hour_cutoff)
    )
    result["deleted"] = del_result.rowcount or 0

    # ---- 2/3. 分桶聚合 ----
    result["five_min_aggregated"] = _aggregate_range(
        db, raw_cutoff, five_min_cutoff, bucket_seconds=300, label="原始→5分钟"
    )
    result["hour_aggregated"] = _aggregate_range(
        db, five_min_cutoff, hour_cutoff, bucket_seconds=3600, label="5分钟→1小时"
    )

    db.commit()
    system_logger.info("指标清理完成：%s", result)
    return result


def _bucket_expr(bucket_seconds: int):
    """
    构造时间桶表达式（唯一允许使用方言函数的地方，且仅用于数据维护任务）。

    SQLite：strftime('%Y-%m-%d %H:%M', collect_time)
    MySQL ：DATE_FORMAT(collect_time, '%Y-%m-%d %H:%i')
    用固定字符串返回时间文本，避免时区/类型差异带来的二次解析问题。
    """
    if settings.is_sqlite:
        if bucket_seconds >= 3600:
            return func.strftime("%Y-%m-%d %H:00", MetricSample.collect_time)
        return func.strftime("%Y-%m-%d %H:%M", MetricSample.collect_time)
    if bucket_seconds >= 3600:
        return func.date_format(MetricSample.collect_time, "%Y-%m-%d %H:00")
    return func.date_format(MetricSample.collect_time, "%Y-%m-%d %H:%i")


def _aggregate_range(
    db: Session,
    start_cutoff: datetime,
    end_cutoff: datetime,
    bucket_seconds: int,
    label: str = "",
) -> int:
    """
    把 [end_cutoff, start_cutoff) 区间内的明细数据按时间桶聚合，并删除明细。

    区间含义：end_cutoff 是更早的边界，start_cutoff 是较新的边界。
    例如 raw_cutoff=7天前、five_min_cutoff=30天前 →
    聚合区间为 [30天前, 7天前)。
    """
    # 该区间没有数据时直接返回，避免执行无意义的 SQL
    has_data = db.execute(
        select(func.count(MetricSample.id)).where(
            MetricSample.collect_time >= end_cutoff,
            MetricSample.collect_time < start_cutoff,
        )
    ).scalar_one()
    if not has_data:
        return 0

    bucket = _bucket_expr(bucket_seconds)

    # 用 SQL 分组取聚合值（数据量大时比拉到 Python 快得多）
    grouped = db.execute(
        select(
            MetricSample.device_id,
            MetricSample.metric_key,
            func.avg(MetricSample.metric_value).label("avg_value"),
            func.max(MetricSample.str_value).label("str_value"),
            func.max(MetricSample.unit).label("unit"),
            func.max(MetricSample.collect_time).label("last_time"),
            bucket.label("bucket"),
        )
        .where(
            MetricSample.collect_time >= end_cutoff,
            MetricSample.collect_time < start_cutoff,
        )
        .group_by(MetricSample.device_id, MetricSample.metric_key, bucket)
    ).all()

    # 先删除明细，再插入聚合结果，避免同区间数据重复
    db.execute(
        delete(MetricSample).where(
            MetricSample.collect_time >= end_cutoff,
            MetricSample.collect_time < start_cutoff,
        )
    )

    for row in grouped:
        # bucket 是聚合后的时间文本，用解析回 datetime 保证字段类型一致
        bucket_time = _parse_bucket_time(str(row.bucket))
        db.add(MetricSample(
            device_id=row.device_id,
            metric_key=row.metric_key,
            metric_value=float(row.avg_value) if row.avg_value is not None else None,
            str_value=row.str_value,
            unit=row.unit,
            collect_time=bucket_time or row.last_time,
        ))

    db.commit()
    system_logger.info("降采样完成[%s]：原始 %s 行 → 聚合 %s 行", label, has_data, len(grouped))
    return len(grouped)


def _parse_bucket_time(text_value: str) -> Optional[datetime]:
    """解析聚合出来的时间文本。格式非法时返回 None，由调用方回退用 last_time。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:00", "%Y-%m-%d %H:00:00"):
        try:
            return datetime.strptime(text_value, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ============================================================================
# 采集健康度统计（采集日志页顶部指标卡）
# ============================================================================

def collect_success_rate(db: Session, hours: int = 24) -> Dict[str, Any]:
    """
    统计最近 N 小时的采集成功率与平均耗时。

    用一条聚合 SQL 完成，避免把日志全查出来后在 Python 里算。
    """
    from app.models.log import CollectLog

    since = datetime.now() - timedelta(hours=hours)
    row = db.execute(
        select(
            func.count(CollectLog.id),
            # case() 来自 sqlalchemy 而非 func：func.case 会生成不存在的 SQL 函数 CASE(...)
            func.sum(case((CollectLog.status == "success", 1), else_=0)),
            func.avg(CollectLog.duration_ms),
        ).where(CollectLog.collect_time >= since)
    ).one()

    total = int(row[0] or 0)
    success = int(row[1] or 0)
    avg_ms = int(row[2] or 0)
    return {
        "total": total,
        "success": success,
        "fail": total - success,
        "success_rate": round(success * 100.0 / total, 2) if total else 0.0,
        "avg_duration_ms": avg_ms,
    }
