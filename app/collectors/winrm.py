"""
WinRM 采集器（Windows Server / 支持 OpenSSH-WinRM 的信创主机）
===============================================================================
**技术路线选择：为什么用 PowerShell + CIM，而不是 WMI 包**
  依赖清单里原本列了 WMI（pywinrm 的兄弟库），但 WMI 包依赖 pythoncom，
  **只能在 Windows 采集机上运行**。而本平台的定位是"一台 Linux 服务器
  也能把 Windows 机器管起来"，因此统一走 WinRM + PowerShell：
      WMI 包      → 只能在 Windows 上跑，且 win32com 的调用栈难以调试
      WinRM       → 纯 Python（pywinrm），Windows 上需开启 WinRM 服务
      CIM 类      → Get-CimInstance 是 WMI 的现代化封装，字段完全一致
  结论：WMI 包不是必需依赖，实际采集不需要它（见 requirements.txt 的说明）。

**★ 中文 Windows 的输出编码是最大的坑**
  Windows 上 PowerShell 的 stdout 默认用**系统 ANSI 代码页**编码，
  中文环境就是 GBK/CP936。但 pywinrm 拿到的是**字节流**，
  若直接 `.decode("utf-8")` 会抛 UnicodeDecodeError 或得到乱码
  （典型症状：device_version 显示成 "Microsoft Windows Server 2019 ���׼��"）。
  两层防御：
    1. 脚本开头把 [Console]::OutputEncoding 设为 UTF-8（能生效时最干净）
    2. 解码用级联回退 utf-8 → gbk → utf-16-le → latin-1（兜住设置不生效的情况）
  另外，静态输出项的键名全部用 ASCII、机器名/服务名本身也是 ASCII，
  即使编码解码失败也只会影响"操作系统名称"这一个指标，不会污染数值指标。

**采集项与 PowerShell 命令的对应**
  一次 run_ps 取回全部指标（只建立一次 WinRM 会话，省掉 6~7 次往返）：
      OS_CAPTION      Win32_OperatingSystem.Caption      → device_version
      UPTIME_SECONDS  Now - LastBootUpTime               → device_uptime
      MEM_TOTAL_KB    TotalVisibleMemorySize             → mem_total（KB→MB）
      MEM_FREE_KB     FreePhysicalMemory                 → 反算 mem_used
      CPU_PERCENT     Win32_Processor.LoadPercentage 均值 → cpu_usage
      PROCESS_COUNT   (Get-Process).Count                → process_count
      DISK            本地固定磁盘（DriveType=3）逐盘        → disk_*
      SERVICE         自动启动但未运行的服务               → service_status

  CPU 取**所有逻辑处理器负载的平均值**（Measure-Object -Average）：
  与 Linux 解析器取 /proc/stat 总体的口径一致，
  避免"16 核里 1 核打满"被报成 100%。
===============================================================================
"""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

from app.collectors.base import BaseCollector, CollectorRegistry, CollectResult
from app.core.logger import collect_logger

# 单次 WinRM 采集最多上报多少个"自动启动但未运行"的服务。
# 为什么设上限：一台装了 SQL Server + IIS 的机器可能同时有十几个服务异常，
# 全量上报会把指标表刷满、也会让"服务异常"这一条告警被淹没。
# 只报前 10 个，并按其重要性（名称排序）稳定输出，便于对比历史。
MAX_SERVICE_REPORTS = 10

# 解码级联顺序：UTF-8 优先（脚本已设 OutputEncoding），
# 其余为兜底，最后 latin-1 保证"任何字节序列都能解码成功"，绝不抛异常
_DECODE_CHAIN = ("utf-8", "gbk", "gb18030", "utf-16-le", "latin-1")


