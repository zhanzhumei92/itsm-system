"""
亿网通IT运维监控管理平台 —— 应用入口
===============================================================================
启动流程（lifespan 生命周期）：

  启动阶段
    1. 打印启动横幅（版本、数据库类型、监听地址）
    2. init_db()        建表（首次部署自动创建 16 张表）
    3. DEMO_MODE 时灌入演示数据（便于演示大屏效果，生产环境请关闭）
    4. 启动采集线程池    get_executor().start()
    5. 启动调度器        start_scheduler()（受 SCHEDULER_ENABLED 控制）
  关闭阶段（顺序与启动相反）
    1. 停止调度器（不再产生新任务）
    2. 等待线程池中正在执行的采集任务结束（让指标写完再退出，避免丢数据）

一键启动：
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  或直接运行本文件：
    python app/main.py

接口文档：
    Swagger UI  → http://127.0.0.1:8000/docs
    ReDoc       → http://127.0.0.1:8000/redoc
===============================================================================
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 允许以 `python app/main.py` 方式直接运行时也能找到 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.router import api_router                       # noqa: E402
from app.core.config import settings                        # noqa: E402
from app.core.database import check_db_connection, init_db   # noqa: E402
from app.core.exceptions import register_exception_handlers  # noqa: E402
from app.core.logger import silence_noisy_loggers, system_logger  # noqa: E402


def _print_banner() -> None:
    """控制台启动横幅：一眼确认版本、数据库、监听地址是否正确"""
    banner = f"""
+==============================================================================+
|  {settings.APP_NAME}  v{settings.APP_VERSION}
+==============================================================================+
|  数据库      : {'SQLite' if settings.is_sqlite else 'MySQL'}
|  监听地址    : http://{settings.HOST}:{settings.PORT}
|  接口文档    : http://127.0.0.1:{settings.PORT}/docs
|  大屏地址    : http://127.0.0.1:{settings.PORT}/
|  采集调度    : {'已启用' if settings.SCHEDULER_ENABLED else '已关闭'}
|  线程池并发  : {settings.COLLECT_THREAD_POOL_SIZE}
|  演示模式    : {'开启' if settings.DEMO_MODE else '关闭'}
+==============================================================================+
"""
    print(banner)
    system_logger.info("平台启动中：%s v%s", settings.APP_NAME, settings.APP_VERSION)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动与关闭的编排（详见模块 docstring）"""
    # ---------------- 启动 ----------------
    _print_banner()
    silence_noisy_loggers()

    if not check_db_connection():
        system_logger.error("数据库连接失败，请检查 .env 中的 DATABASE_URL 配置")
        raise RuntimeError("数据库连接失败，平台无法启动")

    init_db()

    # 演示数据：仅在 DEMO_MODE=true 时执行，生产环境保持关闭
    if settings.DEMO_MODE:
        try:
            from scripts.seed_demo import seed_demo_data

            seed_demo_data()
            system_logger.info("演示数据已就绪")
        except Exception as exc:
            system_logger.warning("演示数据生成失败（不影响平台运行）：%s", exc)

    # 采集线程池先行启动，调度器的任务才有地方投递
    from app.tasks.executor import get_executor

    get_executor().start()

    from app.scheduler.scheduler import start_scheduler

    start_scheduler()

    system_logger.info("平台启动完成，访问 http://127.0.0.1:%s/ 进入大屏", settings.PORT)

    yield

    # ---------------- 关闭 ----------------
    system_logger.info("平台正在关闭...")
    from app.scheduler.scheduler import stop_scheduler
    from app.tasks.executor import get_executor as _get_executor

    stop_scheduler()
    # wait=True：等待正在执行的采集任务结束，避免"刚采到的数据没写库就退出"
    _get_executor().shutdown(wait=True)
    system_logger.info("平台已安全退出")


def create_app() -> FastAPI:
    """创建 FastAPI 应用（工厂函数，便于单元测试构造独立实例）"""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "面向政企弱电/系统集成场景的统一监控平台：\n"
            "- 多协议采集：SNMP / SSH / WinRM / ICMP\n"
            "- IT 资产台账、可视化大屏、多渠道告警（邮件/企微/钉钉/微信）"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ---------------- 跨域 ----------------
    # 内网单机部署时前后端同源，本配置无实际作用；
    # 大屏单独部署到另一台机器/端口时才需要，故默认允许全部、可按需收紧。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------- 全局异常处理 ----------------
    register_exception_handlers(app)

    # ---------------- 业务接口 ----------------
    app.include_router(api_router)

    # ---------------- 前端静态资源 ----------------
    _mount_web(app)

    return app


def _mount_web(app: FastAPI) -> None:
    """
    挂载前端静态资源与页面路由。

    设计：前端与后端同源部署（FastAPI 直接托管 Vue/ECharts 静态文件），
    好处是现场只需要开一个端口、一套启动脚本，不需要额外的 Nginx。
    """
    web_dir = settings.web_dir
    static_dir = web_dir / "static"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        system_logger.info("静态资源目录已挂载：%s", static_dir)
    else:
        system_logger.warning("静态资源目录不存在，前端页面将无法访问：%s", static_dir)

    def _page(filename: str):
        """返回页面文件；不存在时给出明确提示而不是 404 空白页"""
        target = web_dir / filename
        if target.exists():
            return FileResponse(str(target))
        return JSONResponse(
            status_code=200,
            content={
                "code": 404,
                "msg": f"页面 {filename} 尚未部署，请确认 web 目录下是否存在该文件",
                "data": None,
            },
        )

    @app.get("/", include_in_schema=False, summary="可视化大屏")
    def dashboard_page():
        """根路径 → 可视化大屏"""
        return _page("dashboard.html")

    @app.get("/admin", include_in_schema=False, summary="后台管理")
    def admin_page():
        """后台管理页面（设备/资产/告警/配置）"""
        return _page("admin.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """浏览器默认请求图标，直接返回空响应避免控制台出现 404 噪音"""
        return JSONResponse(content={})


# 模块级应用实例（uvicorn 的入口：app.main:app）
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
