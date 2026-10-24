"""
统一响应封装
===============================================================================
设计说明：

1. 所有接口返回统一结构，前端只需写一次拦截器：
       {"code": 0, "msg": "成功", "data": {...}}

2. HTTP 状态码与业务 code 都用起来，职责不同：
   - HTTP 状态码：表达"请求本身是否被正确处理"（401 未登录、403 无权限、400 参数错）
   - code 业务码：表达"业务规则是否通过"（如 1001 资产编号重复）
   前端 axios 拦截器对 401 统一跳登录，对业务码统一弹提示。

3. 分页结构固定为 total / page / page_size / items / pages，
   前端表格组件可复用，不必每页各写一套字段名。
===============================================================================
"""
from __future__ import annotations

import math
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

# 业务错误码（HTTP 状态码之外的扩展约定）
# ---------------------------------------------------------------------------
# 划分原则（务必遵守，否则前端要写两套判断逻辑）：
#   【协议层 / 权限层错误】→ 用 HTTP 4xx，表示"请求本身没有被正确处理"
#       400 参数格式或必填项不合法
#       401 未登录或令牌失效（前端拦截器据此跳登录页）
#       403 已登录但权限不足
#       404 路由不存在
#   【业务规则不通过】→ 统一 HTTP 200 + 下方 1xxx 业务码，
#       表示"请求被正确处理了，但业务规则不允许执行"，前端只需弹 body.msg
# ---------------------------------------------------------------------------
CODE_SUCCESS = 0
CODE_PARAM_ERROR = 400
CODE_UNAUTHORIZED = 401
CODE_FORBIDDEN = 403
CODE_NOT_FOUND = 404
CODE_SERVER_ERROR = 500
CODE_DUPLICATE = 1001        # 数据重复（资产编号、设备 IP、账号已存在）
CODE_DEVICE_BUSY = 1002      # 设备正在采集中，操作被拒
CODE_CONFIG_MISSING = 1003   # 关键配置缺失（如未配置企微 Webhook）
CODE_DENIED = 1004           # 操作被业务规则拒绝（内置数据保护、被依赖占用）
CODE_STATE_INVALID = 1005    # 当前状态不允许该操作（如告警已恢复不能再确认）
CODE_FILE_INVALID = 1006     # 上传文件格式或内容不合法（Excel 导入）


class Resp(BaseModel, Generic[T]):
    """
    统一响应模型。

    用法（在路由上声明，OpenAPI 文档自动展示返回结构）：
        @router.get("/{device_id}", response_model=Resp[DeviceOut])
        def get_device(...):
            return resp_ok(device_out)
    """
    code: int = Field(default=CODE_SUCCESS, description="业务码，0 表示成功")
    msg: str = Field(default="成功", description="提示信息")
    data: Optional[T] = Field(default=None, description="业务数据")


class PageData(BaseModel, Generic[T]):
    """分页数据体（作为 Resp 的 data 字段）"""
    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1, description="当前页码，从 1 开始")
    page_size: int = Field(default=20, description="每页条数")
    pages: int = Field(default=0, description="总页数")
    items: List[T] = Field(default_factory=list, description="当前页数据")


def resp_ok(data: Any = None, msg: str = "成功") -> dict:
    """成功响应"""
    return {"code": CODE_SUCCESS, "msg": msg, "data": data}


def resp_fail(msg: str, code: int = CODE_PARAM_ERROR, data: Any = None) -> dict:
    """失败响应（一般由异常处理器统一生成，路由内少用）"""
    return {"code": code, "msg": msg, "data": data}


def build_page(items: List[Any], total: int, page: int, page_size: int) -> dict:
    """
    构造分页响应体。

    :param items: 当前页数据列表
    :param total: 满足条件的总记录数
    :param page: 当前页
    :param page_size: 每页条数
    """
    page = max(page, 1)
    page_size = max(page_size, 1)
    pages = math.ceil(total / page_size) if total else 0
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "items": items,
    }
