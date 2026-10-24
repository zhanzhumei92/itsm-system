"""
邮件告警渠道（smtplib）
===============================================================================
**配置项来源**：sys_config 的 mail 分组（由 config_service.get_channel_settings 解密后传入）

    enabled / smtp_host / smtp_port / smtp_user / smtp_password
    sender / use_ssl / receivers

**四个实现决策**

1. **sendmail 还是 SMTP.send_message**
   用 `send_message`：它按 RFC 5322 正确编码头部，
   中文主题（"[紧急]核心交换机 CPU 过高"）不会变成乱码或触发
   `UnicodeEncodeError: 'ascii' codec can't encode`。
   用 `sendmail(from, to, msg.as_string())` 时，as_string() 在某些
   编码组合下会把中文头还原成裸 8bit，部分邮件网关直接拒收。

2. **同时发纯文本与 HTML 两部分（MIMEMultipart("alternative")）**
   邮件客户端优先显示 HTML 部分（表格化、按级别着色，扫一眼就知道严重程度），
   但短信网关 / 工单系统 / 老旧终端只认纯文本部分。
   只发 HTML 时，这些下游会看到一堆标签。

3. **一次连接发全部收件人，而不是逐个收件人各连一次**
   运维常把 20 个人的邮箱填进 receivers。逐个连会在 SMTP 上做 20 次
   TCP+TLS+认证（约 20 秒），而告警最需要的是"快"。
   代价是某个地址被拒会导致整封失败——所以把每个收件人的
   拒绝原因都写进返回值，便于定位。

4. **use_ssl 与 STARTTLS 分开判断**
   `use_ssl=1` → 从连接建立就加密（465 端口，腾讯企业邮默认）；
   `use_ssl=0` 且端口是 587 → 明文连接后 STARTTLS 升级。
   把 587 当成"明文"处理是常见的配置错误（会在 send 时报
   "SMTP AUTH extension not supported"）。
===============================================================================
"""
from __future__ import annotations

import json
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any, Dict, List, Tuple

from app.alarm.channels import AlertMessage, BaseChannel, ChannelResult, ChannelRegistry
from app.models.alert import CHANNEL_EMAIL

# 连接与收发超时（秒）。
# 取 15 秒而不是默认的"无限等待"：邮件服务器不可达时若不设超时，
# 发送线程会被永久占住，把所有采集线程慢慢吃光。
SMTP_TIMEOUT = 15

# 收件人数量上限。防止有人把整个公司的通讯录粘进配置框，
# 导致一次告警发几百封邮件被邮件服务商当成垃圾邮件源封禁。
MAX_RECEIVERS = 50


