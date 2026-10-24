"""设备与协议凭据 Schema（对应模块1：设备接入与协议参数配置）"""
from __future__ import annotations

from typing import List, Optional

from pydantic import Field, computed_field, field_validator, model_validator

from app.schemas.common import BaseSchema, OptionalDateTimeStr

# ---------------- 合法枚举（与前端下拉框取值一一对应）----------------
VALID_PROTOCOLS = {"snmp", "ssh", "winrm", "icmp"}

# 设备类型：覆盖政企弱电/系统集成场景的常见被监控对象。
# 注意必须保留 "other" 兜底——集成项目里总会有非标设备（如环境监测仪、
# 广播主机），没有兜底值会导致这类设备无法录入平台。
VALID_DEVICE_TYPES = {
    "switch", "router", "firewall", "server", "storage",
    "camera", "access", "ap", "ups", "other",
}

PROTOCOL_TEXT = {"snmp": "SNMP", "ssh": "SSH", "winrm": "WinRM/WMI", "icmp": "ICMP长Ping"}
DEVICE_TYPE_TEXT = {
    "switch": "交换机", "router": "路由器", "firewall": "防火墙",
    "server": "服务器", "storage": "存储设备",
    "camera": "视频监控设备", "access": "门禁设备", "ap": "无线AP",
    "ups": "UPS 电源", "other": "其他设备",
}
ONLINE_TEXT = {"online": "在线", "offline": "离线", "unknown": "未知"}

# 厂商中文名（第 6 步补）。
#
# ⚠ **为什么会漏掉这张表**：`mon_device.vendor` 是一个自由字符串（不枚举校验），
# 因为选型时考虑"客户现场可能遇到任何品牌，强制枚举会让新增品牌必须改代码"。
# 结果就是 type/protocol/online 三张映射表都有了，vendor 一直是原始编码值 ——
# 后台列表显示 "huawei"，大屏厂商分布环图显示 "sangfor / hikvision"，
# 而真正需要看这张图的场景恰恰是"演示给客户看信创适配了哪些品牌"。
#
# 注意这里**保留兜底**：不在表内的厂商原样透出（不显示"未知"）。
# 因为原样透出的 "zte" 是一个有效信息，而 "未知" 会让运维以为数据有问题。
VENDOR_TEXT = {
    "huawei": "华为", "h3c": "新华三", "cisco": "思科", "ruijie": "锐捷",
    "sangfor": "深信服", "hikvision": "海康威视", "dahua": "大华",
    "windows": "Windows", "linux": "Linux", "kylin": "麒麟", "uos": "统信",
    "centos": "CentOS", "ubuntu": "Ubuntu", "vmware": "VMware",
    "dell": "戴尔", "hpe": "新华三 HPE", "lenovo": "联想", "inspur": "浪潮",
    "other": "其他",
}

# netmiko 支持的设备类型（本平台实际会用到的主要是华为与华三）
VALID_SSH_DEVICE_TYPES = {
    "huawei", "hp_comware", "cisco_ios", "linux", "autodetect",
}


