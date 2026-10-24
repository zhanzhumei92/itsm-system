"""
告警通知渠道：统一消息体 + 渠道基类 + 注册器
===============================================================================
**为什么要抽象出「统一消息体」**

四个渠道的消息格式完全不同，但**内容必须一致**：
同一场故障，邮件里写"CPU 93% 阈值 85%"、企微里写"CPU 使用率过高"，
值班人员会怀疑"到底哪个是真的"。所以先把告警翻译成一个与渠道无关的
AlertMessage，再由各渠道负责"怎么排版"：

    告警事件 + 规则 + 设备  ──►  AlertMessage  ──┬──► EmailChannel    （纯文本 + HTML）
                                                 ├──► WecomChannel    （markdown）
                                                 ├──► DingtalkChannel （markdown）
                                                 └──► WechatChannel   （纯文本）

**新增一个渠道的完整步骤**
  1. 在本目录新建模块，如 feishu.py
  2. 继承 BaseChannel，定义 key / text / config_group
  3. 实现 check(cfg) 与 send(message, cfg)
  4. 用 @ChannelRegistry.register("feishu") 装饰
  5. 在本文件末尾的 _OPTIONAL_MODULES 中登记模块名（不 import 就不会注册）
  完成后 evaluator / notifier / 前端下拉框全部自动认识它，无需改任何既有代码。

**两个贯穿所有渠道的约定**
  1. **绝不把配置里的密钥写进日志或返回值**：
     webhook 地址里带 key、corpsecret 是凭据，
     `_mask_url()` 统一脱敏后才允许出现在 response_msg 与日志中
     —— 告警详情的"推送记录"是运维天天看的页面，泄漏概率极高。
  2. **"渠道没配好"不是"发送失败"**：
     check() 返回未通过时，ChannelResult.retryable=False，
     notifier 不会浪费 2 次重试与退避等待。
===============================================================================
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Type

from app.models.alert import ALERT_LEVEL_TEXT

# ---------------------------------------------------------------------------
# 渠道标识（与 models/alert.py 的 CHANNEL_* 常量、sys_config.config_group 对齐）
# 直接引用模型层常量而不是重写字面量，避免两处对不上。
# ---------------------------------------------------------------------------
from app.models.alert import (
    CHANNEL_DINGTALK,
    CHANNEL_EMAIL,
    CHANNEL_WECHAT,
    CHANNEL_WECOM,
)

CHANNEL_TEXT = {
    CHANNEL_EMAIL: "邮件",
    CHANNEL_WECOM: "企业微信",
    CHANNEL_DINGTALK: "钉钉",
    CHANNEL_WECHAT: "微信",
}

# 单条消息的字节上限（企业微信机器人限制 4096 字节，取最严的作为统一上限）。
# 超长告警正文必须截断，否则渠道返回 400 而整条告警推不出去
# —— 截断并标注"已截断"比推不出去好。
MAX_MESSAGE_BYTES = 4000


# ===========================================================================
# 一、统一消息体
# ===========================================================================

@dataclass
class AlertMessage:
    """
    与渠道无关的告警消息。

    字段刻意做成"已经格式化好的文本"而不是原始数值：
    每个渠道都自己判断"这个指标该带 % 还是 ms"是重复且容易不一致的逻辑，
    统一在 notifier.build_message() 里算好一次。
    """

    event_id: int
    event_no: str
    title: str
    device_name: str
    device_ip: str
    device_type: str = ""
    location: str = ""
    metric_name: str = ""
    metric_value_text: str = "无法获取"
    threshold_text: str = ""
    level: str = "major"
    occurred_at: datetime = field(default_factory=datetime.now)
    is_recovery: bool = False
    duration_text: str = ""
    occur_count: int = 1
    platform_name: str = "亿网通IT运维监控管理平台"
    platform_url: str = ""
    #: 事件正文（services/alert_service._build_content 生成的多行文本，库里已存一份）
    content: str = ""

    # ---------------- 展示辅助 ----------------

    @property
    def level_text(self) -> str:
        """级别中文名（字典外的历史取值原样透出，不崩）"""
        return ALERT_LEVEL_TEXT.get(self.level, self.level)

    @property
    def action_text(self) -> str:
        return "告警恢复" if self.is_recovery else "触发告警"

    @property
    def level_icon(self) -> str:
        """纯文本渠道用图标代替颜色，一眼分辨严重程度"""
        return {
            "critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵",
        }.get(self.level, "⚪")

    # ---------------- 各渠道共用的两种排版 ----------------

    def as_text(self) -> str:
        """
        纯文本排版（邮件正文、企业微信应用消息）。

        行序刻意与 alert_service._build_content 保持一致：
        运维在平台上看的详情、邮件里看的正文、企微里看到的文字，
        字段顺序相同才能"扫一眼就对上"。
        """
        head = "【告警恢复】" if self.is_recovery else "【告警通知】"
        lines = [
            f"{head}{self.title}",
            "-" * 36,
            f"设备名称：{self.device_name}",
            f"设备IP　：{self.device_ip}",
        ]
        if self.device_type:
            lines.append(f"设备类型：{self.device_type}")
        if self.location:
            lines.append(f"位置　　：{self.location}")
        lines += [
            f"监控指标：{self.metric_name}",
            f"当前值　：{self.metric_value_text}",
        ]
        if self.threshold_text:
            lines.append(f"告警阈值：{self.threshold_text}")
        lines.append(f"告警级别：{self.level_text}")
        if self.is_recovery:
            if self.duration_text:
                lines.append(f"持续时长：{self.duration_text}")
            lines.append(f"恢复时间：{self.occurred_at:%Y-%m-%d %H:%M:%S}")
        else:
            lines.append(f"告警时间：{self.occurred_at:%Y-%m-%d %H:%M:%S}")
            if self.occur_count > 1:
                lines.append(f"累计发生：{self.occur_count} 次")
        lines.append(f"事件编号：{self.event_no}")
        if self.platform_url:
            lines.append(f"平台地址：{self.platform_url}")
        lines.append("-" * 36)
        lines.append(f"来自 {self.platform_name}")
        return "\n".join(lines)

    def as_markdown(self) -> str:
        """
        Markdown 排版（企业微信机器人、钉钉机器人）。

        两家的 markdown 都只支持**子集**：不支持表格、不支持图片语法。
        因此统一只用「标题 + 引用 + 加粗 + font color」，两边都能正确渲染。
        """
        color = {
            "critical": "warning", "major": "comment",
            "minor": "comment", "info": "info",
        }.get(self.level, "comment")
        rows = [
            f"### {self.level_icon} {self.title}",
            f"> **设备**：{self.device_name}（{self.device_ip}）",
        ]
        if self.location:
            rows.append(f"> **位置**：{self.location}")
        rows.append(f"> **指标**：{self.metric_name}")
        rows.append(f'> **当前值**：<font color="{color}">{self.metric_value_text}</font>')
        if self.threshold_text:
            rows.append(f"> **阈值**：{self.threshold_text}")
        rows.append(f"> **级别**：{self.level_text}")
        if self.is_recovery:
            if self.duration_text:
                rows.append(f"> **持续时长**：{self.duration_text}")
            rows.append(f"> **恢复时间**：{self.occurred_at:%Y-%m-%d %H:%M:%S}")
        else:
            rows.append(f"> **时间**：{self.occurred_at:%Y-%m-%d %H:%M:%S}")
            if self.occur_count > 1:
                rows.append(f"> **累计发生**：{self.occur_count} 次")
        rows.append(f"> **事件编号**：{self.event_no}")
        if self.platform_url:
            rows.append(f"\n[{self.platform_name}]({self.platform_url})")
        else:
            rows.append(f"\n{self.platform_name}")
        return "\n".join(rows)

    def subject(self, max_len: int = 120) -> str:
        """邮件标题：带级别前缀与恢复标记，便于在邮件列表里排序筛选"""
        prefix = f"[恢复][{self.level_text}]" if self.is_recovery else f"[{self.level_text}]"
        return f"{prefix}{self.title}"[:max_len]


# ===========================================================================
# 二、发送结果
# ===========================================================================

@dataclass
class ChannelResult:
    """
    单次发送结果。

    :param retryable: 是否值得重试。
        网络超时 / 5xx / 机器人限流 → True；
        地址没填、密钥错误、参数非法 → False
        （重试只会浪费重试配额，还会拖慢其它渠道的送达）。
    """

    success: bool
    message: str = ""
    target: str = ""
    retryable: bool = True

    def __bool__(self) -> bool:      # 便于 `if result:` 直接判断
        return self.success


# ===========================================================================
# 三、渠道基类
# ===========================================================================

class BaseChannel(ABC):
    """
    告警渠道基类。

    子类必须实现 check() 与 send()，且**都不得抛出未捕获异常**：
    notifier 在采集线程池里调用它们，异常逃逸会污染线程池的 Future。
    统一用 self.fail() / self.ok() 构造结果。
    """

    #: 渠道标识（email / wecom / dingtalk / wechat），与 alert_rule.notify_channels 取值一致
    key: str = ""

    #: 中文名（前端展示、日志）
    text: str = ""

    #: sys_config 中该渠道配置的分组名（config_service.get_channel_settings 的入参）
    config_group: str = ""

    # ---------------- 子类必须实现 ----------------

    @abstractmethod
    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        """
        检查配置是否可用。

        :param cfg: config_service.get_channel_settings(db, config_group) 的结果，
                    敏感项（口令 / webhook / secret）**已解密**
        :return: (是否可用, 不可用的原因)。原因文本会写进推送记录，
                 所以要写成人话，例如"未填写收件人"而不是"receivers empty"
        """
        raise NotImplementedError

    @abstractmethod
    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        """
        发送一条告警。

        :return: ChannelResult。**实现里必须自行捕获所有异常**，
                 异常要转成 ChannelResult(success=False) 而不是向上抛。
        """
        raise NotImplementedError

    # ---------------- 结果构造助手 ----------------

    def ok(self, target: str = "", message: str = "发送成功") -> ChannelResult:
        return ChannelResult(success=True, message=message, target=target)

    def fail(self, message: str, target: str = "", retryable: bool = False) -> ChannelResult:
        return ChannelResult(success=False, message=message, target=target, retryable=retryable)

    # ---------------- 通用助手 ----------------

    def enabled(self, cfg: Dict[str, Any]) -> bool:
        """
        渠道是否启用。

        兼容 1/0、true/false/yes/on，与 config_service.get_bool 的判断口径一致
        （这里不直接调 get_bool 是因为 cfg 已是"取出来的字典"，不再持有 Session）。
        """
        raw = cfg.get("enabled")
        if raw is None:
            return False
        return str(raw).strip().lower() in ("1", "true", "yes", "on", "启用")

    @staticmethod
    def _mask_url(url: Optional[str]) -> str:
        """
        URL 脱敏：保留域名与路径，把 key/token/secret 一类参数替换成 ***。

        为什么必须做：企业微信 / 钉钉的 webhook 地址本身就等价于密钥，
        谁能看到它谁就能往群里发消息。而"推送记录"是运维天天要看的页面。
        """
        if not url:
            return ""
        masked = re.sub(
            r"(?i)\b(key|token|access_token|secret|corpsecret|sign|webhook)=([^&\s]+)",
            r"\1=***", url,
        )
        return masked[:200]

    #: 截断提示后缀。它的字节数必须在截断时被预留出来（见 _truncate）
    TRUNCATE_SUFFIX = "\n…（内容过长已截断）"

    @classmethod
    def _truncate(cls, text: str, limit: int = MAX_MESSAGE_BYTES) -> str:
        """
        按**字节**截断（不是字符），并保证结果不超过 limit 字节。

        企业微信的限制是 4096 字节，一个中文字符占 3 字节，
        按字符截断会在纯中文长消息上超限，渠道直接返回 400 ——
        整条告警推不出去，比截断糟糕得多。

        ⚠ 后缀的字节数必须从 limit 里扣掉：
        早期版本写的是"预留 20 字节"，而后缀 `\\n…（内容过长已截断）`
        实际占 34 字节，于是截断结果其实是 limit+14 字节。
        当时没被发现是因为 4014 < 4096（渠道真实上限）、2014 < 2048，
        恰好都没超 —— 但函数的契约是假的，换个更严的渠道就会踩。
        这里改成按后缀真实字节数预留，契约与实现一致。
        """
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text
        suffix_bytes = len(cls.TRUNCATE_SUFFIX.encode("utf-8"))
        keep = raw[: max(limit - suffix_bytes, 1)]
        # 避免把多字节字符截成半个（UnicodeDecodeError 的来源）
        for cut in range(4):
            try:
                return keep[: len(keep) - cut].decode("utf-8") + cls.TRUNCATE_SUFFIX
            except UnicodeDecodeError:
                continue
        # 理论上不可达（UTF-8 单个字符最多 4 字节，cut 到 3 必然对齐）；
        # 保底按字符数粗截，宁可不精确也不能抛异常让告警推不出去
        return text[: max(limit // 3, 1)] + cls.TRUNCATE_SUFFIX

    @staticmethod
    def _post_json(url: str, payload: Dict[str, Any], timeout: int = 10) -> Dict[str, Any]:
        """
        发送 JSON 请求并解析响应。

        用标准库 urllib 而**不引入 requests/httpx**：
        告警渠道是"平台出故障时唯一还能用的通路"，依赖越少越好；
        而且这几个平台都是"一个 POST 返回一个 JSON"，标准库足够。
        （httpx 只是 TestClient 的测试依赖，不应让业务代码依赖它。）

        异常统一包装成 {"_http_ok": False, "_error": "..."} 返回，
        由调用方决定是否可重试，本函数不抛异常。
        """
        import urllib.error
        import urllib.request

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return {"_http_ok": True, "_status": resp.status, "_data": json.loads(raw)}
                except ValueError:
                    # 部分反向代理会返回 HTML 错误页而不是 JSON
                    return {"_http_ok": True, "_status": resp.status, "_raw": raw[:500]}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - 读错误响应体失败不影响主流程
                pass
            return {"_http_ok": False, "_status": exc.code, "_raw": detail,
                    "_error": f"HTTP {exc.code}"}
        except urllib.error.URLError as exc:
            return {"_http_ok": False, "_error": f"网络不可达：{exc.reason}"}
        except Exception as exc:  # noqa: BLE001 - 超时、SSL 错误等
            return {"_http_ok": False, "_error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _get_json(url: str, timeout: int = 10) -> Dict[str, Any]:
        """发送 GET 请求并解析 JSON（企业微信应用消息换取 access_token 用）"""
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return {"_http_ok": True, "_data": json.loads(raw)}
                except ValueError:
                    return {"_http_ok": True, "_raw": raw[:500]}
        except urllib.error.HTTPError as exc:
            return {"_http_ok": False, "_error": f"HTTP {exc.code}"}
        except urllib.error.URLError as exc:
            return {"_http_ok": False, "_error": f"网络不可达：{exc.reason}"}
        except Exception as exc:  # noqa: BLE001
            return {"_http_ok": False, "_error": f"{type(exc).__name__}: {exc}"}


# ===========================================================================
# 四、渠道注册器
# ===========================================================================

class ChannelRegistry:
    """
    通知渠道注册器。

    与 CollectorRegistry / ParserRegistry 同构（本平台三处扩展点统一风格）。
    注册的是**实例**：渠道实现无状态，配置通过 cfg 参数传入，
    因此可以安全地被多线程并发调用。
    """

    _registry: Dict[str, BaseChannel] = {}

    @classmethod
    def register(cls, key: str):
        """装饰器：注册渠道"""
        def decorator(channel_cls: Type[BaseChannel]) -> Type[BaseChannel]:
            k = (key or "").lower().strip()
            if not k:
                raise ValueError("渠道标识不能为空")
            channel_cls.key = k
            channel_cls.config_group = channel_cls.config_group or k
            if not channel_cls.text:
                channel_cls.text = CHANNEL_TEXT.get(k, k)
            cls._registry[k] = channel_cls()
            return channel_cls

        return decorator

    @classmethod
    def get(cls, key: str) -> Optional[BaseChannel]:
        """按标识取渠道实例"""
        return cls._registry.get((key or "").lower().strip())

    @classmethod
    def available(cls) -> List[str]:
        """已注册的渠道标识列表"""
        return sorted(cls._registry.keys())

    @classmethod
    def describe(cls) -> List[Dict[str, str]]:
        """渠道清单（供前端展示"当前支持哪些通知渠道"）"""
        return [
            {"key": k, "text": v.text, "config_group": v.config_group}
            for k, v in sorted(cls._registry.items())
        ]

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅单元测试使用）"""
        cls._registry.clear()


# ---------------------------------------------------------------------------
# 渠道模块注册入口
#
# 与采集器 / 解析器同样采用容错导入：某个渠道模块写坏了 / 依赖缺失，
# 只影响该渠道，其它渠道照常推送。
# ---------------------------------------------------------------------------
_OPTIONAL_MODULES = ("email", "wecom", "dingtalk", "wechat")

for _module_name in _OPTIONAL_MODULES:
    try:
        __import__(f"app.alarm.channels.{_module_name}")
    except ImportError as _exc:  # pragma: no cover - 仅在模块缺失时触发
        from app.core.logger import system_logger as _log
        _log.warning("告警渠道模块 app.alarm.channels.%s 未加载：%s", _module_name, _exc)

__all__ = [
    "AlertMessage",
    "ChannelResult",
    "BaseChannel",
    "ChannelRegistry",
    "CHANNEL_TEXT",
    "MAX_MESSAGE_BYTES",
    "CHANNEL_EMAIL",
    "CHANNEL_WECOM",
    "CHANNEL_DINGTALK",
    "CHANNEL_WECHAT",
]
