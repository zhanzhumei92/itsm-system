"""
演示数据生成脚本
===============================================================================
用途：让平台在**没有真实设备**的情况下也能跑出完整效果
      （大屏有数字、曲线有数据、告警中心有条目），
      便于向客户演示、也便于前端开发同学本地联调。

触发方式：
  1. .env 中设置 DEMO_MODE=true，平台启动时自动执行
  2. 手动执行：python scripts/seed_demo.py

安全：仅在设备表为空时灌数据（除非显式传 force=True），
      避免在已投产的环境里混入假设备造成误判。
===============================================================================
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 允许以 `python scripts/seed_demo.py` 方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.core.database import session_scope  # noqa: E402
from app.core.logger import system_logger  # noqa: E402
from app.core.security import encrypt_secret  # noqa: E402
from app.models.alert import AlertEvent, AlertNotifyRecord, AlertRule  # noqa: E402
from app.models.asset import AssetInfo  # noqa: E402
from app.models.device import MonDevice, MonDeviceCredential  # noqa: E402
from app.models.metric import MetricLatest, MetricSample  # noqa: E402
from app.services.alert_service import build_fingerprint  # noqa: E402

# ---------------------------------------------------------------------------
# 演示设备清单：覆盖 4 类协议 + 政企弱电项目典型设备组合
# 字段顺序：名称, IP, 类型, 厂商, 协议, 型号, 位置, 采集周期, 操作系统
# ---------------------------------------------------------------------------
DEMO_DEVICES = [
    ("核心交换机-华为", "10.10.1.1", "switch", "huawei", "snmp", "S5731-S48T4X", "总部三楼机房", 60, None),
    ("汇聚交换机-华三", "10.10.1.2", "switch", "h3c", "snmp", "S5560X-30C-EI", "总部三楼机房", 60, None),
    ("接入交换机-华为", "10.10.1.3", "switch", "huawei", "snmp", "S2720-28TP-EI", "总部二楼弱电间", 120, None),
    ("出口防火墙-深信服", "10.10.1.254", "firewall", "sangfor", "snmp", "AF-2000-B2100", "总部三楼机房", 60, None),
    ("业务服务器-Win", "10.10.2.10", "server", "windows", "winrm", "Dell R750", "总部三楼机房", 60, "Windows Server 2019"),
    ("数据库服务器-Win", "10.10.2.11", "server", "windows", "winrm", "Dell R750", "总部三楼机房", 60, "Windows Server 2022"),
    ("应用服务器-麒麟", "10.10.2.20", "server", "kylin", "ssh", "浪潮 NF5280M6", "总部三楼机房", 600, "银河麒麟 V10"),
    ("文件服务器-统信", "10.10.2.21", "server", "uos", "ssh", "浪潮 NF5280M6", "总部三楼机房", 600, "统信 UOS Server"),
    ("会议室无线AP", "10.10.3.10", "ap", "huawei", "icmp", "AP4050DN", "总部四楼会议室", 60, None),
    ("门禁控制器-海康", "10.10.3.20", "access", "hikvision", "icmp", "DS-K2604", "总部一楼大厅", 60, None),
    ("NVR存储-海康", "10.10.3.21", "storage", "hikvision", "icmp", "DS-8632N-K8", "总部一楼弱电间", 120, None),
]

# 每台设备生成的指标（键, 单位, 基准值, 波动幅度）
DEMO_METRICS = [
    ("cpu_usage", "%", 35, 25),
    ("mem_usage", "%", 58, 15),
    ("disk_usage", "%", 62, 6),
    ("device_uptime", "小时", 5200, 10),
    ("ping_latency", "ms", 12, 8),
    ("ping_loss", "%", 0, 2),
]


def _pick_metric_key(protocol: str) -> list:
    """按协议选择合理的演示指标集合（避免出现"SSH 设备采到端口流量"这类不合逻辑的数据）"""
    if protocol == "snmp":
        keys = {"cpu_usage", "mem_usage", "device_uptime", "ping_latency", "ping_loss"}
        return [m for m in DEMO_METRICS if m[0] in keys]
    if protocol == "winrm":
        keys = {"cpu_usage", "mem_usage", "disk_usage", "ping_latency", "ping_loss"}
        return [m for m in DEMO_METRICS if m[0] in keys]
    if protocol == "ssh":
        keys = {"cpu_usage", "mem_usage", "disk_usage"}
        return [m for m in DEMO_METRICS if m[0] in keys]
    # icmp：只有延迟与丢包
    return [m for m in DEMO_METRICS if m[0] in {"ping_latency", "ping_loss"}]


def seed_demo_data(force: bool = False) -> dict:
    """
    灌入演示数据。

    :param force: True 时即便已有设备也继续灌（会与现有数据混合，慎用）
    :return: 生成数据摘要
    """
    random.seed(20260921)   # 固定随机种子，保证每次演示数据一致，便于截图对比

    with session_scope() as db:
        existing = db.execute(select(func.count(MonDevice.id))).scalar_one() or 0
        if existing and not force:
            system_logger.info("设备表已有 %s 条数据，跳过演示数据生成", existing)
            return {"skipped": True, "existing_devices": existing}

        now_ts = datetime.now()
        created_devices = []

        # ---------------- 1. 设备与凭据 ----------------
        for name, ip, dtype, vendor, protocol, model, location, interval, os_type in DEMO_DEVICES:
            device = MonDevice(
                name=name, ip=ip, device_type=dtype, vendor=vendor, protocol=protocol,
                model=model, location=location, os_type=os_type,
                collect_interval=interval, timeout=10, enabled=True,
                # 演示数据里让大部分设备在线、1 台离线，这样大屏的"离线数"不为 0
                online_status="offline" if ip == "10.10.3.20" else "online",
                fail_count=5 if ip == "10.10.3.20" else 0,
                last_collect_at=now_ts, last_online_at=now_ts,
                remark="演示数据",
            )
            db.add(device)
            db.flush()
            created_devices.append(device)

            # 凭据：演示用弱口令，真实环境请务必使用强口令
            cred = MonDeviceCredential(
                device_id=device.id,
                protocol=protocol,
                snmp_version="v2c",
                snmp_community=encrypt_secret("public"),
                snmp_port=161,
                ssh_port=22,
                ssh_username="monitor",
                ssh_password_enc=encrypt_secret("Demo@12345"),
                ssh_device_type="huawei" if vendor == "huawei" else ("hp_comware" if vendor == "h3c" else "linux"),
                winrm_port=5985,
                winrm_username="administrator",
                winrm_password_enc=encrypt_secret("Demo@12345"),
                winrm_transport="ntlm",
                winrm_scheme="http",
                ping_count=5, ping_interval=1, ping_timeout=2, ping_size=56,
            )
            db.add(cred)

        # ---------------- 2. 资产台账（与设备绑定）----------------
        for idx, device in enumerate(created_devices, start=1):
            warranty_end = now_ts + timedelta(days=random.choice([-15, 20, 75, 400, 620]))
            asset = AssetInfo(
                asset_code=f"ZC-2026-{idx:04d}",
                name=device.name,
                device_type=device.device_type,
                vendor=device.vendor,
                model=device.model,
                serial_no=f"SN{random.randint(10**9, 10**10 - 1)}",
                ip=device.ip,
                mac=":".join(f"{random.randint(0, 255):02X}" for _ in range(6)),
                room_location=device.location,
                rack_position=f"A-{idx:02d}",
                online_at=now_ts - timedelta(days=random.randint(200, 900)),
                warranty_start=now_ts - timedelta(days=random.randint(300, 800)),
                warranty_end=warranty_end,
                owner=random.choice(["詹柱美", "李工", "王工", "陈工"]),
                owner_phone="18025081188",
                supplier="广东亿网通科技有限公司",
                purchase_at=now_ts - timedelta(days=random.randint(300, 800)),
                purchase_price=random.choice([12800, 28000, 56000, 86000]),
                status="in_use",
                device_id=device.id,
                remark="演示数据",
            )
            db.add(asset)

        # ---------------- 3. 历史指标（最近 2 小时，60 秒一个点）----------------
        sample_rows = []
        latest_rows = []
        for device in created_devices:
            for metric_key, unit, base, swing in _pick_metric_key(device.protocol):
                # 离线设备不生成实时指标（与大屏"离线设备无数据"的表现一致）
                if device.online_status == "offline":
                    continue
                for minutes_ago in range(120, -1, -1):
                    ts = now_ts - timedelta(minutes=minutes_ago)
                    # 简单正弦 + 随机扰动，让曲线自然（不是一条直线，演示效果更真实）
                    value = round(
                        base + swing * __import__("math").sin(minutes_ago / 18.0)
                        + random.uniform(-swing / 6, swing / 6), 2
                    )
                    value = max(0.0, value)
                    sample_rows.append({
                        "device_id": device.id, "metric_key": metric_key,
                        "metric_value": value, "str_value": None,
                        "unit": unit, "collect_time": ts,
                    })
                    if minutes_ago == 0:
                        latest_rows.append({
                            "device_id": device.id, "metric_key": metric_key,
                            "metric_value": value, "str_value": None, "unit": unit,
                            "collect_time": ts, "created_at": now_ts, "updated_at": now_ts,
                        })

        if sample_rows:
            db.bulk_insert_mappings(MetricSample, sample_rows)
        if latest_rows:
            db.bulk_insert_mappings(MetricLatest, latest_rows)

        # ---------------- 4. 演示告警事件 ----------------
        rules = db.execute(select(AlertRule)).scalars().all()
        rule_by_key = {rule.metric_key: rule for rule in rules}

        demo_alerts = [
            (created_devices[0], "cpu_usage", 92.6, "critical", "firing", 3, "核心交换机 CPU 持续过高"),
            (created_devices[4], "disk_usage", 91.2, "major", "acked", 2, "业务服务器系统盘使用率超 90%"),
            (created_devices[9], "online_status", 0.0, "critical", "firing", 1, "门禁控制器离线，无法采集"),
            (created_devices[2], "ping_latency", 128.4, "minor", "firing", 1, "接入交换机网络延迟偏高"),
            (created_devices[7], "mem_usage", 88.1, "major", "recovered", 4, "文件服务器内存已恢复"),
        ]

        for device, metric_key, value, level, status, occur, title in demo_alerts:
            rule = rule_by_key.get(metric_key)
            rule_id = rule.id if rule else 0
            first_occur = now_ts - timedelta(minutes=random.randint(5, 90))
            event = AlertEvent(
                event_no=f"AL{first_occur.strftime('%Y%m%d%H%M%S')}{random.randint(100, 999)}",
                rule_id=rule_id,
                device_id=device.id,
                device_name=device.name,
                device_ip=device.ip,
                metric_key=metric_key,
                metric_name=metric_key,
                metric_value=value,
                threshold=float(rule.threshold) if rule else 0.0,
                operator=rule.operator if rule else "gt",
                level=level,
                title=f"【{title}】",
                content=(
                    f"设备名称：{device.name}\n设备IP：{device.ip}\n"
                    f"设备类型：{device.device_type}\n位置：{device.location}\n"
                    f"监控指标：{metric_key}\n当前值：{value}\n"
                    f"告警时间：{first_occur.strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                fingerprint=build_fingerprint(device.id, rule_id),
                status=status,
                occur_count=occur,
                first_occur_at=first_occur,
                last_occur_at=now_ts - timedelta(minutes=random.randint(0, 4)),
                recover_at=(now_ts - timedelta(minutes=10)) if status == "recovered" else None,
                ack_at=(now_ts - timedelta(minutes=20)) if status == "acked" else None,
                ack_user="admin" if status == "acked" else None,
                ack_remark="已通知现场处理" if status == "acked" else None,
            )
            db.add(event)
            db.flush()

            # 通知记录：演示"邮件成功、企微成功"的正常形态
            for channel, target in (("email", "ops@enetop.com"), ("wecom", "监控告警群")):
                db.add(AlertNotifyRecord(
                    event_id=event.id, channel=channel, target=target,
                    status="success", retry_count=0,
                    response_msg="发送成功", sent_at=first_occur,
                ))

        summary = {
            "devices": len(created_devices),
            "assets": len(created_devices),
            "metric_samples": len(sample_rows),
            "metric_latest": len(latest_rows),
            "alert_events": len(demo_alerts),
        }
        system_logger.info("演示数据生成完成：%s", summary)
        return summary


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    result = seed_demo_data(force=force_flag)
    print("\n演示数据生成结果：")
    for key, value in result.items():
        print(f"  {key:16} : {value}")
    print("\n默认账号：admin / admin123    只读账号：viewer / viewer123")
