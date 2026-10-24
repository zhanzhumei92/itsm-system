"""
华为 VRP（Versatile Routing Platform）命令输出解析器
===============================================================================
适用设备：华为 S 系列交换机、AR 系列路由器、USG 防火墙（VRP 5.x / 8.x）

**为什么单独为华为写解析器**
不同 VRP 版本的输出格式并不完全一致（例如老版本 `display cpu-usage` 带
"CPU Usage Stat. Cycle"、新版本没有），所以这里每条指标都准备了多个候选正则，
按顺序尝试、命中即用。宁可多写几行正则，也不要在现场因为版本差异导致一片空白。

**命令选型说明**
  display version                版本 + 运行时长（一次取两样，省一次往返）
  display device manufacture-info 序列号（version 里不含 S/N）
  display cpu-usage              CPU 使用率
  display memory-usage           内存使用率
  display interface brief        接口状态（不取流量：见下方说明）

**为什么不从 brief 里取端口流量**
`display interface brief` 输出的是 InUti/OutUti —— **带宽利用率百分比**，
而指标字典里 if_in_rate/if_out_rate 的单位是 Mbps（由 SNMP 的 ifHCInOctets
两次采样做差算出）。两者语义与量纲都不同，混用会让大屏上的"端口速率"
出现"0.5 Mbps"这种看着像故障、其实是 0.5% 的假数据。
端口吞吐请走 SNMP 采集；SSH 只负责"通/断"这一件确定的事。
利用率本身有价值（能看出"这条链路快跑满了"），因此单独产出
if_in_util / if_out_util（单位 %），与 SNMP 的速率指标并存、互不冒充。
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

# 单台设备最多产出多少条接口指标。
# 为什么要设上限：一台 48 口交换机会产出 48×3=144 条接口指标，
# 而 200 台设备的采集周期只有 60 秒——不设上限时，一次采集写库量会被
# 交换机的端口数放大到不可控（约 3 万行/轮），SQLite 必然跟不上。
# 取 256 约为 80 个端口的三类指标，覆盖常规接入交换机；超出部分记入日志。
MAX_INTERFACE_METRICS = 256


@ParserRegistry.register("huawei")
class HuaweiVrpParser(BaseCommandParser):
    """华为 VRP 解析器"""

    vendor = "huawei"

    commands = {
        CMD_VERSION: "display version",
        CMD_DEVICE_INFO: "display device manufacture-info",
        CMD_CPU: "display cpu-usage",
        CMD_MEMORY: "display memory-usage",
        CMD_INTERFACE: "display interface brief",
    }

    # ---------------- 分派 ----------------

    def parse(self, key: str, output: str) -> List[Dict[str, Any]]:
        """按语义命令名分派到具体解析方法"""
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

        典型输出（新版本）：
            CPU Usage Stat. Cycle: 60 (Second)
            CPU Usage            : 8%  Max: 51%
        典型输出（老版本 V200R003 等）：
            Slot 1 CPU usage information:
            CPU Usage            : 8%
        老版本还可能出现按核输出：
            CPU Usage of Slot 1 CPU 0: 8%

        有多个槽位/多核时会匹配到多个值，取最大值——
        CPU 监控关心的是"最忙的那个核"，取平均会掩盖单核打满的真实故障。
        """
        values = [self.num(r"CPU\s+Usage\s*(?:of[^:]*)?:\s*(\d+(?:\.\d+)?)\s*%", text)]
        # 多核/多槽位场景：全局扫描所有出现的百分数
        all_hits = re.findall(r"CPU\s+Usage[^:]*:\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
        if len(all_hits) > 1:
            values = [float(v) for v in all_hits]

        clean = [v for v in values if v is not None]
        if not clean:
            return []
        return [self.metric("cpu_usage", round(max(clean), 2), UNIT_PERCENT)]

    # ---------------- 内存 ----------------

    def _parse_memory(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display memory-usage`。

        典型输出：
            Memory utilization statistics at 2026-09-21 10:00:00+08:00
            System Total Memory Is: 1048576 kbytes
            Total Memory Used Is: 314572 kbytes
            Memory Using Percentage Is: 30%

        单位换算：华为用 kbytes，指标字典里 mem_total/mem_used 是 MB，故 /1024。
        注意：部分型号的 `Total Memory Used Is` 会带上 "(30%)" 后缀，正则只取纯数字。
        """
        metrics: List[Dict[str, Any]] = []

        usage = self.num(r"Memory\s+Using\s+Percentage\s+Is\s*:\s*(\d+(?:\.\d+)?)\s*%", text)
        total_kb = self.num(r"System\s+Total\s+Memory\s+Is\s*:\s*(\d+)", text)
        used_kb = self.num(r"Total\s+Memory\s+Used\s+Is\s*:\s*(\d+)", text)

        if usage is not None:
            metrics.append(self.metric("mem_usage", usage, UNIT_PERCENT))
        if total_kb:
            metrics.append(self.metric("mem_total", round(total_kb / 1024, 2), "MB"))
        if used_kb:
            metrics.append(self.metric("mem_used", round(used_kb / 1024, 2), "MB"))

        # 兜底：只有总量与已用、没有百分比时（少数型号），自己算出来
        if usage is None and total_kb and used_kb:
            metrics.append(
                self.metric("mem_usage", round(used_kb / total_kb * 100, 2), UNIT_PERCENT)
            )
        return metrics

    # ---------------- 版本 / 型号 / 运行时长 ----------------

    def _parse_version(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display version`，一次取出三样东西：
          1. device_version  版本号（含补丁号）
          2. device_uptime   运行时长（折算成小时）
          3. 设备型号（拼进版本串，便于运维在一屏里认出"这是哪台什么设备"）

        典型输出：
            Huawei Versatile Routing Platform Software
            VRP (R) software, Version 5.170 (S5735 V200R021C00SPC600)
            HUAWEI S5735-L24T4S-A1 Routing Switch uptime is 12 days, 3 hours, 15 minutes
        """
        metrics: List[Dict[str, Any]] = []

        # 版本号：优先取 "Version x.xxx (型号 补丁)" 这种最完整的形态
        m = re.search(r"Version\s+([\w.]+)\s*\(([^)]+)\)", text, re.IGNORECASE)
        if m:
            version_text = f"{m.group(1)}({m.group(2).strip()})"
        else:
            m2 = re.search(r"Version\s+([\w.]+)", text, re.IGNORECASE)
            version_text = m2.group(1) if m2 else ""

        # 型号：从 "HUAWEI S5735-xxx Routing Switch uptime is ..." 里取
        model_m = re.search(r"HUAWEI\s+(\S+)\s+(?:Routing\s+Switch|Router|Firewall)", text,
                            re.IGNORECASE)
        if model_m:
            model = model_m.group(1)
            version_text = f"{version_text} [{model}]" if version_text else model

        if version_text:
            metrics.append(self.metric("device_version", None, "", str_value=version_text))

        hours = self.uptime_to_hours(text)
        if hours is not None:
            metrics.append(self.metric("device_uptime", hours, UNIT_HOUR))
        return metrics

    def _parse_device_info(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display device manufacture-info`，取序列号。

        典型输出（注意 "Devicd S/N" 是华为固件里的真实错别字，不是笔误）：
            Slot 1 CPU 0 information
            ---------------------------------------------------------
              Device Type   : S5735-L24T4S-A1
              Devicd S/N    : 2198010627T24C001234

        多槽位设备会输出多段，取第一段即可（整机序列号通常在第一段）。
        """
        m = re.search(r"Devic[ed]\s+S/?N\s*:\s*(\S+)", text, re.IGNORECASE)
        if not m:
            return []
        return [self.metric("device_serial", None, "", str_value=m.group(1).strip())]

    # ---------------- 接口 ----------------

    def _parse_interface(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `display interface brief`，产出每个端口的通断状态与利用率。

        典型输出（表头后每行一个端口）：
            Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
            GigabitEthernet0/0/1        up      up          0.01%  0.01%          0          0
            GigabitEthernet0/0/2        down    down           0%     0%          0          0

        字段含义（决定状态怎么判）：
            PHY      = 物理层（网线/光模块是否插好）
            Protocol = 协议层（链路协商是否成功、是否被 STP 阻塞）
        监控口径：**看 PHY**。PHY down 说明"线没插/光模块坏"，是现场最常处理的故障；
        Protocol down 而 PHY up（如被 STP 阻塞）属正常拓扑行为，不应告警。
        """
        metrics: List[Dict[str, Any]] = []

        # 表头与说明行要跳过：这些行不含端口名 + up/down 的组合，正则天然匹配不上，
        # 但仍显式排除 "^PHY" 这类前缀，避免误匹配
        line_re = re.compile(
            r"^([A-Za-z][\w/\-.:]*)\s+"                 # 端口名，如 GigabitEthernet0/0/1
            r"(up|down|\*down|\^down|\^upup|--)\s+"     # PHY 状态
            r"(up|down|\*down|\^down|--)\s*"            # Protocol 状态
            r"([\d.]+%|--)?\s*([\d.]+%|--)?",           # InUti / OutUti
            re.MULTILINE | re.IGNORECASE,
        )

        for m in line_re.finditer(text):
            port = m.group(1)
            phy = m.group(2).lower().lstrip("*^")     # 归一化：*down → down
            in_uti = m.group(4)
            out_uti = m.group(5)

            metrics.append(
                self.metric(f"if_status:{port}", None, "", str_value="up" if phy == "up" else "down")
            )
            # 利用率为 "--" 或缺失时不产出，避免把"未知"写成 0
            if in_uti and in_uti.endswith("%"):
                metrics.append(self.metric(f"if_in_util:{port}", float(in_uti[:-1]), UNIT_PERCENT))
            if out_uti and out_uti.endswith("%"):
                metrics.append(self.metric(f"if_out_util:{port}", float(out_uti[:-1]), UNIT_PERCENT))

            if len(metrics) >= MAX_INTERFACE_METRICS:
                break

        return metrics
