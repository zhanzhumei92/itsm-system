"""
采集插件层
===============================================================================
新增协议采集器的完整步骤（以"某厂商私有协议"为例）：

  1. 在 app/collectors/ 下新建模块，例如 vendor_x.py
  2. 继承 BaseCollector 并用装饰器注册：
         @CollectorRegistry.register("vendor_x")
         class VendorXCollector(BaseCollector):
             def collect(self, device_id, params) -> CollectResult: ...
  3. **在下方 _OPTIONAL_MODULES 里登记模块名**（关键：不 import 就不会执行注册代码）
  4. 若涉及厂商命令差异，在 app/collectors/parsers/ 下新增解析器并注册到 ParserRegistry

完成后无需修改调度器、Service、API、前端。
===============================================================================
"""
from app.collectors.base import (
    STATUS_FAIL,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    BaseCollector,
    CollectorRegistry,
    CollectResult,
)
from app.collectors.factory import CollectorFactory

# ---------------------------------------------------------------------------
# 协议采集器注册入口
#
# 这些 import 的唯一目的是"执行各模块顶部的注册装饰器"。
# 采用容错导入的原因：平台按协议分阶段交付，某个采集器模块缺失时，
# 应只影响该协议的设备采集，而不是让整个平台起不来
# （现场排障时"能启动"比"功能全"优先级更高）。
# ---------------------------------------------------------------------------
_OPTIONAL_MODULES = ("snmp", "ssh", "winrm", "icmp")

for _module_name in _OPTIONAL_MODULES:
    try:
        __import__(f"app.collectors.{_module_name}")
    except ImportError as _exc:  # pragma: no cover - 仅在模块未交付时触发
        from app.core.logger import system_logger as _log
        _log.warning("采集器模块 app.collectors.%s 未加载：%s", _module_name, _exc)

__all__ = [
    "BaseCollector",
    "CollectResult",
    "CollectorRegistry",
    "CollectorFactory",
    "STATUS_SUCCESS",
    "STATUS_FAIL",
    "STATUS_TIMEOUT",
]
