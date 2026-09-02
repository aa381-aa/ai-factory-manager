"""
Auth 认证API模块
================

文件用途：
    实现用户认证API，包括登录、登出、获取用户信息，
    基于JWT签发token，用户来源为数据库 users 表。

技术规格章节：
    - §1.1.3 Coordinator Agent（用户身份与权限通过本模块建立会话）
    - 各领域Agent（依赖本模块建立的权限上下文）

接口列表：
    - POST /api/login: 登录，返回JWT token
    - POST /api/logout: 登出，使token失效
    - GET /api/user/info: 获取当前登录用户信息

设计说明：
    - 登录查询数据库用户表（不再使用硬编码用户）
    - 密码采用哈希存储（如bcrypt），不存明文
    - JWT token包含user_id、role、permissions，Agent据此做权限判断
    - 登出时将token加入黑名单（Redis）直至过期
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, request, g
from prog.utils.api_response import api_response, error_response

from prog.core.debug import DEBUG
from prog.runtime.auth import Authenticator, MockUserSource

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

# JWT配置（从环境变量读取，缺省为开发默认值）
JWT_SECRET = os.environ.get('JWT_SECRET', 'ai_factory_dev_secret')
# A6：access token 短期（默认 2h，用短窗口降低泄露风险）+ refresh token
# 长期（默认 7d，仅用于 /api/auth/refresh 换取新 access）
JWT_EXPIRE_SECONDS = int(os.environ.get('JWT_EXPIRE_SECONDS', '7200'))
JWT_REFRESH_EXPIRE_SECONDS = int(os.environ.get('JWT_REFRESH_EXPIRE_SECONDS', '604800'))

# Token 临近过期的自动续期阈值（秒）：剩余有效期小于该值即重签新 token
RENEW_THRESHOLD = int(os.environ.get('JWT_RENEW_THRESHOLD', '1800'))

# 无需携带 token 即可访问的路径（登录/刷新端点自身处理凭证；健康探针供
# K8s/部署脚本免鉴权探测，见 run_server._register_health_routes）
AUTH_EXEMPT_PATHS = ("/api/auth/login", "/api/auth/refresh",
                   "/api/auth/sso/providers", "/api/auth/sso/oidc/login",
                   "/health", "/ready")  # S7：SSO 登录前需匿名访问


def _ensure_secure_secret() -> Optional[str]:
    """A-2：非 DEBUG 环境校验 JWT_SECRET 强度——生产用公开默认值/弱密钥可被
    伪造任意身份 token（含 admin）绕过全部认证。返回错误信息，None 表示通过。

    不在模块导入时抛错（避免测试/降级环境 import 中断），改为在签发 token
    （login/refresh）入口 fail-closed 拒绝。
    """
    if DEBUG:
        return None
    if JWT_SECRET == 'ai_factory_dev_secret':
        return "生产环境禁止使用默认 JWT_SECRET（请配置强密钥）"
    if len(JWT_SECRET) < 32:
        return "生产环境 JWT_SECRET 长度必须 ≥32 字节"
    return None


def _token_jti(token: str) -> Optional[str]:
    """从 JWT payload 解析 jti（JWT ID）；无 jti 或 token 无效返回 None。"""
    try:
        payload_part = token.split('.')[1]
        payload = json.loads(_auth.signer._b64d(payload_part).decode('utf-8'))
        return payload.get('jti')
    except Exception:
        return None


def _coerce_dt(value: Any) -> Optional[datetime]:
    """兼容 DB 返回 TIMESTAMP（datetime/str）与 API 层 datetime.now()。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M:%S%z"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _is_token_blacklisted(token: str) -> bool:
    """判断 token 是否已失效（撤销/登出）。

    v6.97 A.2：以 DB user_tokens.revoked_at 为唯一权威源（DB 优先），
    Redis 黑名单仅作加速缓存。DB 不可用时兜底拒绝（fail-closed）——
    原实现 Redis 不可用即放行，禁用/登出后的 token 仍可复用。
    """
    if not token:
        return False
    jti = _token_jti(token)
    db = _get_db()
    if db is not None:
        if jti:
            try:
                row = db.query_one('user_tokens', {'jti': jti})
            except Exception:
                row = None
            if row:
                # 找到签发记录：revoked_at 非空即已撤销；未撤销则有效
                if row.get('revoked_at'):
                    return True
                return False
            # 查无 jti 记录：迁移前签发的旧 token（无 jti/未落库）→ 放行兼容
            return False
        # 无 jti 的旧 token：DB 无法判定 → 放行兼容
        return False
    # DB 不可用：兜底拒绝（fail-closed，宁缺勿纵）
    return True

