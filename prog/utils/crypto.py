"""
字段级加密工具（Fernet）
========================
- 基于 `cryptography.fernet` 提供敏感字段的落盘加密/读取解密（D6 字段级加密）。
- 密钥来源（优先级）：
    1. 环境变量 ENCRYPTION_KEY：base64 urlsafe 编码的 32 字节 Fernet 密钥（推荐）
    2. 兜底：由 JWT_SECRET 派生（SHA-256 派生 32 字节，跨重启稳定），
       使存量部署无需新增环境变量即可获得真加密能力
- 密文格式：`fernet:<token>`；读取时透明兼容旧版 `b64:` 前缀与明文。
- 密钥指纹落地：register_encryption_key_id() 将当前密钥指纹写入
  system_configs.config_key='business_rules.encryption_key_id'，支撑密钥轮换/审计。

设计约束：
- 加密失败不阻断写入（降级为原值，读取侧兼容），避免加密成为业务故障点。
- 密钥派生为确定性函数（lru_cache），不引入额外状态。
"""

import base64
import hashlib
import os
from functools import lru_cache
from typing import Optional

_FERNET_PREFIX = "fernet:"
_LEGACY_B64_PREFIX = "b64:"


@lru_cache(maxsize=1)
def _get_key() -> bytes:
    """获取 Fernet 密钥字节（ENCRYPTION_KEY 优先，缺省由 JWT_SECRET 派生）。"""
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        key = _derive_key(os.environ.get("JWT_SECRET", "ai_factory_dev_secret"))
    return key.encode("ascii")


def _derive_key(secret: str) -> str:
    """由 secret 派生 Fernet 密钥（SHA-256 → base64url 32 字节，稳定跨重启）。"""
    return base64.urlsafe_b64encode(
        hashlib.sha256(str(secret).encode("utf-8")).digest()
    ).decode("ascii")


@lru_cache(maxsize=1)
def _fernet():
    """Fernet 实例（lazy 单例）。ENCRYPTION_KEY 非法时回退派生密钥。"""
    from cryptography.fernet import Fernet

    try:
        return Fernet(_get_key())
    except Exception:
        # 环境变量 ENCRYPTION_KEY 非合法 Fernet 密钥时回退派生密钥
        return Fernet(_derive_key(
            os.environ.get("JWT_SECRET", "ai_factory_dev_secret")
        ).encode("ascii"))


def encrypt_text(plain: Optional[str]) -> Optional[str]:
    """加密明文字符串，返回 `fernet:<token>`；空值/失败原样返回（不阻断写入）。"""
    if not plain:
        return plain
    try:
        token = _fernet().encrypt(str(plain).encode("utf-8"))
        return _FERNET_PREFIX + token.decode("ascii")
    except Exception:
        return plain


def decrypt_text(cipher: Optional[str]) -> Optional[str]:
    """解密 `fernet:` 密文；透明兼容旧版 `b64:` 混淆与明文。失败返回空串。"""
    if not isinstance(cipher, str) or not cipher:
        return cipher
    if cipher.startswith(_FERNET_PREFIX):
        try:
            return _fernet().decrypt(cipher[len(_FERNET_PREFIX):].encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    if cipher.startswith(_LEGACY_B64_PREFIX):
        try:
            return base64.b64decode(cipher[len(_LEGACY_B64_PREFIX):]).decode("utf-8")
        except Exception:
            return ""
    return cipher  # 明文兼容


def is_encrypted(value: Optional[str]) -> bool:
    """判断值是否为 Fernet 密文（用于幂等写入，避免层层加密）。"""
    return isinstance(value, str) and value.startswith(_FERNET_PREFIX)


def get_key_id() -> str:
    """当前加密密钥指纹（sha256 前 16 位），标识所用密钥供轮换/审计。"""
    return "k_" + hashlib.sha256(_get_key()).hexdigest()[:16]


def register_encryption_key_id() -> str:
    """将当前加密密钥指纹写入 system_configs（business_rules.encryption_key_id）。

    幂等 upsert；DB 不可用或写入失败静默，不阻断业务（加密不应成为故障点）。
    """
    key_id = get_key_id()
    try:
        from prog.runtime.database import get_database
        db = get_database()
        if db is not None:
            db.execute(
                """
                INSERT INTO system_configs
                    (config_key, config_value, config_type, description, updated_at)
                VALUES
                    (:key, :value, 'string', '当前字段级加密密钥指纹（Fernet）', NOW())
                ON CONFLICT (config_key)
                DO UPDATE SET config_value = EXCLUDED.config_value,
                              updated_at = NOW()
                """,
                {"key": "business_rules.encryption_key_id", "value": key_id},
            )
    except Exception:
        pass
    return key_id


__all__ = [
    "encrypt_text",
    "decrypt_text",
    "is_encrypted",
    "get_key_id",
    "register_encryption_key_id",
]
