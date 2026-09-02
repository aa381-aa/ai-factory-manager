"""
租户引导：获取公共库租户标识与初始化
====================================
用途：
    开源社区版首次启动时向工厂登记租户，获取唯一 tenant_id 与上报密钥。
    工厂侧根据部署情况分配，本模块仅提供本地配置检查与提示，
    不涉及云端注册 API（避免对开源版引入外部依赖）。

配置（.env）：
    COMMUNITY_TENANT_ID=<工厂分配>    # 必需，缺省 'default'（单租户演示）
    COMMUNITY_DB_ENABLED=true         # 开启公共库采集

说明：
    - 租户登记由工厂在部署时提供（现场/远程方式），本模块只做本地校验；
    - 未配置 COMMUNITY_TENANT_ID 时回退 'default'，仍可正常使用本地功能。
"""

from __future__ import annotations

import os
from typing import Optional


def get_tenant_id() -> str:
    """读取当前租户标识。"""
    return os.environ.get("COMMUNITY_TENANT_ID") or "default"


def is_community_enabled() -> bool:
    """公共库采集是否开启。"""
    return os.environ.get("COMMUNITY_DB_ENABLED", "").lower() in (
        "1", "true", "yes")


def config_status() -> dict:
    """返回配置状态（供启动日志/健康检查展示）。"""
    enabled = is_community_enabled()
    tenant = get_tenant_id()
    missing = []
    if enabled:
        for k in ("RDS_HOST", "RDS_USER", "RDS_PASSWORD"):
            if not os.environ.get(k):
                missing.append(k)
    return {
        "community_db_enabled": enabled,
        "tenant_id": tenant,
        "missing_env": missing,
        "note": ("" if (not enabled or not missing)
                 else f"公共库启用但缺少配置项：{', '.join(missing)}，"
                      f"上报将自动跳过"),
    }
