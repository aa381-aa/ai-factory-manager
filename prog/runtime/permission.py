"""
RBAC+ABAC 权限系统
==================
文件用途：
    定义 PermissionSystem 类，实现 RBAC（基于角色的访问控制）+
    ABAC（基于属性的访问控制）权限系统，作为七层审核链第 2 层
    permission_check 的实现。

设计说明：
    1. RBAC（角色维度）：
       定义 5 个角色，每个角色拥有固定的权限集（操作资源 + 动作）。
           - operator   操作员（生产/仓管，查库存、不可改单）
           - sales      销售员（折扣≤5%、不可审批、不可查成本）
           - manager    经理（折扣≤15%、可审批、可查成本）
           - finance    财务（收款、信用额度管理、可查成本）
           - admin      管理员（全权限，含 override 非硬规则）
    2. ABAC（属性维度）：
       在角色权限基础上叠加属性约束：
           - 部门属性：销售部不可操作生产排产，生产部不可改单
           - 金额范围：大额订单需更高级别审批
           - 订单状态：已发货订单不可修改明细
    3. RBAC 决定「能不能做」，ABAC 决定「在当前属性下能不能做」，
       两者均通过方可放行。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - RBAC+ABAC 双校验权限系统：RBAC 决定「能不能做」，ABAC 决定「在当前属性下能不能做」，两者均通过方可放行（来源：SPEC §3.5）
        - RBAC 5 角色基线（operator/sales/manager/finance/admin，admin 全权限通配）+ 折扣上限 + 审批能力矩阵（来源：SPEC §3.5）
        - ABAC 三约束：部门属性 / 金额范围（>10 万需 manager、>50 万需 admin）/ 订单锁定状态（来源：SPEC §3.5 / 业务规格书 v6.32 金额线配置化）
        - 关键阈值配置化：PERM_ABAC / PERM_AGENT_ACCESS（system_configs）、ROLE-PERMS（business_rules）可训练覆盖，DB 不可用降级内置（来源：业务规格书 v6.32 / v6.45 / CHANGELOG v21）
        - 工具级 RBAC 门禁配合：query_order/query_product 只读动作（v6.82）（来源：业务规格书 v6.82 / CHANGELOG v38）
    对外接口（方法/API）：
        - PermissionSystem.check_permission(user, action, resource=None)：校验顺序=①RBAC（含通配）②SOD 标记 ③admin 绕过 ABAC ④部门 ⑤金额 ⑥订单状态，返回 bool（来源：SPEC §3.5）
        - PermissionSystem.get_user_permissions(user)：返回 {role, permissions, discount_max, can_approve, can_view_cost, can_block_override: False, sod_violations}（来源：SPEC §3.5）
        - PermissionSystem.check_sod_compliance(user)：admin 通配展开后命中全部 SOD 规则（预期行为，用于审计标记）（来源：SPEC §3.5）
        - PermissionSystem.can_access_agent(user, agent_type)：按 AGENT_ROLE_ACCESS 校验 Agent 角色访问限制，未配置默认允许（来源：SPEC §3.5）
        - get_perm_abac() / get_agent_role_access() / get_role_discount_max() / get_role_can_approve() / get_role_permissions_override()：配置读取辅助函数（DB 优先 + 内置兜底）（来源：业务规格书 v6.32 / v6.45）
    错误处理要求：
        - 无数据库/roles 表不存在：使用内置默认权限矩阵，不阻断降级运行（来源：SPEC §3.5 / §2.3）
        - can_block_override 恒为 False：所有角色（含 admin）均不可绕过硬规则（bypass=false）（来源：SPEC §3.5）
        - SOD 违规仅记录标记不阻断已有操作（在审计中标记）（来源：SPEC §3.5）
"""

from typing import Optional, Dict, List, Any
import logging
from datetime import datetime

from prog.runtime.rule_registry import RuleResult

_logger = logging.getLogger(__name__)


# ============================================================
# 角色常量定义（RBAC）
# ============================================================
ROLE_OPERATOR = "operator"    # 操作员（生产/仓管）
ROLE_SALES = "sales"          # 销售员
ROLE_MANAGER = "manager"      # 经理（销售总监）
ROLE_FINANCE = "finance"      # 财务
ROLE_ADMIN = "admin"          # 管理员（总经理）


# Agent 访问控制映射（Agent 角色限制）
AGENT_ROLE_ACCESS = {
    "sales_agent": [ROLE_SALES, ROLE_MANAGER, ROLE_ADMIN],
    "finance_agent": [ROLE_FINANCE, ROLE_ADMIN],
    "production_agent": [ROLE_OPERATOR, ROLE_MANAGER, ROLE_ADMIN],
    "warehouse_agent": [ROLE_OPERATOR, ROLE_MANAGER, ROLE_ADMIN],
    "qc_agent": [ROLE_OPERATOR, ROLE_MANAGER, ROLE_ADMIN],
    "technical_agent": [ROLE_MANAGER, ROLE_ADMIN],
    "knowledge_assistant": [ROLE_OPERATOR, ROLE_SALES, ROLE_MANAGER, ROLE_FINANCE, ROLE_ADMIN],
}

