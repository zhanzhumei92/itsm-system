"""
用户管理接口（模块5：多账号、角色权限）
===============================================================================
权限策略：
  - 查询：管理员与查看员均可（查看员需要看到"责任人"信息）
  - 增删改、重置密码：仅管理员
===============================================================================
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ROLE_ADMIN, ROLE_VIEWER, get_current_user, require_role
from app.core.response import PageData, Resp, build_page, resp_ok
from app.models.user import SysUser
from app.schemas.user import PasswordReset, UserCreate, UserOut, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["用户管理"])


@router.get("/roles", response_model=Resp, summary="角色字典")
def list_roles(user: SysUser = Depends(get_current_user)):
    """前端下拉框数据源，集中定义避免前后端角色取值不一致"""
    return resp_ok([
        {"value": ROLE_ADMIN, "label": "管理员", "description": "全部权限"},
        {"value": ROLE_VIEWER, "label": "运维查看员", "description": "只读 + 告警确认"},
    ])


@router.get("", response_model=Resp[PageData[UserOut]], summary="用户列表")
def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: Optional[str] = Query(default=None, description="账号或姓名模糊搜索"),
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    """分页查询用户（所有登录用户可见，便于查看责任人信息）"""
    rows, total = user_service.list_users(db, page, page_size, keyword)
    items = [UserOut.model_validate(row) for row in rows]
    return resp_ok(build_page(items, total, page, page_size))


@router.get("/{user_id}", response_model=Resp[UserOut], summary="用户详情")
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    user: SysUser = Depends(get_current_user),
):
    return resp_ok(UserOut.model_validate(user_service.get_user(db, user_id)))


@router.post("", response_model=Resp[UserOut], summary="新增用户（仅管理员）")
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    created = user_service.create_user(db, payload, operator=operator.username)
    return resp_ok(UserOut.model_validate(created), msg="用户创建成功")


@router.put("/{user_id}", response_model=Resp[UserOut], summary="编辑用户（仅管理员）")
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    updated = user_service.update_user(db, user_id, payload, operator=operator.username)
    return resp_ok(UserOut.model_validate(updated), msg="用户信息已更新")


@router.delete("/{user_id}", response_model=Resp, summary="删除用户（仅管理员）")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """内置 admin 账号与当前登录账号不允许删除（防止把自己锁在系统外）"""
    user_service.delete_user(db, user_id, operator=operator.username)
    return resp_ok(msg="用户已删除")


@router.post("/{user_id}/reset-password", response_model=Resp, summary="重置密码（仅管理员）")
def reset_password(
    user_id: int,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    operator: SysUser = Depends(require_role(ROLE_ADMIN)),
):
    """
    管理员重置他人密码。

    默认要求对方下次登录强制改密——避免管理员知晓他人永久密码造成的责任不清。
    """
    user_service.reset_password(
        db, user_id, payload.new_password, payload.must_change_pwd, operator=operator.username
    )
    return resp_ok(msg="密码已重置")
