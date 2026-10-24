"""
解析器单元测试（离线，不依赖任何网络设备）
===============================================================================
**为什么要专门为解析器写测试**
解析器是"文本进、指标出"的纯函数，也是整个采集链路里最容易因为
设备型号/固件版本差异而失效的一环。把真实设备的命令行输出固化成测试样本后：
  - 不需要现场有华为/华三交换机就能验证解析逻辑
  - 修改正则时能立刻发现"改好了一款、改坏了另一款"
  - 新同事接手时，样本本身就是一份"设备输出长什么样"的活文档

样本来源：各厂商官方文档与现场实测输出，覆盖新老两种固件格式。
运行：pytest tests/test_parsers.py -v
===============================================================================
"""
from __future__ import annotations

import pytest

from app.collectors.parsers import (
    CMD_CPU,
    CMD_DEVICE_INFO,
    CMD_DISK,
    CMD_INTERFACE,
    CMD_MEMORY,
    CMD_VERSION,
    ParserRegistry,
)

# ============================================================================
# 华为 VRP 样本
# ============================================================================

HUAWEI_CPU = """Slot 1 CPU usage information:
CPU Usage Stat. Cycle: 60 (Second)
CPU Usage            : 8%  Max: 51%
CPU Usage details:
  CPU 0: 8%
"""

HUAWEI_MEM = """Memory utilization statistics at 2026-09-21 10:00:00+08:00
System Total Memory Is: 1048576 kbytes
Total Memory Used Is: 483328 kbytes
Memory Using Percentage Is: 46%
"""

HUAWEI_VER = """Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.170 (S5735 V200R021C00SPC600)
Copyright (C) 2012-2021 Huawei Technologies Co., Ltd.
HUAWEI S5735-L24T4S-A1 Routing Switch uptime is 12 days, 3 hours, 15 minutes
"""

# 注意 "Devicd S/N" 是华为固件里的真实错别字，不是样本笔误，不要"修正"它
HUAWEI_INFO = """Slot 1 CPU 0 information
---------------------------------------------------------
  Device Type   : S5735-L24T4S-A1
  Devicd S/N    : 2198010627T24C001234
"""

HUAWEI_IF = """PHY: Physical
*down: administratively down
^down: standby
(l): loopback
Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/1        up      up          0.01%  0.01%          0          0
GigabitEthernet0/0/2        down    down           0%     0%          0          0
GigabitEthernet0/0/24       *down   down           0%     0%          0          0
"""

# ============================================================================
# 华三 Comware 样本
# ============================================================================

H3C_CPU = """Slot 1 CPU usage:
        1% CPU utilization in the last 5 seconds
        3% CPU utilization in the last 1 minute
        2% CPU utilization in the last 5 minutes
"""

H3C_MEM = """Memory statistics are measured in KB:
Slot 1:
             Total    Used    Free  Shared  Buffers  Cached
Mem:        512000  300000  200000       0     1000  100000
-/+ Buffers/Cache: 100000  412000
Swap:            0       0       0
"""

H3C_VER = """H3C Comware Software, Version 7.1.070, Release 6555P01
Copyright (c) 2004-2021 New H3C Technologies Co., Ltd. All rights reserved.
H3C S5130S-28P-EI uptime is 0 weeks, 1 day, 2 hours, 30 minutes
Boot image version: 7.1.070, Release 6555P01
"""

H3C_INFO = """Slot 1:
DEVICE_NAME          : S5130S-28P-EI
DEVICE_SERIAL_NUMBER : 219801A0TNB18A000123
MAC_ADDRESS          : 0023-8912-3456
"""

H3C_IF = """Brief information on interfaces in route mode:
Link: ADM - administratively down; Stby - standby
Protocol: (s) - spoofing
Interface            Link Speed   Duplex Type PVID Description
GE1/0/1              UP   1G      full   Access 1
GE1/0/2              DOWN auto    auto   Access 1
XGE1/0/25            UP   10G     full   Trunk  1
GE1/0/3              ADM  auto    auto   Access 1
"""

# ============================================================================
# 信创 Linux（麒麟 V10）样本
# ============================================================================

