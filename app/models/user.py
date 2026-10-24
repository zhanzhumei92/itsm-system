"""用户模型（sys_user）—— 对应模块5：用户管理与角色权限"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_type


class SysUser(Base, TimestampMixin):
    """
    系统用户。

    角色（role）：
      - admin  管理员：设备/资产/告警规则/系统配置/用户 全部读写权限
      - viewer 运维查看员：只读 + 告警确认（一线值班人员适用）

    安全：password_hash 存 bcrypt 哈希，**永远不可逆**；
         接口返回时由 Pydantic Schema 决定是否输出该字段（默认不输出）。
    """

    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="登录账号")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="bcrypt 哈希")
    real_name: Mapped[Optional[str]] = mapped_column(String(64), comment="姓名")
    role: Mapped[str] = mapped_column(String(32), default="viewer", nullable=False, comment="admin/viewer")
    email: Mapped[Optional[str]] = mapped_column(String(128), comment="邮箱，可接收告警邮件")
    phone: Mapped[Optional[str]] = mapped_column(String(32), comment="手机号")
    status: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="True启用 False禁用")
    must_change_pwd: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="首次登录强制改密"
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最后登录时间")
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(64), comment="最后登录 IP")
    remark: Mapped[Optional[str]] = mapped_column(String(255), comment="备注")

    # ---------------- 便捷属性 ----------------
    @property
    def is_admin(self) -> bool:
        """是否管理员。用于 Service 层少数需要区分角色的分支逻辑。"""
        return self.role == "admin"

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<SysUser {self.id} {self.username} {self.role}>"
