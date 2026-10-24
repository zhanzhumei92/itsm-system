"""
依赖注入：当前用户、角色守卫
===============================================================================
设计说明：

1. **每次请求都重新查库取用户**，不信任 JWT 中的 role 字段。
   理由：管理员把某账号从 admin 降为 viewer 后必须立即生效；
   若直接用令牌里的 role，用户在令牌过期前仍保有旧权限——这是真实的安全漏洞。

2. **角色守卫用工厂函数 require_role("admin")**，写路由时声明式表达权限：
       @router.post("", dependencies=[Depends(require_admin)])
   比在每个函数体里写 if user.role != "admin" 更不易遗漏。

3. 角色定义（对应模块 5 需求）：
   - admin  管理员：全部权限（设备/资产/规则/配置/用户增删改）
   - viewer 运维查看员：只读 + 告警确认
===============================================================================
"""
from __future__ import annotations

from typing import Callable, List

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import AuthException, PermissionException
from app.core.security import decode_access_token
from app.models.user import SysUser

# auto_error=False：未携带令牌时不由 FastAPI 直接抛错，而是交由本模块统一处理，
# 保证返回结构与其它接口一致（避免出现 FastAPI 默认的 {"detail": "Not authenticated"}）
bearer_scheme = HTTPBearer(auto_error=False, description="登录后获取的 JWT 访问令牌")

# 角色常量（集中定义，避免各处硬编码字符串）
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> SysUser:
    """
    解析令牌并返回当前登录用户对象。

    校验顺序（任一失败均返回 401）：
      1. 是否携带令牌
      2. 令牌签名与有效期
      3. 用户是否存在
      4. 用户是否被禁用
    """
    if credentials is None or not credentials.credentials:
        raise AuthException("未提供访问令牌，请先登录")

    payload = decode_access_token(credentials.credentials)
    if not payload:
        raise AuthException()

    user_id = payload.get("sub")
    if not user_id:
        raise AuthException("令牌内容异常，请重新登录")

    user = db.get(SysUser, int(user_id))
    if user is None:
        raise AuthException("账号不存在，请重新登录")
    if not user.status:
        raise AuthException("账号已被禁用，请联系管理员")

    return user


def require_role(*roles: str) -> Callable[..., SysUser]:
    """
    角色守卫工厂。

    用法：
        # 仅管理员可写
        @router.post("", dependencies=[Depends(require_role(ROLE_ADMIN))])

        # 管理员与查看员都可用（如确认告警）
        @router.post("/{alert_id}/ack", dependencies=[Depends(require_role(ROLE_ADMIN, ROLE_VIEWER))])

    返回值同时可作为依赖拿到当前用户对象：
        def handler(user: SysUser = Depends(require_role(ROLE_ADMIN))):
    """
    allowed: List[str] = list(roles) or [ROLE_ADMIN]

    def _guard(user: SysUser = Depends(get_current_user)) -> SysUser:
        if user.role not in allowed:
            raise PermissionException(
                f"该操作需要 {'/'.join(allowed)} 权限，当前账号角色为 {user.role}"
            )
        return user

    return _guard


# 常用守卫的快捷引用（路由中直接 Depends(require_admin)）
require_admin = require_role(ROLE_ADMIN)
require_login = require_role(ROLE_ADMIN, ROLE_VIEWER)
