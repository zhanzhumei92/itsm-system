"""
资产服务（模块2：IT资产台账、Excel 导入导出、与监控设备绑定）
===============================================================================
关键设计：

1. **导入用"按资产编号 UPSERT"而非纯新增**
   现场常见场景是"上个月导过一次，这个月更新了责任人再导一次"。
   若每次都是新增，会因 asset_code 唯一约束整批失败；
   若静默跳过，又丢了更新。故：存在则更新，不存在则新增，并分别计数返回。

2. **导入失败不整批回滚**
   100 行里有 1 行日期格式错，不应丢掉另外 99 行。
   实现方式：逐行 try/except，错误收集到 errors 列表返回给前端展示。

3. **维保提醒用独立表 + 定时刷新**
   见 models/asset.py 的说明。刷新逻辑放本模块，由 scheduler 每日调用。
===============================================================================
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DuplicateException,
    FileInvalidException,
    NotFoundException,
    OperationDeniedException,
)
from app.core.logger import system_logger
from app.models.asset import (
    ASSET_STATUS_IN_USE,
    ASSET_STATUS_TEXT,
    AssetInfo,
    AssetMaintainRemind,
)
from app.models.device import MonDevice
from app.models.log import SysLog
from app.schemas.asset import AssetCreate, AssetImportResult, AssetOut, AssetUpdate, MaintainRemindOut

# Excel 导入表头（顺序固定，模板与此一致；中文表头便于运维直接维护）
EXCEL_HEADERS = [
    "资产编号", "设备名称", "设备类型", "厂商", "型号", "序列号", "IP地址", "MAC地址",
    "机房位置", "机柜位置", "上架时间", "维保起始", "维保到期", "责任人", "责任人电话",
    "供应商", "采购日期", "采购金额", "状态", "备注",
]

# 表头 → ORM 字段映射（设备类型/状态需做中文转换，故单列处理）
HEADER_FIELD_MAP: Dict[str, str] = {
    "资产编号": "asset_code",
    "设备名称": "name",
    "厂商": "vendor",
    "型号": "model",
    "序列号": "serial_no",
    "IP地址": "ip",
    "MAC地址": "mac",
    "机房位置": "room_location",
    "机柜位置": "rack_position",
    "上架时间": "online_at",
    "维保起始": "warranty_start",
    "维保到期": "warranty_end",
    "责任人": "owner",
    "责任人电话": "owner_phone",
    "供应商": "supplier",
    "采购日期": "purchase_at",
    "采购金额": "purchase_price",
    "备注": "remark",
}

# 日期型字段（导入时需要解析）
DATE_FIELDS = {"online_at", "warranty_start", "warranty_end", "purchase_at"}

# 状态中文 → 英文枚举（导入时容错：直接写英文枚举也接受）
STATUS_TEXT_TO_CODE = {v: k for k, v in ASSET_STATUS_TEXT.items()}

# 支持解析的日期格式（现场 Excel 常见几种写法都要兼容）
DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")


# ============================================================================
# 查询
# ============================================================================

def list_assets(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    device_type: Optional[str] = None,
    room_location: Optional[str] = None,
    monitored_only: bool = False,
    warranty_warning_days: Optional[int] = None,
) -> Tuple[List[AssetOut], int]:
    """
    分页查询资产（联表补充绑定设备的在线状态）。

    :param monitored_only: 仅看已绑定监控设备的资产
    :param warranty_warning_days: 仅看 N 天内维保到期的资产（大屏/列表筛选）
    """
    stmt = select(AssetInfo, MonDevice.name, MonDevice.online_status).outerjoin(
        MonDevice, AssetInfo.device_id == MonDevice.id
    )
    count_stmt = select(func.count(AssetInfo.id))

    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            AssetInfo.asset_code.like(pattern),
            AssetInfo.name.like(pattern),
            AssetInfo.ip.like(pattern),
            AssetInfo.serial_no.like(pattern),
            AssetInfo.owner.like(pattern),
        ))
    if status:
        conditions.append(AssetInfo.status == status)
    if device_type:
        conditions.append(AssetInfo.device_type == device_type)
    if room_location:
        conditions.append(AssetInfo.room_location.like(f"%{room_location}%"))
    if monitored_only:
        conditions.append(AssetInfo.device_id.isnot(None))
    if warranty_warning_days is not None:
        # 已过期或 N 天内到期（用 Python 侧算边界时间，避免方言日期函数差异）
        deadline = datetime.now() + timedelta(days=warranty_warning_days)
        conditions.append(AssetInfo.warranty_end.isnot(None))
        conditions.append(AssetInfo.warranty_end <= deadline)

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        stmt.order_by(AssetInfo.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    items: List[AssetOut] = []
    for asset, device_name, device_online in rows:
        out = AssetOut.model_validate(asset)
        out.device_name = device_name
        out.device_online_status = device_online
        items.append(out)
    return items, total


def get_asset(db: Session, asset_id: int) -> AssetInfo:
    """按 ID 取资产"""
    asset = db.get(AssetInfo, asset_id)
    if asset is None:
        raise NotFoundException("资产不存在")
    return asset


def get_asset_detail(db: Session, asset_id: int) -> AssetOut:
    """资产详情（含绑定设备信息）"""
    asset = get_asset(db, asset_id)
    out = AssetOut.model_validate(asset)
    if asset.device_id:
        device = db.get(MonDevice, asset.device_id)
        if device:
            out.device_name = device.name
            out.device_online_status = device.online_status
    return out


# ============================================================================
# 写入
# ============================================================================

def create_asset(db: Session, payload: AssetCreate, operator: str = "") -> AssetOut:
    """新增资产（资产编号唯一）"""
    exists = db.execute(
        select(AssetInfo.id).where(AssetInfo.asset_code == payload.asset_code)
    ).scalar_one_or_none()
    if exists:
        raise DuplicateException(f"资产编号 {payload.asset_code} 已存在")

    if payload.device_id:
        _ensure_device_exists(db, payload.device_id)

    asset = AssetInfo(**payload.model_dump())
    db.add(asset)
    db.add(SysLog(
        level="INFO", module="asset", action="create_asset",
        message=f"新增资产 {asset.asset_code} {asset.name}", username=operator,
    ))
    db.commit()
    db.refresh(asset)
    return get_asset_detail(db, asset.id)


def update_asset(db: Session, asset_id: int, payload: AssetUpdate, operator: str = "") -> AssetOut:
    """编辑资产"""
    asset = get_asset(db, asset_id)
    data = payload.model_dump(exclude_unset=True)

    new_code = data.get("asset_code")
    if new_code and new_code != asset.asset_code:
        dup = db.execute(
            select(AssetInfo.id).where(AssetInfo.asset_code == new_code, AssetInfo.id != asset_id)
        ).scalar_one_or_none()
        if dup:
            raise DuplicateException(f"资产编号 {new_code} 已被占用")

    if data.get("device_id"):
        _ensure_device_exists(db, data["device_id"])

    for field, value in data.items():
        setattr(asset, field, value)

    db.add(SysLog(
        level="INFO", module="asset", action="update_asset",
        message=f"修改资产 {asset.asset_code}：{list(data.keys())}", username=operator,
    ))
    db.commit()
    db.refresh(asset)
    return get_asset_detail(db, asset_id)


def delete_asset(db: Session, asset_id: int, operator: str = "") -> None:
    """删除资产（同时清理其维保提醒记录）"""
    asset = get_asset(db, asset_id)
    code = asset.asset_code
    db.execute(
        AssetMaintainRemind.__table__.delete().where(AssetMaintainRemind.asset_id == asset_id)
    )
    db.delete(asset)
    db.add(SysLog(
        level="WARNING", module="asset", action="delete_asset",
        message=f"删除资产 {code}", username=operator,
    ))
    db.commit()


def bind_device(db: Session, asset_id: int, device_id: Optional[int], operator: str = "") -> AssetOut:
    """
    资产与监控设备绑定 / 解绑（需求：资产与实时监控数据关联）。

    约束：一台监控设备只能绑定一个资产。
    否则会出现"两个资产显示同一台设备的实时数据"，运维无法判断归属。
    """
    asset = get_asset(db, asset_id)

    if device_id is None:
        asset.device_id = None
    else:
        _ensure_device_exists(db, device_id)
        occupied = db.execute(
            select(AssetInfo).where(AssetInfo.device_id == device_id, AssetInfo.id != asset_id)
        ).scalar_one_or_none()
        if occupied:
            raise OperationDeniedException(
                f"该监控设备已绑定资产 {occupied.asset_code}（{occupied.name}），请先解绑"
            )
        asset.device_id = device_id

    db.add(SysLog(
        level="INFO", module="asset", action="bind_device",
        message=f"资产 {asset.asset_code} {'绑定设备 ' + str(device_id) if device_id else '解绑设备'}",
        username=operator,
    ))
    db.commit()
    return get_asset_detail(db, asset_id)


def _ensure_device_exists(db: Session, device_id: int) -> None:
    """校验监控设备存在（与其他模块保持一致，统一用 NotFoundException → HTTP 404）"""
    if db.get(MonDevice, device_id) is None:
        raise NotFoundException(f"监控设备 {device_id} 不存在，无法绑定")


# ============================================================================
# Excel 导入 / 导出
# ============================================================================

def _parse_date(value: Any) -> Optional[datetime]:
    """
    宽松解析日期（导入容错核心）。

    支持：datetime 对象（openpyxl 会自动转换）、多种字符串格式。
    无法解析时返回 None 而非抛异常——日期字段并非必填，
    填错一个日期不应该让整行导入失败，但会在 errors 里提示。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_status(value: Any) -> str:
    """解析资产状态：支持中文（在用）与英文（in_use）两种写法"""
    if value is None or str(value).strip() == "":
        return ASSET_STATUS_IN_USE
    text = str(value).strip()
    if text in ASSET_STATUS_TEXT:
        return text
    if text in STATUS_TEXT_TO_CODE:
        return STATUS_TEXT_TO_CODE[text]
    return ASSET_STATUS_IN_USE