# 开源版：已移除 _ACTION_SOD_PERM_MAP（SOD 职责分离映射）

_DEFAULT_ROLE_DISCOUNT_MAX: Dict[str, float] = {
    ROLE_OPERATOR: 0.0, ROLE_SALES: 0.05,
    ROLE_MANAGER: 0.15, ROLE_FINANCE: 0.0, ROLE_ADMIN: 1.0,
}

# 角色审批能力默认矩阵（v6.45：DB business_rules(ROLE-PERMS) 可训练覆盖）
_DEFAULT_ROLE_CAN_APPROVE: Dict[str, bool] = {
    ROLE_OPERATOR: False, ROLE_SALES: False,
    ROLE_MANAGER: True, ROLE_FINANCE: False, ROLE_ADMIN: True,
}

# 缓存区（get_perm_abac / get_role_discount_max 共用）
_AGENT_ACCESS_CACHE: Dict = {}
_PERM_ABAC_CACHE: Dict = {}

# 开源版：已移除 _TEMP_GRANT_PERM_MAP（跨部门临时授权映射）


def get_perm_abac() -> dict:
    """读取 ABAC 属性阈值（system_configs.PERM_ABAC JSON），DB 不可用降级内置。

    v6.32：大额/超大额审批金额线、订单锁定状态表配置化，调整无需改代码；
    配置键 PERM_ABAC 存 {"amount_high": 100000, "amount_vip": 500000,
    "order_locked_statuses": ["shipped","delivered","completed","closed"]}。
    """
    cached = _PERM_ABAC_CACHE.get("abac")
    if cached:
        return cached
    val = {
        "amount_high": 100000, "amount_vip": 500000,
        "order_locked_statuses": ["shipped", "delivered", "completed", "closed"],
    }
    try:
        import json
        from prog.runtime.database import get_database
        db = get_database()
        row = db.query_one("system_configs", {"config_key": "PERM_ABAC"})
        if row and row.get("config_value"):
            parsed = json.loads(row["config_value"])
            if isinstance(parsed, dict) and parsed:
                merged = dict(val)
                for k, v in parsed.items():
                    if isinstance(v, (int, float, list)):
                        merged[k] = v
                val = merged
    except Exception:
        pass
    _PERM_ABAC_CACHE["abac"] = val
    return val


