"""
企业微信**群机器人**通知渠道
===============================================================================
**配置项来源**：sys_config 的 wecom 分组

    enabled / webhook / msg_type（text | markdown）

**与企业微信「应用消息」（wechat 渠道）的区别——现场最常混淆的一对**

| 维度     | 群机器人（本文件）                    | 应用消息（channels/wechat.py）        |
|----------|---------------------------------------|---------------------------------------|
| 接入方式 | 群里"添加群机器人"，拿一个 Webhook    | 企业微信后台建自建应用，拿 CorpID/Secret |
| 归属     | 发到**某一个群**                      | 发给**指定成员**（可 @all）           |
| 消息留痕 | 就在群里，全员可见，适合运维值班群    | 在应用会话里，适合定向派单给责任人    |
| 配置难度 | 极低（复制一条 URL）                  | 需要企业管理员权限建应用、配可信 IP   |

**为什么两个都要做**：现场实施时，运维值班群几乎一定存在（群机器人 5 分钟搞定），
而"定向发给某位责任人"需要企业管理员配合。两个渠道并存，
实施第一天就能让客户看到告警真的能到人手上，而不是卡在审批流程里。

**实现要点**

1. **msg_type 决定排版**：markdown 能加粗、着色、带链接；text 只有纯文本。
   默认 markdown，但保留 text 是因为部分老版本企业微信客户端
   在群机器人上渲染 markdown 异常（显示成原始符号）。
2. **errcode 2003 是关键错误**："机器人不在群里"或"webhook 被删"，
   属于配置失效，重试无用。这条错误现场出现频率最高，
   单独给出可操作的提示（重新添加机器人并更新 webhook）。
===============================================================================
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from app.alarm.channels import AlertMessage, BaseChannel, ChannelResult, ChannelRegistry
from app.core.logger import system_logger
from app.models.alert import CHANNEL_WECOM

# 机器人发送超时（秒）。比邮件短：机器人不可达时应该快速失败，
# 让同一事件的其它渠道（邮件/钉钉）能尽早开始。
WEBHOOK_TIMEOUT = 8

# 视为"配置失效"的错误码：重试无用，需要人工去企业微信群里重新添加机器人
FATAL_ERRCODES = {
    93000: "机器人不存在或 webhook 已失效",
    2003: "机器人已被移出群聊，请重新添加并更新 webhook",
}


@ChannelRegistry.register(CHANNEL_WECOM)
class WecomChannel(BaseChannel):
    """企业微信群机器人渠道"""

    key = CHANNEL_WECOM
    text = "企业微信"
    config_group = "wecom"

    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        if not self.enabled(cfg):
            return False, "企业微信机器人未启用（配置页 wecom.enabled 开关）"
        webhook = (cfg.get("webhook") or "").strip()
        if not webhook:
            return False, "未填写企业微信群机器人 Webhook 地址"
        if not webhook.startswith("https://"):
            # 企业微信只接受 https；填成 http 会在发送时被重定向后失败，
            # 提前拦下来能省一次无效的网络往返
            return False, "Webhook 地址必须以 https:// 开头"
        if "key=" not in webhook:
            return False, "Webhook 地址缺少 key 参数（可能复制不完整）"
        return True, ""

    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        ready, reason = self.check(cfg)
        if not ready:
            return self.fail(reason, retryable=False)

        webhook = str(cfg.get("webhook")).strip()
        target = self._mask_url(webhook)
        msg_type = str(cfg.get("msg_type") or "markdown").strip().lower()
        if msg_type not in ("text", "markdown"):
            # 配置里填了别的值时退化为 markdown，而不是直接失败
            system_logger.warning("企业微信 msg_type 取值异常（%s），已按 markdown 发送", msg_type)
            msg_type = "markdown"

        if msg_type == "markdown":
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": self._truncate(message.as_markdown())},
            }
        else:
            payload = {
                "msgtype": "text",
                # text 类型不支持超长内容，统一截断到 2000 字符以内更安全
                "text": {"content": self._truncate(message.as_text(), 2000)},
            }

        resp = self._post_json(webhook, payload, timeout=WEBHOOK_TIMEOUT)
        return self._interpret(resp, target)

    @staticmethod
    def _interpret(resp: Dict[str, Any], target: str) -> ChannelResult:
        """
        解读企业微信返回。

        企业微信的失败有两种形态，必须分开处理：
          - HTTP 200 + errcode != 0：业务错误（机器人失效、内容超限）
          - HTTP != 200 / 无响应：网络层问题（可重试）
        """
        if not resp.get("_http_ok"):
            return ChannelResult(
                success=False, target=target, retryable=True,
                message=f"请求失败：{resp.get('_error') or resp.get('_raw') or '无响应'}",
            )

        data = resp.get("_data")
        if data is None:
            return ChannelResult(
                success=False, target=target, retryable=True,
                message=f"响应不是 JSON（可能被代理拦截）：{resp.get('_raw', '')[:200]}",
            )

        errcode = data.get("errcode", 0)
        if errcode == 0:
            return ChannelResult(success=True, target=target, message="发送成功")

        if errcode in FATAL_ERRCODES:
            return ChannelResult(
                success=False, target=target, retryable=False,
                message=f"errcode={errcode}：{FATAL_ERRCODES[errcode]}",
            )
        if errcode == 45009:
            return ChannelResult(
                success=False, target=target, retryable=True,
                message="errcode=45009：机器人调用频率超限，稍后重试",
            )
        return ChannelResult(
            success=False, target=target, retryable=True,
            message=f"errcode={errcode}：{data.get('errmsg', '未知错误')}",
        )