class CredentialIn(BaseSchema):
    """
    协议凭据输入（明文）。

    ⚠ 安全约定：本 Schema 中的密码/团体字字段是**明文入参**，
    由 DeviceService 在写库前调用 encrypt_secret() 加密，
    **绝不允许直接把本对象 dump 后写入数据库**。
    """

    protocol: str = Field(..., description="snmp/ssh/winrm/icmp")

    # ---- SNMP ----
    snmp_version: Optional[str] = Field(default="v2c", description="v2c/v3")
    snmp_community: Optional[str] = Field(default=None, max_length=128, description="v2c 团体字")
    snmp_port: int = Field(default=161, ge=1, le=65535, description="SNMP 端口，默认 161")
    snmp_username: Optional[str] = Field(default=None, max_length=64, description="v3 安全名")
    snmp_auth_proto: Optional[str] = Field(default="SHA", description="MD5/SHA/SHA256")
    snmp_auth_key: Optional[str] = Field(default=None, max_length=128, description="v3 认证口令")
    snmp_priv_proto: Optional[str] = Field(default=None, description="DES/AES/AES256")
    snmp_priv_key: Optional[str] = Field(default=None, max_length=128, description="v3 加密口令")
    snmp_timeout: int = Field(default=5, ge=1, le=60, description="SNMP 超时（秒）")
    snmp_retries: int = Field(default=2, ge=0, le=5, description="SNMP 重试次数")

    # ---- SSH ----
    ssh_port: int = Field(default=22, ge=1, le=65535, description="SSH 端口，默认 22")
    ssh_username: Optional[str] = Field(default=None, max_length=64, description="SSH 登录账号")
    ssh_password: Optional[str] = Field(default=None, max_length=128, description="SSH 登录口令（明文入参，写库前加密）")
    ssh_key_path: Optional[str] = Field(default=None, max_length=255, description="私钥文件路径；留空表示用口令登录")
    ssh_device_type: Optional[str] = Field(default="autodetect", description="huawei/hp_comware/linux")

    # ---- WinRM ----
    winrm_port: int = Field(default=5985, ge=1, le=65535, description="WinRM 端口，默认 5985")
    winrm_username: Optional[str] = Field(default=None, max_length=64, description="Windows 账号")
    winrm_password: Optional[str] = Field(default=None, max_length=128, description="Windows 口令（明文入参，写库前加密）")
    winrm_transport: Optional[str] = Field(default="ntlm", description="ntlm/basic/ssl")
    winrm_scheme: Optional[str] = Field(default="http", description="http/https")

    # ---- ICMP 长 Ping ----
    ping_count: int = Field(default=5, ge=1, le=20, description="每轮发包数")
    ping_interval: int = Field(default=1, ge=1, le=10, description="发包间隔（秒）")
    ping_timeout: int = Field(default=2, ge=1, le=10, description="单包超时（秒）")
    ping_size: int = Field(default=56, ge=8, le=1500, description="包大小（字节）")

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, value: str) -> str:
        if value not in VALID_PROTOCOLS:
            raise ValueError(f"协议只能是 {'/'.join(sorted(VALID_PROTOCOLS))}")
        return value

    @field_validator("ssh_device_type")
    @classmethod
    def _check_ssh_type(cls, value: Optional[str]) -> Optional[str]:
        if value and value not in VALID_SSH_DEVICE_TYPES:
            raise ValueError(f"SSH 设备类型不支持：{value}")
        return value

    @model_validator(mode="after")
    def _check_required_by_protocol(self) -> "CredentialIn":
        """
        按协议校验必填项。

        为什么在 Schema 层校验而不是 Service：
        让错误在进入业务逻辑前就被拦下，报错信息能精确指向字段，
        前端也能直接显示（例如"选择 SNMP 必须填写团体字或 v3 认证信息"）。
        """
        if self.protocol == "snmp":
            if self.snmp_version == "v2c" and not self.snmp_community:
                raise ValueError("SNMP v2c 必须填写团体字（community）")
            if self.snmp_version == "v3" and not (self.snmp_username and self.snmp_auth_key):
                raise ValueError("SNMP v3 必须填写安全名与认证口令")
        elif self.protocol == "ssh":
            if not self.ssh_username:
                raise ValueError("SSH 采集必须填写登录账号")
            if not self.ssh_password and not self.ssh_key_path:
                raise ValueError("SSH 采集必须填写登录口令或指定私钥路径")
        elif self.protocol == "winrm":
            if not (self.winrm_username and self.winrm_password):
                raise ValueError("WinRM 采集必须填写账号与口令")
        return self


class CredentialPatchIn(CredentialIn):
    """
    编辑设备时的凭据入参（**宽松模式**）。

    与 CredentialIn 的唯一区别：跳过"按协议校验必填项"。

    为什么必须宽松：编辑设备的凭据字段采用"敏感项留空 = 保持不变"语义
    （运维只想改设备名，不可能每次都重新输入一遍团体字/口令）。
    若沿用 CredentialIn 的完整性校验，用户只传 {"protocol":"snmp"} 就会被
    400 拦下，导致"改个设备名都改不了"。真实口令由 Service 层用旧密文补齐。

    注意：protocol 取值本身的合法性仍然校验（改了协议必须给对的值）。
    """

    @model_validator(mode="after")
    def _check_required_by_protocol(self) -> "CredentialPatchIn":
        """刻意留空：编辑场景不校验完整性，由 DeviceService 用库中旧密文补齐"""
        return self


