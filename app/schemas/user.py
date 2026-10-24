"""用户与认证相关 Schema（对应模块5：用户管理）"""
from __future__ import annotations

from typing import Optional

from pydantic import Field, computed_field, field_validator

from app.schemas.common import BaseSchema, OptionalDateTimeStr

# 合法角色取值（与 core/deps.py 中守卫的角色保持一致）
VALID_ROLES = {"admin", "viewer"}


class LoginRequest(BaseSchema):
    """登录请求"""

    username: str = Field(..., min_length=2, max_length=64, description="登录账号")
    password: str = Field(..., min_length=1, max_length=128, description="登录密码")


class TokenOut(BaseSchema):
    """
    登录成功返回的令牌信息

    ⚠ **为什么每个字段都要写 description**：
    这是调用方接触的第一个也是最关键的响应 —— 而它恰恰最容易写成"裸字段"。
    接口文档由 OpenAPI 自动生成，没写 description 的字段在文档里就是一行空表格，
    对接的人只能去翻源码。越是"人人都知道"的字段越要写说明，因为文档是给不认识代码的人看的。
    """

    access_token: str = Field(description="JWT 访问令牌；后续请求以 `Authorization: Bearer <此值>` 携带")
    token_type: str = Field(default="bearer", description="令牌类型，固定为 bearer")
    expires_in: int = Field(description="有效期（秒）；到期后接口返回 401，前端需重新登录")
    role: str = Field(description="当前用户角色：admin 管理员 / viewer 运维查看员")
    username: str = Field(description="登录账号")
    real_name: Optional[str] = Field(default=None, description="显示名，未设置时为 None")
    must_change_pwd: bool = Field(
        default=False,
        description="是否需强制修改密码；为 true 时前端应跳转到修改密码页而不是首页"
        "（管理员重置密码后的新账号会带此标记）",
    )


class UserOut(BaseSchema):
    """用户信息输出（**永不包含 password_hash**）"""

    id: int = Field(description="用户 ID")
    username: str = Field(description="登录账号，2-64 位，创建后不可修改")
    real_name: Optional[str] = Field(default=None, description="显示姓名")
    role: str = Field(description="角色：admin 管理员 / viewer 运维查看员")
    email: Optional[str] = Field(default=None, description="邮箱；用于接收邮件告警")
    phone: Optional[str] = Field(default=None, description="手机号")
    status: bool = Field(default=True, description="是否启用；false 后该账号立即无法登录（令牌校验每次都查库）")
    must_change_pwd: bool = Field(default=False, description="是否需强制改密")
    last_login_at: OptionalDateTimeStr = Field(default=None, description="最近登录时间")
    last_login_ip: Optional[str] = Field(default=None, description="最近登录 IP")
    remark: Optional[str] = Field(default=None, description="备注")
    created_at: OptionalDateTimeStr = Field(default=None, description="创建时间")

    @computed_field(description="角色中文名")
    @property
    def role_text(self) -> str:
        """
        使用 computed_field 而非普通 property：
        普通 property 不会出现在 Pydantic v2 的序列化结果里，
        前端仍拿不到中文角色名，必须加 computed_field 才会输出。
        """
        return {"admin": "管理员", "viewer": "运维查看员"}.get(self.role, self.role)


class UserCreate(BaseSchema):
    """新增用户"""

    username: str = Field(..., min_length=2, max_length=64, description="登录账号，2-64 位，创建后不可修改")
    password: str = Field(..., min_length=6, max_length=128, description="初始密码，至少 6 位；建议配合 must_change_pwd 强制首次登录改密")
    real_name: Optional[str] = Field(default=None, max_length=64, description="显示姓名")
    role: str = Field(default="viewer", description="角色：admin 管理员 / viewer 运维查看员")
    email: Optional[str] = Field(default=None, max_length=128, description="邮箱；用于接收邮件告警")
    phone: Optional[str] = Field(default=None, max_length=32, description="手机号")
    status: bool = Field(default=True, description="是否启用；false 后该账号立即无法登录（令牌校验每次都查库）")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: str) -> str:
        """角色白名单校验：避免前端传错值导致权限系统失效"""
        if value not in VALID_ROLES:
            raise ValueError(f"角色只能是 {'/'.join(sorted(VALID_ROLES))}")
        return value


class UserUpdate(BaseSchema):
    """编辑用户（字段全部可选，只更新传入项）"""

    real_name: Optional[str] = Field(default=None, max_length=64, description="显示姓名")
    role: Optional[str] = Field(default=None, description="角色：admin 管理员 / viewer 运维查看员；降级后立即生效")
    email: Optional[str] = Field(default=None, max_length=128, description="邮箱")
    phone: Optional[str] = Field(default=None, max_length=32, description="手机号")
    status: Optional[bool] = Field(default=None, description="是否启用；false 立即禁止登录")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")

    @field_validator("role")
    @classmethod
    def _check_role(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in VALID_ROLES:
            raise ValueError(f"角色只能是 {'/'.join(sorted(VALID_ROLES))}")
        return value


class PasswordReset(BaseSchema):
    """管理员重置他人密码"""

    new_password: str = Field(..., min_length=6, max_length=128, description="新密码，至少 6 位")
    must_change_pwd: bool = Field(default=True, description="是否要求对方下次登录改密")


class ChangePassword(BaseSchema):
    """用户自行修改密码（须验证旧密码）"""

    old_password: str = Field(..., min_length=1, description="当前密码；不知道密码改不了账号，防止令牌被盗后直接接管")
    new_password: str = Field(..., min_length=6, max_length=128, description="新密码，至少 6 位")
