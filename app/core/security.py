"""
安全模块：密码哈希、JWT 令牌、设备口令对称加密
===============================================================================
设计说明：

1. **密码哈希用 bcrypt（直接调用，不经 passlib）**
   passlib 1.7.x 与 bcrypt 4.x 存在兼容告警，且 passlib 已多年不更新；
   直接使用 bcrypt 库更可控。cost=12 兼顾安全与登录速度（约 250ms/次）。

2. **JWT 用 PyJWT**
   载荷只放 sub（用户ID）、username、role 三个字段——
   不放权限明细，避免令牌过大；权限以数据库当前角色为准，改权限立即生效。

3. **设备口令用 Fernet 对称加密（可逆）**
   与登录密码的处理必须区分：
   - 用户登录密码：只需"验证"，用 bcrypt 单向哈希，**永不还原**
   - 设备 SNMP 团体字 / SSH 口令：采集时必须还原出明文去连接设备，必须可逆
   这是"密码不能明文入库"与"业务必须用明文连接设备"之间的标准解法。
   密钥来自 .env 的 FERNET_KEY，**密钥轮换会导致旧口令不可解，需重新录入**。

4. 未配置 FERNET_KEY 时**明确报错**，不做"降级为明文存储"这种危险妥协。
===============================================================================
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import settings
from app.core.logger import system_logger

# ============================================================================
# 一、登录密码：bcrypt 单向哈希
# ============================================================================

_BCRYPT_ROUNDS = 12          # 迭代轮数，2^12 次，约 250ms，足以抵御离线爆破
_BCRYPT_MAX_BYTES = 72       # bcrypt 只取前 72 字节，超长需先截断否则新版库直接报错


def hash_password(plain_password: str) -> str:
    """
    生成密码哈希（用于 sys_user.password_hash）。

    :param plain_password: 明文密码
    :return: 形如 $2b$12$... 的哈希字符串
    """
    raw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """
    校验密码。任何异常（哈希格式非法、空值）统一返回 False，
    不向调用方抛异常，避免登录接口因脏数据 500。
    """
    if not plain_password or not password_hash:
        return False
    try:
        raw = plain_password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except Exception as exc:  # pragma: no cover
        system_logger.warning("密码校验异常：%s", exc)
        return False


# ============================================================================
# 二、JWT 令牌
# ============================================================================

def create_access_token(
    user_id: int,
    username: str,
    role: str,
    expires_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    签发访问令牌。

    :return: {"access_token": "...", "token_type": "bearer", "expires_in": 秒, "role": "..."}
    """
    minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=minutes)

    payload = {
        "sub": str(user_id),      # JWT 规范要求 sub 为字符串
        "username": username,
        "role": role,             # admin / viewer，供接口层快速判权（真实权限仍以库为准）
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": minutes * 60,
        "role": role,
        "username": username,
    }


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """
    解析并校验令牌。过期/签名错误/格式错误统一返回 None，
    由调用方（deps.get_current_user）转成 401，保持错误处理单一出口。
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except ExpiredSignatureError:
        # 令牌过期是正常现象（用户挂机超时），记 INFO 即可，不算异常
        system_logger.info("令牌已过期")
        return None
    except JWTError as exc:
        # 签名错误、格式错误、算法不匹配统一归为"令牌无效"
        system_logger.warning("令牌解析失败：%s", exc)
        return None


# ============================================================================
# 三、设备口令 / 告警密钥：Fernet 对称加密
# ============================================================================

_fernet_cache: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """
    获取 Fernet 实例（惰性初始化 + 缓存）。

    未配置 FERNET_KEY 时抛 ValueError 而非静默返回明文——
    宁可启动报错要求配置，也不能让设备口令以明文落库。
    """
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache

    if not settings.FERNET_KEY:
        raise ValueError(
            "未配置 FERNET_KEY，无法加密设备口令。"
            '请生成密钥并写入 .env：python -c "from cryptography.fernet import Fernet;'
            'print(Fernet.generate_key().decode())"'
        )
    try:
        _fernet_cache = Fernet(settings.FERNET_KEY.encode("utf-8"))
    except Exception as exc:
        raise ValueError(f"FERNET_KEY 格式非法（应为 44 位 base64 字符串）：{exc}") from exc
    return _fernet_cache


def encrypt_secret(plain_text: Optional[str]) -> Optional[str]:
    """
    加密敏感字段（SNMP 团体字、SSH/WinRM 口令、告警机器人密钥）。

    空值原样返回 None，避免把 None 加密成有效密文导致"看起来配置了其实没有"。
    """
    if plain_text is None or plain_text == "":
        return None
    return _get_fernet().encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_secret(cipher_text: Optional[str]) -> Optional[str]:
    """
    解密敏感字段。

    解密失败（密钥更换、密文被手工改坏）时返回 None 并记日志，
    由采集器判定为"凭据不可用"并写入 collect_log，而不是让整个采集任务崩掉。
    """
    if not cipher_text:
        return None
    try:
        return _get_fernet().decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        system_logger.error(
            "密文解密失败：可能是 FERNET_KEY 被更换。请重新录入相关设备凭据后再试。"
        )
        return None
    except Exception as exc:  # pragma: no cover
        system_logger.error("密文解密异常：%s", exc)
        return None


def mask_secret(value: Optional[str]) -> str:
    """
    前端展示用的脱敏字符串。
    已配置返回 ***（前端只显示"已配置"），未配置返回空串。
    """
    return "***" if value else ""


def generate_fernet_key() -> str:
    """生成一个新的 Fernet 密钥（供 scripts/init_db.py 首次部署时调用）"""
    return Fernet.generate_key().decode("utf-8")