@CollectorRegistry.register("winrm")
class WinrmCollector(BaseCollector):
    """
    WinRM 采集器。

    产出的指标：
        cpu_usage / mem_usage / mem_total / mem_used
        disk_usage / disk_total / disk_used / disk_free
        device_version / device_uptime / process_count
        service_status:<服务名>（仅上报"自动启动但未运行"的服务）

    前提条件（部署时需在目标 Windows 上执行一次）：
        Enable-PSRemoting -Force
        # 工作组环境还需信任目标主机：
        Set-Item WSMan:\\localhost\\Client\\TrustedHosts -Value "<采集机IP>" -Force
    """

    protocol = "winrm"

    metric_keys = [
        "cpu_usage", "mem_usage", "mem_total", "mem_used",
        "disk_usage", "disk_total", "disk_used", "disk_free",
        "device_version", "device_uptime", "process_count", "service_status",
    ]

    def collect(self, device_id: int, params: Dict[str, Any]) -> CollectResult:
        ip = str(params.get("ip") or "").strip()
        if not ip:
            return self.fail("设备 IP 为空")

        username = params.get("winrm_username")
        password = params.get("winrm_password")
        if not username or not password:
            return self.fail("WinRM 采集缺少用户名或口令，请在设备凭据中补全")

        # 延迟导入：理由见 ssh.py 的模块 docstring（保证协议照常注册、报错精确）
        try:
            import winrm
            from winrm.exceptions import (
                InvalidCredentialsError,
                WinRMOperationTimeoutError,
                WinRMTransportError,
            )
        except ImportError as exc:
            return self.fail(
                f"依赖库未安装：pywinrm（{exc}）。请执行 pip install -r requirements.txt 后重启平台"
            )

        started = self.now_ms()
        device_timeout = int(params.get("timeout") or 10)
        scheme = str(params.get("winrm_scheme") or "http").lower()
        port = int(params.get("winrm_port") or (5986 if scheme == "https" else 5985))
        transport = str(params.get("winrm_transport") or "ntlm").lower()
        endpoint = f"{scheme}://{ip}:{port}/wsman"

        # 操作超时给两倍预算：WinRM 的建连（尤其是 https + NTLM 协商）
        # 比命令执行本身慢，两者共用一个偏紧的超时会误报超时
        session_kwargs: Dict[str, Any] = {
            "transport": transport,
            "operation_timeout_sec": max(device_timeout, 5),
            "read_timeout_sec": max(device_timeout * 2, 15),
        }
        if scheme == "https":
            # 内网 Windows 服务器几乎都用自签名证书，证书校验必然失败。
            # 此处忽略校验是行业常规做法；若客户有合规要求，
            # 应改为在采集机上导入该证书并去掉这一行（见部署文档"安全加固"章节）。
            session_kwargs["server_cert_validation"] = "ignore"

        try:
            session = winrm.Session(endpoint, auth=(str(username), str(password)),
                                    **session_kwargs)
            result = session.run_ps(self._build_script())

            stdout = self._decode(result.std_out)
            stderr = self._decode(result.std_err)

            if result.status_code != 0 and not stdout.strip():
                # 有 stderr 无 stdout：通常是脚本语法问题或权限不足
                return self.fail(
                    f"WinRM 命令执行失败（退出码 {result.status_code}）：{self._brief(stderr)}",
                    duration_ms=self.now_ms() - started,
                )

            metrics, warnings = self._parse_script_output(stdout)
            duration = self.now_ms() - started

            if not metrics:
                return self.fail(
                    f"WinRM 已连通但未取到任何指标。原始输出片段：{self._brief(stdout)}。"
                    f"排查建议：确认账号属于 Administrators 组、且 WinRM 服务已启用",
                    duration_ms=duration,
                )

            out = self.ok(metrics, duration_ms=duration, online_status="online")
            if warnings or stderr.strip():
                notes = warnings + ([f"stderr: {self._brief(stderr)}"] if stderr.strip() else [])
                out.raw = "；".join(notes)
            return out

        except InvalidCredentialsError:
            return self.fail("WinRM 认证失败：用户名或口令错误",
                             duration_ms=self.now_ms() - started)
        except WinRMOperationTimeoutError:
            return self.fail(
                "WinRM 操作超时：目标可达但响应过慢。"
                "排查建议：检查目标机 CPU/内存是否过载，或调大设备采集超时",
                duration_ms=self.now_ms() - started,
            )
        except WinRMTransportError as exc:
            return self.fail(
                f"WinRM 传输层错误：{self._brief(str(exc))}。"
                f"排查建议：1) 目标机执行 Enable-PSRemoting -Force "
                f"2) 防火墙放通 {(5986 if scheme == 'https' else 5985)} 端口 "
                f"3) 工作组环境需配置 TrustedHosts",
                duration_ms=self.now_ms() - started,
            )
        except Exception as exc:  # noqa: BLE001
            # 同 ssh.py：只取类型与首行，避免异常文本里回带含口令的连接参数
            brief = str(exc).splitlines()[0][:200] if str(exc) else ""
            collect_logger.warning("WinRM 采集异常 | 设备ID=%s | IP=%s | %s | %s",
                                   device_id, ip, type(exc).__name__, brief)
            return self.fail(f"WinRM 采集异常：{type(exc).__name__}: {brief}",
                             duration_ms=self.now_ms() - started)

    # ========================================================================
    # PowerShell 脚本
    # ========================================================================

    @staticmethod
    def _build_script() -> str:
        """
        构造采集脚本。

        三个工程要点：
          1. `$ErrorActionPreference='SilentlyContinue'` —— 单条命令失败
             （如某台机器没有 WMI 计数器权限）不应中断整个脚本，
             否则会一条不懂的项把全部指标带崩。
          2. 输出格式固定为 `KEY=VALUE` 单行，磁盘与服务用 `|` 分隔多字段。
             刻意不用 JSON：PowerShell 的 ConvertTo-Json 在不同版本里
             对管道对象、单元素数组的处理不一致（单元素会被序列化成对象而非数组），
             解析代码要为版本差异写一堆兼容；KEY=VALUE 是版本无关的。
          3. 所有数值用 [math]::Round 或 [int] 归一化，避免输出
             "1.2345678901234E+15" 这类科学计数法给解析添麻烦。
        """
        return r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$os = Get-CimInstance Win32_OperatingSystem
