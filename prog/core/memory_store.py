"""
Memory 持久化读写 API（业务侧封装）
====================================
对应规格 §1.1.3.6 Memory 读写 API（业务侧保留，依赖业务表）：
    - get_project_memory(memory_key)：读取 project_memory 表 memory_value
    - get_user_profile(user_id)：读取 user_profile 表完整记录

落库表（038 迁移）：
    - project_memory：项目级记忆（rule / config / knowledge / training），
      UNIQUE(memory_type, memory_key)，版本递增保留修改历史
    - user_profile：用户画像（角色/部门/偏好/常用客户/常用产品/L3 参数）

降级原则：DB 不可用时静默降级进程内存（仅 debug 状态），不阻断业务；
          恢复后可重写（DB 为准）。
"""
from typing import Any, Dict, List, Optional

# 进程内存降级存储（DB 不可用时的 debug 兜底）
_MEMORY_STORE: Dict[str, Dict[str, Any]] = {}
_PROFILE_STORE: Dict[str, Dict[str, Any]] = {}


def _get_db():
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def get_project_memory(memory_key: str,
                       memory_type: str = "knowledge") -> Optional[Any]:
    """读取项目级记忆值（规格 §1.1.3.6 get_project_memory）。

    DB 不可用/查无返回 None（debug 降级查进程内存）。
    """
    db = _get_db()
    if db is not None:
        try:
            row = db.query_one("project_memory", {
                "memory_type": memory_type,
                "memory_key": memory_key,
            })
            if row:
                return row.get("memory_value")
        except Exception:
            pass
    # debug 降级：进程内存
    return _MEMORY_STORE.get(f"{memory_type}:{memory_key}")


def set_project_memory(memory_key: str, memory_value: Any,
                       memory_type: str = "knowledge",
                       version: Optional[int] = None) -> bool:
    """写入/更新项目级记忆（upsert，version 自增保留修改历史）。

    返回是否写入成功（DB 不可用时仅写进程内存，返回 False 标记降级）。
    """
    db = _get_db()
    if db is not None:
        try:
            import json as _json
            existed = db.query_one("project_memory", {
                "memory_type": memory_type,
                "memory_key": memory_key,
            })
            value_json = _json.dumps(memory_value, ensure_ascii=False,
                                     default=str) if isinstance(
                memory_value, (dict, list)) else memory_value
            if existed:
                new_ver = version or (int(existed.get("version") or 0) + 1)
                db.update("project_memory", {
                    "memory_value": value_json,
                    "version": new_ver,
                }, {
                    "memory_type": memory_type,
                    "memory_key": memory_key,
                })
            else:
                db.insert("project_memory", {
                    "memory_type": memory_type,
                    "memory_key": memory_key,
                    "memory_value": value_json,
                    "version": version or 1,
                })
            return True
        except Exception:
            pass
    # debug 降级：进程内存
    _MEMORY_STORE[f"{memory_type}:{memory_key}"] = memory_value
    return False


def get_user_profile(user_id: str) -> Optional[Dict[str, Any]]:
    """读取用户画像完整记录（规格 §1.1.3.6 get_user_profile）。"""
    db = _get_db()
    if db is not None:
        try:
            row = db.query_one("user_profile", {"user_id": user_id})
            if row:
                return dict(row)
        except Exception:
            pass
    return _PROFILE_STORE.get(user_id)


def upsert_user_profile(user_id: str, role: str = "",
                        department: str = "",
                        preferences: Optional[Dict] = None,
                        frequent_customers: Optional[List[str]] = None,
                        frequent_products: Optional[List[str]] = None,
                        model_params: Optional[Dict] = None) -> bool:
    """写入/更新用户画像（upsert）。返回是否写入 DB（降级时 False）。"""
    db = _get_db()
    if db is not None:
        try:
            data = {
                "role": role or "viewer",
                "department": department or "",
                "preferences": preferences or {},
                "frequent_customers": frequent_customers or [],
                "frequent_products": frequent_products or [],
                "model_params": model_params or {},
            }
            existed = db.query_one("user_profile", {"user_id": user_id})
            if existed:
                db.update("user_profile", data, {"user_id": user_id})
            else:
                db.insert("user_profile", {"user_id": user_id, **data})
            return True
        except Exception:
            pass
    _PROFILE_STORE[user_id] = {
        "user_id": user_id, "role": role or "viewer",
        "department": department or "",
        "preferences": preferences or {},
        "frequent_customers": frequent_customers or [],
        "frequent_products": frequent_products or [],
        "model_params": model_params or {},
    }
    return False