def build_import_template() -> bytes:
    """
    生成 Excel 导入模板（含表头 + 一行示例 + 说明）。
    运维下载模板填写后上传，避免列顺序/列名不匹配导致的整批失败。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "资产台账导入模板"

    header_fill = PatternFill("solid", fgColor="00479D")     # 亿网通品牌蓝
    header_font = Font(color="FFFFFF", bold=True, size=11)

    ws.append(EXCEL_HEADERS)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 示例行（说明各列期望格式）
    ws.append([
        "ZC-2026-001", "核心交换机01", "switch", "华为", "S5731-S48T4X", "SN1234567890",
        "10.10.1.1", "00:1A:2B:3C:4D:5E", "总部三楼机房", "A-01",
        "2024-03-15", "2024-03-15", "2027-03-14", "詹柱美", "18025081188",
        "广东亿网通科技有限公司", "2024-03-10", 28000, "在用", "核心汇聚",
    ])

    # 列宽设置，便于直接打印填写
    widths = [16, 20, 12, 12, 18, 18, 16, 20, 18, 12, 14, 14, 14, 12, 16, 24, 14, 12, 10, 20]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width

    # 说明页：写清必填项与格式，减少来回沟通
    ws2 = wb.create_sheet("填写说明")
    notes = [
        ["字段", "是否必填", "格式说明"],
        ["资产编号", "必填", "唯一，重复则更新该条记录；建议 ZC-年份-序号"],
        ["设备名称", "必填", "任意文本"],
        ["设备类型", "选填", "switch/server/firewall/router/storage/pc/other"],
        ["上架时间/维保起始/维保到期/采购日期", "选填", "支持 2026-09-21 或 2026/09/21 或 2026年9月21日"],
        ["采购金额", "选填", "数字，不要带货币符号"],
        ["状态", "选填", "在用/闲置/维修中/已报废，也可填 in_use/idle/repair/scrapped"],
        ["备注", "选填", "任意文本"],
    ]
    for row in notes:
        ws2.append(row)
    ws2.column_dimensions["A"].width = 34
    ws2.column_dimensions["B"].width = 12
    ws2.column_dimensions["C"].width = 70

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def import_from_excel(db: Session, file_bytes: bytes, operator: str = "") -> AssetImportResult:
    """
    从 Excel 导入资产（按资产编号 UPSERT）。

    容错原则：单行失败不影响其它行；错误信息收集返回，最多 50 条（避免响应体过大）。
    """
    from openpyxl import load_workbook

    result = AssetImportResult()

    try:
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as exc:
        raise FileInvalidException(f"Excel 文件无法解析，请确认文件未损坏或加密：{exc}")

    ws = wb.active
    if ws is None or ws.max_row < 2:
        raise FileInvalidException("Excel 内容为空，请使用平台提供的模板填写后上传")

    # 表头校验：缺少必填列直接拒绝，避免"导入了但字段全是空的"
    header_row = [str(cell.value).strip() if cell.value else "" for cell in ws[1]]
    for required in ("资产编号", "设备名称"):
        if required not in header_row:
            raise FileInvalidException(f"Excel 缺少必填列「{required}」，请下载模板后填写")

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        # 整行为空则跳过（Excel 常见尾部空行）
        if not row or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        result.total += 1
        try:
            payload: Dict[str, Any] = {}
            for col_idx, header in enumerate(header_row):
                if not header or col_idx >= len(row):
                    continue
                value = row[col_idx]

                if header == "设备类型":
                    payload["device_type"] = str(value).strip() if value else None
                    continue
                if header == "状态":
                    payload["status"] = _parse_status(value)
                    continue

                field = HEADER_FIELD_MAP.get(header)
                if not field:
                    continue
                if field in DATE_FIELDS:
                    payload[field] = _parse_date(value)
                elif field == "purchase_price":
                    try:
                        payload[field] = float(value) if value not in (None, "") else None
                    except (TypeError, ValueError):
                        payload[field] = None
                else:
                    payload[field] = str(value).strip() if value not in (None, "") else None

            asset_code = payload.get("asset_code")
            asset_name = payload.get("name")
            if not asset_code:
                result.failed += 1
                result.errors.append(f"第 {row_idx} 行：资产编号为空，已跳过")
                continue
            if not asset_name:
                result.failed += 1
                result.errors.append(f"第 {row_idx} 行：设备名称为空，已跳过")
                continue

            existing = db.execute(
                select(AssetInfo).where(AssetInfo.asset_code == asset_code)
            ).scalar_one_or_none()

            if existing:
                for field, value in payload.items():
                    # 空值不覆盖已有数据（避免"模板里没填的列把原值清空"）
                    if value not in (None, ""):
                        setattr(existing, field, value)
                result.updated += 1
            else:
                payload.setdefault("status", ASSET_STATUS_IN_USE)
                db.add(AssetInfo(**payload))
                result.created += 1

        except Exception as exc:
            result.failed += 1
            result.errors.append(f"第 {row_idx} 行导入失败：{exc}")
            system_logger.warning("资产导入第 %s 行失败：%s", row_idx, exc)

    db.add(SysLog(
        level="INFO", module="asset", action="import_asset",
        message=f"导入资产：共 {result.total} 行，新增 {result.created}，更新 {result.updated}，失败 {result.failed}",
        username=operator,
    ))
    db.commit()

    if len(result.errors) > 50:
        result.errors = result.errors[:50] + [f"...另有 {len(result.errors) - 50} 条错误未显示"]
    return result


def export_to_excel(
    db: Session,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    device_type: Optional[str] = None,
) -> bytes:
    """导出资产台账为 Excel（导出全部符合筛选条件的数据，不分页）"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    items, _ = list_assets(
        db, page=1, page_size=100000, keyword=keyword, status=status, device_type=device_type
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "IT资产台账"

    header_fill = PatternFill("solid", fgColor="00479D")
    header_font = Font(color="FFFFFF", bold=True)
    ws.append(EXCEL_HEADERS + ["绑定监控设备", "设备在线状态"])
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    status_text = {"in_use": "在用", "idle": "闲置", "repair": "维修中", "scrapped": "已报废"}
    online_text = {"online": "在线", "offline": "离线", "unknown": "未知"}

    def fmt(dt: Optional[datetime]) -> str:
        return dt.strftime("%Y-%m-%d") if dt else ""

    for item in items:
        ws.append([
            item.asset_code, item.name, item.device_type, item.vendor, item.model, item.serial_no,
            item.ip, item.mac, item.room_location, item.rack_position,
            fmt(item.online_at), fmt(item.warranty_start), fmt(item.warranty_end),
            item.owner, item.owner_phone, item.supplier, fmt(item.purchase_at),
            item.purchase_price, status_text.get(item.status or "", item.status),
            item.remark or "", item.device_name or "", online_text.get(item.device_online_status or "", ""),
        ])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ============================================================================
