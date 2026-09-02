"""
AI工厂管家 - 库存数据模型与数据访问层
==================================================
文件用途：
    定义库存（Inventory）的 SQLAlchemy ORM 模型及 InventoryDAL 数据访问层，
    实现 §1.7.3 五阶段状态流转的持久化存储与查询。

对应技术规格章节：
    - §1.7.3  inventory 表增加五阶段状态流转
              （原料 -> 在制 -> 待检 -> 成品 -> 出库）
    - R.2.6   库存五阶段流转规则（阶段顺序合法性、数量一致性）

替代 demo 文件 / 函数：
    - data_manager.py: DataManager.get_inventory() / get_inventory_of()
    - data_manager.py: DataManager.update_inventory()
    - data_manager.py: _INITIAL_DATA["inventory"] 中 raw/wip_cnc/wip_anode/wip_qc/finished 结构

迁移要点：
    - demo 中库存以 product_id 为键，单条记录聚合多个阶段数量（raw/wip_cnc/...）。
    - 迁移后改为每条 inventory 记录代表「某产品在某阶段、某批次、某库位」的一批物料，
      通过 status 字段标识其当前所处阶段，便于流转审计与五阶段校验。
    - 五阶段 status 枚举：raw_material / in_process / finished / pending_qc / shipped
    - 阶段流转由 InventoryRule（R.2.6）校验后调用 transfer_stage() 执行。
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from prog.models.base import Base


# ============================================================
# 五阶段状态常量（与 R.2.6 库存流转规则保持一致）
# ============================================================
# 阶段流转顺序（单向流转，逆向需特殊审批）：
#   raw_material  -> in_process  : 原料领料投入生产
#   in_process    -> pending_qc  : 在制品加工完成转入待检
#   pending_qc    -> finished    : 质检合格入成品库
#   finished      -> shipped     : 成品出库发货
# 阶段不合法流转示例（应被 InventoryRule 拦截）：
#   raw_material -> finished（跳过在制与质检）
#   shipped      -> raw_material（逆向回流）
INVENTORY_STATUS_RAW_MATERIAL = "raw_material"   # 原料
INVENTORY_STATUS_IN_PROCESS = "in_process"       # 在制
INVENTORY_STATUS_FINISHED = "finished"           # 成品
INVENTORY_STATUS_PENDING_QC = "pending_qc"       # 待检
INVENTORY_STATUS_SHIPPED = "shipped"             # 出库

# 阶段流转合法路径表（供 InventoryRule 校验）
INVENTORY_TRANSFER_PATH = {
    INVENTORY_STATUS_RAW_MATERIAL: INVENTORY_STATUS_IN_PROCESS,
    INVENTORY_STATUS_IN_PROCESS: INVENTORY_STATUS_PENDING_QC,
    INVENTORY_STATUS_PENDING_QC: INVENTORY_STATUS_FINISHED,
    INVENTORY_STATUS_FINISHED: INVENTORY_STATUS_SHIPPED,
}

# 五阶段列名与状态常量的映射（v6.08 采用列设计，每列代表一个阶段的数量）
INVENTORY_STAGE_COLUMNS = ["raw", "wip_cnc", "wip_anode", "wip_qc", "finished"]


class Inventory(Base):
    """库存 ORM 模型

    对应数据库表 inventory，每条记录代表「某产品在某阶段、某批次、某库位」的一批物料。

    字段说明：
        product_code   : 产品编码，主键（VARCHAR(20)，外键 -> products.product_code）
        raw            : 原料数量
        wip_cnc        : CNC 在制数量
        wip_anode      : 阳极在制数量
        wip_qc         : 待检数量
        finished       : 成品数量
        unit           : 计量单位（默认"套"）
        raw_value      : 原料价值
        wip_value      : 在制价值
        finished_value : 成品价值
        safety_stock   : 安全库存
        version        : 乐观锁版本号
        updated_at     : 最后更新时间

    替代 demo：
        替代 data_manager.py 中 inventory[product_id] 的多阶段聚合字典，
        v6.08 改为五阶段列设计（raw/wip_cnc/wip_anode/wip_qc/finished）以支持流转校验。
    """

    __tablename__ = "inventory"

    product_code: Mapped[str] = mapped_column(String(20), ForeignKey("products.product_code"), primary_key=True, comment="产品编码（主键，外键 -> products.product_code）")
    raw: Mapped[int] = mapped_column(Integer, default=0, comment="原料数量")
    wip_cnc: Mapped[int] = mapped_column(Integer, default=0, comment="CNC在制数量")
    wip_anode: Mapped[int] = mapped_column(Integer, default=0, comment="阳极在制数量")
    wip_qc: Mapped[int] = mapped_column(Integer, default=0, comment="待检数量")
    finished: Mapped[int] = mapped_column(Integer, default=0, comment="成品数量")
    unit: Mapped[Optional[str]] = mapped_column(String(10), default="套", comment="计量单位")
    raw_value: Mapped[Optional[float]] = mapped_column(Float, comment="原料价值")
    wip_value: Mapped[Optional[float]] = mapped_column(Float, comment="在制价值")
    finished_value: Mapped[Optional[float]] = mapped_column(Float, comment="成品价值")
    safety_stock: Mapped[Optional[int]] = mapped_column(Integer, comment="安全库存")
    version: Mapped[int] = mapped_column(Integer, default=0, comment="乐观锁版本号")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间")


class InventoryDAL:
    """库存数据访问层

    封装 inventory 表的 CRUD 与五阶段流转操作，替代 demo data_manager.py
    中针对库存字典的读取与更新方法。所有方法均基于 DatabaseManager
    提供的 query_one/query_many/insert/update/delete/execute/transaction 接口。

    替代 demo 方法对照：
        get_all()          -> DataManager.get_inventory()
        get_by_product()   -> DataManager.get_inventory_of()
        update_stock()     -> DataManager.update_inventory() 中阶段数量增减
        transfer_stage()   -> demo 中分散的阶段推进逻辑的统一入口
        check_shortage()   -> 缺料检查（成品库存是否满足需求）
    """

    def __init__(self, session=None):
        """初始化 DAL，设置表名

        Args:
            session: 兼容历史接口的 Session 参数（数据库操作通过 get_database() 获取实例）
        """
        self.session = session
        self.table_name = "inventory"

    def get_all(self, limit: int = None, offset: int = None) -> list:
        """获取全部库存记录

        Args:
            limit: 返回记录上限
            offset: 偏移量（分页）

        Returns:
            list[dict]: 库存字典列表
        """
        from prog.core.database import get_database
        db = get_database()
        return db.query_many(self.table_name, limit=limit, offset=offset, order_by="product_code")

    def get_by_product(self, product_code: str):
        """按产品获取库存

        由于 product_code 为 inventory 表主键，每产品仅一条记录。

        Args:
            product_code: 产品业务编码

        Returns:
            dict | None: 库存字典，无结果返回 None
        """
        from prog.core.database import get_database
        db = get_database()
        return db.query_one(self.table_name, {"product_code": product_code})

    def get_by_status(self, status: str) -> list:
        """按五阶段状态过滤库存

        v6.08 采用列设计（raw/wip_cnc/wip_anode/wip_qc/finished），
        本方法将状态常量映射到对应列名，返回该列数量 > 0 的记录。

        Args:
            status: raw_material / in_process / finished / pending_qc / shipped

        Returns:
            list[dict]: 命中记录
        """
        from prog.core.database import get_database
        db = get_database()
        # 状态常量到列名的映射
        status_col_map = {
            INVENTORY_STATUS_RAW_MATERIAL: "raw",
            INVENTORY_STATUS_IN_PROCESS: "wip_cnc",
            INVENTORY_STATUS_PENDING_QC: "wip_qc",
            INVENTORY_STATUS_FINISHED: "finished",
        }
        col = status_col_map.get(status)
        if not col:
            return []
        # 查询该阶段列数量大于 0 的库存记录
        sql = f"SELECT * FROM {self.table_name} WHERE {col} > 0 ORDER BY product_code"
        result = db.execute(sql)
        return [dict(r._mapping) for r in result.fetchall()]

    def create(self, inventory_data: dict):
        """新增库存记录（通常用于原料入库或成品入库）

        Args:
            inventory_data: 库存字段字典（含 product_code, raw, wip_cnc 等）

        Returns:
            str: 新建库存记录的 product_code
        """
        from prog.core.database import get_database
        db = get_database()
        return db.insert(self.table_name, inventory_data)

    def update_stock(self, product_code: str, stage: str, delta: int) -> bool:
        """更新某阶段库存数量（增量更新）

        对指定产品的指定阶段列执行 stock = stock + delta 操作。
        五阶段列：raw / wip_cnc / wip_anode / wip_qc / finished

        Args:
            product_code: 产品业务编码
            stage: 阶段列名（raw / wip_cnc / wip_anode / wip_qc / finished）
            delta: 增量（可为负）

        Returns:
            bool: 是否更新成功
        """
        from prog.core.database import get_database
        db = get_database()
        # 白名单校验，防止 SQL 注入
        if stage not in INVENTORY_STAGE_COLUMNS:
            return False
        sql = f"UPDATE {self.table_name} SET {stage} = {stage} + :delta WHERE product_code = :product_code"
        result = db.execute(sql, {"delta": delta, "product_code": product_code})
        return result.rowcount > 0

    def transfer_stage(self, product_code: str, from_stage: str, to_stage: str, qty: int) -> bool:
        """五阶段流转（R.2.6）

        将指定产品的库存从源阶段列流转到目标阶段列，数量为 qty。
        使用事务保证原子性：源阶段扣减与目标阶段增加同时成功或同时失败。

        流转合法性由调用方先经 InventoryRule.check_transfer() 校验，本方法假定已通过。

        Args:
            product_code: 产品业务编码
            from_stage: 源阶段列名（raw / wip_cnc / wip_anode / wip_qc / finished）
            to_stage: 目标阶段列名
            qty: 流转数量

        Returns:
            bool: 是否流转成功
        """
        from prog.core.database import get_database
        from sqlalchemy import text
        db = get_database()
        # 白名单校验，防止 SQL 注入
        if from_stage not in INVENTORY_STAGE_COLUMNS or to_stage not in INVENTORY_STAGE_COLUMNS:
            return False
        # 先检查源阶段库存是否充足，并读取乐观锁版本号
        inv = db.query_one(self.table_name, {"product_code": product_code})
        if not inv or inv.get(from_stage, 0) < qty:
            return False
        version = inv.get("version", 0) or 0
        # 使用事务保证原子性：源阶段扣减 + 目标阶段增加
        # P5 修复：乐观锁——源阶段 UPDATE 带 version 前置条件，并发修改时
        # rowcount 为 0 抛错回滚（D9 并发覆盖防护）
        try:
            with db.transaction() as session:
                from_res = session.execute(
                    text(f"UPDATE {self.table_name} SET {from_stage} = {from_stage} - :qty, "
                         f"version = version + 1 "
                         f"WHERE product_code = :pc AND version = :ver"),
                    {"qty": qty, "pc": product_code, "ver": version}
                )
                if from_res.rowcount == 0:
                    raise RuntimeError("乐观锁冲突：库存已被并发修改")
                session.execute(
                    text(f"UPDATE {self.table_name} SET {to_stage} = {to_stage} + :qty "
                         f"WHERE product_code = :pc"),
                    {"qty": qty, "pc": product_code}
                )
            return True
        except Exception:
            return False

    def check_shortage(self, product_code: str, required_qty: int) -> dict:
        """缺料检查

        检查成品库存是否满足需求量，返回缺料详情。

        Args:
            product_code: 产品业务编码
            required_qty: 需求数量

        Returns:
            dict: 缺料检查结果：
                {
                    "product_code": "A-202",
                    "required_qty": 100,
                    "available_qty": 80,
                    "shortage_qty": 20,
                    "is_shortage": True
                }
        """
        from prog.core.database import get_database
        db = get_database()
        inv = db.query_one(self.table_name, {"product_code": product_code})
        available = inv.get("finished", 0) if inv else 0
        shortage_qty = max(0, required_qty - available)
        return {
            "product_code": product_code,
            "required_qty": required_qty,
            "available_qty": available,
            "shortage_qty": shortage_qty,
            "is_shortage": shortage_qty > 0,
        }

    def update_status(self, inventory_id, new_status: str) -> bool:
        """直接更新某条库存的状态（不走流转校验，仅供内部/审批回写使用）

        v6.08 采用五阶段列设计（raw/wip_cnc/wip_anode/wip_qc/finished），
        不再使用单一 status 字段，业务侧应优先使用 transfer_stage() 或 update_stock()。

        Args:
            inventory_id: 库存记录主键（product_code）
            new_status: 新状态枚举值（未使用）

        Returns:
            bool: 始终返回 False（v6.08 列设计下此方法不适用）
        """
        return False
