"""
全局配置模块
===============================================================================
设计说明：
1. 所有可变参数集中在 .env 中，代码里绝不硬编码（对应需求"禁止硬编码"原则）。
2. 使用 pydantic-settings，具备类型校验：写错类型时启动即报错，而不是运行期才炸。
3. 部分运行期可调参数（采集线程数、采集周期）同时存在于数据库 sys_config 表，
   优先级为：数据库配置 > .env > 代码默认值。因为现场运维需要"不改文件就调参"。
   —— 数据库读取逻辑见 app/services/config_service.py 的 get_int/get_bool。
===============================================================================
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：.../Itsm_System（本文件位于 app/core/config.py，故上溯两级）
BASE_DIR: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """平台全局配置对象（单例，通过 get_settings() 获取）"""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",          # 忽略 .env 中多余的键，避免因历史遗留配置导致启动失败
        case_sensitive=False,    # 环境变量大小写不敏感，Windows 下行为一致
    )

    # ---------------- 应用基础 ----------------
    APP_NAME: str = "亿网通IT运维监控管理平台"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEMO_MODE: bool = False      # 开启后自动灌入演示设备数据，便于大屏演示

    # ---------------- 数据库 ----------------
    # 迁移 MySQL 只需改这一行，业务代码零改动（所有访问均走 SQLAlchemy ORM）
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'data' / 'itsm.db'}"

    # ---------------- 安全 ----------------
    SECRET_KEY: str = "change-me-in-production"
    FERNET_KEY: str = ""         # 空值时说明未配置，加密功能会明确报错而非静默明文落库
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720   # 默认 12 小时，运维值守一个班次免重复登录
    ALGORITHM: str = "HS256"

    # ---------------- 日志 ----------------
    LOG_DIR: str = "./logs"
    LOG_LEVEL: str = "INFO"
    LOG_KEEP_DAYS: int = 30      # 按天切割，保留 30 天

    # ---------------- 调度与采集 ----------------
    SCHEDULER_ENABLED: bool = True
    COLLECT_THREAD_POOL_SIZE: int = 60   # 200 台设备 / 60 并发 ≈ 4 批完成轮询
    COLLECT_DEFAULT_INTERVAL: int = 60
    COLLECT_DEFAULT_TIMEOUT: int = 10

    # ---------------- 跨域 ----------------
    CORS_ORIGINS: str = "*"

    # ---------------- 派生属性 ----------------
    @property
    def is_sqlite(self) -> bool:
        """是否使用 SQLite。用于决定是否需要开启 WAL 等 SQLite 专属优化。"""
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def log_dir(self) -> Path:
        """日志目录绝对路径，不存在时自动创建（避免首次部署因目录缺失启动失败）"""
        p = Path(self.LOG_DIR)
        if not p.is_absolute():
            p = BASE_DIR / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        """数据目录（SQLite 文件、导出文件存放处）"""
        p = BASE_DIR / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def web_dir(self) -> Path:
        """前端静态资源根目录"""
        return BASE_DIR / "web"

    @property
    def cors_origin_list(self) -> List[str]:
        """CORS 白名单列表；配置为 * 时允许全部（内网部署默认宽松）"""
        raw = (self.CORS_ORIGINS or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取配置单例。
    使用 lru_cache 而非模块级全局变量，好处是：
    单元测试中可通过 get_settings.cache_clear() 重置配置，便于注入测试环境变量。
    """
    return Settings()


# 便捷引用：绝大多数模块直接 `from app.core.config import settings`
settings = get_settings()
