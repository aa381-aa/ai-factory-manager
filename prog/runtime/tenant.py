"""
C3 多租户上下文（SaaS 化骨架，可商用部署功能补充建议 · C3）
==========================================================
文件用途：
    提供线程安全的当前租户上下文（contextvars），业务代码可读取
    当前请求所属租户。默认 "default" 单租户，不强制接线到业务查询，
    避免破坏现有单租户行为。

数据模型：
    089 迁移已为 customers / suppliers / orders / products / inventory
    增加 tenant_id 列（默认 'default'），tenants 表已存在。

RLS 策略开启方法（生产按需，勿默认开启）：
    1. 为每张业务表启用行级安全：
        ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
    2. 创建租户隔离策略（策略名唯一）：
        CREATE POLICY tenant_isolation ON orders
        USING (tenant_id = current_setting('app.tenant_id'));
    3. 连接建立/请求入口设置租户 GUC：
        SET app.tenant_id = '<tenant_id>';   -- 或 use set_config(..., false)
    4. 注意事项：
        - 必须先为超级用户/应用账号关闭 RLS（BYPASSRLS 或 policy 放行），
          否则管理员也受隔离限制；
        - 现有单租户数据（tenant_id='default'）在开启后需单独策略放行。

接口：
    get_current_tenant() -> str                当前租户（默认 "default"）
    set_current_tenant(tenant_id) -> str       显式设置当前租户
    with_tenant(tenant_id) -> ContextManager   上下文管理器（退出自动恢复）
"""

import contextvars
from contextlib import contextmanager
from typing import Iterator

#: 当前租户（默认 "default" 单租户）
_tenant_var: contextvars.ContextVar = contextvars.ContextVar(
    "tenant_id", default="default")


def get_current_tenant() -> str:
    """获取当前上下文的租户 ID（未设置时返回 "default"）。"""
    return _tenant_var.get()


def set_current_tenant(tenant_id: str) -> str:
    """显式设置当前上下文的租户 ID。

    参数:
        tenant_id: 租户 ID（空值回落为 "default"）

    返回:
        设置后的租户 ID
    """
    _tenant_var.set(tenant_id or "default")
    return _tenant_var.get()


@contextmanager
def with_tenant(tenant_id: str) -> Iterator[None]:
    """上下文管理器：在 with 块内临时切换租户，退出自动恢复。

    用法：
        with with_tenant("tenant_a"):
            assert get_current_tenant() == "tenant_a"
    """
    token = _tenant_var.set(tenant_id or "default")
    try:
        yield
    finally:
        _tenant_var.reset(token)
