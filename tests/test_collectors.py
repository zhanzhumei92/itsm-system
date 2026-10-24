"""
采集器单元测试
===============================================================================
三类测试，成本与覆盖度递增：

1. **纯函数测试**（无 IO）
   速率差值换算、计数器回绕、MetricLatest 之外的指标构造。
   这类最容易出隐蔽 bug（回绕、重置、除零），必须覆盖。

2. **注入式测试**（无网络）
   SNMP 采集器把 `_get` / `_walk` 作为参数在内部传递，这带来一个测试便利：
   可以直接注入"假设备"（一个返回预设 OID 数据的协程），
   从而完整验证**指标构造逻辑**（厂商 OID 回退、多行取最大、内存单位换算、
   接口裁剪、利用率计算），而不需要现场有一台交换机。
   未被覆盖的只剩 pysnmp 的调用管道本身——那部分由"不可达设备"用例验证
   （能正确收到 timeout 错误码并转成友好提示）。

3. **真机测试**（本机回环）
   ICMP 打 127.0.0.1，验证真实探测链路与降级逻辑。

运行：pytest tests/test_collectors.py -v
===============================================================================
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Tuple

import pytest

from app.collectors import CollectorFactory
from app.collectors.icmp import IcmpCollector
from app.collectors.snmp import (
    OID_HR_PROCESSOR_LOAD,
    OID_HR_STORAGE_DESCR,
    OID_HR_STORAGE_SIZE,
    OID_HR_STORAGE_UNITS,
    OID_HR_STORAGE_USED,
    OID_IF_HC_IN_OCTETS,
    OID_IF_HC_OUT_OCTETS,
    OID_IF_HIGH_SPEED,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    SnmpCollector,
    _diff_to_mbps,
    reset_counter_cache,
)
from app.collectors.winrm import WinrmCollector


# ============================================================================
# 助手：假 SNMP 设备
# ============================================================================

def run(coro):
    """同步执行协程（测试里不必引入 pytest-asyncio）"""
    return asyncio.run(coro)


class FakeSNMPDevice:
    """
    假 SNMP 设备：按 OID 返回预设数据。

    模拟真实 SNMP 表遍历的返回形态 [(索引, 值)]，
    这样 `_pick_numeric` / 内存行匹配 / 接口装配等逻辑都能被真实触发。
    """

    def __init__(self, tables: Dict[str, List[Tuple[str, Any]]] | None = None,
                 scalars: Dict[str, Any] | None = None):
        self.tables = tables or {}
        self.scalars = scalars or {}
        self.walk_calls: List[str] = []

    async def get(self, oid: str):
        return self.scalars.get(oid)

    async def walk(self, oid: str) -> List[Tuple[str, Any]]:
        self.walk_calls.append(oid)
        return self.tables.get(oid, [])


def _device(device_type: str = "switch", vendor: str = "huawei", timeout: int = 10) -> Dict[str, Any]:
    """构造 build_collect_params 形态的参数（不含真实凭据）"""
    return {"ip": "192.0.2.1", "vendor": vendor, "device_type": device_type, "timeout": timeout}


def _value(metrics, key):
    """取指标值（数值优先，否则字符串）"""
    for m in metrics:
        if m["metric_key"] == key:
            return m["metric_value"] if m["metric_value"] is not None else m["str_value"]
    return None


def _has(metrics, key) -> bool:
    return any(m["metric_key"] == key for m in metrics)


# ============================================================================
# 1. 纯函数：速率换算
# ============================================================================

class TestRateCalculation:
    def test_正常换算(self):
        """1 亿字节 / 10 秒 = 80 Mbps"""
        assert _diff_to_mbps(0, 100_000_000, 10) == 80.0

    def test_高频小增量保留三位小数(self):
        """低速链路（如 10Kbps）不能被四舍五入抹成 0，否则大屏上看不到流量"""
        result = _diff_to_mbps(0, 1250, 1)     # 1250 字节/秒 = 0.01 Mbps
        assert result == 0.01

    def test_64位计数器回绕补偿(self):
        """
        计数器接近 2^64 后回绕。若不补偿会得到巨大负数，
        大屏曲线会出现把 Y 轴拉爆的深坑
        """
        wrapped = _diff_to_mbps(2**64 - 5000, 5000, 10)
        assert wrapped is not None and wrapped > 0
        # 10000 字节 / 10 秒 = 0.008 Mbps
        assert wrapped == pytest.approx(0.008, abs=0.001)

    def test_计数器重置时放弃本轮(self):
        """
        设备重启导致计数器清零：补偿后的增量仍不合理时必须返回 None，
        宁可少一个数据点，也不要一个假的流量尖峰
        """
        assert _diff_to_mbps(900_000_000_000, 100, 10) is None


# ============================================================================
# 2. 注入式：SNMP 指标构造
# ============================================================================

class TestSnmpCpu:
    def test_华为私有OID优先(self):
        dev = FakeSNMPDevice(tables={
            "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5": [("1", 37), ("2", 12)],
        })
        m = run(SnmpCollector()._collect_cpu(dev.get, dev.walk, "huawei"))
        # 多板卡取最大值：监控要暴露最热的那块板，平均会把故障抹平
        assert _value(m, "cpu_usage") == 37.0

    def test_厂商OID无数据时回退标准MIB(self):
        """现场多数"CPU 采不到"是设备只支持标准 hrProcessorLoad"""
        dev = FakeSNMPDevice(tables={
            "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5": [],
            OID_HR_PROCESSOR_LOAD: [("1", 12), ("2", 45), ("3", 8)],
        })
        m = run(SnmpCollector()._collect_cpu(dev.get, dev.walk, "huawei"))
        assert _value(m, "cpu_usage") == 45.0

    def test_两条路径都无数据时返回空而不是0(self):
        """没有数据 ≠ 使用率为 0。返回 0 会让大屏显示"设备很闲"的假象"""
        dev = FakeSNMPDevice()
        assert run(SnmpCollector()._collect_cpu(dev.get, dev.walk, "huawei")) == []

    def test_负值被过滤(self):
        """部分设备用 -1 表示"不支持该传感器"，不能当使用率上报"""
        dev = FakeSNMPDevice(tables={OID_HR_PROCESSOR_LOAD: [("1", -1), ("2", 20)]})
        m = run(SnmpCollector()._collect_cpu(dev.get, dev.walk, "other"))
        assert _value(m, "cpu_usage") == 20.0


class TestSnmpMemory:
    def test_华为内存换算为MB(self):
        """hwEntityMemSize 单位是字节，指标字典里 mem_total 是 MB"""
        dev = FakeSNMPDevice(tables={
            "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7": [("1", 46)],           # hwEntityMemUsage %
            "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.9": [("1", 4 * 1024**3)],  # hwEntityMemSize 4GB
        })
        m = run(SnmpCollector()._collect_memory(dev.get, dev.walk, "huawei"))
        assert _value(m, "mem_usage") == 46.0
        assert _value(m, "mem_total") == pytest.approx(4096.0, abs=1)
        # mem_used 由 总量 × 使用率 推导，避免多发一次 walk
        assert _value(m, "mem_used") == pytest.approx(1884.16, abs=1)

    def test_标准hrStorage识别物理内存行(self):
        """
        通过描述关键字而非固定索引找物理内存行：
        不同系统上该行索引不一样（Windows 常见 4、Linux 1 或 3）
        """
        dev = FakeSNMPDevice(tables={
            OID_HR_STORAGE_DESCR: [("1", "Physical memory"), ("3", "Swap space"), ("4", "/")],
            OID_HR_STORAGE_UNITS: [("1", 4096), ("3", 4096), ("4", 4096)],
            OID_HR_STORAGE_SIZE: [("1", 2097152), ("3", 1048576), ("4", 26214400)],  # 单元数
            OID_HR_STORAGE_USED: [("1", 1048576), ("3", 1000), ("4", 100)],
        })
        m = run(SnmpCollector()._collect_memory(dev.get, dev.walk, "other"))
        # 2097152 单元 × 4096 字节 = 8 GB
        assert _value(m, "mem_total") == pytest.approx(8192.0, abs=1)
        assert _value(m, "mem_usage") == pytest.approx(50.0, abs=0.1)

    def test_没有物理内存行时返回空(self):
        dev = FakeSNMPDevice(tables={OID_HR_STORAGE_DESCR: [("4", "/"), ("5", "/boot")]})
        assert run(SnmpCollector()._collect_memory(dev.get, dev.walk, "other")) == []


class TestSnmpTemperature:
    def test_有效温度上报(self):
        dev = FakeSNMPDevice(tables={"1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11": [("1", 42), ("2", 48)]})
        m = run(SnmpCollector()._collect_temperature(dev.walk, "huawei"))
        assert _value(m, "device_temp") == 48.0

    def test_零度与异常大值被过滤(self):
        """0 表示"该传感器不存在"（部分设备用 0 占位），>200 是脏数据"""
        dev = FakeSNMPDevice(tables={"1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11": [("1", 0), ("2", 65535)]})
        assert run(SnmpCollector()._collect_temperature(dev.walk, "huawei")) == []


class TestSnmpInterfaces:
    def setup_method(self):
        # 计数器缓存是进程级的，用例之间必须隔离，否则速率会串
        reset_counter_cache()

    @staticmethod
    def _tables(in_in: int, out_out: int) -> Dict[str, List[Tuple[str, Any]]]:
        return {
            OID_IF_OPER_STATUS: [("1", 1), ("2", 2), ("3", 1)],
            OID_IF_NAME: [("1", "GigabitEthernet0/0/1"), ("2", "GigabitEthernet0/0/2"),
                          ("3", "XGigabitEthernet0/0/3")],
            OID_IF_HIGH_SPEED: [("1", 1000), ("2", 1000), ("3", 10000)],
            OID_IF_HC_IN_OCTETS: [("1", in_in), ("2", 0), ("3", in_in)],
            OID_IF_HC_OUT_OCTETS: [("1", out_out), ("2", 0), ("3", out_out)],
        }

    def test_端口状态与带宽(self):
        dev = FakeSNMPDevice(tables=self._tables(0, 0))
        m, _ = run(SnmpCollector()._collect_interfaces(7, dev.walk, {}))
        assert _value(m, "if_status:GigabitEthernet0/0/1") == "up"
        assert _value(m, "if_status:GigabitEthernet0/0/2") == "down"
        assert _value(m, "if_bandwidth:XGigabitEthernet0/0/3") == 10000.0

    def test_首轮无速率基线(self):
        """平台刚重启时没有上一轮计数，只产累计值不产速率，并给出说明"""
        dev = FakeSNMPDevice(tables=self._tables(1_000_000, 2_000_000))
        m, note = run(SnmpCollector()._collect_interfaces(7, dev.walk, {}))
        assert _has(m, "if_in_octets:GigabitEthernet0/0/1")
        assert not _has(m, "if_in_rate:GigabitEthernet0/0/1")
        assert note and "首轮" in note

    def test_第二轮算出速率与利用率(self):
        """
        两轮之间注入真实的时间间隔（sleep 1.1 秒），
        增量 1.25 亿字节 / 约 1.1 秒 ≈ 909 Mbps（千兆口），利用率应接近 100% 上限
        """
        collector = SnmpCollector()
        dev1 = FakeSNMPDevice(tables=self._tables(0, 0))
        run(collector._collect_interfaces(7, dev1.walk, {}))

        time.sleep(1.1)
        dev2 = FakeSNMPDevice(tables=self._tables(125_000_000, 12_500_000))
        m, _ = run(collector._collect_interfaces(7, dev2.walk, {}))

        rate = _value(m, "if_in_rate:GigabitEthernet0/0/1")
        assert rate is not None and rate > 500
        # 利用率 = 速率/带宽，必须封顶 100%（超千兆的瞬时值不该显示成 130%）
        util = _value(m, "if_in_util:GigabitEthernet0/0/1")
        assert util is not None and 0 < util <= 100

    def test_无IF_MIB时返回说明而不是空数组(self):
        """没有 IF-MIB 的设备（如部分打印机）要给出明确说明，便于判断是设备能力问题"""
        dev = FakeSNMPDevice()
        m, note = run(SnmpCollector()._collect_interfaces(1, dev.walk, {}))
        assert m == []
        assert note and "IF-MIB" in note

    def test_端口数超上限时截断并说明(self):
        """48 口交换机 × 7 指标会放大写库量，必须可裁剪且有说明"""
        tables = {
            OID_IF_OPER_STATUS: [(str(i), 1) for i in range(1, 61)],
            OID_IF_NAME: [(str(i), f"Gi0/0/{i}") for i in range(1, 61)],
        }
        dev = FakeSNMPDevice(tables=tables)
        m, note = run(SnmpCollector()._collect_interfaces(1, dev.walk, {}))
        ports = {k.split(":", 1)[1] for k in (x["metric_key"] for x in m) if ":" in k}
        assert len(ports) <= 48
        assert note and "超过上限" in note

    def test_端口索引按数值排序而非字符串(self):
        """
        索引 1,2,...,10 若按字符串排序会是 1,10,2，导致裁剪时先砍掉编号大的端口。
        用例验证被保留的是 1..48 而不是 1,10,11,...
        """
        tables = {
            OID_IF_OPER_STATUS: [(str(i), 1) for i in range(1, 61)],
            OID_IF_NAME: [(str(i), f"Gi0/0/{i}") for i in range(1, 61)],
        }
        dev = FakeSNMPDevice(tables=tables)
        m, _ = run(SnmpCollector()._collect_interfaces(1, dev.walk, {}))
        assert _has(m, "if_status:Gi0/0/48")
        assert not _has(m, "if_status:Gi0/0/60")


class TestSnmpEntryPoint:
    """采集入口的参数校验（不发真实 SNMP 包）"""

    def test_缺少团体字直接返回不等待超时(self):
        """
        凭据不合格时必须在**发起网络请求之前**返回：
        否则 200 台设备每台白等 5 秒超时，一轮采集要十几分钟
        """
        started = time.time()
        result = SnmpCollector().collect(1, {"ip": "192.0.2.1", "snmp_version": "v2c"})
        assert result.success is False
        assert "团体字" in result.error
        assert time.time() - started < 0.5

    def test_v3缺少安全名(self):
        result = SnmpCollector().collect(1, {"ip": "192.0.2.1", "snmp_version": "v3"})
        assert result.success is False
        assert "安全名" in result.error

    def test_不支持的版本(self):
        result = SnmpCollector().collect(1, {"ip": "192.0.2.1", "snmp_version": "v4"})
        assert result.success is False
        assert "不支持的 SNMP 版本" in result.error

    def test_不可达设备给出可操作的排查建议(self):
        """
        真实发一次 SNMP 请求到黑洞地址（TEST-NET-1，RFC 5737 保留段）。
        预期：不抛异常、在超时预算内返回、错误信息含排查方向。
        """
        result = SnmpCollector().collect(1, {
            "ip": "192.0.2.1", "snmp_version": "v2c", "snmp_community": "public",
            "snmp_port": 161, "snmp_timeout": 1, "snmp_retries": 0, "timeout": 3,
        })
        assert result.success is False
        assert result.status in ("fail", "timeout")
        assert "排查建议" in result.error


# ============================================================================
# 3. 真机：ICMP
# ============================================================================

class TestIcmpCollector:
    def test_回环地址在线且有时延指标(self):
        """打本机回环，验证完整探测链路（含无特权时的系统 ping 降级）"""
        result = IcmpCollector().collect(1, {
            "ip": "127.0.0.1", "ping_count": 3, "ping_interval": 0.3,
            "ping_timeout": 1, "ping_size": 56, "timeout": 10,
        })
        assert result.success is True
        assert result.online_status == "online"
        assert _value(result.metrics, "online_status") == "online"
        assert _value(result.metrics, "ping_loss") == 0.0
        latency = _value(result.metrics, "ping_latency")
        assert latency is not None and latency >= 0
        assert result.raw and "探测方式" in result.raw

    def test_不可达地址判离线但采集仍算成功(self):
        """
        关键语义：探测到"离线"这个事实本身就是采集成功。
        若记成失败，collect_log 里就无法区分"网络不通"与"采集器配置错了"
        """
        result = IcmpCollector().collect(1, {
            "ip": "192.0.2.1", "ping_count": 2, "ping_interval": 0.3,
            "ping_timeout": 1, "ping_size": 56, "timeout": 10,
        })
        assert result.success is True
        assert result.online_status == "offline"
        assert _value(result.metrics, "ping_loss") == 100.0
        # 离线时不应产出时延指标：0ms 会被大屏画成"极快"
        assert not _has(result.metrics, "ping_latency")

    def test_参数越界自动收敛(self):
        """现场误填 count=10000 不该打爆设备，应被收敛到上限"""
        result = IcmpCollector().collect(1, {
            "ip": "127.0.0.1", "ping_count": 10000, "ping_interval": 0.01,
            "ping_timeout": 0.001, "ping_size": 99999, "timeout": 10,
        })
        assert result.success is True
        assert "发送=20" in (result.raw or "")


# ============================================================================
# 4. WinRM 输出解析（中文 Windows 编码是本项目最大的坑）
# ============================================================================

class TestWinrmOutputParsing:
    SAMPLE = """OS_CAPTION=Microsoft Windows Server 2019 标准版