# 角色默认权限映射
_ROLE_PERMISSIONS = {
    "sales": {"discount_max": 0.05, "can_view_cost": False,
              "can_modify_order": True, "can_approve": False},
    "sales_director": {"discount_max": 0.15, "can_view_cost": True,
                       "can_modify_order": True, "can_approve": True},
    "finance": {"discount_max": 0, "can_view_cost": True,
                "can_modify_order": False, "can_approve": False},
    # v6.65：admin 通配权限（"*": True）——查询流程权限门禁与 PermissionSystem
    # ROLE_ADMIN 语义一致（全权限含查询门禁 can_inventory 等）
    "admin": {"discount_max": 1.0, "can_view_cost": True,
              "can_modify_order": True, "can_approve": True, "*": True},
}

# 模拟用户表（无数据库时使用，password为明文仅用于演示）
_MOCK_USERS = {
    "S0023": {"password": "123456", "name": "张明", "title": "张经理",
              "department": "销售部", "role": "sales"},
    "S0001": {"password": "admin123", "name": "管理员", "title": "系统管理员",
              "department": "信息技术部", "role": "admin"},
}


def _get_db() -> Any:
    """延迟获取数据库实例，获取失败时返回None（降级为模拟数据）。"""
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _role_permissions(role: str) -> Dict[str, Any]:
    """根据角色返回权限字典。

    discount_max 从 DB 读取（与 runtime/permission.py 同源 get_role_discount_max），
    覆盖 _ROLE_PERMISSIONS 中的默认值；v6.45：can_* 权限从 DB
    business_rules(ROLE-PERMS).role_permissions 读取覆盖（训练可调整）。
    DB 不可用时降级为 _ROLE_PERMISSIONS 默认。
    """
    perms = dict(_ROLE_PERMISSIONS.get(role, {
        "discount_max": 0, "can_view_cost": False,
        "can_modify_order": False, "can_approve": False,
    }))
    # discount_max 从 DB 读取（与 runtime/permission.py 同源），覆盖默认值
    try:
        from prog.runtime.permission import get_role_discount_max
        discount_map = get_role_discount_max()
        if role in discount_map:
            perms["discount_max"] = discount_map[role]
    except Exception:
        pass
    # v6.45：can_* 权限矩阵从 business_rules(ROLE-PERMS) 读取覆盖（训练可调整）
    try:
        from prog.runtime.permission import get_role_permissions_override
        overrides = get_role_permissions_override()
        role_perms = overrides.get(role, {})
        for k, v in role_perms.items():
            if k.startswith("can_"):
                perms[k] = bool(v)
    except Exception:
        pass
    return perms