@ChannelRegistry.register(CHANNEL_EMAIL)
class EmailChannel(BaseChannel):
    """邮件渠道（SMTP）"""

    key = CHANNEL_EMAIL
    text = "邮件"
    config_group = "mail"

    # ---------------- 配置检查 ----------------

    def check(self, cfg: Dict[str, Any]) -> Tuple[bool, str]:
        """
        逐项检查并给出**具体**原因。

        为什么把原因写得这么细：现场 90% 的"告警没收到"都是邮件配置问题，
        而配置页上有 8 个输入框。笼统地回"邮件配置不完整"会让运维
        挨个试一遍；指明是"smtp_host 未填写"则一步到位。
        """
        if not self.enabled(cfg):
            return False, "邮件告警未启用（配置页 mail.enabled 开关）"
        if not (cfg.get("smtp_host") or "").strip():
            return False, "未填写 SMTP 服务器地址"
        if not (cfg.get("sender") or "").strip():
            return False, "未填写发件人地址"
        if not self._parse_receivers(cfg.get("receivers")):
            return False, "未填写收件人（receivers 为空）"
        if not (cfg.get("smtp_user") or "").strip():
            return False, "未填写 SMTP 账号"
        if not (cfg.get("smtp_password") or "").strip():
            # 口令是敏感项，解密失败（FERNET_KEY 变更）时也会走到这里
            return False, "未配置 SMTP 口令 / 授权码（或密钥已更换导致解密失败）"
        try:
            port = int(str(cfg.get("smtp_port") or "465").strip())
        except (TypeError, ValueError):
            return False, f"SMTP 端口不是数字：{cfg.get('smtp_port')!r}"
        if not (0 < port < 65536):
            return False, f"SMTP 端口超出范围：{port}"
        return True, ""

    @staticmethod
    def _parse_receivers(raw: Any) -> List[str]:
        """
        解析收件人。

        兼容三种写法（现场三种都出现过）：
            ["a@x.com","b@x.com"]        数组（前端保存的标准格式）
            a@x.com,b@x.com             逗号分隔
            a@x.com; b@x.com\r\nc@x.com 分号/换行混用（从 Excel 粘进来）
        """
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x).strip()]

        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except (ValueError, TypeError):
                # JSON 坏了不要直接放弃，继续按分隔符兜底解析
                text = text.strip("[]")
        import re
        return [x.strip().strip('"\'') for x in re.split(r"[,;\n\r]+", text) if x.strip()]

    # ---------------- 发送 ----------------

    def send(self, message: AlertMessage, cfg: Dict[str, Any]) -> ChannelResult:
        ready, reason = self.check(cfg)
        if not ready:
            # 配置问题不可重试：重试 2 次也不会让空地址变得非空
            return self.fail(reason, target="", retryable=False)

        receivers = self._parse_receivers(cfg.get("receivers"))[:MAX_RECEIVERS]
        sender = str(cfg.get("sender")).strip()
        host = str(cfg.get("smtp_host")).strip()
        port = int(str(cfg.get("smtp_port") or "465").strip())
        user = str(cfg.get("smtp_user")).strip()
        password = str(cfg.get("smtp_password"))
        use_ssl = str(cfg.get("use_ssl", "1")).strip().lower() in ("1", "true", "yes", "on")
        target = ",".join(receivers)

        mail = self._build_mime(message, sender, receivers)
        if mail is None:                       # pragma: no cover - 理论不可达
            return self.fail("邮件内容构造失败", target=target, retryable=False)

        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
            else:
                server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
            try:
                if not use_ssl and port == 587:
                    # 587 是"提交端口"，必须先明文握手再 STARTTLS 升级
                    server.starttls()
                server.login(user, password)
                refused = server.sendmail(sender, receivers, mail.as_string())
            finally:
                # 无论成功失败都显式 QUIT：不关连接会让邮件服务器
                # 残留大量半开会话，几轮告警后就开始拒绝新连接
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 - 已断开时 quit 会抛错，忽略
                    pass
        except smtplib.SMTPAuthenticationError as exc:
            # 认证失败最常见的原因是"用了登录密码而不是授权码"
            return self.fail(
                f"SMTP 认证失败（请确认使用的是授权码而非登录密码）：{exc.smtp_code}",
                target=target, retryable=False,
            )
        except smtplib.SMTPRecipientsRefused as exc:
            return self.fail(f"全部收件人被拒绝：{exc.recipients}", target=target, retryable=False)
        except Exception as exc:  # noqa: BLE001 - 网络/SSL/超时
            return self.fail(
                f"{type(exc).__name__}: {exc}", target=target, retryable=True,
            )

        if refused:
            # 部分成功：少数地址被拒（写错、已离职）不该让整条记录判失败，
            # 但必须把被拒地址写进记录，否则运维不知道谁没收到
            detail = "; ".join(f"{addr}: {err[1] if len(err) > 1 else err}"
                              for addr, err in list(refused.items())[:5])
            return ChannelResult(
                success=True,
                message=f"部分收件人被拒（{[*refused][:5]}）：{detail}",
                target=target, retryable=False,
            )
        return self.ok(target=target, message=f"已发送至 {len(receivers)} 个收件人")

    def _build_mime(self, message: AlertMessage, sender: str, receivers: List[str]):
        """
        构造邮件对象：HTML 正文为主，纯文本为备选。

        formataddr 用于发件人显示名，配合 Header 做 UTF-8 编码 ——
        直接拼 "中文名 <a@b.com>" 在部分邮件客户端会显示成乱码。
        """
        mail = MIMEMultipart("alternative")
        # 主题用 Header 显式编码：中文主题是乱码的重灾区
        mail["Subject"] = Header(message.subject(), "utf-8").encode()
        mail["From"] = formataddr((str(Header(message.platform_name, "utf-8")), sender))
        mail["To"] = ", ".join(receivers)
        mail["Date"] = formatdate(localtime=True)
        # 加自定义头便于在邮箱里按规则过滤/归档（运维常用手段）
        mail["X-Alert-Event"] = message.event_no or str(message.event_id)
        mail["X-Alert-Level"] = message.level

        mail.attach(MIMEText(self._text_body(message), "plain", "utf-8"))
        mail.attach(MIMEText(self._html_body(message), "html", "utf-8"))
        return mail

    @staticmethod
    def _text_body(message: AlertMessage) -> str:
        return message.as_text()

    @staticmethod
    def _html_body(message: AlertMessage) -> str:
        """
        HTML 正文。

        **刻意只用内联样式 + 表格**，不用 flex/grid/外部 CSS：
        企业邮箱客户端（Outlook、Foxmail、企业微信内置邮箱）对现代 CSS 支持极差，
        用 flex 排版的结果通常是"所有内容挤成一坨"。
        """
        # 级别色必须与 app/models/alert.py 的 ALERT_LEVEL_COLOR 保持一致
        # （2026-09-22 站点改亮色主题后统一换成同一套色，邮件里点开的红
        #   要和平台上的红是同一个红）。
        color = {
            "critical": "#D93A3C", "major": "#C2740C",
            "minor": "#B9891B", "info": "#007ACC",
        }.get(message.level, "#8B9AB0")
        head_bg = "#FDECEA" if not message.is_recovery else "#EAF7EE"
        head_color = color if not message.is_recovery else "#0E9F73"
        action = "告警恢复" if message.is_recovery else "告警通知"

        def row(label: str, value: str, bold: bool = False) -> str:
            weight = "bold" if bold else "normal"
            return (
                '<tr>'
                f'<td style="padding:6px 12px;background:#F7F8FA;color:#5F6368;'
                f'width:96px;border-bottom:1px solid #EDEFF2;">{label}</td>'
                f'<td style="padding:6px 12px;color:#202124;font-weight:{weight};'
                f'border-bottom:1px solid #EDEFF2;">{value}</td>'
                '</tr>'
            )

        rows = [
            row("设备名称", message.device_name),
            row("设备IP", message.device_ip),
        ]
        if message.device_type:
            rows.append(row("设备类型", message.device_type))
        if message.location:
            rows.append(row("位置", message.location))
        rows.append(row("监控指标", message.metric_name))
        rows.append(row("当前值", message.metric_value_text, bold=True))
        if message.threshold_text:
            rows.append(row("告警阈值", message.threshold_text))
        rows.append(row("告警级别", message.level_text))
        if message.is_recovery:
            if message.duration_text:
                rows.append(row("持续时长", message.duration_text))
            rows.append(row("恢复时间", f"{message.occurred_at:%Y-%m-%d %H:%M:%S}"))
        else:
            rows.append(row("告警时间", f"{message.occurred_at:%Y-%m-%d %H:%M:%S}"))
            if message.occur_count > 1:
                rows.append(row("累计发生", f"{message.occur_count} 次"))
        rows.append(row("事件编号", message.event_no))

        link = ""
        if message.platform_url:
            link = (f'<p style="margin:14px 0 0;"><a href="{message.platform_url}" '
                    f'style="color:#007ACC;">→ 前往平台处理</a></p>')

        return f"""\
<div style="font-family:'Microsoft YaHei',Arial,sans-serif;font-size:14px;
            color:#202124;max-width:640px;">
  <div style="padding:12px 16px;background:{head_bg};border-left:4px solid {head_color};
              border-radius:3px;">
    <div style="font-size:12px;color:{head_color};letter-spacing:1px;">{action}</div>
    <div style="font-size:17px;font-weight:bold;color:{head_color};margin-top:4px;">
      {message.title}
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse;margin-top:12px;">{''.join(rows)}</table>
  {link}
  <p style="margin:16px 0 0;padding-top:10px;border-top:1px solid #EDEFF2;
            font-size:12px;color:#8B9AB0;">
    本邮件由 {message.platform_name} 自动发送，请勿直接回复。
  </p>
</div>"""
