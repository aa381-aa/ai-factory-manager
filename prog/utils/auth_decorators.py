"""
认证装饰器（S8/A1）
==================
文件用途：
    提供统一的角色/权限校验装饰器，替代各 Blueprint 内散落的手写校验。
    认证中间件（api/auth.py register_auth_middleware）已将身份注入 g，
    装饰器仅从 g 读取并校验，不重复解析 token。

接口：
    @require_role("admin")           -- 仅允许指定角色
    @require_role("admin", "sales")  -- 多角色任一即可
    @require_permission("user:write")            -- 需同时具备指定权限
    @require_permission("user:read", "user:write")  -- 需同时具备所有权限
"""

from functools import wraps

from flask import g
from prog.utils.api_response import error_response


def require_role(*allowed_roles: str) -> callable:
    """角色校验装饰器：仅允许指定角色的用户访问。

    参数：
        allowed_roles: 允许的角色列表（如 "admin", "sales"）；
                       空表示任意已登录用户均可（仅校验登录态）

    用法：
        @llm_bp.route('/chat', methods=['POST'])
        @require_role('admin')
        def chat():
            ...
    """
    def decorator(fn: callable) -> callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # 认证中间件已注入 g.user_id / g.user_role
            user_id = getattr(g, 'user_id', '')
            user_role = getattr(g, 'user_role', '')
            if not user_id:
                return error_response(401, "未登录"), 401
            if allowed_roles and user_role not in allowed_roles:
                return error_response(
                    403, f"权限不足（需要角色：{'/'.join(allowed_roles)}）"), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def require_permission(*required_perms: str) -> callable:
    """权限校验装饰器：需同时具备指定权限（AND 语义）。

    权限来源：认证中间件注入的 g.permissions（dict，来自 token 载荷）。
    管理员通配：permissions 含 "*" 且值为真时直接放行（对应 _ROLE_PERMISSIONS
    中 admin 的 "*": True 通配），避免 admin 被遗漏权限卡死。

    参数：
        required_perms: 需要的权限键列表（如 "user:write"、"order:approve"）；
                       空表示任意已登录用户均可（仅校验登录态）

    用法：
        @user_bp.route('/users', methods=['POST'])
        @require_permission('user:write')
        def create_user():
            ...
    """
    def decorator(fn: callable) -> callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_id = getattr(g, 'user_id', '')
            if not user_id:
                return error_response(401, "未登录"), 401
            perms = getattr(g, 'permissions', None) or {}
            # 管理员通配：permissions["*"] 为真即放行
            if perms.get("*"):
                return fn(*args, **kwargs)
            missing = [p for p in required_perms if not perms.get(p)]
            if missing:
                return error_response(
                    403, f"权限不足（需要：{'/'.join(missing)}）"), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator
