"""
Linux / 信创主机（麒麟、统信 UOS、openEuler 等）命令解析器
===============================================================================
适用对象：Linux 服务器，特别是**信创主机**（麒麟 Kylin、统信 UOS、openEuler）
—— 这类主机通常不能安装 SNMP 服务（软件源受限/安全加固要求），
SSH 是唯一可用的采集通道，因此本解析器是信创场景的主力。

**设计原则：只用 /proc 与标准命令，不依赖任何第三方工具**
  CPU  读 /proc/stat 两次采样做差（不依赖 sysstat/top 的输出格式）
  内存 读 /proc/meminfo
  磁盘 df -kP（POSIX 标准输出格式，不会像 df -h 那样随 locale 变化单位）
  运行时长 读 /proc/uptime

  为什么不解析 `top -bn1`：top 的输出在第 3 行开始是进程表，
  且不同发行版的表头、字段顺序、是否显示 CPU 行差异很大；
  /proc 是内核稳定接口，从 2.6 到 6.x 格式从未变过。

**SSH 往返次数是要省的成本**
  每次命令下发都包含网络往返 + 设备处理时间。因此把"版本 + 运行时长 + 进程数"
  合成一条复合命令一次取回（见 commands[CMD_VERSION]），
  代价是解析函数要按分隔符切段，收益是省掉两次往返。
===============================================================================
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.collectors.parsers import (
    CMD_CPU,
    CMD_DISK,
    CMD_MEMORY,
    CMD_VERSION,
    UNIT_HOUR,
    UNIT_PERCENT,
    BaseCommandParser,
    ParserRegistry,
)

# 复合命令的分段标记。用这个 ASCII 串而不是空行/特定关键词：
# 命令回显、sh 提示符、日志输出都可能出现空行或常见词，导致切段错位；
# 该标记在 /proc 与 df 的输出里绝不会自然出现。
SPLIT_MARKER = "@@@SPLIT@@@"

# df 输出里需要排除的伪文件系统（关键：不排除就会把内存盘算成磁盘，容量虚高数倍）
PSEUDO_FS_KEYWORDS = (
    "tmpfs", "devtmpfs", "overlay", "shm", "udev", "none", "squashfs",
    "proc", "sysfs", "cgroup", "debugfs", "ramfs", "devfs", "autofs",
)


@ParserRegistry.register("linux")
class LinuxParser(BaseCommandParser):
    """
    Linux / 信创主机解析器。

    产出的指标：
        cpu_usage / mem_usage / mem_total / mem_used
        disk_usage / disk_total / disk_used / disk_free
        device_version（发行版 + 内核）
        device_uptime（小时）
        process_count（进程数）
    """

    vendor = "linux"

    commands = {
        # CPU：一次会话内采样两次。/proc/stat 的 cpu 行是启动至今的累计节拍数，
        # 单次读取只能得到"平均负载"（从开机算起），对监控毫无意义，必须做差。
        CMD_CPU: f"cat /proc/stat; echo {SPLIT_MARKER}; sleep 1; cat /proc/stat",
        CMD_MEMORY: "cat /proc/meminfo",
        CMD_DISK: "df -kP",
        # 复合命令：版本 + 内核 + 运行时长 + 进程数，一次往返取回
        CMD_VERSION: (
            f"cat /etc/os-release 2>/dev/null; echo {SPLIT_MARKER}; "
            f"uname -r; echo {SPLIT_MARKER}; "
            f"cat /proc/uptime; echo {SPLIT_MARKER}; "
            f"ps -e --no-headers 2>/dev/null | wc -l"
        ),
    }

    def parse(self, key: str, output: str) -> List[Dict[str, Any]]:
        if not output:
            return []
        handler = {
            CMD_CPU: self._parse_cpu,
            CMD_MEMORY: self._parse_memory,
            CMD_DISK: self._parse_disk,
            CMD_VERSION: self._parse_system_info,
        }.get(key)
        return handler(output) if handler else []

    # ---------------- CPU ----------------

    def _parse_cpu(self, text: str) -> List[Dict[str, Any]]:
        """
        解析两次 /proc/stat 采样，算出这段间隔内的 CPU 使用率。

        /proc/stat 首行格式（各列都是启动至今的累计节拍 Jiffies）：
            cpu  user nice system idle iowait irq softirq steal guest guest_nice

        使用率公式：
            使用率 = 1 - (idle 增量 + iowait 增量) / 总增量

        **为什么把 iowait 算作"空闲"**：
        iowait 是 CPU 等待磁盘 IO 的时间，此时 CPU 并没有在执行任务。
        业界（top / sar）在计算 CPU 使用率时同样把 iowait 排除在"忙"之外。
        若把 iowait 计入使用率，一台被慢盘拖住的服务器会显示 90% CPU，
        运维会去查 CPU 而真相是磁盘问题 —— 这在现场是常见的误判源头。
        """
        # 用分隔标记切出两次采样
        segments = text.split(SPLIT_MARKER)
        cpu_lines = []
        for seg in segments:
            for line in seg.splitlines():
                if line.startswith("cpu "):     # 注意有空格：排除 "cpu0 "、"cpu1 " 等分核行
                    cpu_lines.append(line)
                    break

        if len(cpu_lines) < 2:
            return []

        first = self._parse_stat_line(cpu_lines[0])
        second = self._parse_stat_line(cpu_lines[-1])
        if first is None or second is None:
            return []

        total_delta = second["total"] - first["total"]
        idle_delta = (second["idle"] + second["iowait"]) - (first["idle"] + first["iowait"])
        if total_delta <= 0:
            # 两次采样数值相同：可能是 sleep 命令被裁剪、或 /proc 读取被缓存
            return []

        usage = round((1 - idle_delta / total_delta) * 100, 2)
        # 数值兜底：极端情况下（节拍计数异常）可能算出负数或超过 100
        usage = max(0.0, min(usage, 100.0))
        return [self.metric("cpu_usage", usage, UNIT_PERCENT)]

    @staticmethod
    def _parse_stat_line(line: str) -> Optional[Dict[str, int]]:
        """
        把 /proc/stat 的 cpu 行拆成各字段计数值。

        字段个数在不同内核版本间略有差异（老内核没有 steal/guest），
        因此按位置取值时要允许缺列：缺的按 0 计，而不是 IndexError。
        """
        parts = line.split()
        if len(parts) < 5:
            return None
        try:
            values = [int(p) for p in parts[1:]]
        except ValueError:
            return None

        # user nice system idle iowait irq softirq steal
        padded = (values + [0] * 8)[:8]
        return {
            "user": padded[0], "nice": padded[1], "system": padded[2], "idle": padded[3],
            "iowait": padded[4], "irq": padded[5], "softirq": padded[6], "steal": padded[7],
            "total": sum(padded),
        }

    # ---------------- 内存 ----------------

    def _parse_memory(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 /proc/meminfo。

        典型内容（单位一律 kB）：
            MemTotal:        8123456 kB
            MemFree:         1234567 kB
            MemAvailable:    5678901 kB
            Buffers:          123456 kB
            Cached:          2345678 kB

        **优先用 MemAvailable 而不是 MemFree**：
        MemFree 把页缓存（Cached/Buffers）算作"已用"，一台正常的 Linux
        服务器 MemFree 常年只有几百 MB，用 MemFree 算使用率会永远显示 90%+，
        运维会以为内存告警了而实际上是缓存。MemAvailable 是内核给出的
        "新程序真正可用的内存"，这才是运维关心的口径。内核 3.14+ 提供。

        老内核（如部分信创系统的 3.10）没有 MemAvailable，回退用
        MemFree + Buffers + Cached 近似。
        """
        def kb(field: str) -> Optional[float]:
            m = re.search(rf"^{field}:\s*(\d+)\s*kB", text, re.MULTILINE)
            return float(m.group(1)) if m else None

        total_kb = kb("MemTotal")
        if not total_kb:
            return []

        available_kb = kb("MemAvailable")
        if available_kb is None:
            free = kb("MemFree") or 0.0
            buffers = kb("Buffers") or 0.0
            cached = kb("Cached") or 0.0
            available_kb = free + buffers + cached

        used_kb = max(total_kb - available_kb, 0.0)
        total_mb = round(total_kb / 1024, 2)

        return [
            self.metric("mem_total", total_mb, "MB"),
            self.metric("mem_used", round(used_kb / 1024, 2), "MB"),
            self.metric("mem_usage", round(used_kb / total_kb * 100, 2), UNIT_PERCENT),
        ]

    # ---------------- 磁盘 ----------------

    def _parse_disk(self, text: str) -> List[Dict[str, Any]]:
        """
        解析 `df -kP`。

        为什么用 -P（POSIX 输出格式）而不是 -h：
        -h 的人类可读单位（G/M/T）随容量变化且受 locale 影响，
        解析代码要处理一堆单位换算；-P 固定为 1024 字节块，且强制单行一条记录
        （长设备名不会被折行，而折行恰恰是 df 解析最常见的失败原因）。

        聚合口径：**按设备去重后累加**。
        一台机器上 / 与 /var 常是同一设备的不同挂载点（bind mount），
        不去重就会把同一块盘算两次，总容量虚高一倍。
        伪文件系统（tmpfs/overlay/udev 等）必须排除 —— 它们是内存盘，
        算进磁盘容量会得到"总容量 16GB 而物理盘只有 500GB"这种荒谬结果。
        """
        lines = (text or "").splitlines()
        if len(lines) < 2:
            return []

        seen_devices: set = set()
        total_kb = used_kb = avail_kb = 0.0
        counted = 0

        for line in lines[1:]:        # 跳过表头
            parts = line.split()
            # POSIX 格式固定 6 列：Filesystem 1024-blocks Used Available Capacity Mounted
            if len(parts) < 6:
                continue
            device = parts[0]
            mounted = parts[-1]

            # 排除伪文件系统：设备名或挂载点命中关键字即跳过
            low_dev, low_mnt = device.lower(), mounted.lower()
            if any(k in low_dev for k in PSEUDO_FS_KEYWORDS):
                continue
            if any(low_mnt.startswith(p) for p in ("/proc", "/sys", "/dev/shm", "/run", "/snap")):
                continue
            # 同一设备多次挂载只算一次
            if device in seen_devices:
                continue
            seen_devices.add(device)

            try:
                total_kb += float(parts[1])
                used_kb += float(parts[2])
                avail_kb += float(parts[3])
                counted += 1
            except ValueError:
                # 极少数情况下某列是 "-"（如权限不足读不到容量），跳过该行
                continue

        if counted == 0 or total_kb <= 0:
            return []

        to_gb = lambda kb: round(kb / 1024 / 1024, 2)   # noqa: E731 - 局部换算，短小可读
        return [
            self.metric("disk_total", to_gb(total_kb), "GB"),
            self.metric("disk_used", to_gb(used_kb), "GB"),
            self.metric("disk_free", to_gb(avail_kb), "GB"),
            self.metric("disk_usage", round(used_kb / total_kb * 100, 2), UNIT_PERCENT),
        ]

    # ---------------- 系统信息（版本 / 运行时长 / 进程数）----------------

    def _parse_system_info(self, text: str) -> List[Dict[str, Any]]:
        """
        解析复合命令的四段输出。

        分段顺序（与 commands[CMD_VERSION] 严格对应）：
            [0] /etc/os-release 内容
            [1] uname -r  → 内核版本
            [2] /proc/uptime → "秒数 空闲秒数"
            [3] ps 计数   → 进程数

        分段数不足时逐段容错：能取几段取几段。
        这很重要——若目标主机没有 /etc/os-release（CentOS 6 等老系统），
        按固定下标取值会导致后面全部错位，反而把内核版本读成进程数。
        """
        segments = (text or "").split(SPLIT_MARKER)
        metrics: List[Dict[str, Any]] = []

        # ---- 段 0：发行版 ----
        # os-release 里最有用的是 PRETTY_NAME（如 "Kylin Linux Advanced Server V10 (Sword)"）
        # 它自带版本与代号，比 NAME+VERSION_ID 拼出来更贴合运维的叫法
        if segments:
            pretty = re.search(r'PRETTY_NAME\s*=\s*"?([^"\n]+)"?', segments[0])
            name = re.search(r'^NAME\s*=\s*"?([^"\n]+)"?', segments[0], re.MULTILINE)
            ver = re.search(r'^VERSION_ID\s*=\s*"?([^"\n]+)"?', segments[0], re.MULTILINE)
            os_text = (pretty.group(1).strip() if pretty
                       else f"{name.group(1).strip() if name else 'Linux'} {ver.group(1).strip() if ver else ''}".strip())

            kernel = segments[1].strip().splitlines()[0].strip() if len(segments) > 1 else ""
            # 只保留首个非空行：uname -r 正常只输出一行，多余行是 shell 提示符残留
            version_text = f"{os_text} / {kernel}" if kernel else os_text
            if os_text:
                metrics.append(self.metric("device_version", None, "", str_value=version_text))

        # ---- 段 2：运行时长 ----
        if len(segments) > 2:
            m = re.search(r"([\d.]+)", segments[2])
            if m:
                seconds = float(m.group(1))
                metrics.append(self.metric("device_uptime", round(seconds / 3600, 2), UNIT_HOUR))

        # ---- 段 3：进程数 ----
        if len(segments) > 3:
            m = re.search(r"(\d+)", segments[3])
            if m:
                metrics.append(self.metric("process_count", int(m.group(1)), "个"))

        return metrics
