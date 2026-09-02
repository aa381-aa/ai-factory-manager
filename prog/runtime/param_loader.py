"""
统一业务规则参数读取辅助
=========================
文件用途：
    为各 Agent / 规则引擎提供从 business_rules 表读取可训练参数的统一入口，
    消除重复的 try/except 读取代码。参数经 L2 训练+审批修改后即时生效。

设计说明：
    - 单参数读取 get_param(rule_id, key, default)：读取失败/无 DB/键缺失
      时返回默认值（与现有各规则降级行为一致）。
    - 整条配置读取 get_param_dict(rule_id, default)：返回 config_json 字典。
    - 不引入额外缓存：business_rules 表规模小，直接查询开销可忽略；
      数据库连接熔断由 DatabaseManager 统一保证快速失败。
    - v6.46 runtime 分离：本文件为 prog/runtime 组成部分（原 agent-runtime-os
      独立副本已取消，仅保留本仓库副本）。

对应技术规格：
    - §2.6 规则引擎（parameter 层参数均存 business_rules 表，可训练修改）

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 从 business_rules 表读取可训练参数的统一入口（get_param/get_param_dict），消除各 Agent/规则引擎重复的 try/except 读取代码（业务规格书 v6.30，§2.6 规则引擎 parameter 层参数可训练修改）
        - 参数经 L2 训练+审批修改后即时生效（业务规格书 v6.30）
        - 不引入额外缓存：business_rules 表规模小直接查询；数据库连接熔断由 DatabaseManager 统一保证快速失败（业务规格书 v6.30 设计说明）
    对外接口（方法/API）：
        - get_param(rule_id, key, default, db=None) -> Any：单参数读取，无 DB/读取失败/键缺失返回默认值（业务规格书 v6.30）
        - get_param_dict(rule_id, default=None, db=None) -> dict：整条规则配置读取（config_json 字典，支持 JSON 字符串，失败返回默认字典）（业务规格书 v6.30）
    错误处理要求：
        - 读取失败/无 DB/键缺失：返回默认值（与既有各规则降级行为一致）（业务规格书 v6.30）
"""

import json
from typing import Any, Dict


def _load_config(rule_id: str, db: Any = None) -> Dict[str, Any]:
    """读取规则配置字典（失败返回空字典）。"""
    try:
        if db is None:
            from prog.runtime.database import get_database
            db = get_database()
        row = db.query_one("business_rules", {"rule_id": rule_id},
                           ["config_json"])
        if row and row.get("config_json"):
            cfg = row["config_json"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def get_param(rule_id: str, key: str, default: Any, db: Any = None) -> Any:
    """读取单参数（无 DB/失败/键缺失返回默认值）。

    Args:
        rule_id: business_rules 规则ID（如 SCHED-HARD）
        key: 参数键（如 shift_hours）
        default: 默认值（代码内原有硬编码值，DB 不可用时降级）
        db: 可选数据库访问层（默认 get_database()）

    Returns:
        参数值或默认值
    """
    cfg = _load_config(rule_id, db)
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return default


def get_param_dict(rule_id: str, default: Dict[str, Any] = None,
                   db: Any = None) -> Dict[str, Any]:
    """读取整条规则配置（无 DB/失败返回默认字典）。"""
    cfg = _load_config(rule_id, db)
    return cfg if cfg else (default or {})
