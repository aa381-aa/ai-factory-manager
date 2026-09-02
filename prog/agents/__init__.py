"""
Agent 模块
==========

模块用途：
    AI工厂管家模块化Agent架构的入口包，承载所有业务领域Agent的实现。

技术规格章节：
    - §1.1.3 Coordinator Agent（协调Agent）
    - §3.2 Sales Agent（销售Agent，P0）
    - §3.3 Production Agent（生产Agent，P1）
    - §3.4 Warehouse-Procurement Agent（仓储采购Agent，P1）
    - §3.5 Technical Agent（技术Agent，P1）
    - §3.6 Finance Agent（财务Agent，P1）
    - §3.7 Knowledge Assistant（知识助手，P2）
    - §3.8 QC Agent（质检Agent，P2可选）

替代demo：
    替代 demo/server.py 中的单文件架构，将 generate_ai_response() 中
    15+ 意图处理器拆分为独立的领域Agent，每个Agent聚焦单一业务域。

架构说明：
    双通道架构：
    - 业务操作通道：SalesAgent / ProductionAgent / WarehouseAgent /
      TechnicalAgent / FinanceAgent / QCAgent，操作 PostgreSQL 业务库
    - 管理咨询通道：KnowledgeAssistant，查询 Milvus 向量库（RAG 问答）

    所有Agent继承 BaseAgent，由 CoordinatorAgent 统一路由分发。
"""

# Agent 模块公开的对外接口（具体实现见各子模块）
# from .base_agent import BaseAgent, AgentResponse, RuleResult
# from .coordinator import CoordinatorAgent
# from .sales_agent import SalesAgent
# from .production_agent import ProductionAgent
# from .warehouse_agent import WarehouseAgent
# from .technical_agent import TechnicalAgent
# from .finance_agent import FinanceAgent
# from .qc_agent import QCAgent
# from .knowledge_assistant import KnowledgeAssistant

__all__ = [
    # "BaseAgent",
    # "AgentResponse",
    # "RuleResult",
    # "CoordinatorAgent",
    # "SalesAgent",
    # "ProductionAgent",
    # "WarehouseAgent",
    # "TechnicalAgent",
    # "FinanceAgent",
    # "QCAgent",
    # "KnowledgeAssistant",
]
