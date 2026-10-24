"""
采集器抽象基类与归一化数据结构
===============================================================================
这是整个平台扩展性的核心文件。

**新增一类设备要改什么？**
   1. 在 app/collectors/ 下新建模块（如 huawei_cloud.py）
   2. 继承 BaseCollector，实现 collect()
   3. 用 @CollectorRegistry.register("新协议") 装饰
   4. 在 collectors/__init__.py 中 import 一次（触发注册）
   完成。无需修改调度器、Service、API、前端任何代码。

**归一化约定（采集器与上层之间的契约）**
   采集器只负责把各家设备的差异消化掉，统一产出：
       [
         {"metric_key": "cpu_usage",    "metric_value": 37.5,  "unit": "%"},
         {"metric_key": "if_in_rate:Gi0/0/1", "metric_value": 12.3, "unit": "Mbps"},
         {"metric_key": "if_status:Gi0/0/1",  "str_value": "up"},
       ]
   端口类指标用 "基名:端口名"（见 metric_service 的命名约定）。
   采集器**绝不**直接写数据库、绝不直接发告警——那是 Service 与告警引擎的职责。
===============================================================================
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

# 采集结果状态（与 collect_log.status 取值一致）
STATUS_SUCCESS = "success"
STATUS_FAIL = "fail"
STATUS_TIMEOUT = "timeout"


@dataclass
class CollectResult:
    """
    采集结果。

    :param success: 是否成功。**注意：设备离线也算"采集成功"**——
        因为我们成功探测到了"它离线"这个事实，离线由 online_status 表达，
        离线告警由告警引擎处理，不应记成采集失败（否则日志里全是 fail 无法区分
        "网络不通"与"SNMP 团体字配错了"）。
    :param metrics: 归一化指标列表
    :param error: 失败原因（面向运维的中文描述，会写进 collect_log.error_msg）
    :param online_status: 在线状态 online/offline/None（None 表示本次不更新状态）
    :param duration_ms: 采集耗时
    :param raw: 原始返回片段（调试用，仅在失败时保留，避免占用内存）
    """

    success: bool
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    online_status: Optional[str] = None
    duration_ms: int = 0
    raw: Optional[str] = None

    @property
    def status(self) -> str:
        """映射为 collect_log.status：超时单独区分，便于统计设备响应质量"""
        if self.success:
            return STATUS_SUCCESS
        if self.error and ("timeout" in self.error.lower() or "超时" in self.error):
            return STATUS_TIMEOUT
        return STATUS_FAIL

    @property
    def metric_count(self) -> int:
        return len(self.metrics)


class BaseCollector(ABC):
    """
    采集器抽象基类。

    子类必须：
      - 定义类属性 protocol（与 mon_device.protocol 取值一致）
      - 实现 collect()

    子类应当：
      - 在 collect() 内部自行处理超时（用 params["timeout"]）
      - 用 self.fail() 构造失败结果，保持错误信息风格一致
      - 绝不抛出未捕获异常（调度器虽有兜底，但自行处理能给出更准确的错误描述）
    """

    #: 协议标识，如 snmp / ssh / winrm / icmp
    protocol: str = ""

    #: 该采集器产出的主要指标键（用于前端提示"该协议能采到哪些指标"）
    metric_keys: List[str] = []

    @abstractmethod
    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        """
        执行一次采集。

        :param device_id: 设备 ID（用于日志与多端口指标拼接）
        :param params: 采集参数（由 DeviceService.build_collect_params 生成，
                       含解密后的账号口令；本方法**不得**把 params 写入日志）
        :return: CollectResult
        """
        raise NotImplementedError

    # ---------------- 结果构造助手 ----------------

    def fail(self, error: str, raw: Optional[str] = None, duration_ms: int = 0) -> CollectResult:
        """构造失败结果"""
        return CollectResult(success=False, error=error, raw=raw, duration_ms=duration_ms)

    def ok(
        self,
        metrics: List[Dict[str, Any]],
        duration_ms: int = 0,
        online_status: Optional[str] = None,
    ) -> CollectResult:
        """构造成功结果"""
        return CollectResult(
            success=True, metrics=metrics, duration_ms=duration_ms, online_status=online_status
        )

    # ---------------- 通用工具 ----------------

    @staticmethod
    def with_unit(metric_key: str, value: Optional[float], unit: Optional[str] = None,
                  str_value: Optional[str] = None) -> Dict[str, Any]:
        """
        构造一条指标（统一字段名，避免各采集器写法不一致）。

        值为 None 时仍然返回该条记录：上层入库会存成 NULL，
        大屏显示"未知"而不是凭空消失——这对排障很重要
        （例如设备不支持温度传感器，就应该显示为空而不是没有这一行）。
        """
        return {
            "metric_key": metric_key,
            "metric_value": value,
            "str_value": str_value,
            "unit": unit,
        }

    @staticmethod
    def now_ms() -> int:
        """当前时间戳（毫秒），用于计算耗时"""
        return int(time.time() * 1000)


class CollectorRegistry:
    """
    采集器注册器（插件注册中心）。

    为什么用注册器而不是 if/elif：
    新增协议时 if/elif 需要改动工厂函数（违反开闭原则），
    注册器让新代码"自己报名"，工厂完全不用动。
    """

    _registry: Dict[str, Type[BaseCollector]] = {}

    @classmethod
    def register(cls, protocol: str):
        """
        装饰器：注册采集器。

        用法：
            @CollectorRegistry.register("snmp")
            class SnmpCollector(BaseCollector):
                ...
        """
        def decorator(collector_cls: Type[BaseCollector]) -> Type[BaseCollector]:
            key = protocol.lower().strip()
            if not key:
                raise ValueError("协议标识不能为空")
            collector_cls.protocol = key
            cls._registry[key] = collector_cls
            return collector_cls

        return decorator

    @classmethod
    def get_class(cls, protocol: str) -> Optional[Type[BaseCollector]]:
        """按协议取采集器类"""
        return cls._registry.get((protocol or "").lower().strip())

    @classmethod
    def available_protocols(cls) -> List[str]:
        """已注册的协议列表（供前端展示"当前支持哪些协议"）"""
        return sorted(cls._registry.keys())

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅单元测试使用）"""
        cls._registry.clear()
