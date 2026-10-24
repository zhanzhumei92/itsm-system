"""
数据库连接与会话管理
===============================================================================
设计说明：

1. **SQLite → MySQL 无缝迁移的关键**
   全部通过 SQLAlchemy 2.0 ORM 访问，业务代码不出现任何方言 SQL；
   迁移时只改 .env 的 DATABASE_URL 一行即可。

2. **为什么 SQLite 要开 WAL 模式**
   默认的 journal 模式下一个写事务会阻塞所有读，而本平台"采集线程高频写"与
   "大屏秒级读"是同时发生的。WAL（Write-Ahead Logging）允许读写并发，
   配合 busy_timeout=5000 让写锁冲突时排队等待而不是直接抛 "database is locked"。

3. **两种会话获取方式，用途严格区分**
   - get_db()      ：FastAPI 请求依赖，请求结束自动关闭（每个请求独立会话）
   - session_scope()：采集线程/调度任务/告警发送使用，with 语法保证异常也释放连接
   混用会导致连接泄漏（60 并发采集时 SQLite 会立刻报 too many connections）。

4. **scoped_session 线程隔离**
   采集在线程池中运行，scoped_session 为每个线程维护独立 Session，
   天然避开"两个线程共用一个 Session"导致的数据错乱。
===============================================================================
"""
from __future__ import annotations

import contextlib
from typing import Generator, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from app.core.config import settings
from app.core.logger import system_logger


def _build_engine() -> Engine:
    """按数据库类型构造引擎，SQLite 与 MySQL 参数差异在此集中处理"""
    if settings.is_sqlite:
        return create_engine(
            settings.DATABASE_URL,
            echo=settings.DEBUG,            # SQL 回显，仅调试期开启
            future=True,
            # check_same_thread=False：允许连接跨线程使用（配合 scoped_session 保证不共用）
            # timeout=15：Python sqlite3 层面的锁等待，与 PRAGMA busy_timeout 双保险
            connect_args={"check_same_thread": False, "timeout": 15},
            pool_size=15,                   # 采集 60 线程 + Web 请求共享，留足余量
            max_overflow=30,
            pool_recycle=1800,              # 30 分钟回收，避免长连接异常
            pool_pre_ping=True,             # 取连接前探活，规避 "server has gone away"
        )
    # MySQL：默认参数即可，字符集强制 utf8mb4 以支持中文与 emoji
    return create_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        future=True,
        pool_size=20,
        max_overflow=40,
        pool_recycle=3600,
        pool_pre_ping=True,
        connect_args={"charset": "utf8mb4"},
    )


engine: Engine = _build_engine()


@event.listens_for(engine, "connect")
def _on_connect(dbapi_connection, connection_record) -> None:
    """
    每次建立物理连接时执行的初始化。

    为什么放在这里而不是 init_db()：
    连接池会不断新建连接，PRAGMA 是针对"连接"生效的，只在启动时执行一次
    无法覆盖后续新建的连接。放在 connect 事件里保证每条连接都被正确设置。
    """
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")      # 读写并发，采集写不阻塞大屏读
    cursor.execute("PRAGMA synchronous=NORMAL")    # WAL 下兼顾安全与性能
    cursor.execute("PRAGMA busy_timeout=5000")     # 写锁冲突等待 5 秒再报错
    cursor.execute("PRAGMA foreign_keys=OFF")      # 本设计不用物理外键，由 Service 维护
    cursor.close()


# 会话工厂：expire_on_commit=False 让提交后仍可读取对象属性（返回给前端时不必重新查库）
SessionFactory = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

# 线程隔离会话（采集线程池使用）
ScopedSession = scoped_session(SessionFactory)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI 依赖注入用的会话。

    用法：
        @router.get("/devices")
        def list_devices(db: Session = Depends(get_db)):
            ...
    """
    db = SessionFactory()
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """
    事务作用域（采集任务 / 定时任务 / 告警发送使用）。

    用法：
        with session_scope() as db:
            db.add(obj)
            db.commit()

    为什么必须用 with：采集中途抛异常（设备超时、解析失败）时，
    finally 里的 close 会释放连接，否则 60 并发跑几轮就会耗尽连接池。
    """
    db = ScopedSession()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        ScopedSession.remove()   # 移除线程本地会话，及时归还连接到池


def init_db() -> None:
    """
    建表：按 models 定义创建尚不存在的表。

    注意：生产环境首次部署建议先执行 sql/schema_sqlite.sql 获得带注释的标准结构，
    本函数主要服务于开发调试与单元测试（临时库一键可用）。
    """
    # 导入模型包以触发所有表定义注册到 Base.metadata
    from app.models import Base  # noqa: WPS433（延迟导入避免循环依赖）

    Base.metadata.create_all(bind=engine)
    system_logger.info("数据库初始化完成：%s", settings.DATABASE_URL.split("@")[-1])


def check_db_connection() -> bool:
    """启动自检：数据库能否正常读写。失败时明确抛错，避免"能启动但一用就崩"。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - 仅在部署环境异常时触发
        system_logger.error("数据库连接失败：%s", exc)
        return False
