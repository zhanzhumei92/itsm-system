"""
系统配置服务（模块5：统一配置邮件/企微/钉钉/微信密钥、全局采集参数）
===============================================================================
两个关键设计：

一、**敏感项加密存储 + 脱敏返回**
   - 写入：is_secret=1 的项（SMTP 口令、机器人 Webhook、CorpSecret）用 Fernet 加密
   - 读取给前端：一律返回 "***"（已配置）或 ""（未配置），永不回传明文
   - 读取给业务（告警发送）：调用 get_secret() 拿解密后的明文
   这样即便运维的浏览器被同事瞄一眼，也看不到机器人密钥。

二、**进程内 TTL 缓存**
   采集线程每轮都要读 collect.* 配置（60 并发 × 每分钟），
   每次都查库会给 SQLite 带来无谓压力。故做 30 秒 TTL 缓存，
   任何人保存配置后立即失效（不用等 TTL 到期）。
   为什么不用 lru_cache：需要显式失效能力，lru_cache 做不到精确清空单键。
===============================================================================
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logger import system_logger
from app.core.security import decrypt_secret, encrypt_secret
from app.models.config import GROUP_TEXT, SysConfig
from app.schemas.system import (
    ConfigGroupOut,
    ConfigItemOut,
    ConfigUpdateItem,
)

# 缓存有效期（秒）
CACHE_TTL_SECONDS = 30
_cache: Dict[str, Optional[str]] = {}
_cache_loaded_at: float = 0.0
_cache_lock = threading.RLock()

# 默认值兜底：数据库缺项时保证业务仍可运行（避免"配置表被误删导致采集全停"）
DEFAULT_VALUES: Dict[str, str] = {
    "collect.thread_pool_size": "60",
    "collect.default_interval": "60",
    "collect.default_timeout": "10",
    "collect.ssh_slow_interval": "600",
    "collect.offline_threshold": "3",
    "metric.keep_raw_days": "7",
    "metric.keep_5min_days": "30",
    "metric.keep_hour_days": "90",
    "mail.enabled": "0",
    "mail.smtp_port": "465",
    "mail.use_ssl": "1",
    "wecom.enabled": "0",
    "wecom.msg_type": "markdown",
    "dingtalk.enabled": "0",
    "wechat.enabled": "0",
    "wechat.to_user": "@all",
    "system.platform_name": "亿网通IT运维监控管理平台",
    "system.company_name": "广东亿网通科技有限公司",
    "system.dashboard_refresh": "15",
    # 外部通知（邮件/企微/钉钉）里"前往平台处理"的链接地址。
    # 留空则通知里不出现链接——比给一个错误的地址更安全。
    "system.platform_url": "",
}


def _load_cache(db: Session, force: bool = False) -> Dict[str, Optional[str]]:
    """加载全部配置到内存缓存（带 TTL）"""
    global _cache, _cache_loaded_at
    with _cache_lock:
        now = time.time()
        if not force and _cache and (now - _cache_loaded_at) < CACHE_TTL_SECONDS:
            return _cache

        rows = db.execute(select(SysConfig)).scalars().all()
        _cache = {row.config_key: row.config_value for row in rows}
        _cache_loaded_at = now
        return _cache


def invalidate_cache() -> None:
    """清空缓存（配置保存后调用，使新值立即生效）"""
    global _cache, _cache_loaded_at
    with _cache_lock:
        _cache = {}
        _cache_loaded_at = 0.0


# ============================================================================
# 一、业务读取接口（带类型转换与默认值兜底）
# ============================================================================

def get_value(db: Session, key: str, default: Optional[str] = None) -> Optional[str]:
    """
    读取原始配置值（**敏感项返回解密后的明文**，仅供后端业务使用）。

    注意：本函数返回明文，切勿把它直接放进接口响应。
    给前端展示请使用 get_groups() / get_group()。
    """
    cache = _load_cache(db)
    raw = cache.get(key)
    if raw is None:
        raw = DEFAULT_VALUES.get(key, default)

    # 判断是否敏感项：查数据库中的 is_secret 标记
    row = _get_config_row(db, key)
    if row is not None and row.is_secret and raw:
        return decrypt_secret(raw)
    return raw


def get_secret(db: Session, key: str) -> Optional[str]:
    """显式获取解密后的敏感配置（语义更清晰，告警渠道使用）"""
    row = _get_config_row(db, key)
    if row is None or not row.config_value:
        return None
    if row.is_secret:
        return decrypt_secret(row.config_value)
    return row.config_value


def get_int(db: Session, key: str, default: int = 0) -> int:
    """读整数配置（转换失败返回默认值，不让脏配置阻断采集）"""
    raw = get_value(db, key)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    """读布尔配置。兼容 1/0、true/false、yes/no、on/off 多种写法。"""
    raw = get_value(db, key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "启用")


def get_json(db: Session, key: str, default: Optional[Any] = None) -> Any:
    """读 JSON 配置（如收件人列表）"""
    raw = get_value(db, key)
    if not raw:
        return default if default is not None else []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        system_logger.warning("配置 %s 不是合法 JSON，已返回默认值", key)
        return default if default is not None else []


def _get_config_row(db: Session, key: str) -> Optional[SysConfig]:
    """按 key 取配置行（单键查询，供判断 is_secret）"""
    return db.execute(select(SysConfig).where(SysConfig.config_key == key)).scalar_one_or_none()


# ============================================================================
# 二、前端展示接口（脱敏）
# ============================================================================

def _mask(item: SysConfig) -> ConfigItemOut:
    """
    配置项脱敏。

    敏感项：有值 → "***"；无值 → ""（前端据 is_configured 显示状态点）
    非敏感项：原样返回
    """
    value = item.config_value
    if item.is_secret:
        value = "***" if value else ""
    out = ConfigItemOut(
        id=item.id,
        config_key=item.config_key,
        config_value=value,
        config_group=item.config_group,
        value_type=item.value_type,
        is_secret=item.is_secret,
        description=item.description,
        updated_at=item.updated_at,
    )
    return out


def list_groups(db: Session, groups: Optional[Sequence[str]] = None) -> List[ConfigGroupOut]:
    """
    按分组返回配置（系统配置页一个 Tab 一个分组）。

    组内排序按 id，保证与数据库中的登记顺序一致（前端展示稳定）。
    """
    stmt = select(SysConfig).order_by(SysConfig.id.asc())
    rows = db.execute(stmt).scalars().all()

    grouped: Dict[str, List[ConfigItemOut]] = {}
    for row in rows:
        if groups and row.config_group not in groups:
            continue
        grouped.setdefault(row.config_group, []).append(_mask(row))

    # 按预定义的分组顺序输出，避免字典顺序不稳定导致 Tab 乱跳
    order = ["collect", "mail", "wecom", "dingtalk", "wechat", "system"]
    result: List[ConfigGroupOut] = []
    for group in order:
        if group in grouped:
            result.append(ConfigGroupOut(
                group=group,
                group_text=GROUP_TEXT.get(group, group),
                items=grouped[group],
            ))
    # 兜底：数据库里有未在 order 中声明的分组也一并返回
    for group, items in grouped.items():
        if group not in order:
            result.append(ConfigGroupOut(group=group, group_text=GROUP_TEXT.get(group, group), items=items))
    return result


def get_group(db: Session, group: str) -> ConfigGroupOut:
    """取单个分组配置"""
    groups = list_groups(db, groups=[group])
    if groups:
        return groups[0]
    return ConfigGroupOut(group=group, group_text=GROUP_TEXT.get(group, group), items=[])


# ============================================================================
# 三、写入
# ============================================================================

def update_configs(db: Session, items: Sequence[ConfigUpdateItem], operator: str = "") -> int:
    """
    批量更新配置（一个分组一次保存）。

    敏感项处理规则：
      - 前端传 "***"  → 表示用户没改，**保留原密文**
      - 前端传 ""      → 表示要清空该配置（如停用渠道后清除密钥）
      - 前端传其它值   → 加密后覆盖

    这条规则是"前端不回显密钥"与"能正常保存其它字段"之间的必要妥协，
    需与前端约定一致（前端保存时把未修改的敏感项原样提交为 ***）。
    """
    updated = 0
    now_ts = datetime.now()

    for item in items:
        row = _get_config_row(db, item.config_key)
        if row is None:
            system_logger.warning("忽略未知配置项：%s", item.config_key)
            continue

        new_value = item.config_value

        if row.is_secret:
            if new_value == "***":
                continue                      # 未修改，跳过
            if new_value in (None, ""):
                row.config_value = None       # 显式清空
            else:
                row.config_value = encrypt_secret(new_value)   # 加密覆盖
        else:
            row.config_value = new_value

        row.updated_at = now_ts
        updated += 1

    db.commit()
    invalidate_cache()    # 立即失效，保证新配置马上生效
    system_logger.info("系统配置更新 %s 项 | 操作人=%s", updated, operator)
    return updated


def set_value(db: Session, key: str, value: Optional[str], group: str = "system",
              is_secret: bool = False, value_type: str = "string",
              description: str = "") -> SysConfig:
    """
    设置单个配置项（不存在则创建）。

    供初始化脚本与演示数据使用；生产环境参数调整走 update_configs()。
    """
    row = _get_config_row(db, key)
    stored = encrypt_secret(value) if is_secret and value else value

    if row is None:
        row = SysConfig(
            config_key=key,
            config_value=stored,
            config_group=group,
            value_type=value_type,
            is_secret=is_secret,
            description=description,
        )
        db.add(row)
    else:
        row.config_value = stored
        row.updated_at = datetime.now()

    db.commit()
    invalidate_cache()
    return row


# ============================================================================
# 四、常用配置的便捷读取（供采集层/告警层直接调用）
# ============================================================================

def get_collect_settings(db: Session) -> Dict[str, int]:
    """
    采集相关配置一次性取出。

    采集调度器每轮都会调用，合并成一次字典返回可减少多次缓存查询的开销。
    """
    return {
        "thread_pool_size": get_int(db, "collect.thread_pool_size", 60),
        "default_interval": get_int(db, "collect.default_interval", 60),
        "default_timeout": get_int(db, "collect.default_timeout", 10),
        "ssh_slow_interval": get_int(db, "collect.ssh_slow_interval", 600),
        "offline_threshold": get_int(db, "collect.offline_threshold", 3),
    }


def get_retention_settings(db: Session) -> Dict[str, int]:
    """指标保留策略（清理任务使用）"""
    return {
        "keep_raw_days": get_int(db, "metric.keep_raw_days", 7),
        "keep_5min_days": get_int(db, "metric.keep_5min_days", 30),
        "keep_hour_days": get_int(db, "metric.keep_hour_days", 90),
    }


def get_channel_settings(db: Session, channel: str) -> Dict[str, Any]:
    """
    取某告警渠道的全部配置。

    :param channel: mail / wecom / dingtalk / wechat
    敏感项已解密（告警发送必须用明文），调用方不得记录到日志。
    """
    rows = db.execute(
        select(SysConfig).where(SysConfig.config_group == channel).order_by(SysConfig.id.asc())
    ).scalars().all()

    result: Dict[str, Any] = {}
    for row in rows:
        # 配置键形如 mail.smtp_host → 取后半段作为字典键
        short_key = row.config_key.split(".", 1)[-1]
        value = row.config_value
        if row.is_secret and value:
            value = decrypt_secret(value)
        result[short_key] = value
    return result


def get_platform_info(db: Session) -> Dict[str, Any]:
    """
    平台信息（大屏页脚、登录页标题、外部告警消息使用）。

    platform_url 刻意允许为空：留空时通知里不出现"前往平台"链接，
    比给一个连不上的地址更安全（运维会真的去点那个链接）。
    """
    return {
        "platform_name": get_value(db, "system.platform_name", "亿网通IT运维监控管理平台"),
        "company_name": get_value(db, "system.company_name", "广东亿网通科技有限公司"),
        "dashboard_refresh": get_int(db, "system.dashboard_refresh", 15),
        "platform_url": (get_value(db, "system.platform_url", "") or "").strip(),
    }