class _UserSource:
    """用户源（业务侧注入框架认证器）：优先数据库 users 表，无数据库时回退模拟用户。

    模拟用户回退由框架 MockUserSource 门控——仅 DEBUG（RUNTIME_DEBUG=1）可用，
    正式模式禁止，防止无数据库时绕过认证。
    """

    def __init__(self) -> None:
        self._mock = MockUserSource(users=_MOCK_USERS)

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        db = _get_db()
        if db is not None:
            # A-3：DB 可用时由 DB 判定——未命中返回 None，不回退 mock。
            # 原实现 DB 可用但用户不存在（或查询异常）仍回退 MockUserSource，
            # 一旦 DEBUG 开启可用 mock 口令登录为 admin/sales，DB 用户被架空。
            try:
                row = db.query_one('users', {'user_id': user_id})
            except Exception:
                row = None
            if row:
                # DB 行使用 password_hash / role_id 列，统一映射为框架与业务侧
                # 期望的 password / role 键（与 users 表 003 迁移列名对齐）
                row = dict(row)
                if 'password' not in row and 'password_hash' in row:
                    row['password'] = row['password_hash']
                if 'role' not in row and 'role_id' in row:
                    row['role'] = row['role_id']
                # v6.97 A.2：禁用账户拒绝登录——status 非 active 返回 None，
                # 由框架 authenticate 判定认证失败（与 runtime/auth.py 对齐）
                if row.get('status') not in (None, 'active'):
                    return None
                return row
            return None
        # DB 整体不可用（开发降级）时回退模拟用户（MockUserSource 内部按 DEBUG 门控）
        return self._mock.get_user(user_id)


# 认证器（登录 API 能力由框架 runtime.auth 提供，业务侧仅注入用户源与适配 HTTP）
_auth = Authenticator(
    secret=JWT_SECRET,
    user_source=_UserSource(),
    token_ttl=JWT_EXPIRE_SECONDS,
)