if ($os) {
    "OS_CAPTION=" + $os.Caption
    "OS_VERSION=" + $os.Version
    "MEM_TOTAL_KB=" + [int64]$os.TotalVisibleMemorySize
    "MEM_FREE_KB=" + [int64]$os.FreePhysicalMemory
    try {
        $up = [int64]((Get-Date) - $os.LastBootUpTime).TotalSeconds
        "UPTIME_SECONDS=" + $up
    } catch {}
}

$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
if ($null -ne $cpu) { "CPU_PERCENT=" + [math]::Round([double]$cpu, 2) }

$proc = @(Get-Process)
if ($proc.Count -gt 0) { "PROCESS_COUNT=" + $proc.Count }

Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    if ($null -ne $_.Size) { "DISK=" + $_.DeviceID + "|" + [int64]$_.Size + "|" + [int64]$_.FreeSpace }
}

# 只报"自动启动但当前没运行"的服务 —— 这正是运维需要知道的异常
Get-Service | Where-Object { $_.StartType -eq 'Automatic' -and $_.Status -ne 'Running' } |
    Sort-Object Name | Select-Object -First """ + str(MAX_SERVICE_REPORTS) + r""" | ForEach-Object {
    "SERVICE=" + $_.Name + "|" + $_.Status
}
"""

    # ========================================================================
    # 输出解码与解析
    # ========================================================================

    @staticmethod
    def _decode(raw: Any) -> str:
        """
        把 WinRM 返回的字节流解码成文本（见模块 docstring 的编码说明）。

        先做一次"是不是 base64 的 UTF-16LE"的判断：个别 pywinrm 版本
        在特定 transport 下会把输出按原始编码返回，导致出现 base64 串。
        """
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        data: bytes = bytes(raw)
        if not data:
            return ""

        # 探测 pywinrm 偶发的 base64(UTF-16LE) 形态：全是 base64 字符且长度合法
        try:
            head = data[:80].strip()
            if head and all(c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
                            for c in head):
                decoded = base64.b64decode(data, validate=True)
                return decoded.decode("utf-16-le", errors="replace")
        except Exception:  # noqa: BLE001 - 只是探测，失败就按普通字节处理
            pass

        for encoding in _DECODE_CHAIN:
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("latin-1", errors="replace")   # 理论不可达，防御性兜底

    @staticmethod
    def _brief(text: str, limit: int = 200) -> str:
        """压缩多行错误信息为单行摘要，避免日志被堆栈刷屏"""
        return " ".join((text or "").split())[:limit] or "(空)"

    def _parse_script_output(self, stdout: str) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        解析 KEY=VALUE 脚本输出。

        :return: (指标列表, 警告列表)
        """
        metrics: List[Dict[str, Any]] = []
        warnings: List[str] = []

        values: Dict[str, str] = {}
        disks: List[Tuple[str, int, int]] = []
        services: List[Tuple[str, str]] = []

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            key = key.strip().upper()
            raw_value = raw_value.strip()

            if key == "DISK":
                parsed = self._split_pipe(raw_value, 3)
                if parsed and parsed[1].isdigit() and parsed[2].isdigit():
                    disks.append((parsed[0], int(parsed[1]), int(parsed[2])))
            elif key == "SERVICE":
                parsed = self._split_pipe(raw_value, 2)
                if parsed:
                    services.append((parsed[0], parsed[1]))
            else:
                values[key] = raw_value

        # ---- CPU ----
        cpu = self._to_float(values.get("CPU_PERCENT"))
        if cpu is not None:
            metrics.append(self.with_unit("cpu_usage", round(min(max(cpu, 0.0), 100.0), 2), "%"))

        # ---- 内存 ----
        # Windows 的 TotalVisibleMemorySize / FreePhysicalMemory 单位都是 KB
        total_kb = self._to_float(values.get("MEM_TOTAL_KB"))
        free_kb = self._to_float(values.get("MEM_FREE_KB"))
        if total_kb and total_kb > 0:
            metrics.append(self.with_unit("mem_total", round(total_kb / 1024, 2), "MB"))
            if free_kb is not None:
                used_kb = max(total_kb - free_kb, 0.0)
                metrics.append(self.with_unit("mem_used", round(used_kb / 1024, 2), "MB"))
                metrics.append(self.with_unit("mem_usage", round(used_kb / total_kb * 100, 2), "%"))

        # ---- 磁盘 ----
        # 聚合所有本地固定磁盘（DriveType=3 已排除光驱/可移动盘/网络盘）
        if disks:
            total_bytes = sum(d[1] for d in disks)
            free_bytes = sum(d[2] for d in disks)
            if total_bytes > 0:
                to_gb = lambda b: round(b / 1024 / 1024 / 1024, 2)   # noqa: E731
                metrics.append(self.with_unit("disk_total", to_gb(total_bytes), "GB"))
                metrics.append(self.with_unit("disk_used", to_gb(total_bytes - free_bytes), "GB"))
                metrics.append(self.with_unit("disk_free", to_gb(free_bytes), "GB"))
                metrics.append(self.with_unit(
                    "disk_usage", round((total_bytes - free_bytes) / total_bytes * 100, 2), "%"
                ))

        # ---- 运行时长 ----
        seconds = self._to_float(values.get("UPTIME_SECONDS"))
        if seconds is not None:
            metrics.append(self.with_unit("device_uptime", round(seconds / 3600, 2), "小时"))

        # ---- 进程数 ----
        count = self._to_float(values.get("PROCESS_COUNT"))
        if count is not None:
            metrics.append(self.with_unit("process_count", int(count), "个"))

        # ---- 版本 ----
        caption = values.get("OS_CAPTION", "").strip()
        version = values.get("OS_VERSION", "").strip()
        if caption:
            text = f"{caption} ({version})" if version else caption
            metrics.append(self.with_unit("device_version", None, None, text))

        # ---- 服务状态 ----
        for name, status in services[:MAX_SERVICE_REPORTS]:
            # 状态值统一成英文小写（running/stopped），与其它模块的判断口径一致，
            # 不把中文枚举写进指标值，避免前端要多语言映射
            metrics.append(self.with_unit(
                f"service_status:{name}", None, None, (status or "").strip().lower()
            ))
        if services:
            warnings.append(f"发现 {len(services)} 个自动启动但未运行的服务")

        return metrics, warnings

    @staticmethod
    def _split_pipe(value: str, expected: int) -> Optional[List[str]]:
        """按 | 切分并校验段数（段数不符说明该行被截断，直接丢弃）"""
        parts = [p.strip() for p in (value or "").split("|")]
        return parts if len(parts) == expected else None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """安全转 float；PowerShell 偶尔会输出空串或 '-'"""
        if value is None:
            return None
        text = str(value).strip()
        if not text or text in ("-", "N/A"):
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
