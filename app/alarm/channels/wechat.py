"""
微信推送渠道（企业微信**自建应用**消息，定向发给指定成员）
===============================================================================
**配置项来源**：sys_config 的 wechat 分组

    enabled / corp_id / corp_secret / agent_id / to_user

**与「群机器人」（channels/wecom.py）的区别**：见 wecom.py 文件头的对比表。
一句话：机器人发给**群**，本渠道发给**人**。定向派单用这个。

**两步调用（企业微信官方接口）**

    1) GET  https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=&corpsecret=
       → {"errcode":0, "access_token":"...", "expires_in":7200}

    2) POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=...
       → {"errcode":0, "errmsg":"ok", "invaliduser":""}

**access_token 必须缓存，且这个缓存有一个"看起来对、其实会出事故"的写法**

企业微信对 gettoken 有**每日调用次数限制**，而 access_token 有效期 7200 秒。
如果每次发告警都去换一次 token，一个告警风暴（几十台设备同时离线）
就能把当日配额打光，之后所有推送全部失败 —— 而且失败发生在
"最需要告警的时候"，非常糟糕。

因此本实现做了两级保护：
  1. 模块级缓存 token，**提前 300 秒过期**（避免边界上刚好失效）；
  2. 用**锁**保护刷新：并发的多个发送线程只会有一次真的去换 token，
     其余线程复用结果，不会出现"20 个线程同时 gettoken"。

**另一个关键点：`errcode=0` 不等于"所有人都收到了"**
企业微信在部分成员无效时仍返回 errcode=0，只在 `invaliduser` 里列出无效账号。
不解析这个字段，就会出现"运维以为通知了，实际关键责任人根本不在收件人里"。
===============================================================================
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from app.alarm.channels import AlertMessage, BaseChannel, ChannelResult, ChannelRegistry
from app.core.logger import system_logger
from app.models.alert import CHANNEL_WECHAT

TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

HTTP_TIMEOUT = 10

# token 提前过期的秒数。剩余寿命少于这个值就主动刷新，
# 避免"取出时还有 2 秒有效期，请求到达企业微信时已过期"的边界失败。
TOKEN_REFRESH_AHEAD = 300

# 视为"配置失效"的错误码：重试无用，必须去企业微信后台改配置
FATAL_ERRCODES = {
    40013: "CorpID 无效",
    40056: "AgentId 无效",
    40001: "Secret 无效（请确认用的是自建应用的 Secret）",
    60011: "应用没有权限发送消息给该成员（检查应用可见范围）",
    82001: "接收人为空，请填写 to_user（成员账号，多个用 | 分隔，@all 表示全员）",
}

# token 缓存（进程级）。多线程共享，用锁序列化刷新。
_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_token_expire_at: float = 0.0


def reset_token_cache() -> None:
    """
    清空 token 缓存。

    用途：配置页改了 corp_id / corp_secret 之后，旧 token 立刻失效，
    不清缓存会让改完配置的头几分钟仍然报"Secret 无效"，
    运维会以为配置没保存成功。config_service 保存配置后应调用本函数。
    """
    global _cached_token, _token_expire_at
    with _token_lock:
        _cached_token = None
        _token_expire_at = 0.0


@ChannelRegistry.register(CHANNEL_WECHAT)
class WechatChannel(BaseChannel):
    """企业微信自建应用消息渠道（定向发给成员）"""

    key = CHANNEL_WECHAT
    text = "微信"
    config_group = "wechat"

    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        if not self.enabled(cfg):
            return False, "微信推送未启用（配置页 wechat.enabled 开关）"
        if not (cfg.get("corp_id") or "").strip():
            return False, "未填写企业微信 CorpID"
        if not (cfg.get("corp_secret") or "").strip():
            # Secret 是敏感项；密钥更换导致解密失败时也会走到这里
            return False, "未配置应用 Secret（或密钥已更换导致解密失败）"
        agent_id = str(cfg.get("agent_id") or "").strip()
        if not agent_id:
            return False, "未填写应用 AgentId"
        if not agent_id.isdigit():
            # AgentId 在企业微信后台是一串数字，填成名称是常见的手滑
            return False, f"AgentId 应为数字，当前填的是 {agent_id!r}"
        if not (cfg.get("to_user") or "").strip():
            return False, "未填写接收人（可填 @all 表示全员）"
        return True, ""

    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        ready, reason = self.check(cfg)
        if not ready:
            return self.fail(reason, retryable=False)

        to_user = str(cfg.get("to_user")).strip()
        token, err = self._get_token(str(cfg.get("corp_id")).strip(),
                                     str(cfg.get("corp_secret")).strip())
        if token is None:
            # 换 token 失败：网络问题可重试，账号问题不可重试
            return self.fail(f"获取 access_token 失败：{err}", target=to_user,
                             retryable=not str(err).startswith("errcode"))

        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": int(str(cfg.get("agent_id")).strip()),
            # 企业微信应用消息的 text 上限较小，且不渲染 markdown，
            # 所以直接给纯文本（与邮件正文同源，保证内容一致）
            "text": {"content": self._truncate(message.as_text(), 2000)},
            # 关闭重复消息检查：同一秒内多条告警内容相同是正常的
            # （例如同一批设备同时离线），企业微信默认会丢弃，必须显式关闭
            "duplicate_check_interval": 0,
        }

        resp = self._post_json(f"{SEND_URL}?access_token={token}",
                               payload, timeout=HTTP_TIMEOUT)
        return self._interpret(resp, to_user)

    # ---------------- token 管理 ----------------

    def _get_token(self, corp_id: str, corp_secret: str) -> Tuple[Optional[str], str]:
        """
        获取 access_token（带进程级缓存 + 锁）。

        :return: (token, 错误原因)。token 为 None 时第二个值是原因。
        """
        global _cached_token, _token_expire_at

        now = time.time()
        with _token_lock:
            if _cached_token and now < _token_expire_at:
                return _cached_token, ""

        # 锁内重新读取后若仍无有效 token，才真正发起请求。
        # 双重检查的意义：20 个采集线程同时告警时，只会有 1 个真的去换 token。
        #
        # 注意：这里刻意**不写成 f-string 内嵌字典字面量**。
        # `f"...{urlencode({...})}"` 这种带引号与换行的嵌套写法只有在
        # Python 3.12+（PEP 701）才合法，本平台声明支持 Python 3.10+，
        # 在 3.10/3.11 上会直接 SyntaxError —— 整个渠道模块加载失败。
        params = urllib.parse.urlencode({"corpid": corp_id, "corpsecret": corp_secret})
        url = f"{TOKEN_URL}?{params}"
        resp = self._get_json(url, timeout=HTTP_TIMEOUT)

        if not resp.get("_http_ok"):
            return None, f"网络不可达：{resp.get('_error')}（token 获取为网络类错误，可重试）"

        data = resp.get("_data") or {}
        errcode = data.get("errcode", 0)
        if errcode != 0:
            hint = FATAL_ERRCODES.get(errcode, data.get("errmsg", "未知错误"))
            system_logger.warning("企业微信获取 token 失败：errcode=%s %s", errcode, hint)
            return None, f"errcode={errcode}：{hint}"

        token = data.get("access_token")
        if not token:
            return None, "企业微信未返回 access_token"

        expires_in = int(data.get("expires_in") or 7200)
        with _token_lock:
            _cached_token = token
            # 提前 TOKEN_REFRESH_AHEAD 秒过期；若 expires_in 本身很短（异常情况），
            # 保底至少缓存 60 秒，避免每次都重新获取把调用配额打光
            _token_expire_at = time.time() + max(expires_in - TOKEN_REFRESH_AHEAD, 60)
        system_logger.info("企业微信 access_token 已刷新，有效期 %s 秒", expires_in)
        return token, ""

    # ---------------- 响应解读 ----------------

    @staticmethod
    def _interpret(resp: Dict[str, Any], to_user: str) -> ChannelResult:
        if not resp.get("_http_ok"):
            return ChannelResult(
                success=False, target=to_user, retryable=True,
                message=f"请求失败：{resp.get('_error') or resp.get('_raw') or '无响应'}",
            )

        data = resp.get("_data")
        if data is None:
            return ChannelResult(
                success=False, target=to_user, retryable=True,
                message=f"响应不是 JSON（可能被代理拦截）：{resp.get('_raw', '')[:200]}",
            )

        errcode = data.get("errcode", 0)
        if errcode != 0:
            if errcode in FATAL_ERRCODES:
                return ChannelResult(
                    success=False, target=to_user, retryable=False,
                    message=f"errcode={errcode}：{FATAL_ERRCODES[errcode]}",
                )
            if errcode == 42001:      # access_token 过期
                reset_token_cache()   # 下次调用会重新获取
                return ChannelResult(
                    success=False, target=to_user, retryable=True,
                    message="errcode=42001：access_token 已过期，已清除缓存，重试即可",
                )
            if errcode == 45009:
                return ChannelResult(
                    success=False, target=to_user, retryable=True,
                    message="errcode=45009：接口调用超出频率限制，稍后重试",
                )
            return ChannelResult(
                success=False, target=to_user, retryable=True,
                message=f"errcode={errcode}：{data.get('errmsg', '未知错误')}",
            )

        # errcode=0 但仍可能有人没收到，invaliduser 必须解析出来
        invalid = (data.get("invaliduser") or "").strip()
        if invalid:
            return ChannelResult(
                success=True, target=to_user, retryable=False,
                message=f"已送达，但以下成员不存在或不在应用可见范围：{invalid}",
            )
        return ChannelResult(success=True, target=to_user, message="发送成功")
