"""
设备服务（模块1：设备接入、协议参数配置、采集任务启停）
===============================================================================
关键设计：

1. **凭据加密读写只在此模块发生**
   路由层、采集层拿到的都是"已解密"的对象（采集层通过 get_credential_for_collect 获取），
   严禁其它模块直接读写 *_enc 字段，避免某处漏加密导致明文落库。

2. **凭据更新采用"留空即不变"语义**
   前端编辑设备时不会回显原口令（安全要求），若把空字符串当新值写入，
   会出现"改个设备名把 SSH 密码清空"的事故。因此：
   敏感字段传 None 或空串 → 保留数据库中已有密文；只有传了新值才覆盖。

3. **删除设备是级联操作**
   因不使用物理外键，必须显式清理：凭据、最新指标、规则状态、维保提醒绑定。
   历史采样 metric_sample 与各类日志**保留**（审计需要），仅解除关联。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import BizException, DuplicateException, NotFoundException
from app.core.logger import system_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.device import MonDevice, MonDeviceCredential
from app.models.log import SysLog
from app.models.metric import MetricLatest
from app.models.alert import AlertRule, AlertRuleState
from app.models.asset import AssetInfo
from app.schemas.device import (
    CredentialIn,
    CredentialOut,
    CredentialPatchIn,
    DeviceCreate,
    DeviceDetailOut,
    DeviceUpdate,
    TestConnectionResult,
)

# 需要加密存储的凭据字段（新增字段时只需在此登记，加密逻辑自动覆盖）
SECRET_FIELDS = (
    "snmp_community",
    "snmp_auth_key",
    "snmp_priv_key",
    "ssh_password",
    "winrm_password",
)


# ============================================================================
# 查询
# ============================================================================

def list_devices(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    keyword: Optional[str] = None,
    protocol: Optional[str] = None,
    device_type: Optional[str] = None,
    online_status: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Tuple[List[MonDevice], int]:
    """分页 + 多条件筛选设备列表"""
    stmt = select(MonDevice)
    count_stmt = select(func.count(MonDevice.id))

    conditions = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            MonDevice.name.like(pattern),
            MonDevice.ip.like(pattern),
            MonDevice.location.like(pattern),
        ))
    if protocol:
        conditions.append(MonDevice.protocol == protocol)
    if device_type:
        conditions.append(MonDevice.device_type == device_type)
    if online_status:
        conditions.append(MonDevice.online_status == online_status)
    if enabled is not None:
        conditions.append(MonDevice.enabled == enabled)

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            stmt.order_by(MonDevice.id.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
    )
    return list(rows), total


def get_device(db: Session, device_id: int) -> MonDevice:
    """按 ID 取设备，不存在抛 404"""
    device = db.get(MonDevice, device_id)
    if device is None:
        raise NotFoundException("设备不存在")
    return device


def get_devices_by_ids(db: Session, device_ids: Sequence[int]) -> List[MonDevice]:
    """批量取设备（大屏聚合、批量操作使用，避免 N+1 查询）"""
    if not device_ids:
        return []
    rows = db.execute(select(MonDevice).where(MonDevice.id.in_(list(device_ids)))).scalars().all()
    return list(rows)


def get_credential(db: Session, device_id: int) -> Optional[MonDeviceCredential]:
    """取设备凭据（密文形态，仅供本模块内部与采集层使用）"""
    return db.execute(
        select(MonDeviceCredential).where(MonDeviceCredential.device_id == device_id)
    ).scalar_one_or_none()


def to_credential_out(cred: Optional[MonDeviceCredential]) -> Optional[CredentialOut]:
    """
    ORM 凭据 → 脱敏输出对象。

    has_* 字段用于前端显示"已配置/未配置"，敏感值一律不出现在响应里。
    """
    if cred is None:
        return None
    return CredentialOut(
        id=cred.id,
        device_id=cred.device_id,
        protocol=cred.protocol,
        snmp_version=cred.snmp_version,
        snmp_port=cred.snmp_port,
        snmp_username=cred.snmp_username,
        snmp_auth_proto=cred.snmp_auth_proto,
        snmp_priv_proto=cred.snmp_priv_proto,
        snmp_timeout=cred.snmp_timeout,
        snmp_retries=cred.snmp_retries,
        has_snmp_community=bool(cred.snmp_community),
        has_snmp_auth_key=bool(cred.snmp_auth_key_enc),
        has_snmp_priv_key=bool(cred.snmp_priv_key_enc),
        ssh_port=cred.ssh_port,
        ssh_username=cred.ssh_username,
        ssh_device_type=cred.ssh_device_type,
        ssh_key_path=cred.ssh_key_path,
        has_ssh_password=bool(cred.ssh_password_enc),
        winrm_port=cred.winrm_port,
        winrm_username=cred.winrm_username,
        winrm_transport=cred.winrm_transport,
        winrm_scheme=cred.winrm_scheme,
        has_winrm_password=bool(cred.winrm_password_enc),
        ping_count=cred.ping_count,
        ping_interval=cred.ping_interval,
        ping_timeout=cred.ping_timeout,
        ping_size=cred.ping_size,
    )


def get_device_detail(db: Session, device_id: int) -> DeviceDetailOut:
    """设备详情（含脱敏凭据）"""
    device = get_device(db, device_id)
    cred = get_credential(db, device_id)
    detail = DeviceDetailOut.model_validate(device)
    detail.credential = to_credential_out(cred)
    return detail


def build_collect_params(db: Session, device: MonDevice) -> Dict[str, object]:
    """
    组装采集器需要的参数字典（**含解密后的口令**）。

    仅由采集层调用，返回的字典是临时的、生命周期只在一次采集内，
    不写日志、不缓存，降低口令泄漏面。
    """
    cred = get_credential(db, device.id)
    params: Dict[str, object] = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
        "protocol": device.protocol,
        "vendor": device.vendor,
        "device_type": device.device_type,
        "timeout": device.timeout,
    }
    if cred is None:
        return params

    # 注意：以下 decrypt_secret 可能返回 None（密钥变更导致解密失败），
    # 采集器须把"缺凭据"判定为失败并写 collect_log，而不是抛异常中断整批任务
    params.update({
        # SNMP
        "snmp_version": cred.snmp_version,
        "snmp_community": decrypt_secret(cred.snmp_community),
        "snmp_port": cred.snmp_port or 161,
        "snmp_username": cred.snmp_username,
        "snmp_auth_proto": cred.snmp_auth_proto,
        "snmp_auth_key": decrypt_secret(cred.snmp_auth_key_enc),
        "snmp_priv_proto": cred.snmp_priv_proto,
        "snmp_priv_key": decrypt_secret(cred.snmp_priv_key_enc),
        "snmp_timeout": cred.snmp_timeout or 5,
        "snmp_retries": cred.snmp_retries if cred.snmp_retries is not None else 2,
        # SSH
        "ssh_port": cred.ssh_port or 22,
        "ssh_username": cred.ssh_username,
        "ssh_password": decrypt_secret(cred.ssh_password_enc),
        "ssh_key_path": cred.ssh_key_path,
        "ssh_device_type": cred.ssh_device_type or "autodetect",
        # WinRM
        "winrm_port": cred.winrm_port or 5985,
        "winrm_username": cred.winrm_username,
        "winrm_password": decrypt_secret(cred.winrm_password_enc),
        "winrm_transport": cred.winrm_transport or "ntlm",
        "winrm_scheme": cred.winrm_scheme or "http",
        # ICMP
        "ping_count": cred.ping_count or 5,
        "ping_interval": cred.ping_interval or 1,
        "ping_timeout": cred.ping_timeout or 2,
        "ping_size": cred.ping_size or 56,
    })
    return params


# ============================================================================
# 写入
# ============================================================================

def _build_credential(device_id: int, payload: CredentialIn) -> MonDeviceCredential:
    """
    由入参构造凭据 ORM 对象（敏感字段加密）。

    加密集中在此函数，确保"任何写库路径都经过加密"，
    避免将来新增接口时忘记加密。
    """
    cred = MonDeviceCredential(
        device_id=device_id,
        protocol=payload.protocol,
        # ---- SNMP ----
        snmp_version=payload.snmp_version,
        snmp_community=encrypt_secret(payload.snmp_community),
        snmp_port=payload.snmp_port,
        snmp_username=payload.snmp_username,
        snmp_auth_proto=payload.snmp_auth_proto,
        snmp_auth_key_enc=encrypt_secret(payload.snmp_auth_key),
        snmp_priv_proto=payload.snmp_priv_proto,
        snmp_priv_key_enc=encrypt_secret(payload.snmp_priv_key),
        snmp_timeout=payload.snmp_timeout,
        snmp_retries=payload.snmp_retries,
        # ---- SSH ----
        ssh_port=payload.ssh_port,
        ssh_username=payload.ssh_username,
        ssh_password_enc=encrypt_secret(payload.ssh_password),
        ssh_key_path=payload.ssh_key_path,
        ssh_device_type=payload.ssh_device_type,
        # ---- WinRM ----
        winrm_port=payload.winrm_port,
        winrm_username=payload.winrm_username,
        winrm_password_enc=encrypt_secret(payload.winrm_password),
        winrm_transport=payload.winrm_transport,
        winrm_scheme=payload.winrm_scheme,
        # ---- ICMP ----
        ping_count=payload.ping_count,
        ping_interval=payload.ping_interval,
        ping_timeout=payload.ping_timeout,
        ping_size=payload.ping_size,
    )
    return cred


def create_device(db: Session, payload: DeviceCreate, operator: str = "") -> DeviceDetailOut:
    """
    新增设备（设备 + 凭据一次提交）。

    校验：同一 IP + 协议 不允许重复接入，避免重复采集造成设备侧压力翻倍。
    """
    if payload.credential.protocol != payload.protocol:
        raise BizException("凭据协议与设备协议不一致，请检查")

    exists = db.execute(
        select(MonDevice.id).where(MonDevice.ip == payload.ip, MonDevice.protocol == payload.protocol)
    ).scalar_one_or_none()
    if exists:
        raise DuplicateException(f"IP {payload.ip} 已存在相同协议的监控设备")

    device = MonDevice(
        name=payload.name,
        ip=payload.ip,
        device_type=payload.device_type,
        vendor=payload.vendor,
        os_type=payload.os_type,
        protocol=payload.protocol,
        model=payload.model,
        location=payload.location,
        collect_interval=payload.collect_interval,
        timeout=payload.timeout,
        enabled=payload.enabled,
        remark=payload.remark,
        online_status="unknown",
    )
    db.add(device)
    db.flush()   # 先拿到自增 ID，供凭据关联（此时尚未提交，异常时整体回滚）

    db.add(_build_credential(device.id, payload.credential))
    db.add(SysLog(
        level="INFO", module="device", action="create_device",
        message=f"新增设备 {device.name}({device.ip}) 协议={device.protocol}", username=operator,
    ))
    db.commit()
    db.refresh(device)

    system_logger.info("新增设备 | %s(%s) | %s", device.name, device.ip, device.protocol)
    return get_device_detail(db, device.id)


def update_device(
    db: Session, device_id: int, payload: DeviceUpdate, operator: str = ""
) -> DeviceDetailOut:
    """
    编辑设备。

    凭据更新规则（重要）：
      - 未传 credential → 完全不动凭据
      - 传了 credential 但某个敏感字段为空 → **保留原密文**
      - 传了 credential 且敏感字段有值 → 加密覆盖
    这样前端可以在不回显口令的前提下安全地修改其它参数。
    """
    device = get_device(db, device_id)
    data = payload.model_dump(exclude_unset=True, exclude={"credential"})

    # IP 变更时校验唯一性
    new_ip = data.get("ip")
    if new_ip and new_ip != device.ip:
        dup = db.execute(
            select(MonDevice.id).where(
                MonDevice.ip == new_ip,
                MonDevice.protocol == (data.get("protocol") or device.protocol),
                MonDevice.id != device_id,
            )
        ).scalar_one_or_none()
        if dup:
            raise DuplicateException(f"IP {new_ip} 已被其它同协议设备占用")

    for field, value in data.items():
        setattr(device, field, value)

    # ---- 凭据更新 ----
    cred_payload: Optional[CredentialPatchIn] = payload.credential
    if cred_payload is not None:
        cred = get_credential(db, device_id)
        if cred is None:
            # 历史数据可能缺凭据，此处补建
            db.add(_build_credential(device_id, cred_payload))
        else:
            cred.protocol = cred_payload.protocol
            # 非敏感字段直接覆盖
            for field in (
                "snmp_version", "snmp_port", "snmp_username", "snmp_auth_proto",
                "snmp_priv_proto", "snmp_timeout", "snmp_retries",
                "ssh_port", "ssh_username", "ssh_key_path", "ssh_device_type",
                "winrm_port", "winrm_username", "winrm_transport", "winrm_scheme",
                "ping_count", "ping_interval", "ping_timeout", "ping_size",
            ):
                setattr(cred, field, getattr(cred_payload, field))

            # 敏感字段：空值表示"不修改"
            secret_mapping = {
                "snmp_community": "snmp_community",
                "snmp_auth_key": "snmp_auth_key_enc",
                "snmp_priv_key": "snmp_priv_key_enc",
                "ssh_password": "ssh_password_enc",
                "winrm_password": "winrm_password_enc",
            }
            for in_field, db_field in secret_mapping.items():
                new_value = getattr(cred_payload, in_field)
                if new_value:                      # 仅非空才覆盖
                    setattr(cred, db_field, encrypt_secret(new_value))

    db.add(SysLog(
        level="INFO", module="device", action="update_device",
        message=f"修改设备 {device.name}：{list(data.keys())}", username=operator,
    ))
    db.commit()
    db.refresh(device)
    return get_device_detail(db, device_id)


def toggle_device(db: Session, device_id: int, enabled: bool, operator: str = "") -> MonDevice:
    """
    启用 / 停用采集任务（需求：支持启用停用采集任务）。

    停用后调度器下一轮就会跳过该设备，但**已有指标与告警记录保留**，
    重新启用后立即恢复采集，无需重新配置。
    """
    device = get_device(db, device_id)
    device.enabled = enabled
    # 停用时重置失败计数，避免重新启用后因历史失败次数直接判定离线
    if enabled:
        device.fail_count = 0
        device.online_status = "unknown"

    db.add(SysLog(
        level="INFO", module="device", action="toggle_device",
        message=f"{'启用' if enabled else '停用'}设备 {device.name}", username=operator,
    ))
    db.commit()
    db.refresh(device)
    return device


def delete_device(db: Session, device_id: int, operator: str = "") -> Dict[str, int]:
    """
    删除设备（级联清理关联数据）。

    清理范围：凭据、最新指标、**规则运行状态**、资产绑定关系。
    保留范围：metric_sample 历史采样、collect_log、alert_event、alarm_log
             —— 这些是审计与趋势分析资料，删掉就无法回溯"当时为什么告警"。

    ⚠ **为什么 alert_rule_state 必须清理，而 alert_event 反而要保留**

    两者的差别不在"有没有历史价值"，而在**主键是否会复用**：

      - alert_event 用自增 ID 做主键，且查询都靠事件自己的 ID / 指纹，
        残留一行旧事件不会污染新设备的判断 —— 留着是审计资料。
      - alert_rule_state 的业务主键是 (rule_id, device_id)，**device_id 会被复用**
        （SQLite 的 INTEGER PRIMARY KEY 就是 rowid，删掉最大 ID 后再插入会重新拿到它；
         MySQL 的 AUTO_INCREMENT 默认也不回收，但显式插入或 TRUNCATE 后同样会复用）。
        残留状态行的后果不是"多一条脏数据"，而是**新设备一接入就继承了旧设备的告警状态**：
        例如旧设备离线时把 breach_count 累加到 3、status 停在 firing，
        新设备第一次采集若同样取不到值，评估器会认定"该设备已处于告警中"，
        于是既不触发新告警也不发恢复通知 —— 表现为"新接入的设备坏了但没人知道"。

    这个缺陷的隐蔽之处在于：**它是"少了一行 delete"**，不会有任何报错、
    不会影响任何已有设备的运行，只有恰好复用了 ID 才会显现。
    因此本函数末尾由 tests/test_data_hygiene.py 做一次数据驱动的全景校验：
    凡是带 device_id 列的表，要么在这里显式清理，要么在测试的保留清单里写明理由。

    ⚠ **设备专属告警规则为什么"停用而不删除"**

    规则表也有 device_id，但它和上面三张表性质不同 —— 它是**用户配置**，不是运行时数据。
    处理它有两条错路，各自都很隐蔽：

      - **什么都不做**：规则仍是"启用中"，但 device_id 指向一台不存在的设备，
        评估器永远不会为它找到设备 → **规则看起来生效、实际永不触发**。
        管理员半年后翻出来问"这条规则为什么没报过警"，没人能答上来。
        这是最难发现的一类故障：没有报错、没有日志、界面上一切正常。
      - **跟着设备一起删**：规则定义是人工配置，删掉不可恢复；
        而且"删设备顺手把我配的 20 条规则删了"是不该出现的意外。

    所以选择第三条路：**置 enabled=False 并清掉它的运行状态**。
    规则还在列表里（可按"已停用"筛出来），含义明确、可解释、可恢复 ——
    管理员重新接入设备后，重新启用即可，不需要凭记忆重建。

    :return: 实际生效的清理计数，便于调用方/测试核对（0 计数说明有表的清理由别处负责）
    """
    device = get_device(db, device_id)
    device_name = device.name
    device_ip = device.ip

    cred_deleted = db.execute(
        delete(MonDeviceCredential).where(MonDeviceCredential.device_id == device_id)
    ).rowcount
    latest_deleted = db.execute(
        delete(MetricLatest).where(MetricLatest.device_id == device_id)
    ).rowcount
    state_deleted = db.execute(
        delete(AlertRuleState).where(AlertRuleState.device_id == device_id)
    ).rowcount
    # ⚠ 这里必须用 update(AlertRule) 而不是 AlertRule.__table__.update()：
    #    前者是"ORM 级更新"，SQLAlchemy 会顺便把当前会话里已加载的同名对象
    #    一起改掉（synchronize_session）；后者是纯 Core 语句，数据库改了、
    #    会话里那份缓存对象还是旧的 —— 同一请求里紧接着读这条规则会读到 enabled=True，
    #    于是"已停用"的判断当场失真。tests/test_data_hygiene.py 的
    #    test_删设备只停用设备专属规则 就是为此设的守卫。
    rule_disabled = db.execute(
        update(AlertRule).where(AlertRule.device_id == device_id).values(enabled=False)
    ).rowcount
    # 解除资产绑定（资产本身保留，只把"对应监控设备"清空）
    db.execute(
        update(AssetInfo).where(AssetInfo.device_id == device_id).values(device_id=None)
    )
    db.delete(device)

    # 日志里带上计数：删设备是低频高危操作，事后追查"当时到底连带改了什么"只能靠这条日志
    extra = ""
    if rule_disabled:
        extra = f"；停用设备专属规则 {rule_disabled} 条"
    db.add(SysLog(
        level="WARNING", module="device", action="delete_device",
        message=(f"删除设备 {device_name}({device_ip})；"
                 f"清理凭据 {cred_deleted} 条、最新指标 {latest_deleted} 条、"
                 f"规则状态 {state_deleted} 条{extra}"),
        username=operator,
    ))
    db.commit()
    system_logger.warning(
        "删除设备 | %s(%s) | 凭据%d 指标%d 状态%d 停用规则%d",
        device_name, device_ip, cred_deleted, latest_deleted, state_deleted, rule_disabled,
    )
    return {
        "credential": cred_deleted,
        "metric_latest": latest_deleted,
        "rule_state": state_deleted,
        "rule_disabled": rule_disabled,
    }


def update_device_status(
    db: Session,
    device: MonDevice,
    online_status: Optional[str] = None,
    success: bool = True,
    offline_threshold: int = 3,
) -> None:
    """
    采集后回写设备状态（由调度器调用）。

    :param online_status: 采集器判定的在线状态（ICMP 结果），None 表示本次不更新
    :param success: 本轮采集是否成功
    :param offline_threshold: 连续失败多少次判定离线（来自 sys_config）

    设计说明：**失败不是立即判离线**，而是累加 fail_count，达到阈值才置 offline。
    理由：一次网络抖动不应该让整屏设备变红，也不应该触发离线告警。
    """
    now_ts = datetime.now()
    device.last_collect_at = now_ts

    if success:
        device.fail_count = 0
        if online_status:
            device.online_status = online_status
            if online_status == "online":
                device.last_online_at = now_ts
    else:
        device.fail_count = (device.fail_count or 0) + 1
        if device.fail_count >= max(offline_threshold, 1):
            device.online_status = "offline"


# ============================================================================
# 连通性测试（新增/编辑设备时的"测试连接"按钮）
# ============================================================================

def test_connection(db: Session, device_id: int, save_metrics: bool = False) -> TestConnectionResult:
    """
    测试设备连通性与协议可用性。

    实现方式：走与正式采集完全相同的采集器（同一个工厂、同一套解析器），
    保证"测试通过"就等于"采集能成功"，避免两套逻辑导致测试通过但采集失败。

    :param save_metrics: 是否把测试结果写入指标表。默认 False——
        测试按钮不应污染真实监控数据与历史曲线。
    """
    import time

    # 延迟导入：避免 collectors ↔ services 循环依赖，同时让采集模块未安装时
    # 只有"测试连接"功能受影响，而不是整个平台起不来
    from app.collectors import CollectorFactory

    device = get_device(db, device_id)
    params = build_collect_params(db, device)

    collector = CollectorFactory.get(device.protocol)
    if collector is None:
        return TestConnectionResult(
            success=False,
            message=f"暂不支持的采集协议：{device.protocol}（如需扩展请实现对应采集器并在工厂注册）",
        )

    start = time.time()
    try:
        result = collector.collect(device.id, params)
        duration = int((time.time() - start) * 1000)

        if result.success:
            details = [
                f"{item.get('metric_key')} = {item.get('metric_value', item.get('str_value'))}"
                for item in result.metrics[:20]
            ]
            return TestConnectionResult(
                success=True,
                message=f"采集成功，获取 {len(result.metrics)} 项指标",
                duration_ms=duration,
                metric_count=len(result.metrics),
                details=details,
            )
        return TestConnectionResult(
            success=False, message=result.error or "采集失败", duration_ms=duration
        )
    except Exception as exc:  # pragma: no cover - 依赖真实设备，异常路径靠单测覆盖
        duration = int((time.time() - start) * 1000)
        system_logger.warning("测试连接异常 | 设备=%s | %s", device.ip, exc)
        return TestConnectionResult(success=False, message=f"测试失败：{exc}", duration_ms=duration)
