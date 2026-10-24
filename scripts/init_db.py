"""
数据库初始化脚本
===============================================================================
用途：一键完成"建表 + 灌初始化数据"，是部署流程的第一步。

用法：
    python scripts/init_db.py              # 建表 + 初始化数据（已存在的表不动）
    python scripts/init_db.py --reset      # 先删库重建（**会丢数据**，仅限开发环境）
    python scripts/init_db.py --no-data    # 只建表，不灌初始化数据
    python scripts/init_db.py --orm        # 用 ORM 建表（不使用 sql/*.sql，适合结构调试）

为什么优先执行 sql/*.sql 而不是 ORM create_all：
    SQL 脚本带有完整的中文列注释、索引名规范与 WAL 设置，
    直接执行可得到与设计文档完全一致的库结构，便于与客户/审计对照。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import BASE_DIR, settings  # noqa: E402
from app.core.database import engine, init_db  # noqa: E402
from app.core.logger import system_logger  # noqa: E402

SQL_DIR = BASE_DIR / "sql"


def _sqlite_path() -> Path:
    """从 DATABASE_URL 解析出 SQLite 文件路径"""
    raw = settings.DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
    path = Path(raw)
    return path if path.is_absolute() else (BASE_DIR / path)


def _read_sql(filename: str) -> str:
    """读取 SQL 文件（缺失时给出明确报错）"""
    target = SQL_DIR / filename
    if not target.exists():
        raise FileNotFoundError(f"SQL 文件不存在：{target}")
    return target.read_text(encoding="utf-8")


def _split_statements(script: str) -> list:
    """
    按分号拆分 SQL 语句（用于 MySQL）。

    处理规则：跳过以 -- 开头的整行注释与空行。
    注意：本项目 SQL 中不含存储过程/触发器，故简单拆分是安全的。
    """
    statements = []
    buffer = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buffer).rstrip(";").strip())
            buffer = []
    if buffer:
        statements.append("\n".join(buffer).strip())
    return [s for s in statements if s]


def init_sqlite(reset: bool = False, with_data: bool = True) -> None:
    """SQLite 初始化：用标准库 sqlite3 的 executescript 执行，最可靠"""
    db_path = _sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset:
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(db_path) + suffix)
            if target.exists():
                target.unlink()
                print(f"已删除旧文件：{target}")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_read_sql("schema_sqlite.sql"))
        print(f"[1/2] 建表完成：{db_path}")
        if with_data:
            conn.executescript(_read_sql("init_data.sql"))
            print("[2/2] 初始化数据完成（账号 / 指标字典 / 系统配置 / 默认告警规则）")
        conn.commit()
        # 验证
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        table_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM sys_user")
        user_count = cur.fetchone()[0]
        print(f"      数据表 {table_count} 张，用户 {user_count} 个")
    finally:
        conn.close()


def init_mysql(reset: bool = False, with_data: bool = True) -> None:
    """
    MySQL 初始化。

    注意：--reset 在 MySQL 下会 DROP 掉全部相关表，风险极高，
    故仅在建表语句为 IF NOT EXISTS 的语义下执行，并明确提示。
    """
    if reset:
        print("警告：MySQL 模式下 --reset 会删除已有的 16 张业务表！")
        confirm = input("确认继续？输入 YES 继续：").strip()
        if confirm != "YES":
            print("已取消")
            return
        tables = [
            "sys_user", "sys_config", "sys_log", "mon_device", "mon_device_credential",
            "asset_info", "asset_maintain_remind", "metric_catalog", "metric_latest",
            "metric_sample", "alert_rule", "alert_rule_state", "alert_event",
            "alert_notify_record", "collect_log", "alarm_log",
        ]
        with engine.begin() as conn:
            for table in tables:
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
        print("已删除旧表")

    with engine.begin() as conn:
        for stmt in _split_statements(_read_sql("schema_mysql.sql")):
            conn.exec_driver_sql(stmt)
    print("[1/2] MySQL 建表完成")

    if with_data:
        with engine.begin() as conn:
            for stmt in _split_statements(_read_sql("init_data.sql")):
                conn.exec_driver_sql(stmt)
        print("[2/2] 初始化数据完成")


def main() -> None:
    parser = argparse.ArgumentParser(description="亿网通IT运维监控管理平台 - 数据库初始化")
    parser.add_argument("--reset", action="store_true", help="删除旧库后重建（会丢数据！）")
    parser.add_argument("--no-data", action="store_true", help="只建表，不灌初始化数据")
    parser.add_argument("--orm", action="store_true", help="改用 ORM 建表（不使用 sql 脚本）")
    parser.add_argument("--demo", action="store_true", help="额外灌入演示数据（10 台虚拟设备）")
    args = parser.parse_args()

    print(f"目标数据库：{settings.DATABASE_URL}")

    if args.orm:
        init_db()
        print("ORM 建表完成（不含中文列注释与索引命名规范）")
    elif settings.is_sqlite:
        init_sqlite(reset=args.reset, with_data=not args.no_data)
    else:
        init_mysql(reset=args.reset, with_data=not args.no_data)

    if args.demo:
        from scripts.seed_demo import seed_demo_data

        result = seed_demo_data(force=args.reset)
        print(f"演示数据：{result}")

    print("\n初始化完成。默认账号 admin / admin123 ，只读账号 viewer / viewer123")
    system_logger.info("数据库初始化完成")


if __name__ == "__main__":
    main()