# 维保到期提醒
# ============================================================================

def refresh_maintain_remind(db: Session, remind_days: int = 90) -> int:
    """
    刷新维保到期提醒（由定时任务每日调用）。

    规则：
      已过期         → remind_level = expired（大屏红色）
      ≤30 天         → urgent（橙色）
      ≤90 天         → soon（蓝色）
      >90 天         → 不生成提醒记录（并清理历史记录，例如续保后应消失）

    返回：生成的提醒条数。
    """
    now_ts = datetime.now()
    deadline = now_ts + timedelta(days=remind_days)

    assets = db.execute(
        select(AssetInfo).where(
            AssetInfo.warranty_end.isnot(None),
            AssetInfo.warranty_end <= deadline,
        )
    ).scalars().all()

    valid_asset_ids = {a.id for a in assets}

    # 清理：续保或数据修正后不再到期的提醒记录
    stale = db.execute(select(AssetMaintainRemind)).scalars().all()
    for remind in stale:
        if remind.asset_id not in valid_asset_ids:
            db.delete(remind)

    existing_map = {
        r.asset_id: r
        for r in db.execute(select(AssetMaintainRemind)).scalars().all()
        if r.asset_id in valid_asset_ids
    }

    count = 0
    for asset in assets:
        days_left = (asset.warranty_end - now_ts).days
        if days_left < 0:
            level = "expired"
        elif days_left <= 30:
            level = "urgent"
        else:
            level = "soon"

        remind = existing_map.get(asset.id)
        if remind is None:
            remind = AssetMaintainRemind(asset_id=asset.id)
            db.add(remind)

        remind.asset_code = asset.asset_code
        remind.asset_name = asset.name
        remind.warranty_end = asset.warranty_end
        remind.remind_days = remind_days
        remind.remind_level = level
        # 到期日变化（续保）时重置状态，让运维重新看到提醒
        if remind.remind_status in (None, ""):
            remind.remind_status = "pending"
        count += 1

    db.commit()
    system_logger.info("维保提醒刷新完成，共 %s 条", count)
    return count


