"""
钉钉**群机器人**通知渠道（含加签）
===============================================================================
**配置项来源**：sys_config 的 dingtalk 分组

    enabled / webhook / secret（加签密钥，SEC 开头）

**钉钉机器人有三个"安全设置"，必须至少选一个，否则消息一律被拒（errcode=310000）**

| 安全设置 | 配置方式                        | 本渠道支持 | 说明 |
|----------|---------------------------------|------------|------|
| 自定义关键词 | 消息里必须包含该关键词      | 间接支持   | 把关键词拼进标题即可，无需额外配置 |
| 加签     | 后台生成 SEC 开头的密钥         | **已实现** | 推荐，安全性最好，且不受关键词约束 |
| IP 白名单 | 只允许指定出口 IP 调用         | 无需实现   | 由钉钉侧校验，代码无感 |

**加签算法（钉钉官方规定，容易踩坑的两处）**

    string_to_sign = f"{timestamp}\n{secret}"        # ← 中间的 \n 必须有
    sign = urlencode(base64(hmac_sha256(secret, string_to_sign)))
    URL  = webhook + f"&timestamp={timestamp}&sign={sign}"

坑 1：`timestamp` 必须是**毫秒**，且与钉钉服务器时间相差不能超过 1 小时
      （所以不能缓存 timestamp，每次发送都要重新取）。
坑 2：`sign` 必须做 URL 编码（它包含 + / = 等字符），
      手工拼接字符串而不编码会得到 310000。

**为什么遇到 310000 要专门给出提示**：这是钉钉渠道最常见的失败，
原因是"安全设置选了关键词但消息里没有该关键词"。把这个原因直接写进
推送记录，运维一看就知道该去后台改设置，而不是反复重发。
===============================================================================
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any, Dict, Tuple

from app.alarm.channels import AlertMessage, BaseChannel, ChannelResult, ChannelRegistry
from app.models.alert import CHANNEL_DINGTALK

WEBHOOK_TIMEOUT = 8

# 视为"配置失效"的错误码：重试无用
FATAL_ERRCODES = {
    300001: "机器人不在群里，请重新添加并更新 webhook",
    300002: "机器人不存在或 webhook 已失效",
}


@ChannelRegistry.register(CHANNEL_DINGTALK)
class DingtalkChannel(BaseChannel):
    """钉钉群机器人渠道（支持加签）"""

    key = CHANNEL_DINGTALK
    text = "钉钉"
    config_group = "dingtalk"

    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        if not self.enabled(cfg):
            return False, "钉钉机器人未启用（配置页 dingtalk.enabled 开关）"
        webhook = (cfg.get("webhook") or "").strip()
        if not webhook:
            return False, "未填写钉钉机器人 Webhook 地址"
        if not webhook.startswith("https://"):
            return False, "Webhook 地址必须以 https:// 开头"
        if "access_token=" not in webhook:
            return False, "Webhook 地址缺少 access_token 参数（可能复制不完整）"

        secret = (cfg.get("secret") or "").strip()
        if secret and not secret.startswith("SEC"):
            # 钉钉的加签密钥固定以 SEC 开头；填了别的说明复制错了地方
            # （常见错误：把 webhook 里的 access_token 粘到了 secret 里）
            return False, "加签密钥格式不对（应以 SEC 开头）；若使用关键词/IP 白名单请留空此项"
        return True, ""

    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        ready, reason = self.check(cfg)
        if not ready:
            return self.fail(reason, retryable=False)

        webhook = str(cfg.get("webhook")).strip()
        secret = (cfg.get("secret") or "").strip()
        url = self._signed_url(webhook, secret) if secret else webhook
        # 记录时用**未签名**的地址做脱敏：签名每次都变，记下来没有价值还会刷屏
        target = self._mask_url(webhook)

        payload = {
            "msgtype": "markdown",
            "markdown": {
                # 钉钉的 markdown.title 只用于通知栏与部分客户端摘要，正文靠 text
                "title": message.subject(64),
                "text": self._truncate(message.as_markdown()),
            },
        }

        resp = self._post_json(url, payload, timeout=WEBHOOK_TIMEOUT)
        return self._interpret(resp, target)

    # ---------------- 加签 ----------------

    @staticmethod
    def _signed_url(webhook: str, secret: str) -> str:
        """
        生成带 timestamp 与 sign 的完整 URL。

        注意 timestamp 每次都要重新取：钉钉校验"与服务器时间差 < 1 小时"，
        缓存它会让机器人在运行一小时后突然全部发送失败，且报错是 310000
        （与"关键词不匹配"同一个错误码，极难定位）。
        """
        timestamp = str(int(time.time() * 1000))          # 毫秒
        string_to_sign = f"{timestamp}\n{secret}"         # \n 不可省
        digest = hmac.new(
            secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        joiner = "&" if "?" in webhook else "?"
        return f"{webhook}{joiner}timestamp={timestamp}&sign={sign}"

    @staticmethod
    def _interpret(resp: Dict[str, Any], target: str) -> ChannelResult:
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

        if errcode == 310000:
            # 钉钉把"签名错 / 关键词不匹配 / IP 不在白名单"都归到 310000，
            # 因此提示要把三种可能一次说清，避免运维来回试
            return ChannelResult(
                success=False, target=target, retryable=False,
                message=("errcode=310000：消息被拒。请检查机器人「安全设置」——"
                         "① 加签密钥是否正确；② 是否选了自定义关键词而消息未包含该词；"
                         "③ 服务器出口 IP 是否在白名单内"),
            )
        if errcode in FATAL_ERRCODES:
            return ChannelResult(
                success=False, target=target, retryable=False,
                message=f"errcode={errcode}：{FATAL_ERRCODES[errcode]}",
            )
        if errcode == 410100:
            return ChannelResult(
                success=False, target=target, retryable=True,
                message="errcode=410100：机器人发送频率超限，稍后重试",
            )
        return ChannelResult(
            success=False, target=target, retryable=True,
            message=f"errcode={errcode}：{data.get('errmsg', '未知错误')}",
        )
