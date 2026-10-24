"""
资产管理接口（模块2：资产台账 CRUD、Excel 导入导出、与监控设备绑定）
===============================================================================
权限策略：
  - 查询、导出：管理员 + 查看员
  - 增删改、导入、绑定：仅管理员
    （导入是批量写操作，一旦误操作影响面大，必须管理员执行）
===============================================================================
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, get_current_user, require_role
from app.core.exceptions import FileInvalidException
from app.core.response import PageData, Resp, build_page, resp_ok
from app.models.user import SysUser
from app.schemas.asset import (
    AssetBindRequest,
    AssetCreate,
    AssetImportResult,
    AssetOut,
    AssetUpdate,
    MaintainRemindOut,
)
from app.services import asset_service

router = APIRouter(prefix="/assets", tags=["资产管理"])

# 单次上传大小上限（10MB）：一张几百行的台账 Excel 通常不到 1MB，
# 设上限是为了防止误传大文件把内存吃满
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _excel_response(content: bytes, filename: str) -> Response:
    """
    构造 Excel 下载响应。

    文件名用 RFC 5987 编码（filename*=UTF-8''...）：
    因为资产文件名含中文，不编码时部分浏览器会下载成乱码文件名。
    """
    quoted = urllib.parse.quote(filename)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


@router.get("/template", summary="下载资产导入模板")
def download_template(user: SysUser = Depends(get_current_user)):
    """
    下载 Excel 导入模板（含表头、示例行、填写说明页）。

    先给模板再导入，是避免"列名不匹配导致整批失败"最有效的手段。
    """
    content = asset_service.build_import_template()
    return _excel_response(content, "IT资产台账导入模板.xlsx")


@router.get("/export", summary="导出资产台账")
def export_assets(
    keyword: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    device_type: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """按当前筛选条件导出全部资产（不分页）"""
    content = asset_service.export_to_excel(db, keyword, status, device_type)
    stamp = datetime.now().strftime("%Y%m%d%H%M")
    return _excel_response(content, f"IT资产台账_{stamp}.xlsx")


@router.post("/import", response_model=Resp[AssetImportResult], summary="导入资产（仅管理员）")
async def import_assets(
    file: UploadFile = File(..., description="按模板填写的 xlsx 文件"),
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    从 Excel 导入资产。

    导入策略为"按资产编号 UPSERT"：编号已存在则更新，不存在则新增，
    并用 updated / created 两个计数明确告知本次操作结果。
    单行失败不影响其它行，错误明细在 errors 中返回。
    """
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise FileInvalidException("仅支持 .xlsx 格式，请使用平台提供的模板")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise FileInvalidException("文件超过 10MB 上限，请拆分后分批导入")

    result = asset_service.import_from_excel(db, content, operator=operator.username)
    msg = f"导入完成：新增 {result.created} 条，更新 {result.updated} 条，失败 {result.failed} 条"
    return resp_ok(result, msg=msg)


@router.get("/maintain-remind", response_model=Resp[List[MaintainRemindOut]], summary="维保到期提醒列表")
def list_maintain_remind(
    limit: int = Query(default=50, ge=1, le=500),
    only_pending: bool = Query(default=False, description="仅看未处理的提醒"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """
    维保到期提醒（大屏"容量视图"与后台列表共用）。

    数据来自 asset_maintain_remind 表，由每日 08:00 的定时任务刷新。
    """
    return resp_ok(asset_service.list_maintain_remind(db, limit, only_pending))


@router.get("", response_model=Resp[PageData[AssetOut]], summary="资产列表")
def list_assets(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=500),
    keyword: Optional[str] = Query(default=None, description="编号/名称/IP/序列号/责任人模糊搜索"),
    status: Optional[str] = Query(default=None),
    device_type: Optional[str] = Query(default=None),
    room_location: Optional[str] = Query(default=None),
    monitored_only: bool = Query(default=False, description="仅看已绑定监控设备的资产"),
    warranty_warning_days: Optional[int] = Query(
        default=None, ge=1, le=3650, description="仅看 N 天内维保到期的资产"
    ),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """资产列表（含绑定监控设备的在线状态，实现"资产与实时监控数据关联"）"""
    items, total = asset_service.list_assets(
        db, page, page_size, keyword, status, device_type, room_location,
        monitored_only, warranty_warning_days,
    )
    return resp_ok(build_page(items, total, page, page_size))


@router.get("/{asset_id}", response_model=Resp[AssetOut], summary="资产详情")
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    return resp_ok(asset_service.get_asset_detail(db, asset_id))


@router.post("", response_model=Resp[AssetOut], summary="新增资产（仅管理员）")
def create_asset(
    payload: AssetCreate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    return resp_ok(asset_service.create_asset(db, payload, operator=operator.username), msg="资产已新增")


@router.put("/{asset_id}", response_model=Resp[AssetOut], summary="编辑资产（仅管理员）")
def update_asset(
    asset_id: int,
    payload: AssetUpdate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    return resp_ok(
        asset_service.update_asset(db, asset_id, payload, operator=operator.username),
        msg="资产已更新",
    )


@router.delete("/{asset_id}", response_model=Resp, summary="删除资产（仅管理员）")
def delete_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    asset_service.delete_asset(db, asset_id, operator=operator.username)
    return resp_ok(msg="资产已删除")


@router.post("/{asset_id}/bind", response_model=Resp[AssetOut], summary="绑定/解绑监控设备")
def bind_device(
    asset_id: int,
    payload: AssetBindRequest,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    资产与监控设备绑定（device_id 传 null 表示解绑）。

    绑定后可在资产详情查看该设备的实时指标与历史曲线，
    这是"资产台账"与"监控数据"打通的唯一入口。
    """
    result = asset_service.bind_device(db, asset_id, payload.device_id, operator=operator.username)
    return resp_ok(result, msg="绑定关系已更新")
