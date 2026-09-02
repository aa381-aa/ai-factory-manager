"""
AI工厂管家 — 数据模型与数据访问层（DAL）模块
==================================================
文件用途：
    本包聚合所有 SQLAlchemy ORM 数据模型及对应的数据访问层（DAL）类。
    作为 demo 阶段 data_manager.py（基于 factory_data.json 的内存字典访问）
    的 PostgreSQL 迁移目标，提供持久化、事务、并发安全的统一数据入口。

对应技术规格章节：
    - §1.4.2 训练数据存储在 PostgreSQL 的 JSONB 字段中（不用 MongoDB）
    - §1.7  数据模型扩展（图纸版本、信用余额、库存五阶段、订单全生命周期）

替代 demo 文件：
    - data_manager.py 中的 DataManager 类及其 get_*/add_*/update_* 系列方法

子模块清单：
    - product         产品模型 + ProductDAL
    - inventory       库存模型 + InventoryDAL（五阶段状态流转）
    - customer        客户模型 + CustomerDAL（信用余额实时跟踪）
    - order           订单模型 + OrderDAL（8 阶段全生命周期 + 审核链关联）
    - production_line 产线模型 + ProductionLineDAL
    - qc_record       质检记录模型 + QCDAL
    - notification    通知模型 + NotificationDAL
    - audit_log       审核日志模型 + AuditLogDAL（七层审核链哈希链）
    - training_data   训练数据模型 + TrainingDataDAL（JSONB 存储）

设计约定：
    1. 所有 ORM 模型统一使用 SQLAlchemy 2.0 风格声明（Mapped / mapped_column）。
    2. 每个 DAL 类封装该模型的 CRUD 与业务查询，替代 demo DataManager 的对应方法。
    3. 事务边界由调用方（Service 层）控制，DAL 方法不自行 commit。
    4. JSONB 字段用于存储半结构化数据（订单明细、质检项、训练元数据等）。
"""

# 导入各子模块的 ORM 模型与 DAL 类，便于上层统一引用
# 例如：from prog.models import Product, ProductDAL, Order, OrderDAL
#
# from prog.models.product import Product, ProductDAL
# from prog.models.inventory import Inventory, InventoryDAL
# from prog.models.customer import Customer, CustomerDAL
# from prog.models.order import Order, OrderItem, OrderDAL
# from prog.models.production_line import ProductionLine, ProductionLineDAL
# from prog.models.qc_record import QCRecord, QCDAL
# from prog.models.notification import Notification, NotificationDAL
# from prog.models.audit_log import AuditLog, AuditLogDAL
# from prog.models.training_data import TrainingData, TrainingDataDAL

__all__ = [
    # "Product", "ProductDAL",
    # "Inventory", "InventoryDAL",
    # "Customer", "CustomerDAL",
    # "Order", "OrderItem", "OrderDAL",
    # "ProductionLine", "ProductionLineDAL",
    # "QCRecord", "QCDAL",
    # "Notification", "NotificationDAL",
    # "AuditLog", "AuditLogDAL",
    # "TrainingData", "TrainingDataDAL",
]