def get_role_can_approve() -> dict:
    """角色审批能力矩阵（v6.45：DB business_rules(ROLE-PERMS) 可训练覆盖）。

    配置键 ROLE-PERMS.config_json 的 role_can_approve 字典：
    {"operator": False, "sales": False, "manager": True,
     "finance": False, "admin": True}
    DB 不可用/未配置时降级内置默认矩阵。
    """
    cached = _AGENT_ACCESS_CACHE.get("role_can_approve")
    if cached:
        return cached
    val = dict(_DEFAULT_ROLE_CAN_APPROVE)
    try:
        import json
        from prog.runtime.database import get_database
        db = get_database()
        row = db.query_one("business_rules", {"rule_id": "ROLE-PERMS"},
                           ["config_json"])
        if row and row.get("config_json"):
            cfg = row["config_json"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            rca = (cfg or {}).get("role_can_approve")
            if isinstance(rca, dict) and rca:
                merged = dict(val)
                for k, v in rca.items():
                    merged[k] = bool(v)
                val = merged
    except Exception:
        pass
    _AGENT_ACCESS_CACHE["role_can_approve"] = val
    return val


def get_role_permissions_override() -> dict:
    """角色 can_* 权限矩阵（v6.45：DB business_rules(ROLE-PERMS) 可训练覆盖）。

    配置键 ROLE-PERMS.config_json 的 role_permissions 字典：
    {"sales": {"can_approve": True, "can_view_cost": False}, ...}
    供业务侧 auth._role_permissions 覆盖内置 _ROLE_PERMISSIONS。
    DB 不可用/未配置时返回空字典（保持内置默认）。
    """
    cached = _AGENT_ACCESS_CACHE.get("role_permissions_override")
    if cached:
        return cached
    val: Dict[str, dict] = {}
    try:
        import json
        from prog.runtime.database import get_database
        db = get_database()
        row = db.query_one("business_rules", {"rule_id": "ROLE-PERMS"},
                           ["config_json"])
        if row and row.get("config_json"):
            cfg = row["config_json"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            rp = (cfg or {}).get("role_permissions")
            if isinstance(rp, dict) and rp:
                val = rp
    except Exception:
        pass
    _AGENT_ACCESS_CACHE["role_permissions_override"] = val
    return val


def get_role_discount_max() -> dict:
    """角色折扣让利上限（与 discount_rule 同源 DISCOUNT-RULE，训练即时生效）。

    v6.32：permission.py 与 discount_rule.py 共用 business_rules.DISCOUNT-RULE
    的 role_discount_max，消除双轨不一致；DB 不可用降级内置默认。
    """
    cached = _AGENT_ACCESS_CACHE.get("role_discount_max")
    if cached:
        return cached
    val = dict(_DEFAULT_ROLE_DISCOUNT_MAX)
    try:
        import json
        from prog.runtime.database import get_database
        db = get_database()
        row = db.query_one("business_rules", {"rule_id": "DISCOUNT-RULE"}, ["config_json"])
        if row and row.get("config_json"):
            cfg = row["config_json"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            rdm = (cfg or {}).get("role_discount_max")
            if isinstance(rdm, dict) and rdm:
                merged = dict(val)
                for k, v in rdm.items():
                    try:
                        merged[k] = float(v)
                    except (TypeError, ValueError):
                        pass
                val = merged
    except Exception:
        pass
    _AGENT_ACCESS_CACHE["role_discount_max"] = val
    return val


def get_agent_role_access() -> dict:
    """读取 Agent 角色访问矩阵（system_configs.PERM_AGENT_ACCESS JSON），DB 不可用降级内置。

    v6.32：新增 Agent / 调整角色白名单无需改代码，配置键 PERM_AGENT_ACCESS
    存 {"sales_agent": ["sales","manager","admin"], ...}。
    """
    cached = _AGENT_ACCESS_CACHE.get("matrix")
    if cached:
        return cached
    val = dict(AGENT_ROLE_ACCESS)
    try:
        import json
        from prog.runtime.database import get_database
        db = get_database()
        row = db.query_one("system_configs", {"config_key": "PERM_AGENT_ACCESS"})
        if row and row.get("config_value"):
            parsed = json.loads(row["config_value"])
            if isinstance(parsed, dict) and parsed:
                val = {str(k): list(v) for k, v in parsed.items()
                       if isinstance(v, list)}
    except Exception:
        pass
    _AGENT_ACCESS_CACHE["matrix"] = val
    return val



class PermissionSystem:
    """RBAC + ABAC 权限系统

    提供基于角色与属性的双重权限校验。

    属性说明：
        role_permissions : Dict[role, Dict[action, bool]]
                          每个角色对各操作的布尔权限（RBAC 基线）
        role_discount_max: Dict[role, float]
                          每个角色的折扣权限上限
        role_can_approve : Dict[role, bool]
                          每个角色是否可审批
    """

    def __init__(self):
        """初始化权限系统，加载角色权限定义

        内部初始化 role_permissions / role_discount_max / role_can_approve 等映射表。
        权限定义优先从数据库 roles 表加载（叠加默认基线），无数据库时使用内置默认值。
        """
        # RBAC 基线：每个角色对各 action 的布尔权限
        # v6.82：新增 query_order / query_product 只读查询动作（MCP 工具层
        # RBAC 门禁用，业务数据查询工具不得绕过查询权限，见 agent_tools）
        self.role_permissions: Dict[str, Dict[str, bool]] = {
            ROLE_OPERATOR: {
                "query_inventory": True, "query_schedule": True,
                "query_order": True, "query_product": True,
                "transfer_inventory": True, "qc_inspection": True,
                "create_schedule": True,
                "modify_order": False, "approve_order": False,
                "view_cost": False, "manage_credit": False,
                "confirm_payment": False,
            },
            ROLE_SALES: {
                "query_inventory": True, "modify_order": True,
                "create_order": True,
                "query_order": True, "query_product": True,
                "approve_order": False, "view_cost": False,
                "manage_credit": False, "confirm_payment": False,
                "create_schedule": False, "transfer_inventory": False,
                "qc_inspection": False, "query_schedule": False,
            },
            ROLE_MANAGER: {
                "query_inventory": True, "modify_order": True,
                "create_order": True, "approve_order": True,
                "view_cost": True, "create_schedule": True,
                "transfer_inventory": True, "qc_inspection": True,
                "query_schedule": True,
                "query_order": True, "query_product": True,
                "manage_credit": False, "confirm_payment": False,
            },
            ROLE_FINANCE: {
                "view_cost": True, "manage_credit": True,
                "confirm_payment": True, "query_inventory": True,
                "query_order": True, "query_product": True,
                "modify_order": False, "approve_order": False,
                "create_schedule": False, "transfer_inventory": False,
                "qc_inspection": False, "query_schedule": False,
            },
            ROLE_ADMIN: {"*": True},  # 全权限
        }
        # 折扣权限上限（v6.32 与 discount_rule 同源 DISCOUNT-RULE，训练即时生效）
        self.role_discount_max: Dict[str, float] = dict(get_role_discount_max())
        # 是否可审批（v6.45：DB business_rules(ROLE-PERMS) 可训练覆盖）
        self.role_can_approve: Dict[str, bool] = dict(get_role_can_approve())
        # 尝试从数据库加载角色定义（叠加到基线，无数据库时忽略）
        self._load_roles_from_db()
        # 开源版：已移除 SODChecker（职责分离校验器），仅保留 RBAC

    def _load_roles_from_db(self):
        """从 roles 表加载角色权限定义，叠加到内存基线（可选依赖）

        解析顺序：用户级覆盖(ABAC) -> 角色权限(RBAC含继承) -> 默认值。
        此处仅加载角色级 RBAC 基线，用户级 ABAC 在 check_permission 中实时校验。
        """
        try:
            from prog.runtime.database import get_database  # 可选：外部数据库层
            db = get_database()
            roles = db.query_many("roles")
            for r in roles:
                role_id = r.get("role_id")
                if not role_id:
                    continue
                perms = r.get("permissions") or {}
                if isinstance(perms, str):
                    import json
                    perms = json.loads(perms)
                # roles 表 permissions 使用部门域键（sales/production/warehouse/finance/all）
                # 映射为 action 级布尔：all=True 视为通配
                if perms.get("all"):
                    self.role_permissions[role_id] = {"*": True}
                    self.role_discount_max[role_id] = 1.0
                    self.role_can_approve[role_id] = True
                else:
                    # 保留已有 action 级基线，不覆盖（DB 部门域键仅供 ABAC 部门校验使用）
                    if role_id not in self.role_permissions:
                        self.role_permissions[role_id] = {}
        except Exception:
            # 无数据库或 roles 表不存在时，使用内置默认值
            pass

    def check_permission(self, user, action: str, resource: dict = None) -> bool:
        """权限校验主入口（开源版：仅 RBAC 角色校验）

        Args:
            user    : 用户对象（含 role）
            action  : 操作动作（如 "modify_order", "approve_order"）
            resource: 资源与参数（开源版忽略，仅 RBAC）

        Returns:
            bool: 是否允许
        """
        role = self._get_user_role(user)

        # RBAC 角色权限校验
        if not self._check_rbac(role, action):
            return False

        # admin 角色全权限
        if role == ROLE_ADMIN:
            return True

        return True

    def get_user_permissions(self, user) -> dict:
        """获取用户的完整权限集（开源版：仅 RBAC，无 SOD）

        Args:
            user: 用户对象

        Returns:
            dict: 权限字典（含 discount_max, can_approve, 各 action 布尔）
        """
        role = self._get_user_role(user)
        perms = dict(self.role_permissions.get(role, {}))
        is_admin = perms.get("*", False)
        return {
            "role": role,
            "permissions": perms,
            "discount_max": self.role_discount_max.get(role, 0.0),
            "can_approve": self.role_can_approve.get(role, False),
            "can_view_cost": is_admin or perms.get("view_cost", False),
            "can_block_override": False,
        }

    def can_access_agent(self, user, agent_type: str) -> bool:
        """校验用户能否访问指定 Agent

        不同 Agent 对角色有访问限制（如财务 Agent 仅 finance/admin 可用）。

        Args:
            user: 用户对象
            agent_type: Agent 类型（如 "finance_agent"）

        Returns:
            bool: 是否可访问
        """
        role = self._get_user_role(user)
        allowed = get_agent_role_access().get(agent_type)
        if allowed is None:
            # 未配置访问限制的 Agent 默认允许
            return True
        return role in allowed

    # ============================================================
    # 内部辅助方法
    # ============================================================
    def _check_rbac(self, role: str, action: str) -> bool:
        """RBAC 角色权限校验（含通配符）"""
        perms = self.role_permissions.get(role, {})
        if perms.get("*"):
            return True
        return bool(perms.get(action, False))

    def _get_user_role(self, user) -> Optional[str]:
        """从用户对象提取角色标识"""
        if user is None:
            return None
        if isinstance(user, dict):
            return user.get("role") or user.get("role_id")
        return getattr(user, "role", None) or getattr(user, "role_id", None)

    def _get_user_attr(self, user, key: str, default=None):
        """从用户对象提取属性值"""
        if user is None:
            return default
        if isinstance(user, dict):
            return user.get(key, default)
        return getattr(user, key, default)

    # ============================================================
    # 用户标识提取
    # ============================================================
    def _get_user_id(self, user) -> Optional[str]:
        """提取用户标识（dict/对象兼容）。"""
        if user is None:
            return None
        if isinstance(user, dict):
            return user.get("user_id") or user.get("id")
        return getattr(user, "user_id", None) or getattr(user, "id", None)
