"""
SNMP 采集器（pysnmp 7.x / asyncio 内核）
===============================================================================
适用设备：交换机、路由器、防火墙、支持 SNMP 的服务器/存储/打印机

**最大的一处工程决策：异步库怎么塞进同步框架**
  pysnmp 7.x 彻底重写为 asyncio 实现，`get_cmd()` 是协程、`walk_cmd()` 是异步生成器。
  而本平台的采集框架是同步的（见 app/tasks/executor.py 的 thread 池），
  桥接方式是在 collect() 里用 asyncio.run() 起一个一次性事件循环。

  为什么不在采集框架里全面改用 asyncio：
  其余三种协议（netmiko / pywinrm / pythonping）都是**同步阻塞**库，
  为一种协议把整个采集层改成异步，会让另外三种全部要包 run_in_executor，
  复杂度翻倍却没有性能收益。采集是 IO 密集型，线程池已经足够。

  ⚠ 由此带来一条硬约束：**SnmpEngine 必须在协程内部创建**。
   engine 内部持有传输调度器，与创建它的 event loop 绑定；
  若跨调用复用，遇到新的 loop 会报 "attached to a different loop"。
  这也是下面所有 OID 查询共用同一个 engine、但绝不跨次复用的原因
  （一次采集内多次查询共用 engine 是安全的，也避免了重复建引擎的开销）。

**厂商 OID 回退策略**
  华为/华三/思科的 CPU、内存、温度在私有 MIB 里，OID 各不相同；
  而 HOST-RESOURCES-MIB 的 hrProcessorLoad / hrStorageTable 是通用标准。
  策略：先按 device.vendor 试厂商 OID，取不到再退回标准 MIB。
  现场常见的"CPU 采不到"多半是设备不支持标准 hrProcessorLoad，
  这条回退链能覆盖绝大多数型号；两条都失败也不报错，只是没有该指标。

**端口速率怎么来**
  IF-MIB 给的是累计字节数（Counter64），速率必须由**两次采样做差**得到。
  上一次的计数存在进程内存里（见 _COUNTER_CACHE），因此：
    - 平台重启后的第一轮采集没有速率，只有累计值（这是正常的，日志会说明）
    - 计数器回绕（64 位溢出）按 2^64 补偿，避免出现天文数字般的负速率
===============================================================================
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import BaseCollector, CollectorRegistry, CollectResult
from app.core.logger import collect_logger
from app.services.metric_service import make_port_metric_key

# ============================================================================
# 标准 MIB OID（RFC 1213 / RFC 2790 / RFC 2863）
# ============================================================================
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"      # sysDescr    设备描述（也用于嗅探厂商）
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"       # sysName     设备名
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"     # sysUpTime   运行时长（TimeTicks，单位 1/100 秒）

# HOST-RESOURCES-MIB：通用 CPU / 内存（Windows、Linux、部分网络设备支持）
OID_HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"       # hrProcessorLoad，每核一行
OID_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"        # hrStorageDescr，存储项描述
OID_HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"        # hrStorageAllocationUnits，分配单元字节
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"         # hrStorageSize，单元数
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"         # hrStorageUsed，已用单元数

# IF-MIB：接口
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"        # ifName（优先用它做端口名，更贴近运维叫法）
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"          # ifDescr（ifName 缺失时的兜底）
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"    # ifOperStatus
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed，单位 Mbps
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"   # ifHCInOctets，64 位入向字节
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"  # ifHCOutOctets，64 位出向字节

# ifOperStatus 取值含义（RFC 2863）
IF_OPER_STATUS_TEXT = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}

# ============================================================================
# 厂商私有 OID（按 device.vendor 匹配，取不到自动回退标准 MIB）
# ============================================================================
VENDOR_CPU_OIDS: Dict[str, List[str]] = {
    "huawei": ["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5"],       # hwEntityCpuUsage (%)
    "h3c": ["1.3.6.1.4.1.25506.2.6.1.1.1.1.6"],             # hh3cEntityExtCpuUsage (%)
    "cisco": ["1.3.6.1.4.1.9.9.109.1.1.1.1.8",              # cpmCPUTotal5minRev (%)
              "1.3.6.1.4.1.9.9.109.1.1.1.1.5"],             # cpmCPUTotal5sec (%)
}
VENDOR_MEM_USAGE_OIDS: Dict[str, List[str]] = {
    "huawei": ["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7"],       # hwEntityMemUsage (%)
    "h3c": ["1.3.6.1.4.1.25506.2.6.1.1.1.1.8"],             # hh3cEntityExtMemUsage (%)
}
VENDOR_MEM_TOTAL_OIDS: Dict[str, List[str]] = {
    "huawei": ["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.9"],       # hwEntityMemSize (bytes)
    "h3c": ["1.3.6.1.4.1.25506.2.6.1.1.1.1.10"],            # hh3cEntityExtMemSize (bytes)
}
VENDOR_TEMP_OIDS: Dict[str, List[str]] = {
    "huawei": ["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11"],      # hwEntityTemperature (℃)
    "cisco": ["1.3.6.1.4.1.9.9.13.1.3.1.3"],                # ciscoEnvMonTemperatureValue (℃)
}

# 私有 OID 的 group_key：这些表都是按"实体索引"分行的（一台设备多块板卡/多颗 CPU），
# 取最大值而不是平均值 —— 监控要暴露最热的那块板，平均会把故障抹平。
MULTI_ROW_TAKE_MAX = True

# SNMP v3 认证/加密协议名 → pysnmp 协议对象（现场填的大小写不规范，统一大写匹配）
V3_AUTH_PROTOCOLS: Dict[str, str] = {
    "MD5": "usmHMACMD5AuthProtocol",
    "SHA": "usmHMACSHAAuthProtocol",
    "SHA1": "usmHMACSHAAuthProtocol",
    "SHA224": "usmHMAC128SHA224AuthProtocol",
    "SHA256": "usmHMAC192SHA256AuthProtocol",
    "SHA384": "usmHMAC256SHA384AuthProtocol",
    "SHA512": "usmHMAC384SHA512AuthProtocol",
}
V3_PRIV_PROTOCOLS: Dict[str, str] = {
    "DES": "usmDESPrivProtocol",
    "3DES": "usm3DESEDEPrivProtocol",
    "AES": "usmAesCfb128Protocol",
    "AES128": "usmAesCfb128Protocol",
    "AES192": "usmAesCfb192Protocol",
    "AES256": "usmAesCfb256Protocol",
}

# 单台设备最多采集多少个端口。
# 上限的作用：48 口交换机 × 7 个指标 = 336 条/轮，200 台设备同轮采集时
# 写库量会被交换机端口数放大；设上限可让写入量可预估。
MAX_INTERFACES = 48

# 端口计数器缓存条目上限（超过则淘汰最旧的一半）。
# 为什么要有上限：设备删除后其缓存条目不会被主动清理，
# 长期运行会缓慢泄漏内存；用简单的容量控制换实现复杂度。
_COUNTER_CACHE_MAX = 20000

# 指标键最大长度（metric_latest.metric_key 为 String(64)）；
# 超长端口名（如某些厂商的 "Ethernet1/1.100" 再加长描述）会被截断，
# 截断而不是丢弃，保证至少能看到端口存在。
_METRIC_KEY_MAX = 64


@CollectorRegistry.register("snmp")
class SnmpCollector(BaseCollector):
    """
    SNMP 采集器。

    产出的指标：
        cpu_usage                  CPU 使用率 %
        mem_usage / mem_total / mem_used   内存
        device_uptime              运行时长（小时）
        device_temp                设备温度（仅部分型号）
        if_status:<端口>            端口通断 up/down
        if_in_rate / if_out_rate   端口速率 Mbps（需两次采样）
        if_in_util / if_out_util   端口利用率 %（速率 / 带宽）
        if_in_octets / if_out_octets  端口累计字节（Counter）
        if_bandwidth:<端口>         端口带宽 Mbps
    """

    protocol = "snmp"

    metric_keys = [
        "cpu_usage", "mem_usage", "mem_total", "mem_used", "device_uptime", "device_temp",
        "if_status", "if_in_rate", "if_out_rate", "if_in_util", "if_out_util",
        "if_in_octets", "if_out_octets", "if_bandwidth",
    ]

    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        """
        同步入口：把 asyncio 采集协程跑在一次性事件循环上。

        异常处理原则：**任何异常都转成 CollectResult(success=False)**，
        绝不让异常穿透到调度器——否则同一批次的其它设备会受影响。
        """
        # ---- 凭据完整性前置校验：不合格直接返回，省掉 5 秒超时等待 ----
        version = str(params.get("snmp_version") or "v2c").lower()
        if version not in ("v1", "v2c", "v3"):
            return self.fail(f"不支持的 SNMP 版本：{version}（应为 v1/v2c/v3）")

        if version == "v3":
            if not params.get("snmp_username"):
                return self.fail("SNMP v3 缺少安全名（用户名），请在设备凭据中补全")
            # v3 至少要认证口令；只有 noAuthNoPriv 才允许都为空
            if not params.get("snmp_auth_key") and str(params.get("snmp_auth_proto") or "").upper() not in ("", "NONE", "NOAUTH"):
                return self.fail("SNMP v3 配置了认证协议但未填写认证口令")
        else:
            if not params.get("snmp_community"):
                return self.fail("SNMP v2c/v1 缺少团体字（Community），请在设备凭据中补全")

        started = self.now_ms()
        try:
            metrics, online_status, note = asyncio.run(self._collect_async(device_id, params))
        except Exception as exc:  # noqa: BLE001 - 采集器必须自行兜底
            duration = self.now_ms() - started
            collect_logger.warning(
                "SNMP 采集异常 | 设备ID=%s | IP=%s | %s: %s",
                device_id, params.get("ip"), type(exc).__name__, exc,
            )
            return self.fail(f"SNMP 采集异常：{type(exc).__name__}: {exc}", duration_ms=duration)

        duration = self.now_ms() - started
        if not metrics:
            # 一条指标都没取到 —— 最常见原因是团体字错误或该 IP 不是 SNMP 设备。
            # 明确给出排查方向，避免运维只看到"失败"两个字。
            return self.fail(
                f"SNMP 无任何响应数据（已尝试 {version.upper()}）。"
                f"排查建议：1) 团体字/账号是否正确 2) 设备是否放通 UDP 161 "
                f"3) ACL 是否限制管理源 IP",
                duration_ms=duration,
            )

        # 走到这里说明 SNMP 通了。online_status 由 SNMP 可达性推断为 online
        # （比单纯 ICMP 更可信：SNMP 都能应答，设备必然是活的）
        result = self.ok(metrics, duration_ms=duration, online_status=online_status or "online")
        if note:
            result.raw = note
        return result

    # ========================================================================
    # 异步采集核心
    # ========================================================================

    async def _collect_async(
        self, device_id: int, params: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
        """
        一次采集的全部 OID 查询都在**同一个 event loop、同一个 engine** 内完成。

        :return: (指标列表, 在线状态, 备注文本)
        """
        # 延迟导入：让 pysnmp 缺失时只影响 SNMP 设备，
        # 而不是让整个 collectors 包 import 失败（现场可先只用 ICMP 上线）
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData, ContextData, ObjectIdentity, ObjectType,
            SnmpEngine, UdpTransportTarget, get_cmd, walk_cmd,
        )

        ip = str(params.get("ip") or "")
        port = int(params.get("snmp_port") or 161)
        timeout = float(params.get("snmp_timeout") or 5)
        retries = int(params.get("snmp_retries") if params.get("snmp_retries") is not None else 2)

        # 设备级超时（mon_device.timeout，默认 10 秒）是总预算；
        # SNMP 单次请求超时取两者较小值，保证一个"半死不活"的设备
        # 不会用 5 秒 × 重试 3 次把整个采集周期拖长。
        device_timeout = float(params.get("timeout") or 10)
        req_timeout = max(1.0, min(timeout, device_timeout / (retries + 1)))

        engine = SnmpEngine()          # 必须在协程内创建（见模块 docstring）
        auth = self._build_auth_data(params, CommunityData, version_hint=params.get("snmp_version"))
        target = await UdpTransportTarget.create((ip, port), timeout=req_timeout, retries=retries)
        context = ContextData()

        notes: List[str] = []
        metrics: List[Dict[str, Any]] = []

        async def _get(oid: str) -> Optional[Any]:
            """单值 GET；失败返回 None（不抛异常，让上层继续采其它项）"""
            try:
                err_ind, err_status, _, var_binds = await get_cmd(
                    engine, auth, target, context, ObjectType(ObjectIdentity(oid))
                )
                if err_ind or err_status:
                    return None
                return var_binds[0][1] if var_binds else None
            except Exception:  # noqa: BLE001
                return None

        async def _walk(oid: str) -> List[Tuple[str, Any]]:
            """表遍历；返回 [(索引, 值)]。失败返回空列表。"""
            rows: List[Tuple[str, Any]] = []
            try:
                async for err_ind, err_status, _, var_binds in walk_cmd(
                    engine, auth, target, context, ObjectType(ObjectIdentity(oid))
                ):
                    if err_ind or err_status:
                        break
                    for name, value in var_binds:
                        # 索引 = 完整 OID 去掉列前缀（如 1.3.6.1.2.1.2.2.1.8.1 → "1"）
                        index = str(name).replace(oid + ".", "", 1)
                        rows.append((index, value))
            except Exception:  # noqa: BLE001
                return rows
            return rows

        # ---- 1. 系统信息（一次 GET 三样，顺带确认设备是否真的支持 SNMP）----
        sys_descr = await _get(OID_SYS_DESCR)
        if sys_descr is None:
            # sysDescr 取不到基本可以断定 SNMP 不通（它是任何 SNMP 设备必实现的对象）
            return [], "offline", "sysDescr 无响应"

        sys_uptime = await _get(OID_SYS_UPTIME)
        metrics.extend(self._build_system_metrics(sys_uptime))

        # ---- 2. CPU / 内存 / 温度（厂商 OID 优先，回退标准 MIB）----
        vendor = str(params.get("vendor") or "").lower().strip()
        metrics.extend(await self._collect_cpu(_get, _walk, vendor))
        metrics.extend(await self._collect_memory(_get, _walk, vendor))
        metrics.extend(await self._collect_temperature(_walk, vendor))

        # ---- 3. 接口（IF-MIB）----
        iface_metrics, iface_note = await self._collect_interfaces(
            device_id, _walk, params
        )
        metrics.extend(iface_metrics)
        if iface_note:
            notes.append(iface_note)

        if not metrics:
            notes.append("SNMP 已连通但未取到任何指标（可能设备未实现相关 MIB）")

        return metrics, "online", "；".join(notes) or None

    # ========================================================================
    # 认证数据构造
    # ========================================================================

    @staticmethod
    def _build_auth_data(params: Dict[str, Any], CommunityData, version_hint: Any):
        """
        构造 pysnmp 的认证对象。

        v1/v2c → CommunityData(团体字, mpModel=0/1)
        v3     → UsmUserData(安全名, 认证口令, 加密口令, 协议对象)

        ⚠ 本方法会接触明文口令，调用方**绝不可**把返回值或 params 写进日志。
        """
        version = str(version_hint or "v2c").lower()

        if version in ("v1", "v2c"):
            # mpModel: 0=SNMPv1, 1=SNMPv2c
            return CommunityData(str(params.get("snmp_community") or ""), mpModel=0 if version == "v1" else 1)

        from pysnmp.hlapi.v3arch.asyncio import UsmUserData
        import pysnmp.hlapi.v3arch.asyncio as _pysnmp_async

        auth_proto_name = str(params.get("snmp_auth_proto") or "").upper().strip()
        priv_proto_name = str(params.get("snmp_priv_proto") or "").upper().strip()
        auth_key = params.get("snmp_auth_key") or None
        priv_key = params.get("snmp_priv_key") or None

        # 协议名 → 对象；填错时退化为"不认证/不加密"并在日志留下线索，
        # 而不是直接抛异常让采集失败（现场最常见的错是填了 "SHA1" 这类别名）
        auth_protocol = None
        if auth_key and auth_proto_name in V3_AUTH_PROTOCOLS:
            auth_protocol = getattr(_pysnmp_async, V3_AUTH_PROTOCOLS[auth_proto_name], None)
        priv_protocol = None
        if priv_key and priv_proto_name in V3_PRIV_PROTOCOLS:
            priv_protocol = getattr(_pysnmp_async, V3_PRIV_PROTOCOLS[priv_proto_name], None)

        kwargs: Dict[str, Any] = {}
        if auth_protocol is not None:
            kwargs["authProtocol"] = auth_protocol
        if priv_protocol is not None:
            kwargs["privProtocol"] = priv_protocol

        return UsmUserData(str(params.get("snmp_username") or ""), auth_key, priv_key, **kwargs)

    # ========================================================================
    # 指标构造：系统 / CPU / 内存 / 温度
    # ========================================================================

    @staticmethod
    def _build_system_metrics(sys_uptime: Any) -> List[Dict[str, Any]]:
        """
        sysUpTime 是 TimeTicks，单位 1/100 秒，指标字典里 device_uptime 用小时。

        注意排除"刚启动就重启"的设备：uptime < 5 分钟时仍照实上报，
        但值会很小 —— 运维看到 uptime 只有 0.1 小时就知道设备刚重启过，
        这正是这个指标的价值所在。
        """
        ticks = SnmpCollector._to_int(sys_uptime)
        if ticks is None:
            return []
        return [SnmpCollector.with_unit("device_uptime", round(ticks / 100 / 3600, 2), "小时")]

    async def _collect_cpu(self, _get, _walk, vendor: str) -> List[Dict[str, Any]]:
        """CPU：厂商 OID → 标准的 hrProcessorLoad（多核取最大）"""
        for oid in VENDOR_CPU_OIDS.get(vendor, []):
            rows = await _walk(oid)
            value = self._pick_numeric(rows)
            if value is not None:
                return [self.with_unit("cpu_usage", round(value, 2), "%")]

        # 标准回退：hrProcessorLoad 每个逻辑核一行，取最大值反映真实负载
        rows = await _walk(OID_HR_PROCESSOR_LOAD)
        value = self._pick_numeric(rows)
        if value is not None:
            return [self.with_unit("cpu_usage", round(value, 2), "%")]
        return []

    async def _collect_memory(self, _get, _walk, vendor: str) -> List[Dict[str, Any]]:
        """内存：厂商 OID 给的是百分比 + 总字节；标准 MIB 走 hrStorageTable"""
        metrics: List[Dict[str, Any]] = []

        for oid in VENDOR_MEM_USAGE_OIDS.get(vendor, []):
            rows = await _walk(oid)
            value = self._pick_numeric(rows)
            if value is not None:
                metrics.append(self.with_unit("mem_usage", round(value, 2), "%"))
                break

        for oid in VENDOR_MEM_TOTAL_OIDS.get(vendor, []):
            rows = await _walk(oid)
            raw = self._pick_numeric(rows)
            if raw is not None:
                # 私有 MIB 的内存总量单位是字节 → 换算成 MB
                metrics.append(self.with_unit("mem_total", round(raw / 1024 / 1024, 2), "MB"))
                break

        # 标准回退：从 hrStorageTable 里找描述为"物理内存"的那一行
        if not any(m["metric_key"] == "mem_usage" for m in metrics):
            standard = await self._memory_from_hr_storage(_walk)
            metrics.extend(standard)

        # 已用量可由 总量 × 使用率 推出，避免再发一次 walk
        total = next((m["metric_value"] for m in metrics if m["metric_key"] == "mem_total"), None)
        usage = next((m["metric_value"] for m in metrics if m["metric_key"] == "mem_usage"), None)
        if total and usage is not None and not any(m["metric_key"] == "mem_used" for m in metrics):
            metrics.append(self.with_unit("mem_used", round(total * usage / 100, 2), "MB"))
        return metrics

    async def _memory_from_hr_storage(self, _walk) -> List[Dict[str, Any]]:
        """
        从 hrStorageTable 提取内存占用。

        表结构（四张表按同一索引关联）：
            hrStorageDescr(i)         存储项描述，如 "Physical memory" / "Real Memory"
            hrStorageAllocationUnits(i) 每个分配单元多少字节（通常 4096 或 65536）
            hrStorageSize(i)          总单元数 → 总字节 = 单元数 × 单元大小
            hrStorageUsed(i)          已用单元数

        判据用描述关键字而非固定索引：不同系统上物理内存的行号不一样
        （Windows 常见索引 4、Linux 1 或 3），靠关键字匹配才稳。
        """
        descr_rows = await _walk(OID_HR_STORAGE_DESCR)
        if not descr_rows:
            return []
        units_rows = dict(await _walk(OID_HR_STORAGE_UNITS))
        size_rows = dict(await _walk(OID_HR_STORAGE_SIZE))
        used_rows = dict(await _walk(OID_HR_STORAGE_USED))

        # 关键字按优先级排列：先精确匹配物理内存，再退到含 "memory" 的任意行
        candidates = ("physical memory", "real memory", "physical", "memory", "内存")
        for keyword in candidates:
            for index, descr in descr_rows:
                text = str(descr).lower()
                if keyword not in text:
                    continue
                unit = self._to_int(units_rows.get(index)) or 1
                size = self._to_int(size_rows.get(index))
                used = self._to_int(used_rows.get(index))
                if not size:
                    continue
                total_mb = round(size * unit / 1024 / 1024, 2)
                metrics = [self.with_unit("mem_total", total_mb, "MB")]
                if used is not None:
                    used_mb = round(used * unit / 1024 / 1024, 2)
                    metrics.append(self.with_unit("mem_used", used_mb, "MB"))
                    if size:
                        metrics.append(self.with_unit("mem_usage", round(used / size * 100, 2), "%"))
                return metrics
        return []

    async def _collect_temperature(self, _walk, vendor: str) -> List[Dict[str, Any]]:
        """设备温度：仅部分交换机支持，取所有传感器最大值（最热的那颗最危险）"""
        for oid in VENDOR_TEMP_OIDS.get(vendor, []):
            rows = await _walk(oid)
            value = self._pick_numeric(rows)
            # 过滤明显异常值：0 表示"该传感器不存在"，> 200 是脏数据
            if value is not None and 0 < value < 200:
                return [self.with_unit("device_temp", round(value, 2), "℃")]
        return []

    # ========================================================================
    # 接口采集（含速率差值计算）
    # ========================================================================

    async def _collect_interfaces(
        self, device_id: int, _walk, params: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        采集接口状态、速率、利用率、带宽。

        :return: (指标列表, 备注)
        """
        status_rows = dict(await _walk(OID_IF_OPER_STATUS))
        if not status_rows:
            return [], "该设备未实现 IF-MIB ifOperStatus"

        # 端口名优先用 ifName（"Gi0/0/1" 这类运维习惯叫法），缺失时退 ifDescr
        name_rows = dict(await _walk(OID_IF_NAME)) or dict(await _walk(OID_IF_DESCR))
        speed_rows = dict(await _walk(OID_IF_HIGH_SPEED))
        in_rows = dict(await _walk(OID_IF_HC_IN_OCTETS))
        out_rows = dict(await _walk(OID_IF_HC_OUT_OCTETS))

        now_ts = time.time()
        metrics: List[Dict[str, Any]] = []
        collected_ports = 0
        rate_missing_ports = 0
        total_ports = len(status_rows)

        for index, status in sorted(status_rows.items(), key=lambda kv: _index_sort_key(kv[0])):
            # 端口数上限：到顶就停，避免一台 48 口以上设备把采集写库量放大
            if collected_ports >= MAX_INTERFACES:
                break
            collected_ports += 1

            port = str(name_rows.get(index) or f"if{index}").strip()
            status_code = self._to_int(status)
            status_text = IF_OPER_STATUS_TEXT.get(status_code or 0, str(status_code))

            metrics.append(self.with_unit(make_port_metric_key("if_status", port), None, None, status_text))

            # 带宽（Mbps）：ifHighSpeed 已是 Mbps，无需换算
            bandwidth = self._to_float(speed_rows.get(index))
            if bandwidth and bandwidth > 0:
                metrics.append(self.with_unit("if_bandwidth" + ":" + port, round(bandwidth, 2), "Mbps"))

            in_octets = self._to_float(in_rows.get(index))
            out_octets = self._to_float(out_rows.get(index))
            if in_octets is not None:
                metrics.append(self.with_unit("if_in_octets" + ":" + port, in_octets, "Byte"))
            if out_octets is not None:
                metrics.append(self.with_unit("if_out_octets" + ":" + port, out_octets, "Byte"))

            # ---- 速率：两次采样做差 ----
            in_rate, out_rate = _compute_rate(device_id, index, now_ts, in_octets, out_octets)
            if in_rate is None:
                rate_missing_ports += 1
            if in_rate is not None:
                metrics.append(self.with_unit("if_in_rate" + ":" + port, in_rate, "Mbps"))
            if out_rate is not None:
                metrics.append(self.with_unit("if_out_rate" + ":" + port, out_rate, "Mbps"))

            # ---- 利用率：速率 / 带宽 ----
            # 记录它而不是让大屏自己去除，是为了让告警规则能统一写在 if_in_util 上
            # （SNMP 与 SSH 两条采集路径都产出同一个键，规则只写一条）
            if bandwidth and bandwidth > 0:
                if in_rate is not None:
                    metrics.append(self.with_unit("if_in_util" + ":" + port,
                                                  round(min(in_rate / bandwidth * 100, 100), 2), "%"))
                if out_rate is not None:
                    metrics.append(self.with_unit("if_out_util" + ":" + port,
                                                  round(min(out_rate / bandwidth * 100, 100), 2), "%"))

        # 指标键长度保护
        for item in metrics:
            if len(item["metric_key"]) > _METRIC_KEY_MAX:
                item["metric_key"] = item["metric_key"][:_METRIC_KEY_MAX]

        notes: List[str] = []
        if total_ports > collected_ports:
            notes.append(f"接口数 {total_ports} 超过上限 {MAX_INTERFACES}，本轮仅采集前 {collected_ports} 个")
        if rate_missing_ports and rate_missing_ports == collected_ports:
            # 全部端口都算不出速率 → 首轮采集或平台刚重启，属预期行为，需要说明
            notes.append("首轮采集无速率数据（缺上一轮计数器基线），下一轮起正常")
        return metrics, "；".join(notes) or None

    # ========================================================================
    # 取值助手
    # ========================================================================

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        """把 pysnmp 值对象安全转成 int（NoSuchInstance/None/字符串都要能兜住）"""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return int(float(str(value)))
            except (TypeError, ValueError):
                return None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """安全转 float（Counter64 可能超过 double 精度，对流量统计影响可忽略）"""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _pick_numeric(self, rows: List[Tuple[str, Any]]) -> Optional[float]:
        """
        从"多行一列"的表结果里挑一个代表值。

        取最大值的原因见模块 docstring：一块板卡/一颗 CPU 过热就要报警，
        取平均会把单点故障抹平。负数与 None 一律过滤（部分设备用 -1 表示不支持）。
        """
        values = [v for v in (self._to_float(raw) for _, raw in rows) if v is not None and v >= 0]
        if not values:
            return None
        return max(values) if MULTI_ROW_TAKE_MAX else sum(values) / len(values)


