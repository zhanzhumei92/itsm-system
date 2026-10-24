"""
日志模块
===============================================================================
设计说明（对应非功能需求"系统日志、采集日志、告警日志落地保存，可查询"）：

1. 三类日志物理分离，各自独立文件：
   - system.log  ：接口调用、用户操作、系统异常
   - collect.log ：每台设备每次采集的结果（成功/失败/超时/耗时）
   - alarm.log   ：告警触发、恢复、收敛、通知发送
   分开的理由：采集日志量最大（200 台 x 每分钟），与告警日志混在一起会淹没有效信息。

2. 按天切割（TimedRotatingFileHandler），保留 LOG_KEEP_DAYS 天，避免磁盘被写满。

3. 日志同时写入：
   - 文件：长期留存、事后追溯
   - 数据库：页面可查询（由 Service 层显式写 collect_log / alarm_log / sys_log 表）
   两者职责不同：文件是"全量流水"，数据库是"结构化可检索记录"，不做二选一。

4. 采集线程数量多（60 并发），故 handler 采用线程安全写法，并抑制第三方库噪音日志。
===============================================================================
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Dict

from app.core.config import settings

# 日志格式：时间 | 级别 | 模块 | 进程线程 | 消息
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 三类日志的 (logger 名称, 文件名) 映射
_LOG_DEFINITIONS: Dict[str, str] = {
    "system": "system.log",
    "collect": "collect.log",
    "alarm": "alarm.log",
}

# 缓存已创建的 logger，防止重复添加 handler 导致日志打印多份
_LOGGERS: Dict[str, logging.Logger] = {}


def _build_logger(name: str, filename: str) -> logging.Logger:
    """构建单个文件日志器（控制台 + 按天切割文件）"""
    logger = logging.getLogger(f"itsm.{name}")
    logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    logger.propagate = False          # 阻断向 root logger 冒泡，避免控制台重复输出

    if logger.handlers:               # 已初始化过则直接复用
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # ---- 控制台输出（前台运行时便于观察）----
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # ---- 文件输出（按天切割）----
    file_handler = TimedRotatingFileHandler(
        filename=str(settings.log_dir / filename),
        when="midnight",
        interval=1,
        backupCount=settings.LOG_KEEP_DAYS,
        encoding="utf-8",
        delay=True,                   # 首次写日志时才创建文件，避免空文件堆积
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(category: str = "system") -> logging.Logger:
    """
    获取日志器。

    :param category: system / collect / alarm，未知取值统一落到 system
    :return: logging.Logger

    用法：
        from app.core.logger import get_logger
        log = get_logger("collect")
        log.info("设备 10.0.0.1 SNMP 采集成功，耗时 42ms")
    """
    key = category if category in _LOG_DEFINITIONS else "system"
    if key not in _LOGGERS:
        _LOGGERS[key] = _build_logger(key, _LOG_DEFINITIONS[key])
    return _LOGGERS[key]


# 便捷单例（模块级引用，见名知意）
system_logger = get_logger("system")
collect_logger = get_logger("collect")
alarm_logger = get_logger("alarm")


def silence_noisy_loggers() -> None:
    """
    抑制第三方库的噪音日志。

    为什么需要：APScheduler 每次任务执行都会打 INFO 级日志，
    netmiko / paramiko 在握手阶段会打大量 DEBUG 日志，
    60 并发采集时这些日志会瞬间把磁盘写满，掩盖真正有价值的业务日志。
    """
    for noisy in ("apscheduler", "paramiko", "netmiko", "pysnmp", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
