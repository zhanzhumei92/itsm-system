"""
业务异常与全局异常处理
===============================================================================
设计说明：

1. 业务层用异常表达"可预期的失败"（资产编号重复、设备不存在…），
   路由层不再写 if/else 判断错误码，代码更干净。

2. **状态码划分原则**（与 app/core/response.py 的业务码定义配套，务必一致）：
   ┌─ 协议层 / 权限层错误 → HTTP 4xx ─────────────────────────────────────┐
   │  AuthException       401  未登录、令牌失效（前端据此跳登录页）          │
   │  PermissionException 403  已登录但权限不足                            │
   │  NotFoundException   404  资源不存在（REST 语义）                     │
   │  FileInvalidException 400 上传文件格式/内容错（用户要改的是文件本身）   │
   │  BizException        400  参数格式或必填项不合法                      │
   └──────────────────────────────────────────────────────────────────┘
   ┌─ 业务规则不通过 → HTTP 200 + 1xxx 业务码 ───────────────────────────┐
   │  DuplicateException      1001 数据重复                               │
   │  OperationDeniedException 1004 操作被规则拒绝（内置数据保护、被占用）  │
   │  StateInvalidException   1005 当前状态不允许该操作                    │
   └──────────────────────────────────────────────────────────────────┘
   为什么要这么分：请求"被正确处理了"与"业务不允许"是两件事。
   统一成 4xx 会迫使前端 axios 在 catch 分支里再解析 body.code，
   逻辑与 200 分支重复；只有 401/403 保留真实状态码供拦截器统一处理。

3. 全局异常处理器保证**任何异常都不会把堆栈信息暴露给前端**：
   - RequestValidationError：Pydantic 校验失败 → 400，并给出中文字段提示
   - Exception：未预期异常 → 500，完整堆栈写日志，前端只看到"服务器内部错误"

4. 所有异常都记入 system.log，配合 sys_log 表形成可追溯链路。
===============================================================================
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logger import system_logger
from app.core.response import (
    CODE_DENIED,
    CODE_DUPLICATE,
    CODE_FILE_INVALID,
    CODE_PARAM_ERROR,
    CODE_SERVER_ERROR,
    CODE_STATE_INVALID,
    resp_fail,
)


class BizException(Exception):
    """
    业务异常基类。

    :param msg: 面向用户的中文提示（会直接显示在前端弹窗里）
    :param code: 业务码，默认 400
    :param http_status: HTTP 状态码，默认与 code 一致（code 不在 400~599 时取 400）
    :param data: 附加数据（如冲突的记录 ID），可为空
    """

    def __init__(
        self,
        msg: str,
        code: int = CODE_PARAM_ERROR,
        http_status: Optional[int] = None,
        data: Any = None,
    ) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code
        self.data = data
        if http_status is not None:
            self.http_status = http_status
        elif 400 <= code <= 599:
            self.http_status = code
        else:
            self.http_status = 400


class AuthException(BizException):
    """未登录 / 令牌无效（401）"""

    def __init__(self, msg: str = "登录状态已失效，请重新登录") -> None:
        super().__init__(msg, code=401, http_status=401)


class PermissionException(BizException):
    """权限不足（403）"""

    def __init__(self, msg: str = "当前账号无此操作权限") -> None:
        super().__init__(msg, code=403, http_status=403)


class NotFoundException(BizException):
    """资源不存在（404）"""

    def __init__(self, msg: str = "请求的数据不存在") -> None:
        super().__init__(msg, code=404, http_status=404)


class DuplicateException(BizException):
    """
    数据唯一约束冲突（资产编号、设备 IP、登录账号重复）。

    刻意返回 HTTP 200 + 业务码 1001：请求本身被正确处理了，
    只是业务规则（唯一性）不允许执行。前端无需区分 400/1001 两套逻辑。
    """

    def __init__(self, msg: str = "数据已存在", code: int = CODE_DUPLICATE) -> None:
        super().__init__(msg, code=code, http_status=200)


class OperationDeniedException(BizException):
    """
    操作被业务规则拒绝（HTTP 200 + 1004）。

    典型场景：
      - 内置 admin 账号不允许改角色/禁用/删除（防把管理员锁在系统外）
      - 内置指标字典不允许删除（采集与解析代码依赖其存在）
      - 设备已被某资产绑定，不能再绑给另一条资产
    这类"拒绝"是产品有意设计，不是用户输入错误，因此不报 400。
    """

    def __init__(self, msg: str = "当前操作不被允许") -> None:
        super().__init__(msg, code=CODE_DENIED, http_status=200)


class StateInvalidException(BizException):
    """
    当前状态不允许该操作（HTTP 200 + 1005）。

    典型场景：对一条已恢复/已关闭的告警再次执行"确认"，或对离线设备执行需在线的操作。
    """

    def __init__(self, msg: str = "当前状态不支持该操作") -> None:
        super().__init__(msg, code=CODE_STATE_INVALID, http_status=200)


class FileInvalidException(BizException):
    """
    上传文件不合法（HTTP 400 + 1006）。

    与上面三个不同：文件格式/必填列错误确实属于"请求内容有问题"，
    保留 HTTP 400 让用户明确知道要改的是文件本身。
    """

    def __init__(self, msg: str = "上传文件不合法") -> None:
        super().__init__(msg, code=CODE_FILE_INVALID, http_status=400)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（在 main.py 中调用一次）"""

    @app.exception_handler(BizException)
    async def _biz_handler(request: Request, exc: BizException) -> JSONResponse:
        """业务异常：可预期，用 INFO 级别记录，避免日志被正常业务报警刷屏"""
        system_logger.info(
            "业务异常 | %s %s | code=%s | %s",
            request.method, request.url.path, exc.code, exc.msg,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=resp_fail(exc.msg, exc.code, exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """参数校验失败：把 Pydantic 的错误列表翻译成一句中文提示"""
        details = exc.errors()
        if details:
            first = details[0]
            # loc 形如 ("body", "ip")，取最后一段作为字段名
            loc = [str(x) for x in first.get("loc", []) if x not in ("body", "query", "path")]
            field = ".".join(loc) if loc else "参数"
            msg = f"参数校验失败：{field} {first.get('msg', '格式不正确')}"
        else:
            msg = "参数校验失败"
        system_logger.warning("参数校验失败 | %s %s | %s", request.method, request.url.path, details)
        return JSONResponse(
            status_code=400,
            content=resp_fail(msg, CODE_PARAM_ERROR, {"details": str(details)[:800]}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Starlette/FastAPI 内置 HTTP 异常（404 路由不存在、自定义 403 等）"""
        return JSONResponse(
            status_code=exc.status_code,
            content=resp_fail(str(exc.detail), exc.status_code),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        兜底异常：记录完整堆栈到日志，前端只看到通用提示。
        为什么必须兜底：采集/告警涉及大量第三方库（netmiko、pysnmp、requests），
        未捕获异常会直接 500 并暴露内部实现细节，存在信息泄露风险。
        """
        system_logger.exception(
            "未处理异常 | %s %s | %s", request.method, request.url.path, exc
        )
        return JSONResponse(
            status_code=500,
            content=resp_fail("服务器内部错误，请联系管理员查看系统日志", CODE_SERVER_ERROR),
        )
