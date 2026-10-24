"""设备模型（mon_device / mon_device_credential）—— 对应模块1：设备接入与协议采集"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_type


class MonDevice(Base, TimestampMixin):
    """
    监控设备主表。

    协议（protocol）决定用哪个采集器（工厂模式分发）：
      - snmp  ：交换机/服务器通用指标（CPU、内存、端口流量、端口状态、温度）
      - ssh   ：华为/华三/Linux/信创主机低频信息（版本、接口、日志）
      - winrm ：Windows Server（CPU、内存、磁盘、进程、服务）
      - icmp  ：长 Ping 在线状态、延迟、丢包率

    采集周期支持**每台设备独立配置**（collect_interval），
    调度器每轮只挑"已到点 + 已启用"的设备，不强求所有设备同频。
    """

    __tablename__ = "mon_device"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="设备名称")
    ip: Mapped[str] = mapped_column(String(64), nullable=False, comment="管理 IP")
    device_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="switch/server/firewall/router/storage"
    )
    vendor: Mapped[Optional[str]] = mapped_column(
        String(32), comment="huawei/h3c/cisco/windows/linux/kylin/uos/other"
    )
    os_type: Mapped[Optional[str]] = mapped_column(String(64), comment="操作系统，信创区分麒麟/统信")
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, comment="snmp/ssh/winrm/icmp")
    model: Mapped[Optional[str]] = mapped_column(String(64), comment="型号")
    location: Mapped[Optional[str]] = mapped_column(String(128), comment="物理位置")
    asset_id: Mapped[Optional[int]] = mapped_column(pk_type(), comment="关联资产 ID")

    # ---------------- 采集调度相关 ----------------
    collect_interval: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False, comment="采集周期（秒），单台独立配置"
    )
    timeout: Mapped[int] = mapped_column(Integer, default=10, nullable=False, comment="采集超时（秒）")
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="是否启用采集任务"
    )

    # ---------------- 运行状态（由采集任务回写）----------------
    online_status: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False, comment="online/offline/unknown"
    )
    fail_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="连续采集失败次数，用于判定离线"
    )
    last_collect_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, comment="上次采集时间，调度器据此判断是否到点"
    )
    last_online_at: Mapped[Optional[datetime]] = mapped_column(DateTime, comment="最后在线时间")

    remark: Mapped[Optional[str]] = mapped_column(String(255), comment="备注")

    @property
    def is_online(self) -> bool:
        """是否在线（大屏统计与告警判断使用）"""
        return self.online_status == "online"


class MonDeviceCredential(Base, TimestampMixin):
    """
    设备协议凭据表：一表容纳四类协议参数，按 protocol 各取所需字段。

    为什么合并成一张表：四类协议永远是"一台设备只用一种"，
    拆成四张表会让"删除设备时清理凭据"变成四张表轮询，反而增加出错概率。

    ⚠ 安全：带 _enc 后缀的字段存 Fernet 密文，读写一律经
    app.core.security 的 encrypt_secret / decrypt_secret，
    禁止在业务代码中直接接触明文与非加密列名混用。
    """

    __tablename__ = "mon_device_credential"

    id: Mapped[int] = mapped_column(pk_type(), primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        pk_type(), nullable=False, unique=True, comment="关联设备，一台设备一套凭据"
    )
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, comment="snmp/ssh/winrm/icmp")

    # ---------------- SNMP ----------------
    snmp_version: Mapped[Optional[str]] = mapped_column(String(8), default="v2c", comment="v2c/v3")
    snmp_community: Mapped[Optional[str]] = mapped_column(String(255), comment="v2c 团体字（密文）")
    snmp_port: Mapped[Optional[int]] = mapped_column(Integer, default=161)
    snmp_username: Mapped[Optional[str]] = mapped_column(String(64), comment="v3 安全名")
    snmp_auth_proto: Mapped[Optional[str]] = mapped_column(String(16), comment="MD5/SHA/SHA256")
    snmp_auth_key_enc: Mapped[Optional[str]] = mapped_column(String(255), comment="v3 认证口令（密文）")
    snmp_priv_proto: Mapped[Optional[str]] = mapped_column(String(16), comment="DES/AES/AES256")
    snmp_priv_key_enc: Mapped[Optional[str]] = mapped_column(String(255), comment="v3 加密口令（密文）")
    snmp_timeout: Mapped[Optional[int]] = mapped_column(Integer, default=5)
    snmp_retries: Mapped[Optional[int]] = mapped_column(Integer, default=2)

    # ---------------- SSH ----------------
    ssh_port: Mapped[Optional[int]] = mapped_column(Integer, default=22)
    ssh_username: Mapped[Optional[str]] = mapped_column(String(64))
    ssh_password_enc: Mapped[Optional[str]] = mapped_column(String(255), comment="SSH 口令（密文）")
    ssh_key_path: Mapped[Optional[str]] = mapped_column(String(255), comment="私钥路径，优先于口令")
    ssh_device_type: Mapped[Optional[str]] = mapped_column(
        String(32), comment="netmiko device_type：huawei/hp_comware/linux/autodetect"
    )

    # ---------------- WinRM ----------------
    winrm_port: Mapped[Optional[int]] = mapped_column(Integer, default=5985, comment="5985 http / 5986 https")
    winrm_username: Mapped[Optional[str]] = mapped_column(String(64))
    winrm_password_enc: Mapped[Optional[str]] = mapped_column(String(255), comment="WinRM 口令（密文）")
    winrm_transport: Mapped[Optional[str]] = mapped_column(String(16), default="ntlm", comment="ntlm/basic/ssl")
    winrm_scheme: Mapped[Optional[str]] = mapped_column(String(8), default="http", comment="http/https")

    # ---------------- ICMP 长 Ping ----------------
    ping_count: Mapped[Optional[int]] = mapped_column(Integer, default=5, comment="每轮发包数")
    ping_interval: Mapped[Optional[int]] = mapped_column(Integer, default=1, comment="发包间隔（秒）")
    ping_timeout: Mapped[Optional[int]] = mapped_column(Integer, default=2, comment="单包超时（秒）")
    ping_size: Mapped[Optional[int]] = mapped_column(Integer, default=56, comment="包大小（字节）")