def _build_user_info(user_id: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """构造标准用户信息。"""
    role = user.get('role', 'sales')
    name = user.get('name', '')
    return {
        "id": user_id,
        "name": name,
        "title": user.get('title', ''),
        "department": user.get('department', ''),
        "role": role,
        "permissions": _role_permissions(role),
        "avatar_color": "#3b82f6",
        "avatar_text": name[:1] if name else user_id[:1],
    }


def _issue_token(user_info: Dict[str, Any], token_type: str,
                 ttl_seconds: int) -> str:
    """签发指定类型 JWT token（框架 TokenSigner 提供，标准 HS256，自动附加 iat/exp）。

    v6.97 A.2：签发时生成 jti（JWT ID）写入 payload，并持久化到 user_tokens 表，
    供 token 校验（revoked_at）与禁用/登出联动撤销。
    A6：payload 增加 token_type 区分 access / refresh（refresh 仅供续期端点使用，
    不得作为业务 API 凭证——见 register_auth_middleware 中间件拦截）。
    """
    jti = uuid.uuid4().hex
    payload = {
        'user_id': user_info['id'],
        'role': user_info['role'],
        'name': user_info.get('name', ''),
        'title': user_info.get('title', ''),
        'department': user_info.get('department', ''),
        'permissions': user_info.get('permissions', {}),
        'jti': jti,
        'token_type': token_type,
    }
    token = _auth.signer.issue_token(payload, expires_in=ttl_seconds)
    _persist_token(user_info['id'], jti, ttl=ttl_seconds)
    return token


def _create_token(user_info: Dict[str, Any]) -> str:
    """签发 access token（A6：默认 2h 短期凭证）。"""
    return _issue_token(user_info, 'access', JWT_EXPIRE_SECONDS)


def _create_refresh_token(user_info: Dict[str, Any]) -> str:
    """签发 refresh token（A6：默认 7d 长期凭证，仅用于 /api/auth/refresh）。"""
    return _issue_token(user_info, 'refresh', JWT_REFRESH_EXPIRE_SECONDS)


def _persist_token(user_id: str, jti: str, ttl: Optional[int] = None) -> None:
    """签发后落库 user_tokens（DB 不可用/写入失败静默，登录流程不受阻）。"""
    ttl = ttl or JWT_EXPIRE_SECONDS
    db = _get_db()
    if db is None:
        return
    try:
        db.insert('user_tokens', {
            'jti': jti,
            'user_id': user_id,
            'issued_at': datetime.now(),
            'expires_at': datetime.now() + timedelta(seconds=ttl),
            'client_ip': request.remote_addr or None,
        })
    except Exception:
        pass


def _decode_token(token: str) -> Optional[Dict[str, Any]]:
    """解码JWT token（框架 TokenSigner 校验签名与过期），失败返回None。"""
    return _auth.verify(token)


def _get_bearer_token() -> str:
    """从Authorization头解析Bearer token。"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return ''


# --------------------------------------------------------
# 登录
# --------------------------------------------------------
@auth_bp.route('/login', methods=['POST'])
def login():
    """POST /api/auth/login 登录（用户名+密码）。

    认证能力由框架 runtime.auth（Authenticator）提供：
    用户源（DB 优先，模拟用户仅 DEBUG）→ 凭证校验 → 签发 HS256 JWT。
    v6.97 A.2：DB 路径增加 status 校验 / 密码错误锁定（5 次 15min、10 次 24h）/
    登录成功重置失败计数 / token 持久化 user_tokens。
    """
    try:
        body = request.get_json(silent=True) or {}
        user_id = body.get('user_id') or body.get('username', '')
        password = body.get('password', '')

        if not user_id or not password:
            return error_response(400, "user_id 与 password 为必填"), 400

        # A-2：非 DEBUG 环境签发 token 前校验 JWT_SECRET 强度（fail-closed）
        secret_err = _ensure_secure_secret()
        if secret_err:
            return error_response(500, secret_err), 500

        db = _get_db()
        if db is not None:
            result = _authenticate_db(db, user_id, password)
            if isinstance(result, tuple):
                code, msg = result
                return error_response(code, msg), code
            return api_response(code=0, data=result)

        # DB 整体不可用（开发降级）：框架认证（MockUserSource 内部按 DEBUG 门控）
        result = _auth.authenticate(user_id, password)
        if not result:
            return error_response(401, "用户名或密码错误"), 401

        # 业务侧补全展示字段与权限，并用业务载荷签发 token（含 name/permissions，兼容 /me）
        user_info = _build_user_info(user_id, result["user"])
        token = _create_token(user_info)
        # A6：登录同时签发 refresh token
        refresh_token = _create_refresh_token(user_info)

        return api_response(code=0,
                            data={"token": token, "refresh_token": refresh_token,
                                  "user": user_info})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


def _authenticate_db(db: Any, user_id: str, password: str) -> Any:
    """DB 路径完整认证：status 校验 + 锁定检查 + 失败计数/成功重置 + token 签发。

    返回：
        {"token": ..., "refresh_token": ..., "user": ...} 登录成功
        (code, msg) 元组表示失败响应
    """
    from prog.runtime.auth import verify_password

    try:
        row = db.query_one('users', {'user_id': user_id})
    except Exception:
        row = None
    if not row:
        return (401, "用户名或密码错误")
    row = dict(row)
    if 'password' not in row and 'password_hash' in row:
        row['password'] = row['password_hash']
    if 'role' not in row and 'role_id' in row:
        row['role'] = row['role_id']

    # v6.97 A.2：status 校验——禁用账户拒绝登录
    if row.get('status') not in (None, 'active'):
        return (401, "账户已禁用，请联系管理员")

    # v6.97 A.2：锁定检查——locked_until 未过期则拒绝
    now = datetime.now()
    locked_until = _coerce_dt(row.get('locked_until'))
    if locked_until and now < locked_until:
        return (401, f"账户已锁定，请于 {locked_until.strftime('%Y-%m-%d %H:%M')} "
                     f"后重试，或联系管理员解锁")

    # 密码校验
    if not verify_password(row.get('password', ''), password):
        _record_failed_attempt(db, user_id, row, now)
        return (401, "用户名或密码错误")

    # 登录成功：重置失败计数与锁定，记录最后登录
    try:
        db.update('users', {
            'failed_login_attempts': 0,
            'locked_until': None,
            'last_login': now,
        }, {'user_id': user_id})
    except Exception:
        pass

    user_info = _build_user_info(user_id, row)
    token = _create_token(user_info)
    # A6：登录同时签发 refresh token（长期，仅用于 /api/auth/refresh 换新 access）
    refresh_token = _create_refresh_token(user_info)
    return {"token": token, "refresh_token": refresh_token, "user": user_info}


def _record_failed_attempt(db: Any, user_id: str, row: Dict[str, Any],
                           now: datetime) -> None:
    """密码错误：失败计数 +1；达 5 次锁定 15 分钟，达 10 次锁定 24 小时。"""
    try:
        attempts = int(row.get('failed_login_attempts') or 0)
    except (TypeError, ValueError):
        attempts = 0
    attempts += 1
    update = {'failed_login_attempts': attempts}
    if attempts >= 10:
        update['locked_until'] = now + timedelta(hours=24)
    elif attempts >= 5:
        update['locked_until'] = now + timedelta(minutes=15)
    try:
        db.update('users', update, {'user_id': user_id})
    except Exception:
        pass


# --------------------------------------------------------
# 登出
# --------------------------------------------------------
@auth_bp.route('/logout', methods=['POST'])
def logout():
    """POST /api/auth/logout 登出。

    v6.97 A.2：撤销 user_tokens（revoked_at+logout，DB 为权威源），
    并写 Redis 黑名单作加速缓存。
    """
    try:
        token = _get_bearer_token()
        if token:
            # DB 撤销：revoked_at 落库（最终权威）。S1：写失败 fail-closed——
            # 返回 500 提示重试，避免"登出成功但 token 仍可用"
            jti = _token_jti(token)
            db = _get_db()
            if db is not None and jti:
                try:
                    db.execute(
                        "UPDATE user_tokens SET revoked_at = NOW(), "
                        "revoke_reason = 'logout' "
                        "WHERE jti = :jti AND revoked_at IS NULL",
                        {"jti": jti},
                    )
                except Exception:
                    return error_response(
                        500, "登出失败：凭证撤销写入异常，请重试"), 500
            # Redis 黑名单（加速缓存，仅作二次校验）
            try:
                import redis
                r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))
                try:
                    r.setex(f"jwt:blacklist:{token}", JWT_EXPIRE_SECONDS, "1")
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass
            except Exception:
                pass
        return api_response(code=0, data={"success": True})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 获取当前用户信息
# --------------------------------------------------------
@auth_bp.route('/me', methods=['GET'])
def me():
    """GET /api/auth/me 获取当前用户信息。"""
    try:
        payload = _decode_token(_get_bearer_token())
        if not payload:
            return error_response(401, "未登录或token已失效"), 401

        user_info = {
            "id": payload.get('user_id', ''),
            "name": payload.get('name', ''),
            "role": payload.get('role', ''),
            "permissions": payload.get('permissions', {}),
        }
        return api_response(code=0, data=user_info)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 刷新Token
# --------------------------------------------------------
@auth_bp.route('/refresh', methods=['POST'])
def refresh():
    """POST /api/auth/refresh 刷新Token。

    A6：优先接受 refresh token（token_type == 'refresh'，最长 7d）换取新的
    access token（同时轮换 refresh token，缩短泄露窗口）；为保持向后兼容，
    token_type 缺失或为 access 的旧 token 仍可按原逻辑续期 access。
    """
    try:
        token = _get_bearer_token()
        # A-1：已登出（黑名单）的 token 不得续期
        if _is_token_blacklisted(token):
            return error_response(401, "token已失效（已登出）"), 401
        payload = _decode_token(token)
        if not payload:
            return error_response(401, "未登录或token已失效"), 401
        # A-4：载荷完整性校验（与中间件一致，防缺角色续期）
        if not payload.get('user_id') or not payload.get('role'):
            return error_response(401, "token 载荷不完整"), 401
        # A-2：签发前校验 JWT_SECRET 强度
        secret_err = _ensure_secure_secret()
        if secret_err:
            return error_response(500, secret_err), 500

        # A6：区分 refresh token（换新 access + 轮换 refresh）与旧 access token
        # （token_type 缺失或为 access 时走原续期逻辑，兼容历史客户端）
        is_refresh = payload.get('token_type') == 'refresh'

        # 基于原payload重新签发token
        user_info = {
            "id": payload.get('user_id', ''),
            "name": payload.get('name', ''),
            "title": payload.get('title', ''),
            "department": payload.get('department', ''),
            "role": payload.get('role', ''),
            "permissions": payload.get('permissions', {}),
        }
        # 补全头像等展示字段
        user_info = _build_user_info(user_info['id'], {
            'name': user_info['name'], 'role': user_info['role'],
        }) if not payload.get('permissions') else user_info
        token = _create_token(user_info)

        if is_refresh:
            # refresh → 新 access + 新 refresh（轮换）
            refresh_token = _create_refresh_token(user_info)
            return api_response(code=0,
                                data={"token": token, "refresh_token": refresh_token,
                                      "user": user_info})

        return api_response(code=0, data={"token": token, "user": user_info})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# 认证中间件（业务 API 身份唯一来源）
# ============================================================
def register_auth_middleware(app: Any) -> None:
    """为 Flask app 注册认证中间件。

    职责（用户使用时携带 token，后端据此解析身份）：
        1. 拦截所有 /api/* 请求（除 login/refresh），解析 Authorization: Bearer <token>
        2. 校验失败（缺失/伪造/过期）统一返回 401，业务层无法拿到身份
        3. 校验通过后把身份注入 g（user_id/role/name/title/department/permissions），
           业务代码只允许从 g 读取，禁止信任请求头或请求体
        4. Token 临近过期（剩余 < RENEW_THRESHOLD）时自动重签新 token，
           通过响应头 X-New-Token 回发给客户端，实现免打断续期

    参数:
        app: Flask app 实例
    """
    @app.before_request
    def _auth_before_request():
        path = request.path
        if not path.startswith("/api/") or path in AUTH_EXEMPT_PATHS:
            return None

        token = _get_bearer_token()
        # A-1：登出黑名单校验——已登出的 token 立即失效（Redis 不可用时放行，
        # 与登出写黑名单失败静默一致）
        if _is_token_blacklisted(token):
            return error_response(401, "token已失效（已登出）"), 401

        payload = _decode_token(token)
        if not payload:
            return error_response(401, "未登录或token已失效"), 401

        # A-4：token 载荷必须含非空 user_id/role——防止"缺角色 token + 业务侧
        # body.role 兜底"组合提权（training 等端点曾回退信任请求体角色）
        if not payload.get('user_id') or not payload.get('role'):
            return error_response(401, "token 载荷不完整（缺 user_id/role）"), 401

        # A6：refresh token 仅限 /api/auth/refresh 换取新 access，不得作为业务凭证
        if payload.get('token_type') == 'refresh':
            return error_response(
                401, "refresh token 不能用于业务接口，请通过 /api/auth/refresh 换取 access token"), 401

        # 身份注入 g（业务侧唯一可信来源）
        g.user_id = payload.get('user_id', '')
        g.user_role = payload.get('role', '')
        g.user_name = payload.get('name', '')
        g.user_title = payload.get('title', '')
        g.user_department = payload.get('department', '')
        g.permissions = payload.get('permissions', {})

        # v6.71：将当前用户注入 DatabaseManager，供审计钩子记录操作者
        try:
            from prog.core.database import get_database as _get_db
            _db = _get_db()
            if _db is not None:
                _db.set_current_user(g.user_id)
        except Exception:
            pass

        # 临近过期自动续期（issue_token 重置 exp）
        exp = payload.get('exp', 0)
        if exp and int(exp) - int(time.time()) < RENEW_THRESHOLD:
            g.new_token = _auth.signer.issue_token(payload, expires_in=JWT_EXPIRE_SECONDS)
        return None

    @app.after_request
    def _auth_after_request(resp: Any) -> Any:
        new_token = getattr(g, 'new_token', None)
        if new_token:
            resp.headers['X-New-Token'] = new_token
        return resp


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、Blueprint定义、核心路由完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert auth_bp is not None, "auth_bp 未定义"
    hello_world(__name__, "auth_bp 定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