class CredentialOut(BaseSchema):
    """
    协议凭据输出（**脱敏**）。

    只返回非敏感配置项，敏感项用 has_* 布尔标记"是否已配置"。
    这样前端能正确显示"已配置/未配置"，但即便接口被抓包也拿不到口令。
    """

    id: int = Field(description="凭据记录 ID")
    device_id: int = Field(description="所属设备 ID")
    protocol: str = Field(description="协议：snmp/ssh/winrm/icmp")

    snmp_version: Optional[str] = Field(default=None, description="SNMP 版本：v2c/v3")
    snmp_port: Optional[int] = Field(default=None, description="SNMP 端口，默认 161")
    snmp_username: Optional[str] = Field(default=None, description="v3 安全名（非敏感）")
    snmp_auth_proto: Optional[str] = Field(default=None, description="v3 认证协议：MD5/SHA/SHA256")
    snmp_priv_proto: Optional[str] = Field(default=None, description="v3 加密协议：DES/AES/AES256")
    snmp_timeout: Optional[int] = Field(default=None, description="SNMP 超时（秒）")
    snmp_retries: Optional[int] = Field(default=None, description="SNMP 重试次数")
    has_snmp_community: bool = Field(default=False, description="是否已配置团体名（值本身不出网）")
    has_snmp_auth_key: bool = Field(default=False, description="是否已配置认证密钥")
    has_snmp_priv_key: bool = Field(default=False, description="是否已配置加密密钥")

    ssh_port: Optional[int] = Field(default=None, description="SSH 端口，默认 22")
    ssh_username: Optional[str] = Field(default=None, description="SSH 登录账号（非敏感）")
    ssh_device_type: Optional[str] = Field(default=None, description="netmiko device_type，如 huawei/hp_comware/cisco_ios")
    ssh_key_path: Optional[str] = Field(default=None, description="私钥文件路径；为空表示用口令登录")
    has_ssh_password: bool = Field(default=False, description="是否已配置 SSH 口令")

    winrm_port: Optional[int] = Field(default=None, description="WinRM 端口，默认 5985")
    winrm_username: Optional[str] = Field(default=None, description="Windows 账号（非敏感）")
    winrm_transport: Optional[str] = Field(default=None, description="传输方式：ntlm/kerberos/basic/ssl")
    winrm_scheme: Optional[str] = Field(default=None, description="http/https")
    has_winrm_password: bool = Field(default=False, description="是否已配置 WinRM 口令")

    ping_count: Optional[int] = Field(default=None, description="每次探测发几个包")
    ping_interval: Optional[int] = Field(default=None, description="发包间隔（毫秒）")
    ping_timeout: Optional[int] = Field(default=None, description="超时（毫秒）")
    ping_size: Optional[int] = Field(default=None, description="包大小（字节）")


class _DeviceInputValidators:
    """
    设备**入参**校验器集合（DeviceCreate / DeviceUpdate 共用）。

    为什么不直接写在 DeviceBase 上：
      若校验器挂在 DeviceBase，则继承它的 DeviceOut（输出类）在
      `DeviceOut.model_validate(orm_obj)` 时**也会执行校验**。
      一旦库里有枚举外的历史数据（旧版本写入、手工改库、采集器自动发现的新型号
      用了 "other"），整个设备列表接口就会因序列化失败返回 500 ——
      一条脏数据拖垮整张页面。所以输入校验必须与输出映射彻底分离。

    这里对 None 做放行，是因为 DeviceUpdate 的字段全部可选。
    """

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_PROTOCOLS:
            raise ValueError(f"协议只能是 {'/'.join(sorted(VALID_PROTOCOLS))}")
        return value

    @field_validator("device_type")
    @classmethod
    def _check_device_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_DEVICE_TYPES:
            raise ValueError(f"设备类型只能是 {'/'.join(sorted(VALID_DEVICE_TYPES))}")
        return value

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, value: Optional[str]) -> Optional[str]:
        """校验 IPv4 格式。用简易分段校验而不引入 ipaddress 严格模式，兼容部分内网特殊写法。"""
        if value is None:
            return value
        parts = value.split(".")
        if len(parts) != 4:
            raise ValueError("IP 地址格式不正确，应为 x.x.x.x")
        for seg in parts:
            if not seg.isdigit() or not 0 <= int(seg) <= 255:
                raise ValueError("IP 地址格式不正确，每段取值 0-255")
        return value


