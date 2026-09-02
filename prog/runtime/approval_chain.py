"""
统一审批链读取工具（审批链可训练）
==================================
文件用途：
    统一各训练变更入口的审批链读取，消除审批链硬编码（manager 单级）。
    审批链定义存 workflow_configs 表定义行，可经训练端点（B1）修改——
    符合"审批可训练"核心要求。

设计说明：
    1. workflow_configs 表双用途：
       - 定义行（每个 workflow_type 一条）：approval_chain / starter_roles 等
       - 审批实例行（训练变更提交）：thresholds 携带 proposed / current_step
       本模块以"thresholds 为空"区分定义行与实例行，避免把审批实例误当定义。
    2. get_approval_chain(wf_type)：DB 定义行优先；无定义行 / DB 不可用时
       兜底 manager 单级（与历史硬编码行为等价，供降级模式使用）。
    3. update_approval_chain(wf_type, new_chain)：B1 审批链训练审批通过后
       应用新链到定义行；无定义行时新建。
    4. 框架内嵌模块：原 agent-runtime-os 独立副本已取消，仅保留本仓库副本。

对应技术规格：
    - §2.5.5 workflow_configs（approval_chain 层可训练优化）

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 统一审批链读取工具：消除审批链硬编码（manager 单级），审批链定义存 workflow_configs 定义行、可训练（来源：业务规格书 v6.45 / SPEC §2.5.5）
        - 以"thresholds 为空"区分定义行与审批实例行，避免把审批实例误当定义（来源：业务规格书 v6.45 / CHANGELOG v21）
        - 审批链训练变更：update_approval_chain 在 B1 审批通过后应用新链，无定义行自动新建（来源：业务规格书 v6.45）
        - 框架内嵌模块：原 agent-runtime-os 独立副本已取消，仅保留本仓库副本（来源：模块拆分方案 M0）
    对外接口（方法/API）：
        - get_approval_chain(workflow_type='rule_config_change', db=None)：DB 定义行审批链优先（可训练），无定义/DB 不可用兜底 manager 单级（来源：业务规格书 v6.45 / 模块拆分方案 契约5）
        - find_workflow_definition(workflow_type, db=None)：查找定义行（thresholds 为空/缺失），返回 dict 或 None（来源：业务规格书 v6.45）
        - update_approval_chain(workflow_type, new_chain, db=None, modified_by='')：应用审批链变更到定义行；无定义行时新建，返回 bool（来源：业务规格书 v6.45）
    错误处理要求：
        - DB 不可用/无定义行：get_approval_chain 返回 manager 单级兜底链，不阻断降级运行（来源：SPEC §3.10 / 业务规格书 v6.45）
        - 审批链更新失败（DB 不可用）：返回 False（来源：业务规格书 v6.45）
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

_DEFAULT_CHAIN: List[Dict[str, Any]] = [
    {"step": 1, "role": "manager", "action": "审批"}]


def _get_db() -> Any:
    """延迟获取数据库实例（双副本 import 兼容）。"""
    from prog.runtime.database import get_database
    try:
        return get_database()
    except Exception:
        return None


def _parse_chain(value) -> Optional[List[dict]]:
    """解析审批链（str JSON / list）。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, list) and value:
        return value
    return None


def find_workflow_definition(workflow_type: str, db: Any = None) -> Optional[dict]:
    """查找 workflow_type 的定义行（非审批实例行）。

    定义行特征：thresholds 为空/缺失（审批实例行才携带 thresholds）。
    实例行（rule_config_change / slot_defs_change / CHAIN-* 等）含 thresholds，
    不会被误判为定义行。

    Args:
        workflow_type: 流程类型
        db: 可选数据库（测试注入）

    Returns:
        dict 或 None（无定义行 / DB 不可用）
    """
    if db is None:
        db = _get_db()
    if db is None:
        return None
    try:
        rows = db.query_many(
            "workflow_configs", {"workflow_type": workflow_type},
            order_by="config_id ASC") or []
        for row in rows:
            # S9：跳过已停用（is_active=False）的定义行——旧版本定义配置残留
            # 不再被误选（DDL 默认 is_active=1；字段未显式设置视为启用，兼容旧数据）
            if row.get("is_active") is False:
                continue
            th = row.get("thresholds")
            # I9：'null' JSON 串也视为空（定义行特征，与 None/空串/空对象等价）
            if th in (None, "", "{}", {}, "null"):
                return row
    except Exception:
        return None
    return None


def get_approval_chain(workflow_type: str = "rule_config_change",
                       db: Any = None) -> List[Dict[str, Any]]:
    """读取流程类型的审批链（DB 定义优先，可训练）。

    Args:
        workflow_type: 流程类型（如 rule_config_change / slot_defs_change）
        db: 可选数据库（测试注入）

    Returns:
        list: 审批链步骤 [{step, role, action}, ...]；
              DB 无定义 / 不可用时兜底 manager 单级
    """
    row = find_workflow_definition(workflow_type, db=db)
    if row:
        chain = _parse_chain(row.get("approval_chain"))
        if chain:
            return chain
    return list(_DEFAULT_CHAIN)


def update_approval_chain(workflow_type: str, new_chain: List[dict],
                          db: Any = None, modified_by: str = "") -> bool:
    """应用审批链变更（B1 审批链训练审批通过后调用）。

    更新 workflow_type 定义行的 approval_chain；无定义行时新建
    （与 migrations 定义行结构对齐）。

    Args:
        workflow_type: 目标流程类型
        new_chain: 新审批链步骤列表
        db: 可选数据库
        modified_by: 修改人

    Returns:
        bool: 是否更新成功
    """
    if db is None:
        db = _get_db()
    if db is None:
        return False
    chain_str = json.dumps(new_chain, ensure_ascii=False)
    try:
        row = find_workflow_definition(workflow_type, db=db)
        if row:
            db.update(
                "workflow_configs",
                {"approval_chain": chain_str,
                 "updated_by": modified_by,
                 "updated_at": datetime.now().isoformat()},
                {"config_id": row.get("config_id")})
            return True
        db.insert("workflow_configs", {
            "workflow_type": workflow_type,
            "workflow_name": f"{workflow_type}审批",
            "owner_dept": "system",
            "approval_chain": chain_str,
            "is_active": True,
            "is_trained": True,
            "updated_by": modified_by,
        })
        return True
    except Exception:
        return False
