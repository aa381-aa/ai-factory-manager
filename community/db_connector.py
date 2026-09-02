"""
公共数据库连接器
================
用途：
    建立开源社区版到工厂托管公共 PostgreSQL 的连接。
    复用 prog.core.database.DatabaseManager 的 volcano（RDS）模式，
    通过 tenant_id 列实现共享 schema 数据隔离。

配置（.env）：
    COMMUNITY_DB_ENABLED=true          # 是否启用公共库（默认 false 保守关闭）
    RDS_HOST=community.factory.example.com
    RDS_PORT=5432
    RDS_USER=community_user
    RDS_PASSWORD=<工厂分配>
    RDS_DATABASE=ai_factory_community
    COMMUNITY_TENANT_ID=<工厂分配>      # 唯一租户标识
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


class CommunityDBConnector:
    """公共数据库连接器（单例）。

    仅在 COMMUNITY_DB_ENABLED=true 且配置完整时初始化；
    任一环节失败返回 None，本地功能不受影响（fail-open）。
    """

    _instance: Optional["CommunityDBConnector"] = None

    def __init__(self, db: Any = None) -> None:
        self._db = db
        self.tenant_id: str = (
            os.environ.get("COMMUNITY_TENANT_ID") or "default")
        self.enabled: bool = (
            os.environ.get("COMMUNITY_DB_ENABLED", "").lower()
            in ("1", "true", "yes"))

    @classmethod
    def get_instance(cls, db: Any = None) -> Optional["CommunityDBConnector"]:
        """获取单例（延迟初始化，失败返回 None 不抛异常）。"""
        if cls._instance is not None:
            return cls._instance
        if os.environ.get("COMMUNITY_DB_ENABLED", "").lower() not in (
                "1", "true", "yes"):
            return None
        try:
            if db is None:
                from prog.core.database import DatabaseManager
                db = DatabaseManager("volcano").get_session_factory() \
                    if hasattr(DatabaseManager("volcano"),
                               "get_session_factory") else None
                if db is None:
                    db = _build_sqlalchemy_engine()
            cls._instance = cls(db=db)
            return cls._instance
        except Exception as e:  # noqa: BLE001
            logger.warning("公共数据库连接初始化失败（本地功能不受影响）：%s", e)
            return None

    def upload(self, table: str, rows: list) -> bool:
        """批量写入公共库（幂等：主键冲突跳过）。"""
        if not self.enabled or self._db is None or not rows:
            return False
        try:
            for row in rows:
                row = dict(row)
                row.setdefault("tenant_id", self.tenant_id)
                self._db.insert(table, row)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("公共库写入 %s 失败：%s", table, e)
            return False


def _build_sqlalchemy_engine():
    """构造 SQLAlchemy engine（独立连接，SSL 要求）。"""
    from sqlalchemy import create_engine
    host = os.environ.get("RDS_HOST", "")
    port = os.environ.get("RDS_PORT", "5432")
    user = os.environ.get("RDS_USER", "")
    pwd = os.environ.get("RDS_PASSWORD", "")
    dbname = os.environ.get("RDS_DATABASE", "ai_factory_community")
    if not (host and user and pwd):
        return None
    url = (f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{dbname}"
           f"?sslmode=require")
    return create_engine(url, pool_pre_ping=True)


def get_community_db() -> Any:
    """获取公共库连接（无则 None）。"""
    conn = CommunityDBConnector.get_instance()
    return conn._db if conn else None
