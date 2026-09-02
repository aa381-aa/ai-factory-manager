"""
应用设置 - AI工厂管家

文件用途：
    定义 Flask 应用运行所需的全部设置项，作为 Flask app.config 的来源。
    设置值从环境变量读取，遵循三层变量加载机制（环境变量为最高优先级）。

对应技术规格章节：
    §1.7 认证与安全（JWT）
    §1.8.8 统一部署配置
    §1.9 可观测性（日志）

设计说明：
    - Settings 类提供类属性形式的配置项，便于在 Flask 中以 from_object 加载
    - 所有敏感值从环境变量读取，代码中不硬编码密钥
    - 通过 get_settings() 单例避免重复环境变量读取
    - 各功能模块通过开关字段（ENABLE_XXX）控制是否启用，便于灰度
"""

import os
from typing import Any, Optional


class Settings:
    """
    Flask 应用设置类

    通过 app.config.from_object(Settings) 加载到 Flask 配置中。
    所有属性值在类定义时从环境变量读取，并提供安全默认值。

    配置分组:
        - 基础应用配置（APP_ENV / SECRET_KEY 等）
        - JWT 认证配置（§1.7）
        - 日志配置（§1.9）
        - CORS 跨域配置
        - 各功能模块开关（Agent / 审核引擎 / RAG 等）
    """

    # -------------------------------------------------------------------------
    # 基础应用配置
    # -------------------------------------------------------------------------
    # 运行环境：development / production
    APP_ENV: str = os.environ.get("APP_ENV", "development")

    # Flask 会话签名密钥（生产环境必须通过环境变量覆盖）
    SECRET_KEY: str = os.environ.get("APP_SECRET_KEY", "dev-secret-key-change-me")

    # S11：生产环境 SECRET_KEY 强度校验——拒绝默认值/短密钥，防止会话伪造。
    # 与 auth.py JWT_SECRET 校验（非 DEBUG fail-closed）一致：仅 APP_ENV=production
    # 时于加载期强制（fail-fast），开发环境保留 dev 默认值便于本地启动；
    # 未设 APP_ENV 但以 --env prod 启动的路径由 run_server main() 另行兜底校验。
    if os.environ.get("APP_ENV") == "production" and (
        SECRET_KEY == "dev-secret-key-change-me" or len(SECRET_KEY) < 32
    ):
        raise RuntimeError(
            "SECRET_KEY 强度不足：生产环境必须通过 APP_SECRET_KEY 环境变量"
            "设置 ≥32 字节的随机密钥"
        )

    # JSON 默认编码（中文不转义）
    JSON_AS_ASCII: bool = False

    # 监听端口
    PORT: int = int(os.environ.get("APP_PORT", "5000"))

    # -------------------------------------------------------------------------
    # JWT 认证配置（§1.7）
    # -------------------------------------------------------------------------
    # JWT 签名密钥
    JWT_SECRET_KEY: str = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-me")

    # JWT 签名算法
    JWT_ALGORITHM: str = os.environ.get("JWT_ALGORITHM", "HS256")

    # Token 过期时间（小时）
    JWT_EXPIRE_HOURS: int = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))

    # Token 在请求头中的字段名
    JWT_HEADER_NAME: str = "Authorization"

    # Token 前缀
    JWT_HEADER_PREFIX: str = "Bearer"

    # -------------------------------------------------------------------------
    # 日志配置（§1.9）
    # -------------------------------------------------------------------------
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # 日志格式
    LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"

    # 日志文件路径（为空时输出到控制台）
    LOG_FILE: str = os.environ.get("LOG_FILE", "")

    # 日志文件最大大小（MB）
    LOG_MAX_BYTES: int = 10

    # 日志文件保留份数
    LOG_BACKUP_COUNT: int = 5

    # -------------------------------------------------------------------------
    # CORS 跨域配置
    # -------------------------------------------------------------------------
    # 允许的来源（* 表示全部，生产环境建议指定域名列表）
    CORS_ORIGINS: Any = "*"

    # 允许的方法
    CORS_METHODS: Any = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]

    # 允许的请求头
    CORS_ALLOW_HEADERS: Any = ["Content-Type", "Authorization"]

    # 是否允许携带凭证
    CORS_SUPPORTS_CREDENTIALS: bool = False

    # -------------------------------------------------------------------------
    # 功能模块开关
    # -------------------------------------------------------------------------
    # 是否启用审核引擎（七层审核链）
    ENABLE_AUDIT_ENGINE: bool = True

    # 是否启用 RAG 知识库
    ENABLE_RAG: bool = True

    # 是否启用事件总线（异步事件驱动）
    ENABLE_EVENT_BUS: bool = True

    # 是否启用 LLM 响应缓存
    ENABLE_LLM_CACHE: bool = True

    # 是否启用限流
    ENABLE_RATE_LIMIT: bool = True

    # LLM 调用超时（秒，LLM_TIMEOUT 可覆盖，集中登记于 .env）
    LLM_TIMEOUT: int = int(os.environ.get("LLM_TIMEOUT", "60"))

    # LLM 最大 Token 数
    LLM_MAX_TOKENS: int = 4096

    # LLM 温度参数
    LLM_TEMPERATURE: float = 0.3

    # 预签名 URL 过期时间（秒，用于文件存储）
    PRESIGNED_URL_EXPIRE: int = 3600

    # Redis 缓存默认过期时间（秒）
    CACHE_DEFAULT_TTL: int = 3600

    # 限流窗口（秒）
    RATE_LIMIT_WINDOW: int = 60

    # 限流窗口内最大请求数
    RATE_LIMIT_MAX_REQUESTS: int = 100


# 模块级单例缓存
_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """
    获取应用设置单例

    返回:
        Settings 单例实例
    """
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