# 第一次采样 vs 第二次采样：故意让第二次各列有增量，用于验证做差逻辑。
# 增量构造：user+200 nice+10 system+150 idle+700 iowait+50 softirq+20 → 总增量 1130
# 预期使用率 = 1 - (700+50)/1130 ≈ 33.63%
LINUX_CPU = """cpu  1234567 8901 234567 8901234 45678 0 12345 0 0 0
cpu0 617283 4450 117283 4450617 22839 0 6172 0 0 0
@@@SPLIT@@@
cpu  1234767 8911 234717 8901934 45728 0 12365 0 0 0
cpu0 617383 4455 117358 4450967 22864 0 6182 0 0 0
"""

LINUX_MEM = """MemTotal:        8123456 kB
MemFree:         1234567 kB
MemAvailable:    5678901 kB
Buffers:          123456 kB
Cached:          2345678 kB
SwapTotal:             0 kB
"""

# 含 tmpfs / devtmpfs 伪文件系统，且 /dev/sda1 被挂载两次（/ 与 /var）——
# 这两点正是磁盘聚合最容易出错的地方，样本必须覆盖
LINUX_DF = """Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/sda1         52428800  26214400  26214400      50% /
tmpfs              4063256         0   4063256       0% /dev/shm
devtmpfs           2030000         0   2030000       0% /dev
/dev/sdb1        104857600  10485760  94371840      10% /data
/dev/sda1         52428800  26214400  26214400      50% /var
"""

LINUX_VER = """NAME="Kylin Linux Advanced Server"
VERSION="V10 (Sword)"
VERSION_ID="V10"
PRETTY_NAME="Kylin Linux Advanced Server V10 (Sword)"
@@@SPLIT@@@
4.19.90-25.10.v2101.ky10.x86_64
@@@SPLIT@@@
821340.56 3210987.12
@@@SPLIT@@@
247
"""


# ============================================================================
# 助手
# ============================================================================

def _get(metrics, key):
    """从指标列表里取某项（取不到返回 None，便于写简洁断言）"""
    for m in metrics:
        if m["metric_key"] == key:
            return m
    return None


def _value(metrics, key):
    """取某项的值（数值优先，否则取字符串值）"""
    m = _get(metrics, key)
    if m is None:
        return None
    return m["metric_value"] if m["metric_value"] is not None else m["str_value"]


# ============================================================================
# 注册器
# ============================================================================

class TestParserRegistry:
    def test_三个厂商解析器均已注册(self):
        vendors = ParserRegistry.available_vendors()
        assert "huawei" in vendors
        assert "h3c" in vendors
        assert "linux" in vendors

    def test_厂商名带后缀也能命中(self):
        """现场填 device.vendor 时容易写 huawei_vrp / h3c_comware，应能前缀匹配"""
        assert ParserRegistry.get("huawei_vrp") is not None
        assert ParserRegistry.get("H3C_Comware") is not None
        assert ParserRegistry.get("HUAWEI") is not None

    def test_未知厂商返回None(self):
        assert ParserRegistry.get("ruijie") is None
        assert ParserRegistry.get("") is None

    def test_从输出文本嗅探厂商(self):
        """ssh_device_type 填 autodetect 时依赖此能力"""
        assert ParserRegistry.detect(HUAWEI_VER).vendor == "huawei"
        assert ParserRegistry.detect(H3C_VER).vendor == "h3c"
        assert ParserRegistry.detect(LINUX_VER).vendor == "linux"


# ============================================================================
# 华为
# ============================================================================

