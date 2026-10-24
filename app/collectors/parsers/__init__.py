"""
厂商命令输出解析器（ParserRegistry）
===============================================================================
**职责边界（务必守住）**
  解析器只做一件事：把「一段设备命令输出文本」翻译成「归一化指标列表」。
  它不建立连接、不执行命令、不写数据库、不发告警。
  这样拆的好处是：命令输出文本可以离线留样做单元测试
  （把一个真实设备的 display cpu-usage 输出粘进测试用例即可验证解析逻辑），
  不需要现场有一台华为交换机才能测。

**为什么解析器和采集器要分开**
  SSH 只是"把命令送到设备并取回文本"的通道，通道与厂商无关；
  而"文本怎么解读"完全取决于厂商（华为 VRP / 华三 Comware / Linux）。
  两者耦合在一起时，新增一个厂商就必须复制整个 SSH 连接逻辑，
  这是现场最容易失控的技术债。

**新增一个厂商的完整步骤**
  1. 在本目录新建模块，如 ruijie_rgos.py
  2. 继承 BaseCommandParser，定义 vendor 与 commands（语义名 → 实际命令）
  3. 实现 parse(key, output)，用 self.pct()/self.num() 等助手返回归一化指标
  4. 用 @ParserRegistry.register("ruijie") 装饰
  5. 在本文件末尾的 _OPTIONAL_MODULES 中登记模块名（不 import 就不会注册）
  完成后 SSH 采集器无需改动一行代码。

**归一化约定**
  返回值与 app/collectors/base.py 的指标结构完全一致：
      {"metric_key": "cpu_usage", "metric_value": 37.5, "str_value": None, "unit": "%"}
  端口类指标用 "基名:端口名"，例如 "if_status:GigabitEthernet0/0/1"。
  解析不到时返回空列表，**不要返回 None 值占位**——
  命令执行失败与"设备确实没有该项"在文本上无法区分，
  留空列表让上层如实记录"本轮未取到该指标"，避免用 0 假装正常。
===============================================================================
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

# ---------------------------------------------------------------------------
# 语义命令名（采集器与解析器之间的契约）
#
# 采集器只知道"要取 CPU"，由解析器决定"华为用 display cpu-usage、Linux 用 cat /proc/stat"。
# 新增厂商时不需要让采集器认识新命令。
# ---------------------------------------------------------------------------
CMD_CPU = "cpu"              # CPU 使用率
CMD_MEMORY = "memory"        # 内存使用率与总量
CMD_DISK = "disk"            # 磁盘使用率与容量
CMD_VERSION = "version"      # 设备版本、型号、运行时长
CMD_DEVICE_INFO = "device_info"  # 序列号等制造信息
CMD_INTERFACE = "interface"  # 接口列表（状态、速率、利用率）

# 单位常量（集中定义，避免各解析器各写各的）
UNIT_PERCENT = "%"
UNIT_MB = "MB"
UNIT_GB = "GB"
UNIT_HOUR = "小时"
UNIT_MBPS = "Mbps"


class BaseCommandParser(ABC):
    """
    厂商命令解析器基类。

    子类只需要关心两件事：
      - commands：我要在设备上执行哪些命令
      - parse() ：某条命令的输出怎么变成指标
    """

    #: 厂商标识，与 mon_device.vendor 对应（huawei/h3c/linux/...）
    vendor: str = ""

    #: 语义命令名 → 设备上真正执行的命令文本
    commands: Dict[str, str] = {}

    @abstractmethod
    def parse(self, key: str, output: str) -> List[Dict[str, Any]]:
        """
        解析一条命令的输出。

        :param key: 语义命令名（CMD_CPU / CMD_MEMORY / ...）
        :param output: 命令原始输出文本
        :return: 归一化指标列表；无法解析时返回空列表（不要抛异常）
        """
        raise NotImplementedError

    # ---------------- 通用解析助手 ----------------
    # 各厂商输出格式细节不同，但"提取一个数字"的模式是通用的，
    # 抽成助手可以显著减少每个解析器的重复代码。

    @staticmethod
    def pct(pattern: str, text: str, group: int = 1) -> Optional[float]:
        """
        按正则提取一个百分比数值。

        容错点：
          - 输出里常有多余空格/制表符，用 re.MULTILINE + 宽松空白匹配
          - 部分设备会输出 "--" 或 "N/A" 表示不支持，此时返回 None 而不是 0
        """
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if not m:
            return None
        raw = m.group(group).strip()
        try:
            return round(float(raw), 2)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def num(pattern: str, text: str, group: int = 1) -> Optional[float]:
        """按正则提取一个数值（不限百分比）"""
        return BaseCommandParser.pct(pattern, text, group)

    @staticmethod
    def metric(key: str, value: Optional[float], unit: str = "",
               str_value: Optional[str] = None) -> Dict[str, Any]:
        """构造一条指标，字段名与 CollectResult.metrics 约定一致"""
        return {
            "metric_key": key,
            "metric_value": value,
            "str_value": str_value,
            "unit": unit or None,
        }

    @staticmethod
    def uptime_to_hours(text: str) -> Optional[float]:
        """
        把各厂商的"uptime is ..."文本统一折算成小时。

        华为/华三都形如：
            uptime is 12 days, 3 hours, 15 minutes
            uptime is 0 weeks, 1 day, 2 hours, 30 minutes
        Linux 的 /proc/uptime 是秒数，不走这里。
        用"抓到什么算什么"的累加策略：缺失的单位按 0 计，
        这样 "1 day" / "1 day, 2 hours" / "3 weeks, 1 day" 都能算对。
        """
        m = re.search(r"uptime\s+is\s+([^\n\r]+)", text, re.IGNORECASE)
        seg = m.group(1) if m else text
        total = 0.0
        hit = False
        for unit, hours in (("year", 8760), ("week", 168), ("day", 24),
                            ("hour", 1), ("minute", 1 / 60), ("second", 1 / 3600)):
            um = re.search(rf"(\d+)\s*{unit}", seg, re.IGNORECASE)
            if um:
                total += int(um.group(1)) * hours
                hit = True
        return round(total, 2) if hit else None


class ParserRegistry:
    """
    解析器注册器。

    与 CollectorRegistry 同构：新厂商"自己报名"，SSH 采集器只按 vendor 查表，
    因此新增厂商不需要改动采集器（开闭原则）。
    """

    _registry: Dict[str, BaseCommandParser] = {}

    @classmethod
    def register(cls, vendor: str):
        """装饰器：注册厂商解析器（注册的是**实例**，解析器本身无状态可安全共享）"""
        def decorator(parser_cls: Type[BaseCommandParser]) -> Type[BaseCommandParser]:
            key = (vendor or "").lower().strip()
            if not key:
                raise ValueError("厂商标识不能为空")
            parser_cls.vendor = key
            cls._registry[key] = parser_cls()
            return parser_cls

        return decorator

    @classmethod
    def get(cls, vendor: Optional[str]) -> Optional[BaseCommandParser]:
        """
        按厂商取解析器。

        :param vendor: 厂商标识；支持 "huawei5"/"huawei_vrp" 这类带后缀写法，
            统一归一化到注册时的短名（现场填 device.vendor 时手滑多加后缀很常见）
        """
        key = (vendor or "").lower().strip()
        if key in cls._registry:
            return cls._registry[key]
        # 退化匹配：前缀命中即用（如 "huawei_vrp" → "huawei"、"h3c_comware" → "h3c"）
        for registered, parser in cls._registry.items():
            if key.startswith(registered):
                return parser
        return None

    @classmethod
    def detect(cls, text: str) -> Optional[BaseCommandParser]:
        """
        从命令输出文本中嗅探厂商（ssh_device_type 填 autodetect 时使用）。

        判据来自各厂商版本信息的固定特征串，比"让用户先填对厂商"更省事：
        现场新增设备时，运维往往只记得 IP 和口令，厂商是猜的。
        """
        low = (text or "").lower()
        if "huawei versatile routing platform" in low or "vrp (r) software" in low:
            return cls._registry.get("huawei")
        if "h3c comware software" in low or "comware software, version" in low:
            return cls._registry.get("h3c")
        if "cisco ios" in low or "cisco internetwork operating system" in low:
            return cls._registry.get("cisco")
        if "linux" in low or "gnu" in low or "/bin/bash" in low:
            return cls._registry.get("linux")
        return None

    @classmethod
    def available_vendors(cls) -> List[str]:
        """已注册的厂商列表（供前端下拉框与"支持哪些厂商"说明使用）"""
        return sorted(cls._registry.keys())

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅单元测试使用）"""
        cls._registry.clear()


# ---------------------------------------------------------------------------
# 解析器模块注册入口
#
# 与采集器同样采用容错导入：某个厂商解析器写坏了/缺失，只影响该厂商设备，
# 其它厂商与协议照常工作。
# ---------------------------------------------------------------------------
_OPTIONAL_MODULES = ("huawei_vrp", "h3c_comware", "linux")

for _module_name in _OPTIONAL_MODULES:
    try:
        __import__(f"app.collectors.parsers.{_module_name}")
    except ImportError as _exc:  # pragma: no cover - 仅在模块缺失时触发
        from app.core.logger import system_logger as _log
        _log.warning("解析器模块 app.collectors.parsers.%s 未加载：%s", _module_name, _exc)

__all__ = [
    "BaseCommandParser",
    "ParserRegistry",
    "CMD_CPU",
    "CMD_MEMORY",
    "CMD_DISK",
    "CMD_VERSION",
    "CMD_DEVICE_INFO",
    "CMD_INTERFACE",
    "UNIT_PERCENT",
    "UNIT_MB",
    "UNIT_GB",
    "UNIT_HOUR",
    "UNIT_MBPS",
]
