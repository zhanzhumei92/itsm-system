"""
采集器工厂
===============================================================================
职责：按设备协议返回对应的采集器实例。

设计说明：
1. **采集器实例做进程内单例**
   采集器本身无状态（每次采集的上下文都在参数与返回值里），
   60 并发下每轮都 new 一个对象纯属浪费；复用实例还能保留内部的
   连接池/缓存（如 SSH 采集器的连接复用）等优化空间。

2. **工厂返回 None 而不是抛异常**
   协议未实现是"可预期的业务状态"（现场新增了设备类型但采集器还没写），
   调用方据此返回"暂不支持该协议"的友好提示，
   而不是让 500 错误打断用户操作。
===============================================================================
"""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from app.collectors.base import BaseCollector, CollectorRegistry

# 协议 → 实例缓存（进程级单例）
_instances: Dict[str, BaseCollector] = {}


class CollectorFactory:
    """采集器工厂（对外唯一入口）"""

    @staticmethod
    def get(protocol: str) -> Optional[BaseCollector]:
        """
        获取采集器实例。

        :param protocol: snmp / ssh / winrm / icmp
        :return: 采集器实例；协议未注册时返回 None
        """
        key = (protocol or "").lower().strip()
        if not key:
            return None

        if key in _instances:
            return _instances[key]

        collector_cls: Optional[Type[BaseCollector]] = CollectorRegistry.get_class(key)
        if collector_cls is None:
            return None

        instance = collector_cls()
        _instances[key] = instance
        return instance

    @staticmethod
    def available_protocols() -> List[str]:
        """已支持采集的协议列表"""
        return CollectorRegistry.available_protocols()

    @staticmethod
    def is_supported(protocol: str) -> bool:
        """协议是否已支持（创建设备时提前校验，避免建了采集不了的设备）"""
        return CollectorRegistry.get_class(protocol) is not None

    @staticmethod
    def reset() -> None:
        """重置实例缓存（单元测试或热加载时使用）"""
        _instances.clear()
