"""
认证接口：登录、当前用户、修改密码
===============================================================================
接口层职责边界（严格遵守）：
  只做 参数校验 → 调用 Service → 包装统一响应。
  不写业务判断、不直接操作数据库 Session（业务逻辑全在 user_service）。
===============================================================================
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.response import Resp, resp_ok
from app.core.security import create_access_token
from app.models.user import SysUser
from app.schemas.user import ChangePassword, LoginRequest, TokenOut, UserOut
from app.services import user_service

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_ip(request: Request) -> str:
    """
    获取客户端真实 IP。

    内网部署常经 Nginx 反向代理，此时 request.client.host 是代理 IP，
    需要优先读 X-Forwarded-For 的第一段。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post("/login", response_model=Resp[TokenOut], summary="用户登录")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    登录并获取 JWT 访问令牌。

    - 账号或密码错误统一返回 401，不区分"用户不存在"与"密码错误"
    - 登录成功/失败都会写入 sys_log，失败记录可用于排查异常登录
    """
    ip = _client_ip(request)
    user = user_service.authenticate(db, payload.username, payload.password, ip=ip)

    token_data = create_access_token(user_id=user.id, username=user.username, role=user.role)
    out = TokenOut(
        **token_data,
        real_name=user.real_name,
        must_change_pwd=bool(user.must_change_pwd),
    )
    return resp_ok(out, msg="登录成功")


@router.get("/me", response_model=Resp[UserOut], summary="获取当前登录用户信息")
def get_me(user: SysUser = Depends(get_current_user)):
    """
    前端刷新页面时调用，用于恢复用户信息与权限判断。

    注意：每次都从数据库读取，保证角色变更立即生效（不信任令牌内的 role）。
    """
    return resp_ok(UserOut.model_validate(user))


@router.post("/change-password", response_model=Resp, summary="修改自己的密码")
def change_password(
    payload: ChangePassword,
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改密码需验证原密码，防止令牌被盗后直接接管账号"""
    user_service.change_password(db, user, payload.old_password, payload.new_password)
    return resp_ok(msg="密码修改成功，请使用新密码登录")


@router.post("/logout", response_model=Resp, summary="退出登录")
def logout(
    request: Request,
    user: SysUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    退出登录。

    JWT 是无状态的，服务端无法真正"注销"令牌，前端需自行清除本地令牌。
    此处仅记录操作日志，便于审计"谁在什么时候退出了系统"。
    """
    from app.services.log_service import write_sys_log

    write_sys_log(
        db=db, level="INFO", module="auth", action="logout",
        message=f"用户 {user.username} 退出登录",
        user_id=user.id, username=user.username, ip=_client_ip(request), commit=True,
    )
    return resp_ok(msg="已退出登录")
