"""
华三 H3C Comware（V5 / V7）命令输出解析器
===============================================================================
适用设备：H3C S 系列交换机、MSR 路由器、SecPath 防火墙

**与华为解析器的两处关键差异（这是必须分开写的原因）**
  1. CPU 输出格式完全不同：
       华为：CPU Usage            : 8%
       华三：1% CPU utilization in the last 1 minute
     华三会给"5 秒 / 1 分钟 / 5 分钟"三个滑动窗口值。
  2. 端口列表字段完全不同：
       华为 brief 给的是 InUti/OutUti（带宽利用率百分比），
       华三 brief 给的是 Speed（1G/100M 这类链路速率标称值），不含利用率。
     因此两种设备的接口指标产出**天然不同**：华三只能给通断 + 带宽，
     利用率需靠 SNMP 的 octets 差值算。本项目如实产出能拿到的部分，
     不做"用带宽冒充利用率"这类会误导运维的换算。

**命令选型**
  display version           版本 + 运行时长
  display device manuinfo   序列号（V5 为 display device manuinfo，V7 同）
  display cpu-usage         CPU（三个窗口）
  display memory            内存
  display interface brief   接口（V7 分 route/bridge 两段，都解析）
===============================================================================
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from app.collectors.parsers import (
    CMD_CPU,
    CMD_DEVICE_INFO,
    CMD_INTERFACE,
    CMD_MEMORY,
    CMD_VERSION,
    UNIT_HOUR,
    UNIT_PERCENT,
    BaseCommandParser,
    ParserRegistry,
)

# 单次采集最多输出的接口指标条数（与华为解析器同口径，理由见 huawei_vrp.py）
MAX_INTERFACE_METRICS = 256

# 华三 brief 里的速率标称值 → Mbps。
# 为什么要显式映射：Speed 列可能是 "1G" / "100M" / "10G" / "2.5G" / "auto" / "ADM"，
# 直接用字符串是没法做"带宽利用率"计算的，必须归一化成数值。
SPEED_TEXT_MAP = {
    "10M": 10.0, "100M": 100.0, "1G": 1000.0, "2.5G": 2500.0,
    "5G": 5000.0, "10G": 10000.0, "25G": 25000.0, "40G": 40000.0,
    "100G": 100000.0,
}


@ParserRegistry.register("h3c")
class H3cComwareParser(BaseCommandParser):
    """华三 Comware 解析器"""

    vendor = "h3c"

    commands = {
        CMD_VERSION: "display version",
        CMD_DEVICE_INFO: "display device manuinfo",
        CMD_CPU: "display cpu-usage",
        CMD_MEMORY: "display memory",
        CMD_INTERFACE: "display interface brief",
    }

    def parse(self, key: str, output: str) -> List[Dict[str, Any]]:
        if not output:
            return []
        handler = {
            CMD_CPU: self._parse_cpu,
            CMD_MEMORY: self._parse_memory,
            CMD_VERSION: self._parse_version,
            CMD_DEVICE_INFO: self._parse_device_info,
            CMD_INTERFACE: self._parse_interface,
        }.get(key)
        return handler(output) if handler else []

    # ---------------- CPU ----------------

    def _parse_cpu(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display cpu-usage`。

        典型输出：
            Slot 1 CPU usage:
                    1% CPU utilization in the last 5 seconds
                    3% CPU utilization in the last 1 minute
                    2% CPU utilization in the last 5 minutes

        **窗口选择**：取"1 分钟"值。
        为什么不取 5 秒：5 秒窗口抖动极大（一个 show 命令就能把它顶到 30%），
        用来做阈值告警会频繁误报；5 分钟又太迟钝，故障恢复后久久不降。
        1 分钟是网络设备 CPU 监控的事实标准窗口（思科的 cpmCPUTotal5minRev
        也是同一思路，只是更长）。
        """
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*CPU\s+utilization\s+in\s+the\s+last\s+1\s+minute",
                      text, re.IGNORECASE)
        if m:
            return [self.metric("cpu_usage", float(m.group(1)), UNIT_PERCENT)]

        # 兜底：某些版本/型号只输出一行，没有窗口说明
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*CPU\s+utilization", text, re.IGNORECASE)
        if m:
            return [self.metric("cpu_usage", float(m.group(1)), UNIT_PERCENT)]

        # V5 老格式：Slot 1 CPU usage: 5%
        value = self.num(r"CPU\s+usage\s*:\s*(\d+(?:\.\d+)?)\s*%", text)
        if value is not None:
            return [self.metric("cpu_usage", value, UNIT_PERCENT)]
        return []

    # ---------------- 内存 ----------------

    def _parse_memory(self, text: str) -> List[Dict[str, Any]]:
        """
        同时兼容 V7 表格格式与 V5 文本格式。

        V7 典型输出（注意单位是 KB，且 Total 列在 Used 之前）：
            Memory statistics are measured in KB:
            Slot 1:
                         Total    Used    Free  Shared  Buffers  Cached
            Mem:        512000  300000  200000       0     1000  100000

        V5 典型输出：
            System Total Memory: 1048576 Kbytes
            Total Used Memory: 314572 Kbytes
            Used Memory Percentage: 30%
        """
        metrics: List[Dict[str, Any]] = []

        # ---- V7 表格：匹配以 Mem: 开头的那一行 ----
        m = re.search(r"^\s*Mem\s*:\s*(\d+)\s+(\d+)\s+(\d+)", text, re.MULTILINE | re.IGNORECASE)
        if m:
            total_kb, used_kb = int(m.group(1)), int(m.group(2))
            if total_kb > 0:
                metrics.append(self.metric("mem_total", round(total_kb / 1024, 2), "MB"))
                metrics.append(self.metric("mem_used", round(used_kb / 1024, 2), "MB"))
                metrics.append(self.metric("mem_usage", round(used_kb / total_kb * 100, 2), UNIT_PERCENT))
                return metrics

        # ---- V5 文本 ----
        percentage = self.num(r"Used\s+Memory\s+Percentage\s*:\s*(\d+(?:\.\d+)?)\s*%", text)
        total_kb = self.num(r"System\s+Total\s+Memory\s*:\s*(\d+)", text)
        used_kb = self.num(r"Total\s+Used\s+Memory\s*:\s*(\d+)", text)

        if percentage is not None:
            metrics.append(self.metric("mem_usage", percentage, UNIT_PERCENT))
        if total_kb:
            metrics.append(self.metric("mem_total", round(total_kb / 1024, 2), "MB"))
        if used_kb:
            metrics.append(self.metric("mem_used", round(used_kb / 1024, 2), "MB"))
        if percentage is None and total_kb and used_kb:
            metrics.append(self.metric("mem_usage", round(used_kb / total_kb * 100, 2), UNIT_PERCENT))
        return metrics

    # ---------------- 版本 / 运行时长 ----------------

    def _parse_version(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display version`。

        典型输出：
            H3C Comware Software, Version 7.1.070, Release 6555P01
            H3C S5130S-28P-EI uptime is 0 weeks, 1 day, 2 hours, 30 minutes

        版本串拼装保留 "Version + Release" 两段：
        华三的补丁信息在 Release 里（如 6555P01），只报 7.1.070 会看不出打过补丁，
        而"是否打了 P01 补丁"恰恰是现场升级决策的依据。
        """
        metrics: List[Dict[str, Any]] = []

        version = ""
        m = re.search(r"Version\s+([\w.]+)", text, re.IGNORECASE)
        if m:
            version = m.group(1)
        rel = re.search(r"Release\s+(\w+)", text, re.IGNORECASE)
        if rel:
            version = f"{version} Release {rel.group(1)}" if version else f"Release {rel.group(1)}"

        model_m = re.search(r"^H3C\s+(\S+)\s+uptime", text, re.IGNORECASE | re.MULTILINE)
        if model_m:
            version = f"{version} [{model_m.group(1)}]" if version else model_m.group(1)

        if version:
            metrics.append(self.metric("device_version", None, "", str_value=version))

        hours = self.uptime_to_hours(text)
        if hours is not None:
            metrics.append(self.metric("device_uptime", hours, UNIT_HOUR))
        return metrics

    def _parse_device_info(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display device manuinfo` 取序列号。

        典型输出（V7）：
            Slot 1:
            DEVICE_NAME          : S5130S-28P-EI
            DEVICE_SERIAL_NUMBER : 219801A0TNB18A000123
            MAC_ADDRESS          : 0023-8912-3456

        V5 用的是 "DEVICE_SERIAL_NUMBER"，个别型号是 "Serial Number"，
        因此正则同时容纳下划线与空格两种写法。
        """
        m = re.search(r"(?:DEVICE[_ ]?SERIAL[_ ]?NUMBER|Serial\s+Number)\s*:\s*(\S+)",
                      text, re.IGNORECASE)
        if not m:
            return []
        return [self.metric("device_serial", None, "", str_value=m.group(1).strip())]

    # ---------------- 接口 ----------------

    def _parse_interface(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display interface brief`。

        典型输出（V7 分 route / bridge 两段，格式一致）：
            Interface            Link Speed   Duplex Type PVID Description
            GE1/0/1              UP   1G      full   Access 1
            GE1/0/2              DOWN auto    auto   Access 1
            XGE1/0/25            UP   10G     full   Trunk  1

        关键字段：
            Link  = UP / DOWN / ADM（ADM 是"管理性关闭"）
            Speed = 1G / 100M / auto

        **ADM 的处理**：管理性关闭（运维主动 shutdown）在监控上的正确含义是
        "预期内不可用"，不是故障。这里仍然记为 down，但由告警规则去区分
        （规则可配置只对非 ADM 端口告警）；采集层不做业务判断，保持数据中立。
        """
        metrics: List[Dict[str, Any]] = []

        # 跳过说明行与表头：用"必须含 up/down 类链接状态"来过滤
        line_re = re.compile(
            r"^([A-Za-z][\w/\-.]*)\s+"                       # 接口名，如 GE1/0/1 / XGE1/0/25
            r"(UP|DOWN|ADM|Stby|UP\(s\)|DOWN\(s\))\s*"        # Link 状态
            r"(\S+)?\s*"                                      # Speed，如 1G / auto / 10G(a)
            r"(\S+)?\s*"                                      # Duplex
            r"(\S+)?",                                        # Type
            re.MULTILINE | re.IGNORECASE,
        )

        for m in line_re.finditer(text):
            port = m.group(1)
            # 表头行的第一列是 "Interface"，会命中正则，需排除
            if port.lower() in ("interface", "link"):
                continue

            link = m.group(2).upper()
            status_text = "up" if link.startswith("UP") else "down"

            metrics.append(self.metric(f"if_status:{port}", None, "", str_value=status_text))

            # 带宽：Speed 列归一化成 Mbps
            speed_text = (m.group(3) or "").upper().replace("(A)", "").replace("(a)", "")
            bandwidth = SPEED_TEXT_MAP.get(speed_text)
            if bandwidth:
                metrics.append(self.metric(f"if_bandwidth:{port}", bandwidth, "Mbps"))

            if len(metrics) >= MAX_INTERFACE_METRICS:
                break

        return metrics
