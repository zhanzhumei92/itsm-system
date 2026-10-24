"""系统配置与日志 Schema（对应模块5：系统配置、日志查询）"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import Field, computed_field, field_validator, model_validator

from app.schemas.common import BaseSchema, OptionalDateTimeStr

# 配置分组中文名（前端 Tab 标题）
CONFIG_GROUP_TEXT = {
    "mail": "邮件告警",
    "wecom": "企业微信机器人",
    "dingtalk": "钉钉机器人",
    "wechat": "微信推送",
    "collect": "采集与保留策略",
    "system": "平台信息",
}


class ConfigItemOut(BaseSchema):
    """
    单个配置项输出。

    is_secret=True 的项，config_value 已被替换为 "***" 或 ""（未配置），
    同时提供 is_configured 布尔值让前端显示"已配置/未配置"状态。
    """

    id: int = Field(description="配置项 ID")
    config_key: str = Field(description="配置键，全平台唯一")
    config_value: Optional[str] = Field(default=None, description="配置值；敏感项已替换为 ***，未提供值时为空")  # fmt: skip
    config_group: str = Field(description="分组标识")
    value_type: str = Field(default="string", description="值类型：string/int/bool/json/password")
    is_secret: bool = Field(default=False, description="是否敏感项；为真时出参已脱敏")
    description: Optional[str] = Field(default=None, description="配置项本身的中文说明")
    updated_at: OptionalDateTimeStr = Field(default=None, description="最后修改时间")

    @computed_field(description="敏感项是否已配置（前端显示绿点/灰点）")
    @property
    def is_configured(self) -> bool:
        """
        判断是否已配置。
        敏感项返回 *** 或空，非敏感项按值是否非空判断。
        """
        if self.is_secret:
            return self.config_value == "***"
        return bool(self.config_value not in (None, ""))


class ConfigGroupOut(BaseSchema):
    """按分组返回的配置集合（系统配置页一个 Tab 一个分组）"""

    group: str = Field(description="分组标识")
    group_text: str = Field(default="", description="分组中文名")
    items: List[ConfigItemOut] = Field(default_factory=list, description="该分组下的配置项")


class ConfigUpdateItem(BaseSchema):
    """单个配置项的更新请求"""

    config_key: str = Field(..., max_length=100, description="要修改的配置键")
    config_value: Optional[str] = Field(default=None, description="敏感项传 *** 表示不修改")


class ConfigBatchUpdate(BaseSchema):
    """批量更新配置（前端"保存"按钮一次提交一个分组的所有项）"""

    items: List[ConfigUpdateItem] = Field(default_factory=list, description="待更新配置项列表")


class ConfigTestRequest(BaseSchema):
    """测试告警渠道连通性"""

    group: str = Field(..., description="mail/wecom/dingtalk/wechat")


class ConfigTestResult(BaseSchema):
    """渠道测试结果"""

    success: bool = Field(description="是否连通成功")
    message: str = Field(description="结果说明；失败时为可直接展示给人看的原因")
    duration_ms: int = Field(default=0, description="耗时（毫秒）")


class SysLogOut(BaseSchema):
    """系统日志输出"""

    id: int = Field(description="日志 ID")
    level: str = Field(description="INFO/WARNING/ERROR")
    module: Optional[str] = Field(default=None, description="模块名")
    action: Optional[str] = Field(default=None, description="动作名")
    message: Optional[str] = Field(default=None, description="日志内容")
    user_id: Optional[int] = Field(default=None, description="操作人 ID")
    username: Optional[str] = Field(default=None, description="操作人账号")
    ip: Optional[str] = Field(default=None, description="来源 IP")
    created_at: OptionalDateTimeStr = Field(default=None, description="发生时间")


class CollectLogOut(BaseSchema):
    """采集日志输出"""

    id: int = Field(description="日志 ID")
    device_id: int = Field(description="设备 ID")
    device_name: Optional[str] = Field(default=None, description="设备名称")
    device_ip: Optional[str] = Field(default=None, description="设备 IP")
    protocol: Optional[str] = Field(default=None, description="采集协议")
    status: str = Field(description="success 成功 / fail 失败 / timeout 超时")
    duration_ms: Optional[int] = Field(default=None, description="耗时（毫秒）")
    error_msg: Optional[str] = Field(default=None, description="失败原因")
    metric_count: Optional[int] = Field(default=0, description="本次采集到的指标数")
    collect_time: OptionalDateTimeStr = Field(default=None, description="采集时间")

    @computed_field(description="状态中文名")
    @property
    def status_text(self) -> str:
        return {"success": "成功", "fail": "失败", "timeout": "超时"}.get(self.status, self.status)

    @computed_field(description="耗时展示，如 42ms / 1.3s")
    @property
    def duration_text(self) -> str:
        if self.duration_ms is None:
            return ""
        return f"{self.duration_ms}ms" if self.duration_ms < 1000 else f"{self.duration_ms / 1000:.2f}s"


class LogQuery(BaseSchema):
    """日志查询参数"""

    level: Optional[str] = Field(default=None, description="日志级别（系统日志）")
    status: Optional[str] = Field(default=None, description="采集状态 success/fail/timeout")
    device_id: Optional[int] = None
    module: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None


class CollectStatOut(BaseSchema):
    """采集健康度统计（用于采集日志页顶部指标卡）"""

    total: int = Field(default=0, description="总采集次数")
    success: int = Field(default=0, description="成功次数")
    fail: int = Field(default=0, description="失败次数")
    timeout: int = Field(default=0, description="超时次数")
    success_rate: float = Field(default=0.0, description="成功率百分比")
    avg_duration_ms: int = Field(default=0, description="平均耗时（毫秒）")


class PlatformInfoOut(BaseSchema):
    """平台基础信息（前端登录页、大屏页脚使用）"""

    platform_name: str = Field(description="平台名称")
    version: str = Field(description="版本号")
    company_name: str = Field(description="公司名称")
    dashboard_refresh: int = Field(default=15, description="大屏自动刷新间隔（秒）")
    scheduler_running: bool = Field(default=False, description="采集调度器是否在运行")
    device_total: int = Field(default=0, description="设备总数")
    device_online: int = Field(default=0, description="在线设备数")
    alert_active: int = Field(default=0, description="未恢复告警数")


# ============================================================================
# 自定义定时任务（系统配置页"定时任务"面板）
# ============================================================================

VALID_TASK_TRIGGERS = {"interval", "cron"}


class ScheduledTaskActionOut(BaseSchema):
    """可被定时调度的维护动作（"新建任务"弹窗的下拉数据源）"""

    action: str = Field(description="动作标识，创建任务时提交用")
    label: str = Field(description="动作中文名")
    description: str = Field(description="动作说明")


class ScheduledTaskIn(BaseSchema):
    """
    创建/编辑自定义定时任务入参。

    周期二选一：trigger_type=interval 必填 interval_seconds（最小 10 秒）；
    trigger_type=cron 必填 cron_expr（5 段表达式，Asia/Shanghai 时区）。
    cron 合法性在这里用 APScheduler 预解析一次，让用户在保存时就拿到报错，
    而不是等到下次调度才无声失败（调度器侧 _build_trigger 仍会兜底校验）。
    """

    name: str = Field(min_length=1, max_length=100, description="任务名称，全平台唯一")
    action: str = Field(min_length=1, max_length=64, description="动作标识，取值见 GET /scheduler/actions")
    trigger_type: str = Field(description="interval=固定间隔 / cron=Cron 表达式")
    interval_seconds: Optional[int] = Field(default=None, ge=10, le=31_536_000,
        description="间隔秒数（trigger_type=interval 时必填），最小 10 秒")
    cron_expr: Optional[str] = Field(default=None, max_length=64,
        description="5 段 cron 表达式（trigger_type=cron 时必填），如 30 3 * * *")
    enabled: bool = Field(default=True, description="保存后是否立即启用调度")
    remark: Optional[str] = Field(default=None, max_length=255, description="备注")

    @field_validator("trigger_type")
    @classmethod
    def _check_trigger(cls, v: str) -> str:
        if v not in VALID_TASK_TRIGGERS:
            raise ValueError(f"触发类型只能是 {'/'.join(sorted(VALID_TASK_TRIGGERS))}")
        return v

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        from app.scheduler.custom_tasks import get_action
        if get_action(v) is None:
            raise ValueError("不支持的任务动作，请从动作目录中选择")
        return v

    @model_validator(mode="after")
    def _check_schedule(self) -> "ScheduledTaskIn":
        """周期二选一的完整性校验 + cron 预解析"""
        if self.trigger_type == "interval":
            if self.interval_seconds is None:
                raise ValueError("固定间隔类型必须填写间隔秒数（最小 10）")
        else:
            if not self.cron_expr:
                raise ValueError("Cron 类型必须填写 cron 表达式")
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger.from_crontab(self.cron_expr.strip(), timezone="Asia/Shanghai")
            except ValueError as exc:
                raise ValueError(f"cron 表达式不合法：{exc}") from exc
        return self


class ScheduledTaskOut(BaseSchema):
    """自定义定时任务出参（管理列表行）"""

    id: int = Field(description="任务 ID")
    name: str = Field(description="任务名称")
    action: str = Field(description="动作标识")
    action_label: str = Field(description="动作中文名（未注册时显示原值）")
    trigger_type: str = Field(description="interval / cron")
    interval_seconds: Optional[int] = Field(default=None, description="间隔秒数（interval 类型）")
    cron_expr: Optional[str] = Field(default=None, description="cron 表达式（cron 类型）")
    enabled: bool = Field(description="是否启用调度")
    remark: Optional[str] = Field(default=None, description="备注")
    last_run_at: Optional[str] = Field(default=None, description="最近一次执行时间")
    last_run_ok: Optional[bool] = Field(default=None, description="最近一次执行是否成功（null=从未执行）")
    last_run_message: Optional[str] = Field(default=None, description="最近一次执行结果摘要或错误信息")
    next_run_at: Optional[str] = Field(default=None, description="下次执行时间（停用或调度器未运行时为空）")
