"""
ORM 基类与公共定义
===============================================================================
设计说明：

1. **自增主键必须做类型变体（本项目最容易被忽略的坑）**
   SQLite 中只有 `INTEGER PRIMARY KEY` 才是 rowid 别名并支持自增；
   若声明为 `BIGINT PRIMARY KEY`，SQLite 不会自增，插入时会得到 NULL 主键。
   而 MySQL 又需要 BIGINT 承载海量指标数据。
   解法：`BigInteger().with_variant(Integer, "sqlite")` ——
   对 MySQL 生成 BIGINT、对 SQLite 生成 INTEGER，两端都正确。

2. **不定义 relationship() 关联**
   采集与告警发生在多线程中，ORM 的懒加载（lazy load）一旦跨 Session
   就会抛 DetachedInstanceError。本项目关联一律在 Service 层显式查询，
   行为可预测，也便于控制 SQL 条数（避免 N+1）。

3. **时间统一使用本地时间（naive datetime）**
   内网运维平台面向"现场值班"，本地时间更直观；
   集中通过 now() 获取，将来若需改 UTC 只需改一处。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    """统一时间源：本地当前时间"""
    return datetime.now()


def pk_type():
    """
    跨库通用的自增主键类型。

    返回新实例而非复用同一对象，避免多个列共享同一 TypeEngine 实例时
    在部分 SQLAlchemy 版本上出现类型缓存串扰问题。
    """
    return BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类"""
    pass


class TimestampMixin:
    """创建/更新时间混入（绝大多数业务表都需要）"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now, nullable=True, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now, onupdate=now, nullable=True, comment="更新时间"
    )