# ============================================================================
# 端口计数器缓存与速率计算（模块级，供所有采集调用共享）
# ============================================================================

# 结构：{(device_id, if_index): (采样时间戳, 入向字节, 出向字节)}
_COUNTER_CACHE: Dict[Tuple[int, str], Tuple[float, Optional[float], Optional[float]]] = {}
_COUNTER_LOCK = threading.Lock()


def _compute_rate(
    device_id: int,
    if_index: str,
    now_ts: float,
    in_octets: Optional[float],
    out_octets: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """
    用两次采样的计数器差值计算速率（Mbps）。

    公式：速率 = (本次字节 - 上次字节) × 8 / 时间差 / 1e6

    三个必须处理的边界：
      1. **首轮无基线** → 返回 (None, None)，上层不产出速率指标
         （不写 0：0 会被大屏画成"链路空闲"，而真相是"还不知道"）
      2. **64 位计数器回绕** → 差值为负时按 2^64 补偿。
         高速链路跑满几个月就可能回绕一次，不处理会出现负速率把曲线拉爆
      3. **计数器重置**（设备重启） → 差值为负且补偿后仍不合理时放弃本轮速率
         （宁可少一个点，也不要一个假的尖峰）
    """
    key = (device_id, if_index)
    prev: Optional[Tuple[float, Optional[float], Optional[float]]] = None

    with _COUNTER_LOCK:
        prev = _COUNTER_CACHE.get(key)
        _COUNTER_CACHE[key] = (now_ts, in_octets, out_octets)
        _evict_cache_if_needed()

    if prev is None or in_octets is None or out_octets is None:
        return None, None

    prev_ts, prev_in, prev_out = prev
    if prev_in is None or prev_out is None:
        return None, None

    elapsed = now_ts - prev_ts
    # 采样间隔过短（< 1 秒）会让差值噪声被放大成假尖峰（重复采集、手动测试等场景）
    if elapsed < 1:
        return None, None

    return _diff_to_mbps(prev_in, in_octets, elapsed), _diff_to_mbps(prev_out, out_octets, elapsed)


def _diff_to_mbps(prev_value: float, curr_value: float, elapsed: float) -> Optional[float]:
    """把字节差值换算成 Mbps，并处理回绕与重置"""
    delta = curr_value - prev_value
    if delta < 0:
        # 64 位无符号计数器回绕
        wrapped = delta + float(2**64)
        # 回绕后的增量若仍不合理（远大于满带宽 × 时间的上限），
        # 判定为设备重启导致计数器清零，放弃本轮
        if wrapped / elapsed / 1e6 * 8 > 1_000_000:   # 1 Tbps 上限，明显不合理
            return None
        delta = wrapped
    return round(delta * 8 / elapsed / 1e6, 3)


def _evict_cache_if_needed() -> None:
    """
    缓存容量控制。

    采用"超限即丢弃最旧一半"而非 LRU：实现简单、无额外数据结构，
    而被丢弃的只是速率计算基线，下一轮采集会自动重建（代价是多等一个采集周期）。
    """
    if len(_COUNTER_CACHE) <= _COUNTER_CACHE_MAX:
        return
    items = sorted(_COUNTER_CACHE.items(), key=lambda kv: kv[1][0])
    for old_key, _ in items[: len(items) // 2]:
        _COUNTER_CACHE.pop(old_key, None)


def _index_sort_key(index: str) -> tuple:
    """
    端口索引排序键。

    SNMP 索引是数字串（"1"、"10"），直接字符串排序会得到 1,10,11,2 这种顺序，
    导致裁剪端口时被截掉的永远是编号最大的那几个（不是我们想要的）。
    这里把纯数字索引转成整数参与排序。
    """
    try:
        return (0, int(index), "")
    except (TypeError, ValueError):
        return (1, 0, index)


def reset_counter_cache() -> None:
    """清空计数器缓存（单元测试使用）"""
    with _COUNTER_LOCK:
        _COUNTER_CACHE.clear()
