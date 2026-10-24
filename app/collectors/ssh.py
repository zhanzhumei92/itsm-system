"""
SSH 采集器（netmiko）
===============================================================================
适用设备：华为 / 华三 交换机路由器的低频信息采集、Linux 与信创主机
（麒麟 Kylin、统信 UOS、openEuler 等）

**为什么 SSH 采集周期应该比 SNMP 长**
  一次 SSH 采集要经历 TCP 握手 → SSH 密钥协商 → 认证 → 执行多条命令，
  在跨网段或设备 CPU 忙时会持续数秒。若和 SNMP 一样 60 秒一轮、
  200 台设备并发执行，对被采设备的控制平面是明显的负担
  （交换机 CPU 会被 SSH 会话本身顶上去，反而制造出假的 CPU 告警）。
  因此本采集器读取 collect.ssh_slow_interval（默认 600 秒）作为建议周期，
  由设备自身的 collect_interval 控制实际频率。

**厂商识别策略（autodetect 的实现）**
  现场新增设备时，运维往往只确定 IP 和口令，厂商是猜的（甚至填错）。
  因此当 ssh_device_type 为 autodetect 或无法确定厂商时：
      1) 先执行 `display version`，用 ParserRegistry.detect() 嗅探
      2) 未命中则再执行 `uname -a`（Linux/信创主机）
  两条探测命令都是只读的，执行失败仅回显一条错误，对设备无影响。
  识别成功后**当次沿用**，不缓存到数据库——避免因运维更正了 vendor 字段
  却因为旧缓存继续走错解析器。

**依赖缺失的处理方式（重要）**
  netmiko 采用**函数内延迟导入**而不是模块顶部导入。原因是：
  顶部导入失败会让整个 ssh.py 模块加载不了，进而"ssh"协议根本没有注册，
  用户看到的现象是"暂不支持的采集协议：ssh"——这个提示会把人带偏到
  "平台不支持 SSH"，而真相只是某个包没装上。
  延迟导入让采集器照常注册，失败时报出精确原因："netmiko 未安装"。
===============================================================================
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import BaseCollector, CollectorRegistry, CollectResult
from app.collectors.parsers import ParserRegistry
from app.core.logger import collect_logger

# 厂商标识 → netmiko device_type
# 只在用户没填 ssh_device_type（或填了 autodetect）时作为兜底推断
VENDOR_TO_DEVICE_TYPE = {
    "huawei": "huawei",
    "h3c": "hp_comware",
    "cisco": "cisco_ios",
    "linux": "linux",
    "kylin": "linux",        # 麒麟
    "uos": "linux",          # 统信
    "openeuler": "linux",
    "ubuntu": "linux",
    "centos": "linux",
    "redhat": "linux",
}

# 用于嗅探厂商的探测命令（按顺序尝试，命中即止）
DETECT_COMMANDS = ("display version", "uname -a")

# SSH 连接建立阶段的超时（秒）。握手比命令执行更容易被网络质量影响，
# 给它一个独立且略宽松的预算，避免"连接慢"被误判成"设备不可达"。
CONNECT_TIMEOUT_MIN = 5
CONNECT_TIMEOUT_MAX = 30


@CollectorRegistry.register("ssh")
class SshCollector(BaseCollector):
    """
    SSH 采集器。

    产出的指标由厂商解析器决定，通常包括：
        device_version / device_serial / device_uptime
        cpu_usage / mem_usage / mem_total / mem_used
        disk_usage / disk_total / disk_used / disk_free（Linux/信创）
        if_status:<端口> / if_bandwidth:<端口>（网络设备）
        process_count（Linux/信创）

    ⚠ 安全约定：本采集器的所有异常信息与日志里**绝不能出现口令**。
    netmiko 的异常文本可能包含连接参数，因此出错时只记录
    "IP + 异常类型 + 经过裁剪的简短原因"，不直接 dump 异常对象。
    """

    protocol = "ssh"

    metric_keys = [
        "cpu_usage", "mem_usage", "mem_total", "mem_used",
        "disk_usage", "disk_total", "disk_used", "disk_free",
        "device_version", "device_serial", "device_uptime",
        "process_count", "if_status", "if_bandwidth",
    ]

    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        ip = str(params.get("ip") or "").strip()
        if not ip:
            return self.fail("设备 IP 为空")

        username = params.get("ssh_username")
        password = params.get("ssh_password")
        key_path = params.get("ssh_key_path")
        if not username:
            return self.fail("SSH 采集缺少用户名，请在设备凭据中补全")
        if not password and not key_path:
            return self.fail("SSH 采集缺少口令或私钥，请在设备凭据中补全至少一项")

        # 延迟导入（原因见模块 docstring）
        try:
            from netmiko import ConnectHandler
            from netmiko.exceptions import (
                NetmikoAuthenticationException,
                NetmikoTimeoutException,
            )
        except ImportError as exc:
            return self.fail(
                f"依赖库未安装：netmiko（{exc}）。"
                f"请执行 pip install -r requirements.txt 后重启平台"
            )

        started = self.now_ms()
        device_timeout = int(params.get("timeout") or 10)
        connect_timeout = max(CONNECT_TIMEOUT_MIN, min(device_timeout * 2, CONNECT_TIMEOUT_MAX))

        # ---- 解析 netmiko device_type ----
        declared = str(params.get("ssh_device_type") or "autodetect").strip().lower()
        vendor = str(params.get("vendor") or "").strip().lower()
        device_type = self._resolve_device_type(declared, vendor)

        connection = None
        try:
            conn_args: Dict[str, Any] = {
                "device_type": device_type,
                "host": ip,
                "port": int(params.get("ssh_port") or 22),
                "username": str(username),
                "password": str(password or ""),
                "timeout": connect_timeout,
                # fast_cli 会走"少读一次回显"的快速路径，对华为/华三老固件
                # 与信创主机的 shell 提示符识别容易出错（表现为命令输出被截断），
                # 采集是低频任务，牺牲一点速度换稳定是划算的
                "fast_cli": False,
            }
            if key_path:
                # 私钥优先于口令：用密钥登录时哪怕口令填了也不会用到
                conn_args["use_keys"] = True
                conn_args["key_file"] = str(key_path)
            # 每台设备留一份会话日志会占磁盘，这里显式关闭
            conn_args["session_log"] = None

            connection = ConnectHandler(**conn_args)

            # ---- 厂商识别（必要时）----
            parser = ParserRegistry.get(vendor) if vendor else None
            if parser is None:
                parser, probe_note = self._detect_parser(connection, device_timeout)
                if parser is None:
                    return self.fail(
                        f"无法识别设备厂商，SSH 已连通但探测命令均无有效输出（{probe_note}）。"
                        f"请在设备信息中明确填写厂商（huawei / h3c / linux）"
                    )
                collect_logger.info(
                    "设备 %s 厂商自动识别为 %s", ip, parser.vendor
                )

            # ---- 按解析器定义的命令清单逐条采集 ----
            metrics: List[Dict[str, Any]] = []
            failed_commands: List[str] = []
            remaining = max(device_timeout, 5)
            for key, command in parser.commands.items():
                if remaining <= 0:
                    failed_commands.append(f"{command}(时间预算耗尽)")
                    break
                cmd_started = time.time()
                output = self._run_command(connection, command, int(min(remaining, device_timeout)))
                remaining -= int(time.time() - cmd_started) + 1

                if output is None:
                    failed_commands.append(command)
                    continue
                metrics.extend(parser.parse(key, output))

            duration = self.now_ms() - started

            if not metrics:
                return self.fail(
                    f"SSH 已连通（{device_type}）但未解析出任何指标。"
                    f"失败命令：{failed_commands or '无'}。"
                    f"排查建议：确认厂商填写是否正确、账号是否有查看权限",
                    duration_ms=duration,
                )

            result = self.ok(metrics, duration_ms=duration, online_status="online")
            if failed_commands:
                # 部分命令失败仍然算成功——拿到多少算多少，但要如实标注缺了哪些
                result.raw = f"部分命令未成功：{', '.join(failed_commands)}"
            return result

        except NetmikoAuthenticationException:
            return self.fail("SSH 认证失败：用户名或口令错误（也可能是账号被锁定/权限不足）",
                             duration_ms=self.now_ms() - started)
        except NetmikoTimeoutException:
            return self.fail("SSH 连接超时：网络不可达或 22 端口被防火墙拦截",
                             duration_ms=self.now_ms() - started)
        except Exception as exc:  # noqa: BLE001
            # 只取异常类型与首行文本：netmiko 的异常消息里可能回带连接参数（含口令），
            # 打印完整异常对象存在凭据泄漏风险
            brief = str(exc).splitlines()[0][:200] if str(exc) else ""
            collect_logger.warning("SSH 采集异常 | 设备ID=%s | IP=%s | %s | %s",
                                   device_id, ip, type(exc).__name__, brief)
            return self.fail(f"SSH 采集异常：{type(exc).__name__}: {brief}",
                             duration_ms=self.now_ms() - started)
        finally:
            # 无论成功失败都要断开，否则会话泄漏会很快耗尽设备侧 vty 数量
            # （华为默认只允许 5 个 vty，泄漏 5 个之后所有 SSH 都连不上）
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    # ========================================================================
    # 内部实现
    # ========================================================================

    @staticmethod
    def _resolve_device_type(declared: str, vendor: str) -> str:
        """
        决定 netmiko 的 device_type。

        优先级：用户明确指定（非 autodetect） > 按 vendor 推断 > autodetect。
        netmiko 自带的 autodetect 会逐个试驱动，耗时较长但最省心，
        因此只在完全没有线索时才用它。
        """
        if declared and declared not in ("autodetect", "auto", ""):
            return declared
        if vendor in VENDOR_TO_DEVICE_TYPE:
            return VENDOR_TO_DEVICE_TYPE[vendor]
        return "autodetect"

    def _detect_parser(self, connection: Any, timeout: int) -> Tuple[Optional[Any], str]:
        """
        用探测命令识别厂商。

        :return: (解析器实例或 None, 探测过程说明)

        探测过程说明会写进失败信息里，让运维看到"平台试了什么、设备回了什么"，
        而不是一句无用的"识别失败"。
        """
        traces: List[str] = []
        for command in DETECT_COMMANDS:
            output = self._run_command(connection, command, timeout)
            if not output:
                traces.append(f"{command}→无输出")
                continue
            parser = ParserRegistry.detect(output)
            if parser is not None:
                return parser, f"{command}→{parser.vendor}"
            traces.append(f"{command}→未能识别")
        return None, "；".join(traces)

    @staticmethod
    def _run_command(connection: Any, command: str, read_timeout: int) -> Optional[str]:
        """
        执行单条命令。

        :return: 命令输出文本；执行失败返回 None（由调用方决定是忽略还是记失败）

        为什么用 send_command 而不是 send_config_set：
        本采集器只发只读的 display/show/cat 类命令，send_command 会自动
        处理分页提示（---- More ----）与提示符匹配，不需要额外处理。
        read_timeout 兜住"命令发出去了但设备不返回"的卡死场景。
        """
        try:
            return connection.send_command(
                command,
                read_timeout=max(read_timeout, 5),
                # 不抛异常而是返回带错误标记的文本，便于上层统一判断
                strip_prompt=True,
                strip_command=True,
            )
        except Exception as exc:  # noqa: BLE001
            collect_logger.debug("SSH 命令执行失败 | %s | %s: %s",
                                 command, type(exc).__name__, str(exc)[:120])
            return None