class TestHuaweiVrpParser:
    def setup_method(self):
        self.p = ParserRegistry.get("huawei")

    def test_CPU使用率(self):
        m = self.p.parse(CMD_CPU, HUAWEI_CPU)
        assert _value(m, "cpu_usage") == 8.0

    def test_内存三项齐全且单位正确(self):
        m = self.p.parse(CMD_MEMORY, HUAWEI_MEM)
        assert _value(m, "mem_usage") == 46.0
        # 1048576 kbytes = 1024 MB
        assert _value(m, "mem_total") == 1024.0
        # 483328 kbytes = 472 MB
        assert _value(m, "mem_used") == 472.0

    def test_版本与型号拼装(self):
        m = self.p.parse(CMD_VERSION, HUAWEI_VER)
        version = _value(m, "device_version")
        assert "5.170" in version
        assert "V200R021C00SPC600" in version
        assert "S5735-L24T4S-A1" in version

    def test_运行时长折算为小时(self):
        m = self.p.parse(CMD_VERSION, HUAWEI_VER)
        # 12 天 3 小时 15 分 = 291.25 小时
        assert _value(m, "device_uptime") == pytest.approx(291.25, abs=0.1)

    def test_序列号_兼容固件错别字(self):
        """必须能识别 "Devicd S/N" 这个华为固件真实错别字"""
        m = self.p.parse(CMD_DEVICE_INFO, HUAWEI_INFO)
        assert _value(m, "device_serial") == "2198010627T24C001234"

    def test_接口状态与利用率(self):
        m = self.p.parse(CMD_INTERFACE, HUAWEI_IF)
        assert _value(m, "if_status:GigabitEthernet0/0/1") == "up"
        assert _value(m, "if_status:GigabitEthernet0/0/2") == "down"
        assert _value(m, "if_in_util:GigabitEthernet0/0/1") == 0.01

    def test_管理性关闭归一化为down(self):
        """*down（管理性关闭）在采集层统一记 down，业务区分交给告警规则"""
        m = self.p.parse(CMD_INTERFACE, HUAWEI_IF)
        assert _value(m, "if_status:GigabitEthernet0/0/24") == "down"

    def test_表头行不会被误当成端口(self):
        m = self.p.parse(CMD_INTERFACE, HUAWEI_IF)
        keys = [x["metric_key"] for x in m]
        assert not any("Interface:" in k or k.endswith(":Interface") for k in keys)

    def test_空输出返回空列表不抛异常(self):
        assert self.p.parse(CMD_CPU, "") == []
        assert self.p.parse(CMD_VERSION, "") == []
        assert self.p.parse("不存在的语义键", HUAWEI_CPU) == []


# ============================================================================
# 华三
# ============================================================================

class TestH3cComwareParser:
    def setup_method(self):
        self.p = ParserRegistry.get("h3c")

    def test_CPU取1分钟窗口而非5秒(self):
        """5 秒窗口抖动大，做阈值告警会频繁误报，必须取 1 分钟值"""
        m = self.p.parse(CMD_CPU, H3C_CPU)
        assert _value(m, "cpu_usage") == 3.0

    def test_内存表格格式(self):
        m = self.p.parse(CMD_MEMORY, H3C_MEM)
        # 512000 KB = 500 MB，300000 KB ≈ 292.97 MB
        assert _value(m, "mem_total") == 500.0
        assert _value(m, "mem_used") == pytest.approx(292.97, abs=0.1)
        assert _value(m, "mem_usage") == pytest.approx(58.59, abs=0.1)

    def test_版本串保留Release补丁号(self):
        """华三的补丁信息在 Release 里，丢掉就看出不有没有打补丁"""
        m = self.p.parse(CMD_VERSION, H3C_VER)
        version = _value(m, "device_version")
        assert "7.1.070" in version
        assert "6555P01" in version

    def test_运行时长(self):
        m = self.p.parse(CMD_VERSION, H3C_VER)
        # 0 周 + 1 天 + 2 小时 30 分 = 26.5 小时
        assert _value(m, "device_uptime") == pytest.approx(26.5, abs=0.1)

    def test_序列号(self):
        m = self.p.parse(CMD_DEVICE_INFO, H3C_INFO)
        assert _value(m, "device_serial") == "219801A0TNB18A000123"

    def test_接口状态与带宽归一化(self):
        """华三 brief 给的是 Speed 标称值（1G/10G），必须换算成 Mbps 才能参与计算"""
        m = self.p.parse(CMD_INTERFACE, H3C_IF)
        assert _value(m, "if_status:GE1/0/1") == "up"
        assert _value(m, "if_bandwidth:GE1/0/1") == 1000.0
        assert _value(m, "if_bandwidth:XGE1/0/25") == 10000.0

    def test_管理性关闭ADM记为down(self):
        m = self.p.parse(CMD_INTERFACE, H3C_IF)
        assert _value(m, "if_status:GE1/0/3") == "down"

    def test_auto速率不产出带宽指标(self):
        """auto 协商结果未知，不能瞎猜成某个数值"""
        m = self.p.parse(CMD_INTERFACE, H3C_IF)
        assert _get(m, "if_bandwidth:GE1/0/2") is None


# ============================================================================
# Linux / 信创
# ============================================================================

