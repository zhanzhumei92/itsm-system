"""
公共 Schema：基类、时间格式、分页参数
===============================================================================
设计说明：

1. **统一时间输出格式**
   Pydantic 默认把 datetime 序列化为 ISO 8601（2026-09-21T14:00:00）。
   本项目通过 DateTimeStr 类型统一输出为 `2026-09-21 14:00:00`，
   好处：前端 ECharts 时间轴、表格列都不必再做格式转换，减少前端重复代码。

2. **BaseSchema.from_attributes=True**
   使 Schema 能直接从 ORM 对象构造：`DeviceOut.model_validate(device_orm)`，
   省掉手写 dict 转换，也避免字段漏掉。

3. **分页参数用 PageQuery 复用**
   所有列表接口统一 `?page=1&page_size=20&keyword=xxx`，前端表格组件通用。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")

# 统一时间字符串类型：输出 "YYYY-MM-DD HH:MM:SS"
DateTimeStr = Annotated[
    datetime,
    PlainSerializer(
        lambda value: value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else None,
        return_type=str,
        when_used="json",
    ),
]

OptionalDateTimeStr = Optional[DateTimeStr]


class BaseSchema(BaseModel):
    """所有 Schema 的基类：允许从 ORM 对象直接构造"""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,     # 允许用字段名或别名赋值，兼容前端两种写法
        str_strip_whitespace=True, # 自动去除首尾空格，避免"IP 后面多个空格"这类脏数据
    )


class PageQuery(BaseSchema):
    """列表接口通用查询参数"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=500, description="每页条数，最大 500")
    keyword: Optional[str] = Field(default=None, description="模糊搜索关键词")

    @property
    def offset(self) -> int:
        """SQL 偏移量，供 Service 层直接使用"""
        return (max(self.page, 1) - 1) * self.page_size


class IdsRequest(BaseSchema):
    """批量操作请求体（批量删除、批量启停）"""

    ids: List[int] = Field(default_factory=list, description="ID 列表")


class OpResult(BaseSchema):
    """通用操作结果"""

    success: int = Field(default=0, description="成功条数")
    failed: int = Field(default=0, description="失败条数")
    messages: List[str] = Field(default_factory=list, description="失败原因列表")


class PageResult(BaseSchema, Generic[T]):
    """分页返回体（与 core/response.build_page 结构一致）"""

    total: int = 0
    page: int = 1
    page_size: int = 20
    pages: int = 0
    items: List[T] = Field(default_factory=list)
