"""
认证模块（登录 API 能力，框架层 · prog 内嵌副本）
================================================
认证能力为框架内嵌模块（原 agent-runtime-os 独立副本已取消，仅保留本仓库副本），
提供统一的认证能力：凭证校验 + Token 签发/校验。纯标准库实现。

组成：
    - TokenSigner      : HS256 JWT 签发/校验（标准库实现，兼容标准 JWT 格式）
    - Authenticator    : 认证器（查用户源 -> 校验密码 -> 签发 token）
    - MockUserSource   : 模拟用户源（仅 DEBUG 模式可用，正式模式禁用，防止绕过认证）

设计说明：
    - 认证（Authentication）与授权（Authorization）分离：本模块负责"你是谁"，
      权限校验（RBAC/ABAC）由 prog/runtime/permission.py 负责。
    - 用户数据源通过鸭子类型注入：提供 get_user(user_id) -> dict|None 的对象
      （dict 需含 password 字段）；未注入且非 DEBUG 时认证必然失败。
    - 业务侧只需薄适配：构造 Authenticator + 注入业务用户源 + 暴露 HTTP 端点
      （见 prog/api/auth.py）。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 认证模块（v1.3 提取）：统一认证能力 = 凭证校验 + Token 签发/校验，纯标准库实现（来源：SPEC v1.3 / 模块拆分方案 M0 清单）
        - 认证与授权分离：本模块负责「你是谁」，权限校验（RBAC/ABAC）由 permission.py 负责（来源：SPEC 设计说明）
        - HS256 JWT 签发/校验：标准库实现（不依赖 PyJWT），与标准 JWT 格式互验（来源：模块 docstring）
        - 用户数据源鸭子类型注入：get_user(user_id) -> dict|None；未注入且非 DEBUG 时认证必然失败（来源：模块 docstring）
    对外接口（方法/API）：
        - TokenSigner.issue_token(payload, expires_in=86400)：签发 HS256 JWT（自动附加 iat/exp），返回标准 JWT 字符串（来源：模块 docstring）
        - TokenSigner.verify_token(token)：校验格式/签名（hmac.compare_digest 防时序攻击）/过期时间，返回载荷或 None（来源：模块 docstring）
        - Authenticator.authenticate(user_id, password)：登录认证，成功返回 {"token", "user"}，失败返回 None（来源：模块 docstring）
        - Authenticator.verify(token)：校验已签发 Token，返回载荷或 None（来源：模块 docstring）
        - MockUserSource.get_user(user_id)：模拟用户源（仅 DEBUG 模式可用），正式模式恒返回 None（来源：模块 docstring）
        - verify_password(stored, plain)：bcrypt 哈希（$2 开头）优先，否则明文比对（来源：模块 docstring）
    错误处理要求：
        - 认证失败不区分「用户不存在/密码错误」（防枚举）（来源：模块 docstring）
        - MockUserSource 非 DEBUG（RUNTIME_DEBUG=1 未开启）恒返回 None：防止无数据库时绕过认证（来源：模块 docstring）
        - Token 无效/过期/签名不符：verify_token 返回 None（来源：模块 docstring）
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import prog.runtime.debug as _debug_mod

logger = logging.getLogger(__name__)


# ============================================================
# HS256 JWT 签名器（标准库实现，兼容标准 JWT 格式）
# ============================================================
class TokenSigner:
    """HS256 JWT Token 签发与校验器。

    标准 JWT 结构：header.payload.signature，signature = HMAC-SHA256(header.payload, secret)。
    不依赖 PyJWT，可用同一 secret 与任何标准 JWT 实现互验。

    属性:
        secret: 签名密钥（生产环境必须通过环境变量注入强随机值）
    """

    def __init__(self, secret: str) -> None:
        """初始化签名器。

        参数:
            secret: HMAC 签名密钥，建议 32+ 字节随机值
        """
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret

    # ------------------------------------------------------------
    # 编码辅助
    # ------------------------------------------------------------
    @staticmethod
    def _b64e(data: bytes) -> str:
        """base64url 编码（去除填充）。"""
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64d(part: str) -> bytes:
        """base64url 解码（自动补填充）。"""
        padding = "=" * (-len(part) % 4)
        return base64.urlsafe_b64decode(part + padding)

    # ------------------------------------------------------------
    # 签发 / 校验
    # ------------------------------------------------------------
    def issue_token(self, payload: Dict[str, Any],
                    expires_in: int = 86400) -> str:
        """签发 HS256 JWT Token。

        参数:
            payload: 载荷（须含 user_id 等身份字段；自动附加 iat/exp）
            expires_in: 有效期（秒，默认 86400 = 24h）

        返回:
            标准 JWT 字符串
        """
        now = int(time.time())
        header = {"alg": "HS256", "typ": "JWT"}
        claims = dict(payload)
        claims.setdefault("iat", now)
        claims["exp"] = now + int(expires_in)

        signing_input = (
            f"{self._b64e(json.dumps(header, separators=(',', ':')).encode('utf-8'))}"
            f".{self._b64e(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
        )
        signature = hmac.new(
            self.secret, signing_input.encode("utf-8"), hashlib.sha256
        ).digest()
        return f"{signing_input}.{self._b64e(signature)}"

    def verify_token(self, token: str) -> Optional[Dict[str, Any]]:
        """校验 Token，返回载荷；无效/过期返回 None。

        校验内容：格式、签名（hmac.compare_digest 防时序攻击）、过期时间。
        """
        if not token or not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_part, payload_part, sig_part = parts
        try:
            header = json.loads(self._b64d(header_part).decode("utf-8"))
            claims = json.loads(self._b64d(payload_part).decode("utf-8"))
            expected_sig = hmac.new(
                self.secret,
                f"{header_part}.{payload_part}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
            actual_sig = self._b64d(sig_part)
        except Exception:
            return None
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        if header.get("alg") != "HS256":
            return None
        exp = claims.get("exp")
        if exp and int(exp) < int(time.time()):
            return None
        return claims


# ============================================================
# 密码校验（bcrypt 可选 / 明文兜底，与业务侧既有行为一致）
# ============================================================
def verify_password(stored: str, plain: str) -> bool:
    """校验密码：bcrypt 哈希（$2 开头）优先，否则明文比对（兼容既有行为）。"""
    if not stored:
        return False
    if stored.startswith("$2"):
        try:
            import bcrypt
            return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
        except Exception:
            return False
    # W10：非 bcrypt 存储退化为明文比对——与业务侧既有行为一致（保持功能/测试
    # 契约不变），但记录告警日志，提示生产环境应升级为 bcrypt 哈希存储。
    logger.warning("密码存储非 bcrypt 格式，正在使用明文比对（建议升级 bcrypt 哈希）")
    return stored == plain


# ============================================================
# 模拟用户源（仅 DEBUG 可用）
# ============================================================
class MockUserSource:
    """模拟用户源：无数据库开发时使用，仅 DEBUG（RUNTIME_DEBUG=1）模式可用。

    正式模式（DEBUG=False）下 get_user 恒返回 None，认证必然失败，
    防止无数据库时绕过认证。账号字典可注入（业务侧自定义账号）或使用内置通用账号。

    内置账号：
        admin / admin123   （系统管理员）
        dev / dev123456    （开发测试账号）
    """

    DEFAULT_USERS: Dict[str, Dict[str, Any]] = {
        "admin": {"password": "admin123", "name": "系统管理员",
                  "title": "系统管理员", "department": "信息技术部", "role": "admin"},
        "dev": {"password": "dev123456", "name": "开发测试",
                "title": "开发工程师", "department": "信息技术部", "role": "sales"},
    }

    def __init__(self, users: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        """初始化模拟用户源。

        参数:
            users: 自定义账号字典（覆盖内置账号），形如 {"user_id": {"password": ...}}
        """
        self._users = dict(self.DEFAULT_USERS)
        if users:
            self._users.update(users)

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """按 user_id 返回模拟用户；非 DEBUG 或不存在时返回 None。

        动态读取 debug 模块开关（支持运行时 set_debug 切换）。
        """
        if not _debug_mod.DEBUG:
            return None
        user = self._users.get(user_id)
        if not user:
            return None
        return {"user_id": user_id, **user}


# ============================================================
# 认证器（登录 API 核心逻辑）
# ============================================================
class Authenticator:
    """统一认证器：凭证校验 + Token 签发。

    职责（"你是谁"）：
        1. 从用户源获取用户（注入 get_user(user_id) 的对象）
        2. 校验密码
        3. 签发 HS256 JWT token

    用法：
        auth = Authenticator(secret=SECRET, user_source=user_source)
        result = auth.authenticate("admin", "admin123")
        # -> {"token": "...", "user": {...}} 或 None

    属性:
        signer: TokenSigner 实例
        user_source: 用户数据源（get_user(user_id) -> dict|None），
                     未注入时使用 MockUserSource（DEBUG 门控）
        token_ttl: Token 有效期（秒）
    """

    def __init__(self, secret: str,
                 user_source: Any = None,
                 token_ttl: int = 86400) -> None:
        """初始化认证器。

        参数:
            secret: Token 签名密钥
            user_source: 用户数据源（鸭子类型：get_user(user_id) -> dict|None），
                         默认 MockUserSource（仅 DEBUG 可用）
            token_ttl: Token 有效期（秒）
        """
        self.signer = TokenSigner(secret)
        self.user_source = user_source if user_source is not None else MockUserSource()
        self.token_ttl = int(token_ttl)

    def authenticate(self, user_id: str, password: str
                     ) -> Optional[Dict[str, Any]]:
        """登录认证：校验凭证，成功返回 {"token", "user"}，失败返回 None。

        失败不区分"用户不存在/密码错误"（防枚举）。
        """
        if not user_id or not password:
            return None
        user = self._fetch_user(user_id)
        if not user:
            return None
        # v6.97 A.2：禁用账户拒绝登录——status 非 active 即拒绝。
        # 无 status 字段的模拟用户（仅 DEBUG 降级）不拦截，保持降级路径可用。
        if user.get("status") not in (None, "active"):
            return None
        if not verify_password(user.get("password", ""), password):
            return None

        user_info = self._build_user_info(user_id, user)
        token = self.signer.issue_token(
            {"user_id": user_id, "role": user_info.get("role", "sales")},
            expires_in=self.token_ttl,
        )
        return {"token": token, "user": user_info}

    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        """校验已签发 Token，返回载荷；无效返回 None。"""
        return self.signer.verify_token(token)

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------
    def _fetch_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """从用户源获取用户，兼容字典或 get_user 可调用对象。"""
        getter = getattr(self.user_source, "get_user", None)
        if callable(getter):
            try:
                return getter(user_id)
            except Exception:
                return None
        return None

    @staticmethod
    def _build_user_info(user_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
        """构造标准用户信息（不含密码）。"""
        role = user.get("role", "sales")
        name = user.get("name", "")
        return {
            "id": user_id,
            "name": name,
            "title": user.get("title", ""),
            "department": user.get("department", ""),
            "role": role,
        }


__all__ = [
    "TokenSigner",
    "Authenticator",
    "MockUserSource",
    "verify_password",
]