class TestLinuxParser:
    def setup_method(self):
        self.p = ParserRegistry.get("linux")

    def test_CPU两次采样做差(self):
        """
        验证核心算法：使用率 = 1 - (idle+iowait 增量)/总增量
        样本增量：总 1130，idle+iowait = 700+50 = 750
        → 1 - 750/1130 = 33.63%
        """
        m = self.p.parse(CMD_CPU, LINUX_CPU)
        assert _value(m, "cpu_usage") == pytest.approx(33.63, abs=0.05)

    def test_只有一次采样时返回空(self):
        """缺基线不能瞎算——用开机至今的平均值冒充实时值会严重误导"""
        single = LINUX_CPU.split("@@@SPLIT@@@")[0]
        assert self.p.parse(CMD_CPU, single) == []

    def test_内存优先用MemAvailable口径(self):
        """MemFree 会把页缓存算成已用，永远显示 90%+，必须用 MemAvailable"""
        m = self.p.parse(CMD_MEMORY, LINUX_MEM)
        # (8123456 - 5678901) / 8123456 = 30.09%
        assert _value(m, "mem_usage") == pytest.approx(30.09, abs=0.05)
        assert _value(m, "mem_total") == pytest.approx(7933.06, abs=1)

    def test_老内核无MemAvailable时回退估算(self):
        """部分信创系统内核 3.10 没有 MemAvailable，必须能回退"""
        old = "MemTotal: 8000000 kB\nMemFree: 2000000 kB\nBuffers: 500000 kB\nCached: 3000000 kB\n"
        m = self.p.parse(CMD_MEMORY, old)
        # 可用 = 2000000+500000+3000000 = 5500000 → 使用率 31.25%
        assert _value(m, "mem_usage") == pytest.approx(31.25, abs=0.05)

    def test_磁盘排除伪文件系统并按设备去重(self):
        """
        样本里 /dev/sda1 挂载了两次（/ 与 /var），tmpfs/devtmpfs 是内存盘。
        正确结果：只算 sda1(50G) + sdb1(100G) = 150G，而不是 50+100+50+4+2
        """
        m = self.p.parse(CMD_DISK, LINUX_DF)
        assert _value(m, "disk_total") == pytest.approx(150.0, abs=0.01)
        # 已用 = 25G + 10G = 35G
        assert _value(m, "disk_used") == pytest.approx(35.0, abs=0.01)
        assert _value(m, "disk_usage") == pytest.approx(23.33, abs=0.05)

    def test_系统信息复合命令四段全部解析(self):
        m = self.p.parse(CMD_VERSION, LINUX_VER)
        assert _value(m, "device_version") == (
            "Kylin Linux Advanced Server V10 (Sword) / 4.19.90-25.10.v2101.ky10.x86_64"
        )
        # 821340.56 秒 = 228.15 小时
        assert _value(m, "device_uptime") == pytest.approx(228.15, abs=0.05)
        assert _value(m, "process_count") == 247

    def test_缺os_release时后续段不错位(self):
        """
        老系统没有 /etc/os-release 时，不能让分段整体前移——
        否则会把内核版本读成进程数。分段数不足应按段容错。
        """
        text = "@@@SPLIT@@@\n4.19.90-25.x86_64\n@@@SPLIT@@@\n3600.5 100.0\n@@@SPLIT@@@\n88\n"
        m = self.p.parse(CMD_VERSION, text)
        assert _value(m, "process_count") == 88
        assert _value(m, "device_uptime") == pytest.approx(1.0, abs=0.01)


# ============================================================================
# 健壮性：脏数据不能把解析器打挂
# ============================================================================

class TestParserRobustness:
    """现场会遇到各种截断、乱码、权限报错混进输出的情况，解析器必须不抛异常"""

    @pytest.mark.parametrize("vendor", ["huawei", "h3c", "linux"])
    @pytest.mark.parametrize("key", [CMD_CPU, CMD_MEMORY, CMD_DISK, CMD_VERSION, CMD_INTERFACE])
    def test_各类异常输入均不抛异常(self, vendor, key):
        parser = ParserRegistry.get(vendor)
        for text in ("", "\n\n", "权限不足", "Error: Invalid input detected at '^' marker.",
                     "%% Unrecognized command", "\x00\x01乱码", "1" * 5000):
            result = parser.parse(key, text)
            assert isinstance(result, list)

    def test_命令行回显混入输出不影响解析(self):
        """netmiko 的 enable 回显、分页提示常混进输出，解析仍应正确"""
        noisy = "<SW1>display cpu-usage\n" + HUAWEI_CPU + "\n<SW1>\n  ---- More ----   "
        m = ParserRegistry.get("huawei").parse(CMD_CPU, noisy)
        assert _value(m, "cpu_usage") == 8.0