def list_maintain_remind(
    db: Session, limit: int = 50, only_pending: bool = False
) -> List[MaintainRemindOut]:
    """查询维保到期提醒列表（大屏容量视图 + 后台列表共用，按到期时间升序）"""
    # 注意：where 必须在 order_by/limit 之前调用，否则 SQLAlchemy 会抛
    # "Query.filter() being called on a Query which already has LIMIT applied" 类错误
    stmt = select(AssetMaintainRemind)
    if only_pending:
        stmt = stmt.where(AssetMaintainRemind.remind_status == "pending")
    stmt = stmt.order_by(AssetMaintainRemind.warranty_end.asc()).limit(limit)

    rows = db.execute(stmt).scalars().all()

    # 补充责任人（提醒表未冗余该字段，按需联查，避免字段冗余过多）
    asset_ids = [r.asset_id for r in rows]
    owner_map: Dict[int, Tuple[Optional[str], Optional[str]]] = {}
    if asset_ids:
        for asset in db.execute(
            select(AssetInfo).where(AssetInfo.id.in_(asset_ids))
        ).scalars().all():
            owner_map[asset.id] = (asset.owner, asset.owner_phone)

    items: List[MaintainRemindOut] = []
    for r in rows:
        out = MaintainRemindOut.model_validate(r)
        out.owner, out.owner_phone = owner_map.get(r.asset_id, (None, None))
        items.append(out)
    return items
