"""
AI工厂管家 - 训练数据模型与数据访问层
==================================================
文件用途：
    定义训练数据（TrainingData）的 SQLAlchemy ORM 模型及 TrainingDataDAL
    数据访问层，按 §1.4.2 要求将 Agent 训练样本存储于 PostgreSQL 的 JSONB 字段。

对应技术规格章节：
    - §1.4.2  训练数据存储在 PostgreSQL 的 JSONB 字段中（不用 MongoDB）
              明确禁止使用 MongoDB，统一在 PostgreSQL 内存储半结构化训练样本，
              便于事务一致性、备份恢复与权限统一管理。

替代 demo 文件 / 函数：
    - demo 阶段无独立训练数据存储，训练样本散落在会话上下文与日志中。
    - 本模块作为正式训练数据持久化入口，替代 demo 中临时性的会话存储。

设计说明（§1.4.2 PostgreSQL JSONB 存储，不用 MongoDB）：
    1. 一条训练样本对应一行记录，核心文本字段（user_input/ai_output/user_correction/final_output）
       使用普通字符串列，便于全文检索与索引。
    2. metadata 使用 JSONB 字段存储扩展属性（如意图置信度、模型版本、会话ID、标签等），
       兼顾结构化查询与半结构化扩展。
    3. approved 字段标记样本是否经人工审核可用于训练，未审核样本不进入训练集。
    4. export_dataset() 按过滤条件批量导出为训练框架可消费的格式（如 JSONL）。
"""

import json
from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Boolean, DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from prog.models.base import Base


class TrainingData(Base):
    """训练数据 ORM 模型

    对应数据库表 training_data，存储 Agent 训练样本。

    字段说明：
        id               : 整型主键，自增
        agent_type       : Agent 类型（如 "order_agent"、"inventory_agent"）
        intent           : 意图标签（如 "query_inventory"、"create_order"）
        user_input       : 用户原始输入
        ai_output        : AI 初始输出
        user_correction  : 用户纠正后的正确输出（可为空，表示无需纠正）
        final_output     : 最终采纳的输出（= user_correction 或 ai_output）
        metadata         : 【JSONB §1.4.2】扩展元数据
                           示例：{"confidence": 0.87, "model_version": "v1.2",
                                  "session_id": "sess-xxx", "tags": ["高价值样本"]}
        created_at       : 创建时间
        approved         : 是否经人工审核可用于训练

    设计依据：
        §1.4.2 明确要求训练数据存储在 PostgreSQL 的 JSONB 字段中，不用 MongoDB。
        本模型即按此要求设计，metadata 列使用 JSONB 类型。

    说明：
        由于 SQLAlchemy DeclarativeBase 保留了 metadata 作为 MetaData 对象，
        此处使用 Python 属性名 metadata_ 映射到数据库列 "metadata"，避免命名冲突。
    """

    __tablename__ = "training_data"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_type: Mapped[str] = mapped_column(String(50), index=True, comment="Agent类型")
    intent: Mapped[Optional[str]] = mapped_column(String(100), index=True, comment="意图标签")
    user_input: Mapped[str] = mapped_column(Text, comment="用户原始输入")
    ai_output: Mapped[Optional[str]] = mapped_column(Text, comment="AI初始输出")
    user_correction: Mapped[Optional[str]] = mapped_column(Text, comment="用户纠正后的正确输出")
    final_output: Mapped[Optional[str]] = mapped_column(Text, comment="最终采纳的输出")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, comment="【§1.4.2】扩展元数据 JSONB")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), comment="创建时间")
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否审核通过可用于训练")


