"""告警 Schema（对应模块3：自定义阈值、告警中心、手动确认、通知记录）"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import Field, computed_field, field_validator, model_validator

from app.models.alert import metric_unit
from app.schemas.common import BaseSchema, OptionalDateTimeStr

VALID_OPERATORS = {"gt", "lt", "ge", "le", "eq", "ne"}
VALID_LEVELS = {"critical", "major", "minor", "info"}
VALID_CHANNELS = {"email", "wecom", "dingtalk", "wechat"}


class _AlertRuleInputValidators:
    """
    告警规则**入参**校验器（AlertRuleCreate / AlertRuleUpdate 共用）。

    为什么不挂在 AlertRuleBase 上：AlertRuleOut 继承它时，
    这些校验器会作用到**输出路径**。规则表里若存在枚举外的历史取值
    （旧版本写入、手工改库），整个规则列表接口就会 500。
    入参校验与输出映射必须分离 —— 同类问题在 Device / Asset 上也各修过一次。
    """

    @field_validator("operator")
    @classmethod
    def _check_operator(cls, value: Optional[str]) -> Optional[str]:
        if value is None:      # 编辑场景未传该字段
            return value
        if value not in VALID_OPERATORS:
            raise ValueError(f"比较符只能是 {'/'.join(sorted(VALID_OPERATORS))}")
        return value

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_LEVELS:
            raise ValueError(f"告警级别只能是 {'/'.join(sorted(VALID_LEVELS))}")
        return value

    @field_validator("notify_channels")
    @classmethod
    def _check_channels(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is None:
            return value
        invalid = set(value) - VALID_CHANNELS
        if invalid:
            raise ValueError(f"不支持的通知渠道：{','.join(sorted(invalid))}")
        return value

    @model_validator(mode="after")
    def _check_threshold_by_operator(self):
        """
        按比较符校验阈值合理性。
        例如"等于/不等于"用于状态类指标（如 online_status=0 表示离线），阈值必须是整数；
        而"大于/小于"用于连续量指标。这里只拦明显不合理的组合，避免运维配错后不报警。
        """
        if self.operator in ("eq", "ne") and self.metric_key in ("online_status", "if_status"):
            if self.threshold is not None and self.threshold not in (0, 1):
                raise ValueError("状态类指标使用等于/不等于比较时，阈值只能是 0 或 1")
        return self


class AlertRuleBase(BaseSchema):
    """告警规则公共字段定义（纯字段，不含校验，供输入类继承）"""

    name: str = Field(..., min_length=1, max_length=128, description="规则名称")
    device_id: Optional[int] = Field(default=None, description="null 表示全局规则")
    metric_key: str = Field(..., min_length=1, max_length=64, description="指标键")
    operator: str = Field(default="gt", description="gt/lt/ge/le/eq/ne")
    threshold: float = Field(..., description="阈值")
    duration_n: int = Field(default=3, ge=1, le=60, description="连续 N 次超标才触发")
    recovery_n: int = Field(default=3, ge=1, le=60, description="连续 M 次正常才恢复")
    level: str = Field(default="major", description="critical/major/minor/info")
    converge_window: int = Field(default=300, ge=0, le=86400, description="收敛窗口（秒），0=不收敛")
    converge_max: int = Field(default=3, ge=1, le=100, description="窗口内最大推送条数")
    notify_channels: List[str] = Field(default_factory=list, description="通知渠道列表")
    silence_until: Optional[datetime] = Field(default=None, description="静默截止时间")
    enabled: bool = Field(default=True, description="是否启用；false 时规则不再参与评估")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")


class AlertRuleCreate(_AlertRuleInputValidators, AlertRuleBase):
    """新增规则"""


class AlertRuleUpdate(_AlertRuleInputValidators, BaseSchema):
    """编辑规则（全字段可选，只更新传入字段）

    补上 operator/level/notify_channels 校验的原因：
    此前该接口无任何校验，可以把非法比较符或未知渠道写进数据库，
    再被规则列表的输出校验引爆 500。写入口是最后一道防线。
    """

    name: Optional[str] = Field(default=None, max_length=128, description="规则名称")
    device_id: Optional[int] = Field(default=None, description="适用设备 ID；null 表示全局规则（对所有设备生效）")
    metric_key: Optional[str] = Field(default=None, max_length=64, description="监控指标键，取值见 /api/v1/metrics/catalog")
    operator: Optional[str] = Field(default=None, description="比较符：gt 大于 / ge 大于等于 / lt 小于 / le 小于等于 / eq 等于 / ne 不等于")
    threshold: Optional[float] = Field(default=None, description="阈值；与 operator 组合成判定条件")
    duration_n: Optional[int] = Field(default=None, ge=1, le=60, description="连续多少次超标才触发告警（抖动抑制，避免单点毛刺误报）")
    recovery_n: Optional[int] = Field(default=None, ge=1, le=60, description="连续多少次正常才恢复（滞回，避免告警在阈值附近反复）")
    level: Optional[str] = Field(
        default=None,
        # ⚠ 取值以 AlertRuleBase.level 为准（critical/major/minor/info）。
        # 写这段时误写成 "critical/warning/info" —— 凭印象抄枚举第三次出错了（前两次是
        # protocol 多写 snmpv3、role 多写 operator）。结论：**不要在注释/描述里重抄枚举**，
        # 抄写的那一刻是对的，但真值表改了它不会跟着变。
        description="告警级别，取值同 AlertRuleBase：critical/major/minor/info",
    )
    converge_window: Optional[int] = Field(default=None, ge=0, le=86400, description="收敛窗口（秒）；窗口内同一规则的推送条数受限")
    converge_max: Optional[int] = Field(default=None, ge=1, le=100, description="窗口内最多推送条数，超出部分只落事件不推送")
    notify_channels: Optional[List[str]] = Field(default=None, description="通知渠道：email / wecom / dingtalk / wechat，至少选一个")
    silence_until: Optional[datetime] = Field(default=None, description="静默截止时间；到达前只记录事件不推送")
    enabled: Optional[bool] = Field(default=None, description="是否启用；false 时规则不再评估")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")


class AlertRuleOut(BaseSchema):
    """
    规则输出（附设备名与指标中文名，前端表格直接展示）。

    ⚠ 刻意**不继承** AlertRuleBase：基类上的 operator/level/notify_channels
    校验器会作用到输出路径，库中一旦有枚举外取值，规则列表接口就整体 500。
    输出侧只做映射与兜底，未知取值原样透出（前端显示"未知"而不是白屏）。
    """

    id: int = Field(description="规则 ID")

    name: str = Field(default="", description="规则名称")
    device_id: Optional[int] = Field(default=None, description="null 表示全局规则")
    metric_key: Optional[str] = Field(default=None, description="指标键")
    operator: Optional[str] = Field(default=None, description="gt/lt/ge/le/eq/ne")
    threshold: Optional[float] = Field(default=None, description="阈值")
    duration_n: int = Field(default=3, description="连续 N 次超标才触发")
    recovery_n: int = Field(default=3, description="连续 M 次正常才恢复")
    level: Optional[str] = Field(default="major", description="critical/major/minor/info")
    converge_window: int = Field(default=300, description="收敛窗口（秒），0=不收敛")
    converge_max: int = Field(default=3, description="窗口内最大推送条数")
    notify_channels: List[str] = Field(default_factory=list, description="通知渠道列表")
    silence_until: OptionalDateTimeStr = Field(default=None, description="静默截止时间")
    enabled: bool = Field(default=True, description="是否启用；false 时规则不再参与评估")
    remark: Optional[str] = Field(default=None, description="备注")

    created_at: OptionalDateTimeStr = Field(default=None, description="创建时间")
    updated_at: OptionalDateTimeStr = Field(default=None, description="最后修改时间")

    device_name: Optional[str] = Field(default=None, description="设备名称，全局规则为“全部设备”")
    metric_name: Optional[str] = Field(default=None, description="指标中文名")

    # 规则运行状态（来自 alert_rule_state，便于页面显示"当前连续超标 2/3 次"）
    current_status: Optional[str] = Field(default=None, description="normal/firing/acked/recovered")
    breach_count: Optional[int] = Field(default=None, description="当前连续超标次数（未达 duration_n 时不触发）")
    normal_count: Optional[int] = Field(default=None, description="当前连续正常次数（达 recovery_n 才恢复）")
    last_value: Optional[float] = Field(default=None, description="最近一次评估取到的指标值")
    last_eval_time: OptionalDateTimeStr = Field(default=None, description="最近一次评估时间")
    firing_device_count: Optional[int] = Field(default=None, description="该规则当前触发中的设备数")

    @field_validator("notify_channels", mode="before")
    @classmethod
    def _parse_channels(cls, value):
        """
        兜底解析：alert_rule.notify_channels 在库里是 JSON 字符串（'["email"]'）。
        Service 层正常会转成 list，这里再兜一层，
        避免某条路径漏转换时把 {'email'} 这种字符逐字拆成列表。
        """
        if isinstance(value, str):
            try:
                parsed = json.loads(value or "[]")
                return parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                return []
        return value

    @computed_field(description="级别中文名")
    @property
    def level_text(self) -> str:
        return {"critical": "紧急", "major": "重要", "minor": "一般", "info": "提示"}.get(
            self.level or "", self.level or "未知"
        )

    @computed_field(description="比较符中文名")
    @property
    def operator_text(self) -> str:
        return {
            "gt": "大于", "lt": "小于", "ge": "大于等于",
            "le": "小于等于", "eq": "等于", "ne": "不等于",
        }.get(self.operator or "", self.operator or "未知")

    @computed_field(description="阈值描述，如 CPU使用率 大于 85%")
    @property
    def condition_text(self) -> str:
        # 单位从模型层的 metric_unit() 取，与告警详情、邮件正文、企微/钉钉推送
        # 共用同一份定义 —— 四处各写一套判断就会写出"邮件 93.5%、企微 93.5ms"。
        unit = metric_unit(self.metric_key)
        name = self.metric_name or self.metric_key or "指标"
        threshold = f"{self.threshold:g}" if isinstance(self.threshold, (int, float)) else "-"
        return f"{name} {self.operator_text} {threshold}{unit}"

    @computed_field(description="是否处于静默期")
    @property
    def is_silenced(self) -> bool:
        return bool(self.silence_until and self.silence_until > datetime.now())


class AlertEventOut(BaseSchema):
    """告警事件输出（告警中心列表 / 大屏滚动字幕共用）"""

    id: int = Field(description="事件 ID")
    event_no: Optional[str] = Field(default=None, description="事件编号，用于人工沟通时引用")
    rule_id: Optional[int] = Field(default=None, description="触发该事件的规则 ID")
    device_id: int = Field(description="设备 ID")
    device_name: Optional[str] = Field(default=None, description="设备名称")
    device_ip: Optional[str] = Field(default=None, description="设备 IP")
    metric_key: Optional[str] = Field(default=None, description="超标指标键")
    metric_name: Optional[str] = Field(default=None, description="超标指标中文名")
    metric_value: Optional[float] = Field(default=None, description="触发时刻的实测值")
    threshold: Optional[float] = Field(default=None, description="规则阈值")
    operator: Optional[str] = Field(default=None, description="比较符：gt/ge/lt/le/eq/ne")
    level: str = Field(description="级别：critical/major/minor/info")
    title: Optional[str] = Field(default=None, description="告警标题")
    content: Optional[str] = Field(default=None, description="告警正文，推送时直接使用")
    fingerprint: Optional[str] = Field(default=None, description="去重指纹 = md5(device_id + rule_id)；同一设备同一规则只会有一条未恢复事件")
    status: str = Field(default="firing", description="状态：firing 告警中 / acked 已确认 / recovered 已恢复 / closed 已关闭")
    occur_count: int = Field(default=1, description="累计触发次数；同一事件再次触发时 +1，不新增事件")
    first_occur_at: OptionalDateTimeStr = Field(default=None, description="首次发生时间")
    last_occur_at: OptionalDateTimeStr = Field(default=None, description="最近一次发生时间")
    recover_at: OptionalDateTimeStr = Field(default=None, description="恢复时间；未恢复为 null")
    ack_at: OptionalDateTimeStr = Field(default=None, description="确认时间")
    ack_user: Optional[str] = Field(default=None, description="确认人")
    ack_remark: Optional[str] = Field(default=None, description="确认备注")
    rule_name: Optional[str] = Field(default=None, description="所属规则名称（Service 补充）")

    @computed_field(description="级别中文名")
    @property
    def level_text(self) -> str:
        return {"critical": "紧急", "major": "重要", "minor": "一般", "info": "提示"}.get(
            self.level, self.level
        )

    @computed_field(description="状态中文名")
    @property
    def status_text(self) -> str:
        return {
            "firing": "告警中", "acked": "已确认",
            "recovered": "已恢复", "closed": "已关闭",
        }.get(self.status, self.status)

    @computed_field(description="级别配色，前端直接用")
    @property
    def level_color(self) -> str:
        # ⚠ 色值必须与 app/models/alert.py 的 ALERT_LEVEL_COLOR 保持一致
        #   （2026-09-22 UI 改版为亮色主题，色值同步换成白底可读版）。
        #   这里是同一个事实的第二处定义：schema 层按分层约束不能反向依赖 models 层，
        #   代价是改色必须两处一起改 —— 所以两边都写了互相指向的注释。
        return {
            "critical": "#D93A3C", "major": "#C2740C",
            "minor": "#B9891B", "info": "#007ACC",
        }.get(self.level, "#8B9AB0")

    @computed_field(description="持续时间描述，如 已持续 2小时13分")
    @property
    def duration_text(self) -> str:
        """
        告警持续时长。运维最关心"这个告警挂了多久"，
        在服务端算好可保证列表与详情页展示一致。
        """
        if not self.first_occur_at:
            return ""
        end = self.recover_at or datetime.now()
        seconds = int((end - self.first_occur_at).total_seconds())
        if seconds < 60:
            return f"{seconds}秒"
        if seconds < 3600:
            return f"{seconds // 60}分钟"
        if seconds < 86400:
            return f"{seconds // 3600}小时{(seconds % 3600) // 60}分"
        return f"{seconds // 86400}天{(seconds % 86400) // 3600}小时"


class AlertNotifyRecordOut(BaseSchema):
    """通知记录输出"""

    id: int = Field(description="记录 ID")
    event_id: int = Field(description="所属告警事件 ID")
    channel: str = Field(description="渠道：email/wecom/dingtalk/wechat")
    target: Optional[str] = Field(default=None, description="推送目标（邮箱地址 / 群机器人 URL 摘要）")
    status: str = Field(default="pending", description="pending 待发送 / success 已送出 / failed 发送失败")
    retry_count: int = Field(default=0, description="已重试次数")
    response_msg: Optional[str] = Field(default=None, description="渠道返回的原始信息或错误原因；排障第一现场")
    sent_at: OptionalDateTimeStr = Field(default=None, description="实际发送时间")

    @computed_field(description="渠道中文名")
    @property
    def channel_text(self) -> str:
        return {"email": "邮件", "wecom": "企业微信", "dingtalk": "钉钉", "wechat": "微信"}.get(
            self.channel, self.channel
        )


class AlarmLogOut(BaseSchema):
    """告警处理日志（告警详情时间线）"""

    id: int = Field(description="日志 ID")
    event_id: Optional[int] = Field(default=None, description="关联告警事件 ID")
    device_id: Optional[int] = Field(default=None, description="关联设备 ID")
    action: str = Field(description="动作：trigger/notify/recover/ack/converge/close/suppress")
    channel: Optional[str] = Field(default=None, description="通知动作涉及的渠道")
    level: Optional[str] = Field(default=None, description="级别")
    message: Optional[str] = Field(default=None, description="日志内容")
    operator: Optional[str] = Field(default=None, description="操作人；系统自动动作为 system")
    created_at: OptionalDateTimeStr = Field(default=None, description="发生时间")

    @computed_field(description="动作中文名")
    @property
    def action_text(self) -> str:
        return {
            "trigger": "触发告警", "recover": "告警恢复", "ack": "人工确认",
            "notify": "发送通知", "converge": "告警收敛",
            "close": "关闭事件", "suppress": "静默抑制",
        }.get(self.action, self.action)


class AlertAckRequest(BaseSchema):
    """手动确认告警"""

    remark: Optional[str] = Field(default=None, max_length=255, description="处理备注")
    close: bool = Field(default=False, description="确认同时关闭事件（适用已人工处理完毕的情况）")


class AlertEventQuery(BaseSchema):
    """告警列表查询参数"""

    level: Optional[str] = Field(default=None, description="按级别筛选")
    status: Optional[str] = Field(default=None, description="firing/acked/recovered/closed")
    device_id: Optional[int] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    only_unhandled: bool = Field(default=False, description="只看未处理（firing + acked）")


class AlertLevelStat(BaseSchema):
    """按级别统计（大屏告警总数卡片）"""

    level: str
    level_text: str = ""
    count: int = 0
    color: str = ""


class AlertOverviewOut(BaseSchema):
    """告警总览统计"""

    total: int = Field(default=0, description="告警总数")
    active: int = Field(default=0, description="未恢复告警数")
    today: int = Field(default=0, description="今日新增告警数")
    by_level: Dict[str, int] = Field(default_factory=dict, description="按级别统计 {'critical': 2}")
    by_status: Dict[str, int] = Field(default_factory=dict, description="按状态统计")