class DeviceBase(BaseSchema):
    """设备公共字段定义（纯字段，不含校验，供输入类继承）"""

    name: str = Field(..., min_length=1, max_length=128, description="设备名称")
    ip: str = Field(..., min_length=7, max_length=64, description="管理 IP")
    device_type: str = Field(
        ..., description="switch/router/firewall/server/storage/camera/access/ap/ups/other"
    )
    vendor: Optional[str] = Field(
        default=None, max_length=32,
        description="厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供",
    )
    os_type: Optional[str] = Field(default=None, max_length=64, description="操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019")
    # ⚠ 取值必须以 VALID_PROTOCOLS 为准（当前：snmp / ssh / winrm / icmp）。
    # 写这段时曾误写成 "snmp/snmpv3/..."，凭印象加了一个并不存在的 snmpv3 ——
    # 这也是为什么所有 description 里出现的枚举都应指向一个真值表（此处为 VALID_PROTOCOLS），
    # 而不是重新抄一遍；抄写就是漂移的开始。
    protocol: str = Field(..., description="采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols")
    model: Optional[str] = Field(default=None, max_length=64, description="设备型号")
    location: Optional[str] = Field(default=None, max_length=128, description="物理位置，如 3号楼机房A柜")
    collect_interval: int = Field(default=60, ge=10, le=86400, description="采集周期（秒），最小 10 秒")
    timeout: int = Field(default=10, ge=1, le=120, description="采集超时（秒）")
    enabled: bool = Field(default=True, description="是否启用采集；false 时大屏显示“停用”而不是“在线”")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")


class DeviceCreate(_DeviceInputValidators, DeviceBase):
    """新增设备（含凭据，一次提交完成接入）"""

    credential: CredentialIn = Field(..., description="协议凭据，与 protocol 必须一致")


class DeviceUpdate(_DeviceInputValidators, BaseSchema):
    """编辑设备（全字段可选，只更新传入的字段）"""

    name: Optional[str] = Field(default=None, max_length=128, description="设备名称")
    ip: Optional[str] = Field(default=None, max_length=64, description="管理 IP 或主机名")
    device_type: Optional[str] = Field(default=None, description="设备类型，取值见 /api/v1/devices/types")
    vendor: Optional[str] = Field(default=None, max_length=32, description="厂商标识（huawei/h3c/...），见 GET /api/v1/devices/vendors")
    os_type: Optional[str] = Field(default=None, max_length=64, description="操作系统/固件版本描述")
    protocol: Optional[str] = Field(default=None, description="采集协议，取值见 /api/v1/devices/protocols")
    model: Optional[str] = Field(default=None, max_length=64, description="设备型号")
    location: Optional[str] = Field(default=None, max_length=128, description="物理位置")
    collect_interval: Optional[int] = Field(default=None, ge=10, le=86400, description="采集周期（秒）")
    timeout: Optional[int] = Field(default=None, ge=1, le=120, description="单次采集超时（秒）")
    enabled: Optional[bool] = Field(default=None, description="是否参与采集；false 后大屏显示“停用”而非“在线”")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")
    credential: Optional[CredentialPatchIn] = Field(
        default=None,
        description="传入则更新凭据；敏感项留空表示保持原值不变",
    )