class TrainingDataDAL:
    """训练数据访问层

    封装 training_data 表的 CRUD、审核与导出，作为 Agent 训练样本的
    统一持久化入口（§1.4.2，PostgreSQL JSONB，不用 MongoDB）。

    方法说明：
        get_by_agent()     : 按 Agent 类型查询样本
        get_by_intent()    : 按意图标签查询样本
        create()           : 创建训练记录
        approve()          : 审批训练数据
        get_unapproved()   : 获取未审批样本
        export_dataset()   : 导出训练数据集
    """

    def __init__(self, session=None):
        """初始化 DAL，设置表名

        Args:
            session: 兼容历史接口的 Session 参数（数据库操作通过 get_database() 获取实例）
        """
        self.session = session
        self.table_name = "training_data"

    def get_by_agent(self, agent_type: str, limit: int = 100) -> list:
        """按 Agent 类型查询训练数据

        Args:
            agent_type: Agent 类型
            limit: 返回条数上限（默认100）

        Returns:
            list[dict]: 样本记录列表，按创建时间倒序
        """
        from prog.core.database import get_database
        db = get_database()
        return db.query_many(
            self.table_name,
            filters={"agent_type": agent_type},
            limit=limit,
            order_by="created_at DESC",
        )

    def get_by_intent(self, agent_type: str, intent: str) -> list:
        """按意图查询训练数据

        Args:
            agent_type: Agent 类型
            intent: 意图标签

        Returns:
            list[dict]: 样本记录列表，按创建时间倒序
        """
        from prog.core.database import get_database
        db = get_database()
        filters = {"agent_type": agent_type, "intent": intent}
        return db.query_many(
            self.table_name, filters=filters, order_by="created_at DESC",
        )

    def create(self, data: dict):
        """创建训练记录

        Args:
            data: 样本字段字典（含 agent_type, intent, user_input, ai_output 等）

        Returns:
            新插入记录的主键 id
        """
        from prog.core.database import get_database
        db = get_database()
        # 未指定审批状态时默认为未审批
        if "approved" not in data:
            data["approved"] = False
        return db.insert(self.table_name, data)

    def approve(self, id: int, approved_by: str = None) -> bool:
        """审批训练数据

        标记样本为已审核可用于训练，并将审批人写入 metadata JSONB。

        Args:
            id: 样本主键
            approved_by: 审批人标识（写入 metadata.approved_by）

        Returns:
            bool: 是否审核成功
        """
        from prog.core.database import get_database
        db = get_database()
        rows = db.update(self.table_name, {"approved": True}, {"id": id})
        # 将审批人写入 metadata JSONB（与已有 metadata 合并）
        if rows > 0 and approved_by:
            try:
                db.execute(
                    "UPDATE training_data SET metadata = COALESCE(metadata, '{}'::jsonb) || "
                    "CAST(:meta AS jsonb) WHERE id = :rid",
                    {
                        "meta": json.dumps({"approved_by": approved_by}, ensure_ascii=False),
                        "rid": id,
                    },
                )
            except Exception:
                pass
        return rows > 0

    def get_unapproved(self, agent_type: str = None) -> list:
        """获取未审批的训练数据

        Args:
            agent_type: 可选，限定 Agent 类型

        Returns:
            list[dict]: 未审批样本列表，按创建时间倒序
        """
        from prog.core.database import get_database
        db = get_database()
        filters = {"approved": False}
        if agent_type:
            filters["agent_type"] = agent_type
        return db.query_many(
            self.table_name, filters=filters, order_by="created_at DESC",
        )

    def export_dataset(self, agent_type: str, format: str = 'jsonl') -> list:
        """导出训练数据集

        按过滤条件批量导出已审核样本为训练框架可消费的格式。

        Args:
            agent_type: Agent 类型
            format: 导出格式，'jsonl' 返回 JSON Lines 字符串列表，其他返回字典列表

        Returns:
            list: 数据集记录列表（jsonl 格式时每项为 JSON 字符串）
        """
        from prog.core.database import get_database
        db = get_database()
        records = db.query_many(
            self.table_name,
            filters={"agent_type": agent_type, "approved": True},
            order_by="created_at ASC",
        )
        if format == 'jsonl':
            # 返回 JSONL 格式字符串列表，每条可直接喂入训练
            return [
                json.dumps(r, ensure_ascii=False, default=str)
                for r in records
            ]
        return records


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert TrainingData is not None, "TrainingData 类未定义"
    assert TrainingDataDAL is not None, "TrainingDataDAL 类未定义"
    assert TrainingData.__tablename__ == "training_data", "表名不正确"
    hello_world(__name__, "训练数据模型与DAL定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