OS_VERSION=10.0.17763
MEM_TOTAL_KB=16777216
MEM_FREE_KB=4194304
UPTIME_SECONDS=1234567
CPU_PERCENT=23.45
PROCESS_COUNT=142
DISK=C:|53687091200|21474836480
DISK=D:|107374182400|96636764160
SERVICE=Spooler|Stopped
SERVICE=W3SVC|Stopped
"""

    def setup_method(self):
        self.metrics, self.warnings = WinrmCollector()._parse_script_output(self.SAMPLE)

    def test_CPU与进程数(self):
        assert _value(self.metrics, "cpu_usage") == 23.45
        assert _value(self.metrics, "process_count") == 142

    def test_内存KB换算为MB(self):
        assert _value(self.metrics, "mem_total") == 16384.0
        assert _value(self.metrics, "mem_used") == 12288.0
        assert _value(self.metrics, "mem_usage") == 75.0

    def test_多磁盘聚合(self):
        """C: 50G + D: 100G = 150G，与 Linux 的按设备聚合口径一致"""
        assert _value(self.metrics, "disk_total") == pytest.approx(150.0, abs=0.01)
        assert _value(self.metrics, "disk_used") == pytest.approx(40.0, abs=0.01)
        assert _value(self.metrics, "disk_usage") == pytest.approx(26.67, abs=0.05)

    def test_中文操作系统名称不丢字(self):
        """中文 Windows 下最容易出现的就是这里变成乱码"""
        assert _value(self.metrics, "device_version") == (
            "Microsoft Windows Server 2019 标准版 (10.0.17763)"
        )

    def test_服务状态统一为英文小写(self):
        """指标值不写中文枚举，避免前端要多语言映射"""
        assert _value(self.metrics, "service_status:Spooler") == "stopped"

    def test_自动启动未运行的服务产生警告(self):
        assert any("2 个" in w for w in self.warnings)

    def test_破损行被丢弃不影响其它指标(self):
        """被截断的行段数不对，必须丢弃而不是让解析崩掉或产出错误数据"""
        broken = "DISK=C:\nCPU_PERCENT=10\nSERVICE=NoPipe\nMEM_TOTAL_KB=abc\n"
        metrics, _ = WinrmCollector()._parse_script_output(broken)
        assert _value(metrics, "cpu_usage") == 10.0
        assert not _has(metrics, "disk_total")
        assert not _has(metrics, "mem_total")

    def test_输出为空时不抛异常(self):
        metrics, warnings = WinrmCollector()._parse_script_output("")
        assert metrics == [] and warnings == []


class TestWinrmDecoding:
    """中文 Windows 的输出编码级联解码"""

    def test_UTF8正常解码(self):
        raw = "OS_CAPTION=标准版\n".encode("utf-8")
        assert "标准版" in WinrmCollector._decode(raw)

    def test_GBK输出也能解码(self):
        """未设置 OutputEncoding 时中文 Windows 会给 GBK 字节流"""
        raw = "OS_CAPTION=标准版\n".encode("gbk")
        assert "标准版" in WinrmCollector._decode(raw)

    def test_乱码字节不抛异常(self):
        """最坏情况退到 latin-1，绝不抛 UnicodeDecodeError 中断采集"""
        assert isinstance(WinrmCollector._decode(b"\xff\xfe\x00\x01\x80"), str)

    def test_None与空输入(self):
        assert WinrmCollector._decode(None) == ""
        assert WinrmCollector._decode(b"") == ""


# ============================================================================
# 5. 工厂与注册表
# ============================================================================

class TestCollectorFactory:
    def test_四种协议全部注册(self):
        protocols = CollectorFactory.available_protocols()
        for expected in ("snmp", "ssh", "winrm", "icmp"):
            assert expected in protocols

    def test_实例做进程内单例(self):
        """采集器无状态，复用实例还能保留内部缓存（如 SNMP 计数器基线）"""
        assert CollectorFactory.get("snmp") is CollectorFactory.get("snmp")

    def test_未知协议返回None(self):
        assert CollectorFactory.get("modbus") is None

    def test_协议名大小写与空格容错(self):
        assert CollectorFactory.get(" SNMP ") is not None

    def test_凭据缺失时报错清晰且不泄露口令(self):
        result = CollectorFactory.get("ssh").collect(1, {"ip": "192.0.2.1"})
        assert result.success is False
        assert "用户名" in result.error
