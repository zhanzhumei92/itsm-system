"""
用户服务（模块5：多账号、角色权限）
===============================================================================
职责：登录认证、用户 CRUD、密码管理。
边界：不感知 HTTP（不接收 Request 对象），失败一律抛 BizException，
      由全局异常处理器翻译成响应，保证业务逻辑可脱离 Web 框架单测。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    AuthException,
    BizException,
    DuplicateException,
    NotFoundException,
    OperationDeniedException,
    PermissionException,
)
from app.core.logger import system_logger
from app.core.security import hash_password, verify_password
from app.models.log import SysLog
from app.models.user import SysUser
from app.schemas.user import UserCreate, UserUpdate

# 账号安全策略：项目不大，用常量集中管理，将来可迁移到 sys_config
MIN_PASSWORD_LEN = 6
PROTECTED_USERNAME = "admin"      # 内置管理员，禁止删除/禁用，避免把自己锁在系统外


def _write_login_log(db: Session, user: Optional[SysUser], username: str, ip: str, success: bool) -> None:
    """记录登录日志（成功与失败都记，失败记录是排查暴力破解的依据）"""
    db.add(SysLog(
        level="INFO" if success else "WARNING",
        module="auth",
        action="login" if success else "login_failed",
        message=f"用户 {username} 登录{'成功' if success else '失败'}",
        user_id=user.id if user else None,
        username=username,
        ip=ip,
    ))


def authenticate(db: Session, username: str, password: str, ip: str = "") -> SysUser:
    """
    登录认证。

    安全要点：用户不存在与密码错误返回**同一条提示**，
    避免通过错误信息差异枚举出系统里存在哪些账号。
    """
    stmt = select(SysUser).where(SysUser.username == username)
    user = db.execute(stmt).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        _write_login_log(db, user, username, ip, success=False)
        db.commit()
        system_logger.warning("登录失败 | 账号=%s | IP=%s", username, ip)
        raise AuthException("账号或密码错误")

    if not user.status:
        _write_login_log(db, user, username, ip, success=False)
        db.commit()
        raise PermissionException("该账号已被禁用，请联系管理员")

    # 登录成功：更新最后登录信息
    user.last_login_at = datetime.now()
    user.last_login_ip = ip
    _write_login_log(db, user, username, ip, success=True)
    db.commit()
    db.refresh(user)
    return user


def get_user(db: Session, user_id: int) -> SysUser:
    """按 ID 取用户，不存在则抛 404"""
    user = db.get(SysUser, user_id)
    if user is None:
        raise NotFoundException("用户不存在")
    return user


def list_users(
    db: Session, page: int = 1, page_size: int = 20, keyword: Optional[str] = None
) -> Tuple[list[SysUser], int]:
    """
    分页查询用户。

    :return: (当前页用户列表, 总条数)
    注意：本函数内不抛异常，查询结果为空是正常业务状态，由接口层返回空列表。
    """
    stmt = select(SysUser)
    count_stmt = select(func.count(SysUser.id))

    if keyword:
        pattern = f"%{keyword}%"
        condition = or_(SysUser.username.like(pattern), SysUser.real_name.like(pattern))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(stmt.order_by(SysUser.id.asc()).offset((page - 1) * page_size).limit(page_size))
        .scalars()
        .all()
    )
    return list(rows), total


def create_user(db: Session, payload: UserCreate, operator: str = "") -> SysUser:
    """新增用户。账号唯一性先查后插（并发下由数据库唯一索引兜底）。"""
    exists = db.execute(
        select(SysUser.id).where(SysUser.username == payload.username)
    ).scalar_one_or_none()
    if exists:
        raise DuplicateException(f"账号 {payload.username} 已存在")

    if len(payload.password) < MIN_PASSWORD_LEN:
        raise BizException(f"密码长度不能少于 {MIN_PASSWORD_LEN} 位")

    user = SysUser(
        username=payload.username,
        password_hash=hash_password(payload.password),
        real_name=payload.real_name,
        role=payload.role,
        email=payload.email,
        phone=payload.phone,
        status=payload.status,
        remark=payload.remark,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(SysLog(
        level="INFO", module="user", action="create_user",
        message=f"新增用户 {user.username}（角色 {user.role}）", username=operator,
    ))
    db.commit()
    return user


def update_user(db: Session, user_id: int, payload: UserUpdate, operator: str = "") -> SysUser:
    """编辑用户（只更新传入字段）"""
    user = get_user(db, user_id)
    data = payload.model_dump(exclude_unset=True)

    # 保护措施：不允许把内置 admin 降级或禁用，否则可能失去唯一的全权限账号
    # 这类"产品有意拒绝"用 OperationDeniedException（HTTP 200 + 1004），
    # 而不是 400 参数错误——用户输入本身没有错，是规则不允许。
    if user.username == PROTECTED_USERNAME:
        if data.get("role") and data["role"] != "admin":
            raise OperationDeniedException("内置管理员账号不允许修改角色")
        if data.get("status") is False:
            raise OperationDeniedException("内置管理员账号不允许禁用")

    for field, value in data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    db.add(SysLog(
        level="INFO", module="user", action="update_user",
        message=f"修改用户 {user.username}：{list(data.keys())}", username=operator,
    ))
    db.commit()
    return user


def delete_user(db: Session, user_id: int, operator: str = "") -> None:
    """
    删除用户。

    保护措施：
      1. 不允许删除内置 admin
      2. 不允许删除自己（避免误操作后当前会话立即失效）
    """
    user = get_user(db, user_id)
    if user.username == PROTECTED_USERNAME:
        raise OperationDeniedException("内置管理员账号不允许删除")
    if operator and operator == user.username:
        raise OperationDeniedException("不能删除当前登录的账号")

    db.delete(user)
    db.add(SysLog(
        level="WARNING", module="user", action="delete_user",
        message=f"删除用户 {user.username}", username=operator,
    ))
    db.commit()


def reset_password(db: Session, user_id: int, new_password: str, must_change: bool, operator: str = "") -> None:
    """管理员重置他人密码"""
    user = get_user(db, user_id)
    if len(new_password) < MIN_PASSWORD_LEN:
        raise BizException(f"密码长度不能少于 {MIN_PASSWORD_LEN} 位")

    user.password_hash = hash_password(new_password)
    user.must_change_pwd = must_change
    db.add(SysLog(
        level="WARNING", module="user", action="reset_password",
        message=f"重置用户 {user.username} 的密码", username=operator,
    ))
    db.commit()


def change_password(db: Session, user: SysUser, old_password: str, new_password: str) -> None:
    """
    用户自行修改密码（须验证旧密码）。

    为什么必须验证旧密码：防止令牌被盗用后攻击者直接改密接管账号。
    """
    if not verify_password(old_password, user.password_hash):
        raise BizException("原密码不正确")
    if len(new_password) < MIN_PASSWORD_LEN:
        raise BizException(f"新密码长度不能少于 {MIN_PASSWORD_LEN} 位")
    if old_password == new_password:
        raise BizException("新密码不能与原密码相同")

    user.password_hash = hash_password(new_password)
    user.must_change_pwd = False
    db.add(SysLog(
        level="INFO", module="user", action="change_password",
        message=f"用户 {user.username} 修改密码", user_id=user.id, username=user.username,
    ))
    db.commit()
