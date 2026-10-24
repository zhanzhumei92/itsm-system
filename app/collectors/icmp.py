"""
ICMP 长 Ping 采集器
===============================================================================
职责：设备在线状态、时延、丢包率、抖动。这是唯一"不依赖设备开放任何服务"的
探测手段，因此承担整个平台的**存活判定底线**——SNMP 团体字配错、SSH 端口被墙时，
ICMP 仍然能告诉你"这台设备到底活没活"。

★★★ 关于 pythonping 的一个重要事实纠正 ★★★
    pythonping 走的是 **原始套接字（SOCK_RAW）**，在 Linux/macOS 上需要
    root 或 CAP_NET_RAW 能力，在 Windows 上需要管理员权限。
    这不是"纯 Python 就不用权限"——绕过内核协议栈自己构造 ICMP 包，
    权限是内核强制的，任何纯 Python 实现都一样。
    （早期依赖清单里"无需 root 权限"的注释是错的，已在 requirements.txt 更正。）

**因此本采集器实现两级探测，自动降级：**

    第一级 pythonping       精度高（逐包 RTT、抖动标准差），需特权
        ↓ 遇到 PermissionError
    第二级 系统 ping 命令    借用系统 ping 的 setuid 能力，普通用户即可运行
        （Linux/macOS 的 ping 通常带 setuid 位，Windows 的 ping 也无特权要求）

    两级都不可用 → 明确报错并给出处置建议，而不是含糊的"采集失败"。

**为什么降级要有"记忆"**
  权限问题在整台机器上是一致的：第一台设备报 PermissionError，
  后面 199 台必然也报。若每台都先试一次 pythonping，就是 200 次无谓的
  异常构造与栈展开。故用模块级开关 _RAW_ICMP_DENIED 记住结论，
  后续设备直接走系统 ping（并且把首次原因写成一条 WARNING 日志，便于运维发现）。
===============================================================================
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import BaseCollector, CollectorRegistry, CollectResult
from app.core.logger import collect_logger

# 平台判定（模块加载时算一次：采集是高频调用，避免每次重复探测）
_IS_WINDOWS = platform.system().lower().startswith("win")
_IS_DARWIN = platform.system().lower() == "darwin"

# 原始套接字 ICMP 是否已被判定为"权限不足"（进程级记忆，见模块 docstring）
_RAW_ICMP_DENIED = False
_RAW_LOCK = threading.Lock()

# 系统 ping 命令的执行超时预算（秒）。
# 取值逻辑：发包数 × (间隔 + 单包超时) 是设备侧的理想耗时，
# 命令级超时在此之上再留 3 秒给进程启动与输出解析。
_MIN_CMD_TIMEOUT = 5
_CMD_TIMEOUT_MARGIN = 3


@CollectorRegistry.register("icmp")
class IcmpCollector(BaseCollector):
    """
    ICMP 长 Ping 采集器。

    产出的指标：
        online_status   online / offline（离线告警专用键）
        ping_latency    平均往返时延 ms
        ping_max        最大往返时延 ms
        ping_jitter     抖动 ms（逐包 RTT 的标准差）
        ping_loss       丢包率 %

    设计要点：**设备不可达算"采集成功"**。
    我们成功探测到了"它离线"这个事实，离线由 online_status 表达、
    由告警引擎处理；若记成采集失败，collect_log 里就无法区分
    "网络不通"与"采集器配置错了"，排障时两眼一抹黑。
    """

    protocol = "icmp"

    metric_keys = ["online_status", "ping_latency", "ping_max", "ping_jitter", "ping_loss"]

    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        ip = str(params.get("ip") or "").strip()
        if not ip:
            return self.fail("设备 IP 为空，无法执行 Ping 探测")

        count = self._clamp_int(params.get("ping_count"), 5, 1, 20)
        interval = self._clamp_float(params.get("ping_interval"), 1.0, 0.2, 10.0)
        timeout = self._clamp_float(params.get("ping_timeout"), 2.0, 1.0, 10.0)
        size = self._clamp_int(params.get("ping_size"), 56, 8, 1400)

        started = self.now_ms()

        # ---- 第一级：pythonping（有特权时精度最高）----
        if not _RAW_ICMP_DENIED:
            stats, err = self._ping_by_pythonping(ip, count, interval, timeout, size)
            if stats is not None:
                duration = self.now_ms() - started
                return self._build_result(stats, duration, mode="pythonping")
            if err and "权限" in err:
                self._mark_raw_denied(err)
                # 不 return：立刻降级到系统 ping
            elif err:
                # 非权限类错误（如地址解析失败）→ 没必要再试系统 ping 的第二遍，
                # 但系统 ping 的错误信息更友好，仍试一次以给出更好的提示
                pass

        # ---- 第二级：系统 ping 命令 ----
        stats, err = self._ping_by_system_command(ip, count, interval, timeout, size)
        duration = self.now_ms() - started
        if stats is not None:
            return self._build_result(stats, duration, mode="system_ping")

        return self.fail(
            f"ICMP 探测失败：{err or '未知原因'}。排查建议："
            f"1) 目标 IP 是否可达（在采集机上手动执行 ping {ip}）"
            f"2) 防火墙是否放通 ICMP 3) 若提示权限不足，请以管理员/root 身份运行平台进程",
            duration_ms=duration,
        )

    # ========================================================================
    # 第一级：pythonping
    # ========================================================================

    def _ping_by_pythonping(
        self, ip: str, count: int, interval: float, timeout: float, size: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        用 pythonping 执行探测。

        :return: (统计字典, 错误描述)。成功时错误的 None；失败时统计为 None。

        注意 pythonping 的 timeout 是**单包**超时秒数，interval 是包间隔秒数。
        它内部会自己 sleep，因此本方法的总耗时约为 count × interval，
        对于 count=5 / interval=1 就是 5 秒左右——这也是默认采集超时取 10 秒的原因。
        """
        try:
            from pythonping import ping as _ping

            response = _ping(ip, timeout=timeout, count=count, size=size, interval=interval)

            received = count - int(getattr(response, "stats_packets_lost", 0) or 0)
            loss = round(float(getattr(response, "stats_lost_ratio", 0) or 0) * 100, 2)

            if received <= 0:
                # 全丢包 = 设备离线。这仍然是"采集成功"（见类 docstring）
                return {"online": False, "loss": 100.0, "avg": None,
                        "max": None, "jitter": None, "sent": count, "recv": 0}, None

            return {
                "online": True,
                "loss": loss,
                "avg": self._round_or_none(getattr(response, "rtt_avg_ms", None)),
                "max": self._round_or_none(getattr(response, "rtt_max_ms", None)),
                "jitter": self._stddev_rtts(response),
                "sent": count,
                "recv": received,
            }, None

        except PermissionError:
            return None, "权限不足：原始套接字 ICMP 需要 root/管理员权限"
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _stddev_rtts(response: Any) -> Optional[float]:
        """
        由逐包 RTT 计算抖动（标准差）。

        为什么用标准差而不是"最大-最小"：极差对单个离群点过于敏感，
        一次 200ms 的偶发抖动就能让极差爆表；标准差反映的是"整体稳不稳"，
        更适合作为链路质量的长期观察指标。
        """
        try:
            rtts = [p.time_elapsed_ms for p in response if getattr(p, "success", False)]
            if len(rtts) < 2:
                return None
            mean = sum(rtts) / len(rtts)
            variance = sum((t - mean) ** 2 for t in rtts) / (len(rtts) - 1)
            return round(variance ** 0.5, 2)
        except Exception:  # noqa: BLE001
            return None

    # ========================================================================
    # 第二级：系统 ping 命令
    # ========================================================================

    def _ping_by_system_command(
        self, ip: str, count: int, interval: float, timeout: float, size: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        调用操作系统自带的 ping 命令并解析其统计输出。

        为什么这样做是安全的：参数全部由本方法按类型构造（count/size 已 clamp 为整数，
        ip 经正则白名单校验），且使用参数列表方式调用（shell=False），
        不存在命令注入面。
        """
        if not re.fullmatch(r"[0-9A-Fa-f:.]{3,64}", ip):
            return None, f"IP 地址格式不合法：{ip}"

        cmd = self._build_ping_command(ip, count, interval, timeout, size)
        budget = max(_MIN_CMD_TIMEOUT, int(count * (interval + timeout)) + _CMD_TIMEOUT_MARGIN)

        try:
            proc = subprocess.run(  # noqa: S603 - 参数列表调用，无 shell
                cmd,
                capture_output=True,
                text=True,
                timeout=budget,
                errors="replace",
                encoding=None,          # 交给系统默认编码：Windows 中文环境下输出可能是 GBK
            )
        except subprocess.TimeoutExpired:
            return None, f"系统 ping 命令执行超时（超过 {budget} 秒）"
        except FileNotFoundError:
            return None, "采集机上找不到 ping 命令，请确认系统已安装 iputils/iputils-ping"
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return self._parse_system_ping(output, count)

    @staticmethod
    def _build_ping_command(
        ip: str, count: int, interval: float, timeout: float, size: int
    ) -> List[str]:
        """
        按平台构造 ping 命令。

        平台差异（这是跨平台实现里最容易踩的坑，逐个说明）：
          Linux   -W 单位是**秒**（可小数），-i 间隔秒，-s 包大小
          macOS   -W 单位是**毫秒**，-i 间隔秒，-s 包大小；且 macOS 的 -W 只控制
                  "等待首个响应"的时长，语义与 Linux 略有不同，但足够用
          Windows -n 次数，-w 单包超时**毫秒**，-l 包大小（注意不是 -s）
                  Windows 的 ping 不支持自定义发包间隔，故忽略 interval

        另外 Windows 下加了 -4：内网设备多为 IPv4，避免在双栈机器上
        先用 IPv6 探测再回退造成的额外延迟。
        """
        if _IS_WINDOWS:
            return ["ping", "-4", "-n", str(count), "-w", str(int(timeout * 1000)),
                    "-l", str(size), ip]
        if _IS_DARWIN:
            # macOS: -W 毫秒，-i 秒（0.2 及以下需要 root，这里已经 clamp 到最小 0.2）
            return ["ping", "-c", str(count), "-W", str(int(timeout * 1000)),
                    "-i", f"{interval:g}", "-s", str(size), ip]
        # Linux 及其它 POSIX
        return ["ping", "-c", str(count), "-W", f"{timeout:g}",
                "-i", f"{interval:g}", "-s", str(size), ip]

    def _parse_system_ping(self, output: str, count: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        解析 ping 命令输出。

        需要同时兼容英文与**中文 Windows** 的输出（政企现场大量中文 Windows）：
          英文 Linux/macOS : 3 packets transmitted, 3 received, 0% packet loss
                             rtt min/avg/max/mdev = 0.081/0.139/0.169/0.041 ms
                             round-trip min/avg/max/stddev = 0.081/0.139/0.169/0.041 ms
          英文 Windows     : Packets: Sent = 4, Received = 4, Lost = 0 (0% loss)
                             Minimum = 0ms, Maximum = 0ms, Average = 0ms
          中文 Windows     : 数据包: 已发送 = 4，已接收 = 4，丢失 = 0 (0% 丢失)
                             最短 = 0ms，最长 = 0ms，平均 = 0ms

        丢包率的优先级链：显式百分比 → 由 发送/接收 推算 → 由 RTT 行数推算。
        多级兜底是必要的：不同发行版的 ping 输出裁剪程度不同，
        少一个数字不应该让整次探测判为失败。
        """
        text = output or ""

        # ---- 丢包率 ----
        loss: Optional[float] = None
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet\s+loss|丢失|丢包)", text, re.IGNORECASE)
        if m:
            loss = float(m.group(1))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*loss", text, re.IGNORECASE)
            if m:
                loss = float(m.group(1))

        sent: Optional[int] = None
        recv: Optional[int] = None
        m = re.search(r"(\d+)\s*packets?\s+transmitted.*?(\d+)\s*(?:packets?\s+)?received",
                      text, re.IGNORECASE | re.DOTALL)
        if m:
            sent, recv = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"(?:已发送|Sent)\s*=?\s*(\d+).*?(?:已接收|Received)\s*=?\s*(\d+)",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                sent, recv = int(m.group(1)), int(m.group(2))

        if loss is None and sent:
            recv = recv if recv is not None else sent
            loss = round((sent - recv) / sent * 100, 2)

        # ---- RTT：最小/平均/最大（+ 抖动）----
        avg = mx = mn = jitter = None
        m = re.search(r"(?:rtt|round-trip)\s+min/avg/max/\w+\s*=\s*"
                      r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", text, re.IGNORECASE)
        if m:
            mn, avg, mx, jitter = (float(m.group(1)), float(m.group(2)),
                                   float(m.group(3)), float(m.group(4)))
        else:
            m = re.search(r"(?:最短|Minimum)\s*=?\s*([\d.]+)\s*ms.*?"
                          r"(?:最长|Maximum)\s*=?\s*([\d.]+)\s*ms.*?"
                          r"(?:平均|Average)\s*=?\s*([\d.]+)\s*ms",
                          text, re.IGNORECASE | re.DOTALL)
            if m:
                mn, mx, avg = float(m.group(1)), float(m.group(2)), float(m.group(3))

        # 兜底：从逐包结果行里数响应个数并取 RTT
        if avg is None:
            per_packet = re.findall(r"time[=<]\s*([\d.]+)\s*ms", text, re.IGNORECASE)
            if not per_packet:
                per_packet = re.findall(r"时间[=<]\s*([\d.]+)\s*ms", text, re.IGNORECASE)
            if per_packet:
                values = [float(v) for v in per_packet]
                avg, mx = sum(values) / len(values), max(values)
                if len(values) >= 2:
                    mean = avg
                    jitter = round((sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5, 2)

        if loss is None and avg is None:
            # 既没丢包统计也没 RTT —— 输出完全不认识。
            # 把前 200 字符回带出去，让排障者能看到设备/系统到底输出了什么
            return None, f"无法解析 ping 输出：{text.strip()[:200]}"

        online = bool(recv) or (loss is not None and loss < 100) or (avg is not None)
        return {
            "online": online,
            "loss": round(loss, 2) if loss is not None else (0.0 if online else 100.0),
            "avg": self._round_or_none(avg),
            "max": self._round_or_none(mx),
            "jitter": self._round_or_none(jitter),
            "sent": sent or count,
            "recv": recv if recv is not None else (count if online else 0),
            "min": self._round_or_none(mn),
        }, None

    # ========================================================================
    # 结果构造
    # ========================================================================

    def _build_result(self, stats: Dict[str, Any], duration_ms: int, mode: str) -> CollectResult:
        """
        由统计数据构造采集结果。

        注意 ping_loss = 100 时 online_status 记为 offline，但整体仍是 success=True——
        见类 docstring 的解释。
        """
        online = bool(stats.get("online"))
        metrics: List[Dict[str, Any]] = [
            self.with_unit("online_status", None, None, "online" if online else "offline"),
            self.with_unit("ping_loss", stats.get("loss"), "%"),
        ]
        # 只在有响应时上报时延：0.0 会被大屏画成"极快"，而真相是"没测到"
        if online:
            metrics.append(self.with_unit("ping_latency", stats.get("avg"), "ms"))
            if stats.get("max") is not None:
                metrics.append(self.with_unit("ping_max", stats.get("max"), "ms"))
            if stats.get("jitter") is not None:
                metrics.append(self.with_unit("ping_jitter", stats.get("jitter"), "ms"))

        result = CollectResult(
            success=True,
            metrics=metrics,
            online_status="online" if online else "offline",
            duration_ms=duration_ms,
        )
        # 把探测方式与收发计数写进 raw，便于在"测试连接"里向运维展示细节。
        # 时延为 None 时显示"无响应"而不是"None ms"——界面上少一处让人犯嘀咕的噪音
        avg_text = f"{stats.get('avg')}ms" if stats.get("avg") is not None else "无响应"
        result.raw = (
            f"探测方式={mode} 发送={stats.get('sent')} 接收={stats.get('recv')} "
            f"丢包={stats.get('loss')}% 平均={avg_text}"
        )
        return result

    # ========================================================================
    # 助手
    # ========================================================================

    @staticmethod
    def _mark_raw_denied(reason: str) -> None:
        """记录"原始套接字不可用"，后续设备直接走系统 ping（见模块 docstring）"""
        global _RAW_ICMP_DENIED
        with _RAW_LOCK:
            if _RAW_ICMP_DENIED:
                return
            _RAW_ICMP_DENIED = True
        collect_logger.warning(
            "ICMP 高精度探测不可用（%s），已自动降级为调用系统 ping 命令。"
            "如需逐包 RTT 精度，请以 root（Linux）或管理员（Windows）身份运行平台。",
            reason,
        )

    @staticmethod
    def _round_or_none(value: Any) -> Optional[float]:
        """安全四舍五入到两位小数；None 原样返回（不要变成 0）"""
        try:
            return None if value is None else round(float(value), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
        """整数参数收敛到合理区间：现场误填（如 count=10000）不该打爆设备"""
        try:
            return max(low, min(int(value), high))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
        """浮点参数收敛到合理区间"""
        try:
            return max(low, min(float(value), high))
        except (TypeError, ValueError):
            return default


def raw_icmp_available() -> bool:
    """
    当前进程能否使用原始套接字 ICMP。

    供"系统状态"接口展示，让运维在页面上就能看到
    "当前是降级模式还是高精度模式"，不用去翻日志。
    """
    if _RAW_ICMP_DENIED:
        return False
    if not hasattr(os, "geteuid"):
        return True          # Windows：无法探测，按可用处理（首次调用会自动降级）
    return os.geteuid() == 0


def reset_permission_cache() -> None:
    """重置权限记忆（单元测试使用）"""
    global _RAW_ICMP_DENIED
    with _RAW_LOCK:
        _RAW_ICMP_DENIED = False