class DeviceOut(BaseSchema):
    """
    设备列表/详情输出。

    ⚠ 刻意**不继承** DeviceBase —— 这是踩过坑后的结论：
    DeviceBase 派生的输入类上挂着 device_type / protocol 的枚举校验器，
    一旦输出类继承它，`DeviceOut.model_validate(orm_obj)` 也会执行这些校验。
    后果是库中只要存在一条枚举外的历史数据（旧版本写入、手工改库、
    或采集器自动发现的非标设备），**整个设备列表接口就会 500**，
    一条脏数据拖垮整张页面。

    因此输出侧只做类型映射，不做业务校验；枚举取值用 .get(x, x) 兜底，
    未知类型原样透出给前端（前端展示"其他设备"而非报错），
    字段类型放宽为 Optional 也是同理：即便库里是 NULL 也不影响其它记录正常返回。
    """

    id: int = Field(description="设备 ID")
    name: str = Field(default="", description="设备名称")
    ip: str = Field(default="", description="管理 IP")
    device_type: Optional[str] = Field(default="other", description="switch/router/firewall/server/storage/camera/access/ap/ups/other")
    vendor: Optional[str] = Field(default=None, description="厂商标识，如 huawei/h3c/cisco/ruijie/sangfor/hikvision；前端展示的中文名由 DeviceOut.vendor_text 提供")
    os_type: Optional[str] = Field(default=None, description="操作系统/固件描述，如 CentOS 7.9 / Windows Server 2019")
    protocol: Optional[str] = Field(default=None, description="采集协议：snmp/ssh/winrm/icmp，取值见 /api/v1/devices/protocols")
    model: Optional[str] = Field(default=None, description="设备型号")
    location: Optional[str] = Field(default=None, description="物理位置，如 3号楼机房A柜")
    collect_interval: int = Field(default=60, description="采集周期（秒），最小 10 秒")
    timeout: int = Field(default=10, description="采集超时（秒）")
    enabled: bool = Field(default=True, description="是否启用采集；false 时大屏显示“停用”而不是“在线”")
    remark: Optional[str] = Field(default=None, description="备注")

    online_status: Optional[str] = Field(default="unknown", description="在线状态：online/offline/unknown")
    fail_count: int = Field(default=0, description="连续采集失败次数；达到阈值后判为离线")
    last_collect_at: OptionalDateTimeStr = Field(default=None, description="最近一次采集时间")
    last_online_at: OptionalDateTimeStr = Field(default=None, description="最近一次在线时间")
    created_at: OptionalDateTimeStr = Field(default=None, description="创建时间")

    @computed_field(description="协议中文名")
    @property
    def protocol_text(self) -> str:
        return PROTOCOL_TEXT.get(self.protocol or "", self.protocol or "未知")

    @computed_field(description="设备类型中文名")
    @property
    def device_type_text(self) -> str:
        return DEVICE_TYPE_TEXT.get(self.device_type or "", self.device_type or "未知")

    @computed_field(description="在线状态中文名")
    @property
    def online_text(self) -> str:
        return ONLINE_TEXT.get(self.online_status or "", self.online_status or "未知")

    @computed_field(description="厂商中文名（未登记的品牌原样透出）")
    @property
    def vendor_text(self) -> str:
        code = (self.vendor or "").strip()
        if not code:
            return ""
        # 不在映射表里就原样返回：原样透出的 "zte" 是有效信息，
        # 换成"未知"反而会让运维以为数据出了问题（与 Out 类放宽枚举的同一思路）
        return VENDOR_TEXT.get(code.lower(), code)


class DeviceDetailOut(DeviceOut):
    """设备详情（附脱敏凭据）"""

    credential: Optional[CredentialOut] = Field(default=None, description="已脱敏的协议凭据；敏感项只回传 has_* 标记")


class DeviceSimpleOut(BaseSchema):
    """设备精简信息（供告警/指标页面下拉选择，减少传输量）"""

    id: int = Field(description="设备 ID")
    name: str = Field(default="", description="设备名称")
    ip: str = Field(default="", description="管理 IP")
    device_type: Optional[str] = Field(default="other", description="设备类型")
    protocol: Optional[str] = Field(default=None, description="采集协议")
    online_status: Optional[str] = Field(default="unknown", description="在线状态")
    enabled: bool = Field(default=True, description="是否启用采集")


class TestConnectionResult(BaseSchema):
    """连通性/协议测试结果（新增设备时的"测试连接"按钮）"""

    success: bool = Field(description="是否连通成功")
    message: str = Field(description="结果说明；失败时给出可排查的原因，如“SNMP 团体名错误”")
    duration_ms: int = Field(default=0, description="耗时（毫秒）")
    metric_count: int = Field(default=0, description="本次试采到的指标条数；为 0 说明凭据对但指标不支持")
    details: List[str] = Field(default_factory=list, description="采集到的指标摘要，便于确认凭据正确")
