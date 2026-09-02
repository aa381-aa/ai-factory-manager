"""
运行时流程约束引擎
==================
文件用途：
    定义 WorkflowEnforcer 类，实现运行时流程约束引擎：
    流程实例化执行与链式Gate校验。

功能：
    1. 匹配 workflow_type
    2. 发起者三道校验：
       a. 发起者角色 ∈ workflow_configs.starter_roles
       b. 发起者部门 ∈ workflow_configs.starter_depts
       c. 启动方式满足 workflow_configs.initiation
    3. 创建 workflow_instances 记录
    4. 链式Gate校验（委托 ChainGateChecker）

开源化说明：
    - 数据库依赖通过构造参数 database 注入（可选）：
      数据库不可用时降级为内存模式（使用类变量 _instances dict 存储）。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 运行时流程约束引擎：流程实例化执行 + 链式 Gate 校验（来源：SPEC §3.10）
        - start_workflow 发起者三道校验（starter_roles / starter_depts / initiation）+ 创建 workflow_instances 实例（来源：SPEC §3.10 / 业务规格书 v6.15）
        - advance_step 单步推进审批：按 approval_chain 链长判定全部通过（completed 标志），done_entry 记录审批人/时间（来源：业务规格书 v6.56 / v6.60）
        - 审批留言 add_comment/list_comments：申请人↔审批人同上下文往返，落 workflow_comments 表，DB 不可用降级实例 extra_data.comments（来源：业务规格书 v6.79 / 框架 v1.6.57）
        - 流程定义训练（模块级函数）：submit_workflow_def_change / advance_training_approval / apply_workflow_def_change，变更走 L2 审批链（来源：业务规格书 v6.48 / v6.61 / 模块拆分方案 契约5）
        - 查询流程执行器（模块级函数）：execute_workflow_query / submit_query_flow，query_steps 多步骤查库 + required_permission 门禁 + 表白名单防注入（来源：业务规格书 v6.54 / v6.64）
        - 数据库不可用时降级为类变量内存存储 _instances / _instance_seq（_create_instance / _get_instance / _update_instance 统一封装）（来源：SPEC §3.10.2）
    对外接口（方法/API）：
        - WorkflowEnforcer.start_workflow(workflow_type, biz_type, biz_id, user, initiation='manual')：三道校验 + 创建实例，返回 {"success", "instance_id", "error"}（来源：SPEC §3.10 / 模块拆分方案 契约5）
        - WorkflowEnforcer.advance_step(instance_id, user)：Gate 校验通过后 current_step+1 并追加 steps_done，返回 {"success", "current_step", "completed", "error", "missing"}（来源：SPEC §3.10）
        - WorkflowEnforcer.add_comment(instance_id, step, author, content)：记录审批留言，返回 {"success", "comment_id", "error"}（来源：业务规格书 v6.79）
        - WorkflowEnforcer.list_comments(instance_id)：查询实例审批留言（时间正序），返回留言列表（来源：业务规格书 v6.79）
        - submit_workflow_def_change(proposed, current=None, db=None, changed_by='L2')：提交流程定义变更写 workflow_configs 审批记录，返回 config_id 或 None（来源：业务规格书 v6.48 / 模块拆分方案 契约5）
        - advance_training_approval(config_id, user=None, db=None)：逐级推进流程定义训练审批（角色校验 + admin 代批豁免 + 签字记录），全部通过后 apply_workflow_def_change 生效（来源：业务规格书 v6.61）
        - apply_workflow_def_change(new_def, db=None, modified_by='L2')：审批通过后写入定义行生效（is_trained=TRUE / version+1 / updated_by 空值兜底 admin）（来源：业务规格书 v6.48 / v6.50）
        - execute_workflow_query(workflow_type, params=None, user=None, db=None)：执行查询流程定义的查库项目，返回 {"success", "result", "steps", "error", "permission"}（来源：业务规格书 v6.54）
        - submit_query_flow(proposed, user=None, db=None)：查询流程免审批自建（只读无副作用，校验 query_steps/类型/表白名单/required_permission），留痕 operation_logs（来源：业务规格书 v6.54）
    错误处理要求：
        - 流程类型不存在或未生效：返回 {"success": False, "error": "流程类型 xxx 不存在或未生效"}，不创建实例（来源：SPEC §3.10）
        - 发起者三道校验失败：返回 {"success": False, "error": 具体拒绝原因}（来源：SPEC §3.10 / 业务规格书 v6.48：非 admin 阻断回复拒绝原因）
        - 审批留言内容为空：返回 error "留言内容不能为空"；DB 落库失败返回 error（来源：业务规格书 v6.79）
        - 查询流程每步 required_permission 未声明或权限缺失：一律拒绝不执行查库（来源：业务规格书 v6.54）
        - 查询表不在 _QUERY_STEP_TABLES 白名单内：拒绝（防配置注入）（来源：业务规格书 v6.54 / v6.64）
        - 训练审批角色不匹配：返回 error "当前步骤需「X」审批"（admin 代批豁免）（来源：业务规格书 v6.45 / v6.61）
"""

import json
import threading
from datetime import datetime
from typing import Any, List, Optional, Tuple

# 开源版：已移除 chain_gate（链式 Gate 校验）依赖，基础审批不依赖 Gate 校验

# 内存模式实例 ID 前缀（B.3 P0 ID 空间隔离）：
# DB 模式实例 ID 为 workflow_instances.instance_id SERIAL 纯数字；
# 内存模式实例 ID 为 "M{seq}" 字符串（如 M1/M2），二者永不碰撞。
MEMORY_INSTANCE_PREFIX = "M"


def coerce_instance_id(instance_id):
    """兼容内存模式字符串 ID（'M{seq}'）与 DB 模式整数 ID。

    DB 模式实例 ID 为纯数字（SERIAL）；内存模式为 'M{seq}' 字符串。
    本函数对字符串直通、对数字安全转 int，供调用方替代裸 int() 转换
    （int("M1") 会抛 ValueError）。
    """
    if isinstance(instance_id, str):
        return instance_id
    try:
        return int(instance_id)
    except (TypeError, ValueError):
        return instance_id


class WorkflowEnforcer:
    """运行时流程约束引擎

    流程实例化执行：
    1. 匹配 workflow_type
    2. 发起者三道校验：
       a. 发起者角色 ∈ workflow_configs.starter_roles
       b. 发起者部门 ∈ workflow_configs.starter_depts
       c. 启动方式满足 workflow_configs.initiation
    3. 创建 workflow_instances 记录
    4. 链式Gate校验（委托 ChainGateChecker）
    """

    # 内存模式实例存储（数据库不可用时降级使用）
    _instances: dict = {}
    _instance_seq: int = 0
    # W13：并发保护——内存实例序号原子自增 / advance_step read-modify-write 串行化
    _seq_lock = threading.Lock()
    _advance_lock = threading.Lock()  # 兼容保留（W14 改为按实例粒度锁）
    # W14：按实例粒度锁——不同流程实例的审批互不阻塞（原类级锁全局互斥，
    # 高并发下吞吐受限；同实例的 read-modify-write 仍串行化保证正确性）
    _instance_locks: dict = {}
    _instance_locks_guard = threading.Lock()

    @classmethod
    def _get_instance_lock(cls, instance_id) -> threading.Lock:
        """获取实例粒度的推进锁（W14：按 instance_id 缓存锁对象）。"""
        with cls._instance_locks_guard:
            lock = cls._instance_locks.get(instance_id)
            if lock is None:
                lock = threading.Lock()
                cls._instance_locks[instance_id] = lock
            return lock

    # v6.36：DB降级种子数据（镜像 migrations/007 + 009 的11条 workflow_configs 种子）
    # DB不可用时 _get_workflow_config 从此常量取值，确保流程引擎在降级模式下仍可实例化流程。
    # 字段与 workflow_configs 表对齐：workflow_type/workflow_name/owner_dept/
    # approval_chain/notify_rules/thresholds/gate_checks/is_active/is_trained
    _DEFAULT_WORKFLOW_CONFIGS: dict = {
        "cost_markup_change": {
            "workflow_type": "cost_markup_change",
            "workflow_name": "成本线加价率变更审批",
            "owner_dept": "finance",
            "trigger_rule": "RULE-005",
            "approval_chain": [
                {"step": 1, "role": "sales_manager", "action": "发起"},
                {"step": 2, "role": "finance_manager", "action": "财务确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "version_sm_change": {
            "workflow_type": "version_sm_change",
            "workflow_name": "版本状态机变更审批",
            "owner_dept": "technical",
            "trigger_rule": "VERSION-SM",
            "approval_chain": [
                {"step": 1, "role": "technical_manager", "action": "发起"},
                {"step": 2, "role": "quality_manager", "action": "质量确认"},
                {"step": 3, "role": "production_manager", "action": "生产确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "sched_constraint_change": {
            "workflow_type": "sched_constraint_change",
            "workflow_name": "排产约束变更审批",
            "owner_dept": "production",
            "trigger_rule": "SCHED-HARD",
            "approval_chain": [
                {"step": 1, "role": "production_manager", "action": "发起"},
                {"step": 2, "role": "technical_manager", "action": "工艺确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "inv_stage_change": {
            "workflow_type": "inv_stage_change",
            "workflow_name": "库存阶段定义变更审批",
            "owner_dept": "production",
            "trigger_rule": "INV-STAGE",
            "approval_chain": [
                {"step": 1, "role": "warehouse_manager", "action": "发起"},
                {"step": 2, "role": "production_manager", "action": "工艺确认"},
                {"step": 3, "role": "finance_manager", "action": "财务确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "bom_check_change": {
            "workflow_type": "bom_check_change",
            "workflow_name": "BOM校验项变更审批",
            "owner_dept": "technical",
            "trigger_rule": "BOM-CHECK",
            "approval_chain": [
                {"step": 1, "role": "technical_manager", "action": "发起"},
                {"step": 2, "role": "production_manager", "action": "生产确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "drawing_field_change": {
            "workflow_type": "drawing_field_change",
            "workflow_name": "图纸必填字段变更审批",
            "owner_dept": "technical",
            "trigger_rule": "DRAWING-FIELD",
            "approval_chain": [
                {"step": 1, "role": "technical_manager", "action": "发起"},
                {"step": 2, "role": "quality_manager", "action": "质量确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        "customer_change": {
            "workflow_type": "customer_change",
            "workflow_name": "客户信用额度上调审批",
            "owner_dept": "sales",
            "approval_chain": [
                {"step": 1, "role": "sales_director", "condition": "always"},
                {"step": 2, "role": "finance", "condition": "amount>500000"},
            ],
            "notify_rules": [
                {"event": "approved", "target": "finance", "channel": "system"},
                {"event": "approved", "target": "requester", "channel": "websocket"},
            ],
            "thresholds": {"credit_limit_threshold": 500000, "approval_timeout_hours": 24},
            "gate_checks": {
                "required_approvals": {
                    "1": {"role": "sales_director", "required": True},
                    "2": {"role": "finance", "required": "amount>500000"},
                },
                "required_fields": {"1": ["credit_limit", "reason"]},
                "required_statuses": [],
                "timeout_hours": 24,
            },
            "is_active": True,
            "is_trained": False,
        },
        "product_change": {
            "workflow_type": "product_change",
            "workflow_name": "新产品建档审批",
            "owner_dept": "tech",
            "approval_chain": [
                {"step": 1, "role": "tech_supervisor", "condition": "always"},
                {"step": 2, "role": "production", "condition": "feasibility_check"},
            ],
            "notify_rules": [
                {"event": "effective", "target": "production", "channel": "event_bus"},
                {"event": "effective", "target": "warehouse", "channel": "event_bus"},
                {"event": "effective", "target": "sales", "channel": "event_bus"},
            ],
            "thresholds": {"approval_timeout_hours": 48},
            "gate_checks": {
                "required_approvals": {
                    "1": {"role": "tech_supervisor", "required": True},
                    "2": {"role": "production", "required": True},
                },
                "required_fields": {"1": ["product_code", "product_name", "bom_id"]},
                "required_statuses": [{"entity_type": "drawing", "expected_status": "effective"}],
                "timeout_hours": 48,
            },
            "is_active": True,
            "is_trained": False,
        },
        "drawing_change": {
            "workflow_type": "drawing_change",
            "workflow_name": "图纸版本变更审批",
            "owner_dept": "tech",
            "approval_chain": [
                {"step": 1, "role": "tech_supervisor", "condition": "always"},
                {"step": 2, "role": "production", "condition": "wip_impact_check"},
                {"step": 3, "role": "warehouse", "condition": "bom_diff_check"},
            ],
            "notify_rules": [
                {"event": "effective", "target": "production", "channel": "event_bus"},
                {"event": "effective", "target": "sales", "channel": "event_bus"},
                {"event": "effective", "target": "warehouse", "channel": "event_bus"},
            ],
            "thresholds": {"approval_timeout_hours": 48},
            "gate_checks": {
                "required_approvals": {
                    "1": {"role": "tech_supervisor", "required": True},
                    "2": {"role": "production", "required": True},
                    "3": {"role": "warehouse", "required": True},
                },
                "required_fields": {"1": ["change_description", "new_version_pdf"]},
                "required_statuses": [{"entity_type": "drawing", "expected_status": "effective"}],
                "timeout_hours": 48,
            },
            "is_active": True,
            "is_trained": False,
        },
        "production_schedule": {
            "workflow_type": "production_schedule",
            "workflow_name": "排产方案审批",
            "owner_dept": "production",
            "approval_chain": [
                {"step": 1, "role": "tech", "condition": "feasibility_check"},
                {"step": 2, "role": "warehouse", "condition": "material_check"},
                {"step": 3, "role": "qc", "condition": "qc_standard_check"},
                {"step": 4, "role": "production_manager", "condition": "always"},
            ],
            "notify_rules": [
                {"event": "scheduled", "target": "sales", "channel": "websocket"},
                {"event": "scheduled", "target": "warehouse", "channel": "event_bus"},
            ],
            "thresholds": {"approval_timeout_hours": 12},
            "gate_checks": {
                "required_approvals": {
                    "1": {"role": "tech", "required": True},
                    "2": {"role": "warehouse", "required": True},
                    "3": {"role": "qc", "required": True},
                },
                "required_fields": {
                    "1": ["process_route_version"],
                    "2": ["material_check_result"],
                },
                "required_statuses": [
                    {"entity_type": "drawing", "expected_status": "effective"},
                    {"entity_type": "process_route", "expected_status": "effective"},
                ],
                "timeout_hours": 12,
            },
            "is_active": True,
            "is_trained": False,
        },
        "rule_config_change": {
            "workflow_type": "rule_config_change",
            "workflow_name": "意图规则变更审批",
            "owner_dept": "system",
            "approval_chain": [
                {"step": 1, "role": "manager", "action": "审批"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        # v6.45：费用报销流程（对齐 OPERATIONS_PROMPT_GUIDE L311 训练示例：
        # trigger_keywords=["费用报销","报销"] + 审批链 manager→finance_manager
        # + gate_checks required_fields=[amount,expense_type,reason], timeout_hours=72）
        "expense_reimbursement": {
            "workflow_type": "expense_reimbursement",
            "workflow_name": "费用报销审批",
            "owner_dept": "finance",
            "approval_chain": [
                {"step": 1, "role": "manager", "action": "部门审批"},
                {"step": 2, "role": "finance_manager", "action": "财务审批"},
            ],
            "notify_rules": [
                {"event": "approved", "target": "finance", "channel": "system"},
                {"event": "approved", "target": "requester", "channel": "websocket"},
            ],
            "thresholds": {
                "trigger_keywords": ["费用报销", "报销", "报销流程", "报销单", "报销申请"],
                "approval_timeout_hours": 72,
            },
            "gate_checks": {
                "required_approvals": {
                    "1": {"role": "manager", "required": True},
                    "2": {"role": "finance_manager", "required": True},
                },
                "required_fields": {
                    "1": ["amount", "expense_type", "reason"],
                },
                "required_statuses": [],
                "timeout_hours": 72,
            },
            "is_active": True,
            "is_trained": False,
        },
        # v6.97 B.1：ISO 导入审批链（DB 定义优先可训练；兜底两条——
        # 质量经理 → 生产经理，对齐 E2E 与硬约束，非 manager 单级）
        "iso_import": {
            "workflow_type": "iso_import",
            "workflow_name": "ISO条款导入审批",
            "owner_dept": "quality",
            "approval_chain": [
                {"step": 1, "role": "quality_manager", "action": "审批"},
                {"step": 2, "role": "production_manager", "action": "确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        # v6.97 B.1：审批链变更审批（治理之治理，manager → admin 双级）
        "approval_chain_change": {
            "workflow_type": "approval_chain_change",
            "workflow_name": "审批链变更审批",
            "owner_dept": "system",
            "approval_chain": [
                {"step": 1, "role": "manager", "action": "审批"},
                {"step": 2, "role": "admin", "action": "确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        # v6.97 B.1：流程定义变更审批（技术 → 质量 → 生产，对齐流程治理）
        "workflow_def_change": {
            "workflow_type": "workflow_def_change",
            "workflow_name": "流程定义变更审批",
            "owner_dept": "system",
            "approval_chain": [
                {"step": 1, "role": "technical_manager", "action": "发起"},
                {"step": 2, "role": "quality_manager", "action": "质量确认"},
                {"step": 3, "role": "production_manager", "action": "生产确认"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        # v6.97 B.1：L1 会话学习审批（manager 单级，对齐既有 L1 UX）
        "training_data_approval": {
            "workflow_type": "training_data_approval",
            "workflow_name": "L1会话学习审批",
            "owner_dept": "training",
            "approval_chain": [
                {"step": 1, "role": "manager", "action": "审批"},
            ],
            "is_active": True,
            "is_trained": False,
        },
        # v6.97 B.1：L3 知识文档发布审批（录入 → 复核 → 发布）
        "knowledge_publish": {
            "workflow_type": "knowledge_publish",
            "workflow_name": "知识文档发布审批",
            "owner_dept": "training",
            "approval_chain": [
                {"step": 1, "role": "manager", "action": "录入"},
                {"step": 2, "role": "manager", "action": "复核"},
                {"step": 3, "role": "admin", "action": "发布"},
            ],
            "is_active": True,
            "is_trained": False,
        },
    }

    def __init__(self, database=None):
        """初始化流程执行器。

        参数：
            database: 数据库访问层（可空；为 None 时降级内存模式——
                      workflow_instances 存 _memory_instances，
                      gate 校验走 ChainGateChecker 内存规则）
        装配：_db 数据库句柄 + _gate_checker 七层门链检查器（共用同一 database）
        """
        self._db = database
        # 开源版：已移除 ChainGateChecker，Gate 校验降级为直接通过

    @property
    def db(self) -> Any:
        """公开数据库连接引用（W29 封装：外部模块读取实例/配置数据时使用，
        避免依赖私有属性 _db；数据库为 None 时返回 None 由调用方降级）。"""
        return self._db

    def start_workflow(self, workflow_type: str, biz_type: str, biz_id: str,
                       user: dict, initiation: str = 'manual') -> dict:
        """启动流程实例（创建 workflow_instances 记录）

        三道校验：
        1. 发起者角色 ∈ starter_roles
        2. 发起者部门 ∈ starter_depts
        3. 启动方式满足 initiation

        Args:
            workflow_type: 流程类型（如 customer_change/drawing_change/...）
            biz_type: 业务单据类型（如 order/return_order/drawing_change/...）
            biz_id: 业务单据主键
            user: 发起者用户信息 {"user_id":..., "role":..., "department":...}
            initiation: 启动方式 manual/event/auto

        Returns:
            dict: {"success": bool, "instance_id": int, "error": str}
        """
        # 1. 匹配 workflow_type
        config = self._get_workflow_config(workflow_type)
        if not config:
            return {
                "success": False,
                "instance_id": None,
                "error": f"流程类型 {workflow_type} 不存在或未生效",
            }

        # 2. 发起者三道校验
        passed, error = self._validate_starter(config, user, initiation)
        if not passed:
            return {
                "success": False,
                "instance_id": None,
                "error": error,
            }

        # 3. 创建 workflow_instances 记录
        instance = self._create_instance(config, biz_type, biz_id, user)
        if not instance:
            return {
                "success": False,
                "instance_id": None,
                "error": "流程实例创建失败",
            }

        return {
            "success": True,
            "instance_id": instance.get("instance_id"),
            "error": None,
        }

    def advance_step(self, instance_id: int, user: dict) -> dict:
        """推进流程到下一步

        执行链式Gate校验，通过后更新 current_step 和 steps_done。

        Args:
            instance_id: 流程实例ID
            user: 当前操作用户信息

        Returns:
            dict: {"success": bool, "current_step": int, "completed": bool,
                   "error": str, "missing": list}
        """
        # W13/W14：串行化整个 read-modify-write 段（实例粒度锁），避免并发审批竞态
        with self._get_instance_lock(instance_id):
            return self._advance_step_locked(instance_id, user)

    def _advance_step_locked(self, instance_id: int, user: dict) -> dict:
        """advance_step 加锁后的实际推进逻辑（W13 拆分）。

        校验顺序：实例存在 → 状态守卫（W12）→ 当前步骤角色（S3）→
        链式Gate → 推进步骤（S5 写失败即失败）。
        """
        # 定位流程实例
        instance = self._get_instance(instance_id)
        if not instance:
            return {
                "success": False,
                "current_step": None,
                "error": f"流程实例 {instance_id} 不存在",
                "missing": [],
            }

        # W12：状态守卫——已完成/已取消/已驳回的实例不可重复推进
        status = instance.get("status")
        if status == "completed":
            return {
                "success": False,
                "current_step": instance.get("current_step"),
                "error": "流程已完成，不可重复推进",
                "missing": [],
            }
        if status in ("cancelled", "rejected"):
            return {
                "success": False,
                "current_step": instance.get("current_step"),
                "error": f"流程已{status}，不可推进",
                "missing": [],
            }

        # 获取流程定义中的 gate_checks 配置
        config = self._get_workflow_config(instance.get("workflow_type"))
        gate_config = {}
        if config:
            gate_config = config.get("gate_checks", {})
            if isinstance(gate_config, str):
                try:
                    gate_config = json.loads(gate_config)
                except (json.JSONDecodeError, ValueError):
                    gate_config = {}

        # 将当前审批者注入实例上下文（供 SOD 校验使用）
        instance_ctx = dict(instance)
        instance_ctx["approver"] = user

        # 解析审批链（S3 角色校验 + completed 判定共用）
        chain = config.get("approval_chain") if config else None
        if isinstance(chain, str):
            try:
                chain = json.loads(chain)
            except (json.JSONDecodeError, ValueError):
                chain = None
        chain_list = chain if isinstance(chain, list) else []
        current_step = instance.get("current_step", 1)

        # W3：防御性校验——流程定义存在但审批链为空/非法时拒绝推进
        # （否则 current_step 可无限递增且永不 completed）
        if config and not chain_list:
            return {
                "success": False,
                "current_step": current_step,
                "error": "流程定义缺少有效审批链，无法推进",
                "missing": [],
            }

        # W3 越界守卫（B.3 P0）：审批链缩短后 current_step 超出链长 → 拒绝推进。
        # 否则下方 S3 角色校验 `1 <= current_step <= len(chain_list)` 不成立被跳过，
        # 且 completed 判定 `new_step > chain_len` 误判全部通过，形成安全旁路。
        if chain_list and current_step > len(chain_list):
            return {
                "success": False,
                "current_step": current_step,
                "error": ("实例步骤越界：当前步骤已超出审批链长度，"
                          "请先修正审批链或重建实例"),
                "missing": [],
            }

        # 签字链完整性校验（本会话加固）：前序签字数须等于 current_step-1。
        # 上级审批签字全部到位才允许下级推进——steps_done 丢失/被篡改（或
        # current_step 被直接修改跳级）时拒绝推进，防中间审批被绕过。
        steps_done = instance.get("steps_done", [])
        if isinstance(steps_done, str):
            try:
                steps_done = json.loads(steps_done)
            except (json.JSONDecodeError, ValueError):
                steps_done = []
        if not isinstance(steps_done, list):
            steps_done = []
        sig_err = validate_sig_chain(steps_done, max(0, current_step - 1))
        if sig_err:
            return {
                "success": False,
                "current_step": current_step,
                "error": sig_err,
                "missing": [],
            }

        # S3：当前步骤审批人角色校验（admin 代批豁免，与 advance_training_approval 同规则）
        user_role = user.get("role") if isinstance(user, dict) else None
        if chain_list and 1 <= current_step <= len(chain_list):
            step_cfg = chain_list[current_step - 1]
            required_role = step_cfg.get("role")
            if required_role and user_role != required_role and user_role != "admin":
                return {
                    "success": False,
                    "current_step": current_step,
                    "error": (f"当前步骤需「{required_role}」审批，"
                              f"您的角色「{user_role or '未知'}」无权限"),
                    "missing": [],
                }
            # TG-02（v6.99.2）：审批链 step 配置 dept_key 时执行部门双校验——
            # 审批人 department 须等于实例 extra_data[dept_key]（跨部门临时授权
            # 由"目标部门主管"审批：role=manager + dept=target_dept；admin 代批豁免）。
            # 仅当 step 显式配置 dept_key 时生效，现有审批链（无 dept_key）行为不变。
            dept_key = step_cfg.get("dept_key") or ""
            if dept_key and user_role != "admin":
                instance_extra = instance.get("extra_data") or {}
                if isinstance(instance_extra, str):
                    try:
                        import json as _json
                        instance_extra = _json.loads(instance_extra)
                    except Exception:
                        instance_extra = {}
                expected_dept = ""
                if isinstance(instance_extra, dict):
                    expected_dept = instance_extra.get(dept_key) or ""
                user_dept = user.get("department") if isinstance(user, dict) else ""
                if expected_dept and user_dept != expected_dept:
                    return {
                        "success": False,
                        "current_step": current_step,
                        "error": (f"当前步骤需「{expected_dept}」部门审批，"
                                  f"您的部门「{user_dept or '未知'}」无权限"),
                        "missing": [],
                    }

        # 开源版：已移除链式 Gate 校验，直接推进步骤

        # Gate校验通过，推进步骤
        new_step = current_step + 1

        # 将当前步骤追加到 steps_done（steps_done 已在签字链完整性校验处解析）
        done_entry = {
            "step": current_step,
            "role": user_role,
            # v6.60：审批签字信息——记录审批人工号/姓名，供流程单据查询
            # （报销单样式 HTML）展示"谁在何时审批"的签字痕迹
            "user_id": (user.get("id") or user.get("user_id"))
            if isinstance(user, dict) else None,
            "user_name": (user.get("name") or user.get("username"))
            if isinstance(user, dict) else None,
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        }
        steps_done = list(steps_done)
        steps_done.append(done_entry)

        # v6.56：审批链长度判定是否全部步骤已通过。
        # current_step 语义：1 表示待第 1 步审批；推进后 new_step 超出
        # 审批链步数即全部通过，实例标记 completed。
        chain_len = len(chain_list)
        completed = bool(chain_len) and new_step > chain_len

        update_data = {
            "current_step": new_step,
            "steps_done": steps_done,
        }
        if completed:
            update_data["status"] = "completed"
        # B.3 P0：审批推进审计与实例更新同一事务原子提交（_update_instance
        # 带 audit_row 时走 _persist_step_with_audit，失败整体回滚）
        audit_row = {
            "user_id": (user.get("id") or user.get("user_id"))
            if isinstance(user, dict) else None,
            "action": "workflow_approval",
            "details": {
                "instance_id": instance_id,
                "workflow_type": instance.get("workflow_type"),
                "step": new_step,
                "completed": bool(completed),
                "approver": (user.get("name") or user.get("user_id") or "")
                if isinstance(user, dict) else "",
            },
        }
        # S5：DB 写失败不再静默假成功——更新未生效即返回失败
        if not self._update_instance(instance_id, update_data,
                                     audit_row=audit_row):
            return {
                "success": False,
                "current_step": current_step,
                "error": "流程实例状态更新失败（DB 写入异常），请重试",
                "missing": [],
            }

        return {
            "success": True,
            "current_step": new_step,
            "completed": completed,
            "error": None,
            "missing": [],
        }

    # --------------------------------------------------------
    # 审批留言（多人协作场景①，v1.6.57）
    # 申请人↔审批人同上下文往返：留言落 workflow_comments 表，
    # 供 workflow_query 单据展示；留言不进入知识库（过程性数据）。
    # --------------------------------------------------------

    def add_comment(self, instance_id: int, step: int,
                    author: dict, content: str) -> dict:
        """记录审批留言。

        Args:
            instance_id: 流程实例ID
            step: 当前审批步骤
            author: 留言人信息（含 id/user_id 与 name/username）
            content: 留言内容

        Returns:
            dict: {"success": bool, "comment_id": int|None, "error": str}
        """
        content = (content or "").strip()
        if not content:
            return {"success": False, "comment_id": None,
                    "error": "留言内容不能为空"}
        author = author or {}
        author_id = (author.get("id") or author.get("user_id")
                     if isinstance(author, dict) else None) or "anonymous"
        row = {
            "instance_id": instance_id,
            "step": step or 1,
            "author_id": author_id,
            "content": content,
        }
        comment_id = None
        if self._db is not None:
            try:
                comment_id = self._db.insert("workflow_comments", row)
            except Exception as e:
                return {"success": False, "comment_id": None,
                        "error": f"留言落库失败：{e}"}
        else:
            # 内存降级：写入实例 extra_data.comments
            inst = self._get_instance(instance_id)
            if inst is None:
                return {"success": False, "comment_id": None,
                        "error": f"流程实例 {instance_id} 不存在"}
            extra = inst.get("extra_data") or {}
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except (json.JSONDecodeError, ValueError):
                    extra = {}
            comments = list(extra.get("comments") or [])
            row["comment_id"] = len(comments) + 1
            comments.append(row)
            extra["comments"] = comments
            # W1：检查 _update_instance 返回值——实例不存在/更新失败时留言不静默丢失
            if not self._update_instance(instance_id, {"extra_data": extra}):
                return {"success": False, "comment_id": None,
                        "error": f"流程实例 {instance_id} 状态更新失败"}
            comment_id = row["comment_id"]
        return {"success": True, "comment_id": comment_id, "error": None}

    def list_comments(self, instance_id: int) -> List[dict]:
        """查询实例审批留言（时间正序）。

        Args:
            instance_id: 流程实例ID

        Returns:
            list: 留言记录列表（DB 不可用/无留言时返回空列表）
        """
        if self._db is not None:
            try:
                rows = self._db.query_many(
                    "workflow_comments", {"instance_id": instance_id},
                    order_by="comment_id ASC") or []
                return list(rows)
            except Exception as e:
                # W2：查询失败记录日志——避免"无留言"与"查询失败"无法区分
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "留言查询失败 | instance_id=%s | error=%s",
                        instance_id, e,
                    )
                except Exception:
                    pass
                return []
        inst = self._get_instance(instance_id)
        if inst is None:
            return []
        extra = inst.get("extra_data") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = {}
        return list(extra.get("comments") or [])

    # ============================================================
    # 内部方法
    # ============================================================

    def _validate_starter(self, config: dict, user: dict,
                          initiation: str) -> Tuple[bool, str]:
        """发起者三道校验

        1. starter_roles 校验：user["role"] ∈ config["starter_roles"]
        2. starter_depts 校验：user["department"] ∈ config["starter_depts"]
        3. initiation 校验：config["initiation"] 与传入 initiation 匹配

        Args:
            config: workflow_configs 记录
            user: 用户信息
            initiation: 启动方式

        Returns:
            (passed: bool, error: str)
        """
        user = user or {}

        # 1. 发起者角色校验（v6.43：允许列表含 'all' 表示任意角色可发起）
        starter_roles = config.get("starter_roles")
        if starter_roles:
            starter_roles = self._parse_jsonb_list(starter_roles)
            if starter_roles and "all" not in starter_roles:
                user_role = user.get("role")
                if user_role not in starter_roles:
                    return (False, f"发起者角色 {user_role} 不在允许列表 "
                                   f"{starter_roles} 中")

        # 2. 发起者部门校验（v6.43：允许列表含 'all' 表示任意部门可发起）
        starter_depts = config.get("starter_depts")
        if starter_depts:
            starter_depts = self._parse_jsonb_list(starter_depts)
            if starter_depts and "all" not in starter_depts:
                user_dept = user.get("department")
                if user_dept not in starter_depts:
                    return (False, f"发起者部门 {user_dept} 不在允许列表 "
                                   f"{starter_depts} 中")

        # 3. 启动方式校验
        config_initiation = config.get("initiation")
        if config_initiation:
            config_mode = self._extract_initiation_mode(config_initiation)
            if config_mode and config_mode != initiation:
                return (False, f"启动方式不匹配：期望 {config_mode}，"
                               f"实际 {initiation}")

        return (True, "")

    def _get_workflow_config(self, workflow_type: str) -> dict:
        """从 workflow_configs 表获取流程定义

        查询 is_active=TRUE 的生效流程。
        数据库不可用时降级返回 _DEFAULT_WORKFLOW_CONFIGS 种子数据（v6.36）。
        """
        if self._db is not None:
            try:
                row = self._db.query_one("workflow_configs",
                                         {"workflow_type": workflow_type,
                                          "is_active": True})
                if row:
                    return row
            except Exception:
                pass
        # DB不可用或查询失败 -> 降级种子数据
        fallback = self._DEFAULT_WORKFLOW_CONFIGS.get(workflow_type)
        if fallback:
            return dict(fallback)
        return None

    def _validate_created_by(self, user_id: Optional[str]) -> Optional[str]:
        """created_by 外键校验（v6.94 FK 瑕疵方案 B：代码层双保险）

        workflow_instances.created_by REFERENCES users(user_id)（008 迁移
        §2.5.5 L93）。system 占位账号已由 070 迁移补齐（方案 A）；本方法
        兜底未执行 070 的环境（重建库/新部署）：user_id 不在 users 表时
        回退 NULL（列可空，FK 满足），保证流程仍可发起（不再被外键违规
        吞成"流程实例创建失败"），并记警告提示补跑 070。

        - DB 未配置（内存模式）：无 FK 约束，原样返回
        - 校验查询自身异常：不阻断发起，交由 insert 成败判定
        """
        if self._db is None or not user_id:
            return user_id or None
        try:
            row = self._db.query_one("users", {"user_id": user_id},
                                     columns=["user_id"])
        except Exception:
            return user_id
        if row:
            return user_id
        try:
            import logging
            logging.getLogger(__name__).warning(
                "created_by=%s 不在 users 表（建议执行 070 迁移补 system "
                "账号），外键回退 NULL", user_id,
            )
        except Exception:
            pass
        return None

    def _create_instance(self, config: dict, biz_type: str, biz_id: str,
                         user: dict) -> dict:
        """创建 workflow_instances 记录

        数据库可用时持久化到 workflow_instances 表；
        数据库不可用时降级到内存模式（类变量 _instances dict）。

        Args:
            config: workflow_configs 记录
            biz_type: 业务单据类型
            biz_id: 业务单据主键
            user: 发起者用户信息

        Returns:
            dict: 创建的实例记录
        """
        config_id = config.get("config_id")
        workflow_type = config.get("workflow_type")
        agent_scope = config.get("agent_scope", [])
        # 兼容两种用户信息键名：chat 层注入 {"id": ...}，业务侧 {"user_id": ...}
        user_id = (user.get("user_id") or user.get("id")
                   if isinstance(user, dict) else None)
        # v6.94 FK 双保险：created_by 引用 users(user_id)，写入前校验账号
        # 存在，缺失时回退 NULL（可空列，FK 满足）
        user_id = self._validate_created_by(user_id)

        # 优先尝试数据库持久化
        if self._db is not None:
            try:
                data = {
                    "config_id": config_id,
                    "workflow_type": workflow_type,
                    "biz_type": biz_type,
                    "biz_id": biz_id,
                    "current_step": 1,
                    "status": "running",
                    "created_by": user_id,
                }
                # JSONB 字段需要序列化
                if isinstance(agent_scope, list):
                    data["agent_scope"] = json.dumps(agent_scope)
                else:
                    data["agent_scope"] = agent_scope or "[]"
                data["steps_done"] = "[]"

                instance_id = self._db.insert("workflow_instances", data)
                return {
                    "instance_id": instance_id,
                    "config_id": config_id,
                    "workflow_type": workflow_type,
                    "biz_type": biz_type,
                    "biz_id": biz_id,
                    "current_step": 1,
                    "steps_done": [],
                    "status": "running",
                    "created_by": user_id,
                }
            except Exception as e:
                # W14：DB 已配置但插入失败——不静默降级内存（避免"半脑"实例：
                # DB 查询模式下内存实例不可见），返回 None 由 start_workflow 转失败响应
                try:
                    import logging
                    logging.getLogger(__name__).error(
                        "流程实例创建失败（DB 写入 workflow_instances 异常）| "
                        "biz_type=%s | biz_id=%s | error=%s",
                        biz_type, biz_id, e, exc_info=True,
                    )
                except Exception:
                    pass
                return None

        # 内存模式（仅 DB 未配置时使用）
        # W13：序号原子自增，避免并发创建时 _instance_seq 竞态
        # B.3 P0：内存 ID 加前缀 "M{seq}"，与 DB 模式 SERIAL 纯数字隔离，
        # 避免两空间序号碰撞（如内存 1 与 DB 实例 1 混淆）
        with WorkflowEnforcer._seq_lock:
            WorkflowEnforcer._instance_seq += 1
            instance_id = f"{MEMORY_INSTANCE_PREFIX}{WorkflowEnforcer._instance_seq}"
        instance = {
            "instance_id": instance_id,
            "config_id": config_id,
            "workflow_type": workflow_type,
            "biz_type": biz_type,
            "biz_id": biz_id,
            "agent_scope": (agent_scope if isinstance(agent_scope, list)
                            else []),
            "current_step": 1,
            "steps_done": [],
            "status": "running",
            "created_by": user_id,
            "created_at": datetime.now(),
        }
        WorkflowEnforcer._instances[instance_id] = instance
        return instance

    def _get_instance(self, instance_id) -> dict:
        """获取流程实例

        B.3 P0（ID 空间隔离）：按实例 ID 前缀路由——
        - 内存模式 ID（"M{seq}" 字符串）→ 仅查内存 _instances，不落 DB；
        - DB 模式 ID（纯数字）→ 先查 workflow_instances 表，失败回退内存。
        避免两空间 ID 碰撞时相互串扰（如 DB 实例 1 与内存 M1 混淆）。
        """
        is_memory_id = (isinstance(instance_id, str)
                        and instance_id.startswith(MEMORY_INSTANCE_PREFIX))
        if not is_memory_id and self._db is not None:
            try:
                row = self._db.query_one("workflow_instances",
                                         {"instance_id": instance_id})
                if row:
                    # 解析 JSONB 字段
                    steps_done = row.get("steps_done", [])
                    if isinstance(steps_done, str):
                        try:
                            steps_done = json.loads(steps_done)
                        except (json.JSONDecodeError, ValueError):
                            steps_done = []
                    row["steps_done"] = steps_done
                    return row
            except Exception:
                pass

        # 内存模式
        return WorkflowEnforcer._instances.get(instance_id)

    def _update_instance(self, instance_id, data: dict,
                         audit_row: Optional[dict] = None) -> bool:
        """更新流程实例

        数据库可用时更新 workflow_instances 表；
        数据库不可用时更新内存 _instances dict。

        Args:
            instance_id: 流程实例ID
            data: 待更新字段字典
            audit_row: 可选审计行。提供时（审批推进）与实例更新在**同一事务**
                内写入 operation_logs（B.3 P0：steps_done 与操作日志原子提交，
                任一失败整体回滚，杜绝"推进成功但审计缺失"的不一致）。

        Returns:
            bool: 是否更新成功（S5：DB 模式写失败返回 False，不再静默吞错假成功）
        """
        if self._db is not None:
            try:
                update_data = dict(data)
                # JSONB 字段需要序列化
                if "steps_done" in update_data and isinstance(
                        update_data["steps_done"], list):
                    update_data["steps_done"] = json.dumps(
                        update_data["steps_done"])
                # B.3：带审计行 → 实例更新 + 审计插入同一事务原子提交
                if audit_row:
                    return self._persist_step_with_audit(
                        instance_id, update_data, audit_row)
                # W5：检查更新是否命中——受影响 0 行（实例不存在/已删除）视为失败；
                # 兼容 update 返回 None 的鸭子类型实现（视为成功，无法判定）
                affected = self._db.update("workflow_instances", update_data,
                                           {"instance_id": instance_id})
                if affected is not None and affected == 0:
                    try:
                        import logging
                        logging.getLogger(__name__).warning(
                            "流程实例更新未命中 | instance_id=%s | 实例不存在或已删除",
                            instance_id,
                        )
                    except Exception:
                        pass
                    return False
                return True
            except Exception as e:
                # S5：DB 写失败不静默吞错——记录日志并返回 False，由调用方转失败响应
                try:
                    import logging
                    logging.getLogger(__name__).error(
                        "流程实例更新失败 | instance_id=%s | fields=%s | error=%s",
                        instance_id, list(data), e, exc_info=True,
                    )
                except Exception:
                    pass
                return False

        # 内存模式
        instance = WorkflowEnforcer._instances.get(instance_id)
        if instance:
            instance.update(data)
            return True
        return False

    def _persist_step_with_audit(self, instance_id: int,
                                 update_data: dict,
                                 audit_row: dict) -> bool:
        """原子持久化审批推进：workflow_instances 更新 + operation_logs 审计
        在同一 SQLAlchemy 会话/事务内提交，任一失败整体回滚。

        B.3 P0：原实现 advance_step 先写 steps_done（独立连接提交），
        coordinator 再单独 insert operation_logs（独立连接，异常被吞）——
        审计写失败时 steps_done 已生效，痕迹永久缺失。本方法消除该不一致。

        Returns:
            bool: 全部提交成功返回 True；更新未命中或任一写失败返回 False
        """
        from sqlalchemy import text
        session = None
        try:
            session = self._db.get_session()
            sets = ", ".join(f"{k} = :c_{k}" for k in update_data)
            params = {f"c_{k}": v for k, v in update_data.items()}
            params["c_instance_id"] = instance_id
            res = session.execute(
                text(f"UPDATE workflow_instances SET {sets} "
                     f"WHERE instance_id = :c_instance_id"),
                params,
            )
            if res.rowcount == 0:
                session.rollback()
                return False
            session.execute(
                text(
                    "INSERT INTO operation_logs (user_id, action, details, "
                    "extra_data) VALUES (:user_id, :action, :details, :extra_data)"
                ),
                {
                    "user_id": audit_row.get("user_id"),
                    "action": audit_row.get("action", "workflow_approval"),
                    "details": json.dumps(audit_row.get("details", {}),
                                          ensure_ascii=False, default=str),
                    "extra_data": json.dumps(
                        {"timestamp": datetime.now().isoformat()},
                        ensure_ascii=False, default=str),
                },
            )
            session.commit()
            return True
        except Exception:
            if session is not None:
                try:
                    session.rollback()
                except Exception:
                    pass
            return False
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def _parse_jsonb_list(value) -> list:
        """解析 JSONB 列表字段

        value 可能是 list（已反序列化）或 JSON 字符串。
        """
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                result = json.loads(value)
                if isinstance(result, list):
                    return result
            except (json.JSONDecodeError, ValueError):
                pass
        return []

    @staticmethod
    def _extract_initiation_mode(initiation_value) -> str:
        """从 initiation 配置中提取启动模式

        initiation 可能是：
            dict: {"mode":"manual", "manual_entry":"/api/..."}
            str: "manual" 或 JSON 字符串
        """
        if isinstance(initiation_value, dict):
            return initiation_value.get("mode", "")
        if isinstance(initiation_value, str):
            try:
                parsed = json.loads(initiation_value)
                if isinstance(parsed, dict):
                    return parsed.get("mode", "")
            except (json.JSONDecodeError, ValueError):
                pass
            return initiation_value
        return ""


# --------------------------------------------------------
# 流程定义训练（v6.47 单轨制：业务操作建流程实例的前提——
# 流程定义全部由训练/种子数据提供，代码不硬编码）
# --------------------------------------------------------
# 训练任务 = 新建或修改流程：
#   - 新建流程：定义 workflow_type/审批链/触发关键词/承接意图(intent_map)/
#     必填字段(gate_checks)/发起者(starter_roles·starter_depts)/启动方式
#   - 修改流程：更新既有定义（版本+1）
# 变更走 L2 审批链（workflow_configs(workflow_def_change) 审批记录），
# 审批通过后 apply_workflow_def_change 写入 workflow_configs 生效。
# 与 slot_engine.submit_slot_def_change（槽位定义训练）同一治理机制。
WORKFLOW_DEF_CHANGE_WF = "workflow_def_change"


def _wf_def_json(obj) -> str:
    """流程定义 dict 序列化为 JSON 字符串（审批对比/落库用）。

    参数：
        obj: 流程定义 dict（workflow_configs 记录）
    返回：
        str: ensure_ascii=False 的 JSON 串（保留中文可读）
    """
    return json.dumps(obj, ensure_ascii=False)


def submit_workflow_def_change(proposed: dict, current: Optional[dict] = None,
                               db: Any = None,
                               changed_by: str = "L2") -> Optional[int]:
    """提交流程定义变更（新建/修改流程）：写 workflow_configs 审批记录。

    Args:
        proposed: 新流程定义 dict（workflow_type/workflow_name/owner_dept/
                  approval_chain/notify_rules/thresholds(含 intent_map/
                  trigger_keywords/biz_type/biz_id)/gate_checks/starter_roles/
                  starter_depts/initiation）
        current: 当前流程定义（修改时提供，供审批对比；新建可省略）
        db: 可选数据库
        changed_by: 变更来源（L2 训练 / manual）

    Returns:
        config_id（写库成功）或 None（DB 不可用）
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return None
    try:
        from prog.runtime.approval_chain import get_approval_chain
        wf_type = (proposed or {}).get("workflow_type", "")
        return db.insert("workflow_configs", {
            "workflow_type": WORKFLOW_DEF_CHANGE_WF,
            "workflow_name": f"流程定义变更审批-{wf_type or '新建'}",
            "owner_dept": (proposed or {}).get("owner_dept", "system"),
            "trigger_rule": wf_type or "new",
            # 审批链从 DB workflow_def_change 定义行读取（可训练），
            # 无定义/DB 不可用时由 get_approval_chain 兜底 manager 单级
            "approval_chain": _wf_def_json(get_approval_chain(
                WORKFLOW_DEF_CHANGE_WF, db=db)),
            "thresholds": _wf_def_json({
                "action": "update" if current else "create",
                "proposed": proposed,
                "current": current or {},
                "changed_by": changed_by,
            }),
            "is_active": True,
            "is_trained": False,
        })
    except Exception:
        return None


def apply_workflow_def_change(new_def: dict, db: Any = None,
                              modified_by: str = "L2") -> bool:
    """审批通过后应用流程定义变更：新增或更新 workflow_configs 定义行。

    Args:
        new_def: 新流程定义（与 submit_workflow_def_change 的 proposed 同构）
        db: 可选数据库
        modified_by: 修改人

    Returns:
        bool: 是否更新成功
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return False
    # updated_by 有外键约束指向 users(user_id)；空值兜底 admin 避免静默失败
    modified_by = (modified_by or "admin")[:64]
    try:
        wf_type = (new_def or {}).get("workflow_type")
        if not wf_type:
            return False
        row = db.query_one("workflow_configs",
                           {"workflow_type": wf_type, "is_active": True})
        update_fields = {
            "workflow_name": new_def.get("workflow_name", wf_type),
            "owner_dept": new_def.get("owner_dept", "system"),
            "approval_chain": _wf_def_json(
                new_def.get("approval_chain") or
                [{"step": 1, "role": "manager", "action": "审批"}]),
            "notify_rules": _wf_def_json(new_def.get("notify_rules") or []),
            "thresholds": _wf_def_json(new_def.get("thresholds") or {}),
            "gate_checks": _wf_def_json(new_def.get("gate_checks") or {}),
            "starter_roles": _wf_def_json(new_def.get("starter_roles") or []),
            "starter_depts": _wf_def_json(new_def.get("starter_depts") or []),
            "initiation": new_def.get("initiation", "manual"),
            "is_active": True,
            "is_trained": True,
            "version": int((row or {}).get("version", 1) or 1) + 1,
            "updated_by": modified_by,
        }
        if row:
            db.update("workflow_configs", update_fields,
                      {"config_id": row.get("config_id")})
        else:
            db.insert("workflow_configs", {
                **update_fields,
                "workflow_type": wf_type,
                "trigger_rule": new_def.get("trigger_rule", wf_type),
            })
        return True
    except Exception:
        return False


# ============================================================
# 查询流程执行器（v6.64）：流程定义 gate_checks.query_steps 查库
# ============================================================
# 可查表白名单：query_steps.table 必须在此集合内（防配置注入）。
# 双重防护：白名单 + 定义修改走训练审批链（workflow_def_change 审批才生效）。
_QUERY_STEP_TABLES = frozenset({
    "inventory", "inventory_movements", "products", "orders", "order_items",
    "customers", "suppliers", "supplier_quotes", "supplier_materials",
    "bom", "process_routes", "work_orders", "production_lines",
    "qc_records", "drawings", "return_orders", "purchase_requests",
    "purchase_request_items", "attendance", "work_reports",
    "workflow_instances", "workflow_comments", "notifications",
    "conversation_messages",
})


def execute_workflow_query(workflow_type: str, params: Optional[dict] = None,
                           user: Optional[dict] = None, db: Any = None) -> dict:
    """执行查询流程定义的查库项目（gate_checks.query_steps，可训练）。

    query_steps 结构与 rule_engine.lookup 语义一致（经 workflow_train
    训练提取 + 审批生效）：
        [{"step": 1, "table": "inventory", "key_field": "product_code",
          "source_key": "product_code", "fields": [...], "mode": "one|many",
          "required_permission": "can_inventory"}]

    执行顺序（v6.64）：
        1. 定义存在且含 query_steps（无则返回"无查库项目"）
        2. 权限管控：每步 required_permission 与 user.permissions 匹配——
           未声明权限或权限缺失一律拒绝，不执行查库
        3. 表名白名单：table 必须在 _QUERY_STEP_TABLES 内（防注入）
        4. 执行 db.query_one / query_many

    Args:
        workflow_type: 查询流程类型（workflow_configs.workflow_type）
        params: 查询参数 dict（key_field 值来源，如 {"product_code": "A-202"}）
        user: 当前用户 {"permissions": {...}}
        db: 可选数据库

    Returns:
        dict: {"success": bool, "result": 行dict|行list|None, "steps": int,
               "error": str|None, "permission": bool, "missing": list,
               "step_results": list}
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return {"success": False, "result": None, "steps": 0,
                "error": "数据库不可用", "permission": True}
    try:
        row = db.query_one("workflow_configs",
                           {"workflow_type": workflow_type,
                            "is_active": True})
        if not row:
            return {"success": False, "result": None, "steps": 0,
                    "error": f"查询流程 {workflow_type} 不存在或未生效",
                    "permission": True}
    except Exception as e:
        return {"success": False, "result": None, "steps": 0,
                "error": f"读取流程定义失败：{e}", "permission": True}

    gc = row.get("gate_checks") or {}
    if isinstance(gc, str):
        try:
            gc = json.loads(gc)
        except Exception:
            gc = {}
    steps = gc.get("query_steps") or []
    if not steps:
        return {"success": False, "result": None, "steps": 0,
                "error": "该流程定义无查库项目（gate_checks.query_steps）",
                "permission": True}

    params = dict(params or {})
    user = dict(user or {})
    perms = user.get("permissions")
    perms = perms if isinstance(perms, dict) else {}

    # v6.65 附加过滤条件：业务侧解析的 query_param_parser.filters
    # （{"field","op","value"}，字段名/操作符白名单由 query_filtered 校验）。
    # 仅供 db 步骤使用；权限拒绝时同样不应用（无查库即无副作用）。
    extra_conditions = params.get("_filters") or []
    if not isinstance(extra_conditions, list):
        extra_conditions = []

    # 权限管控：每步 required_permission 必须显式声明且当前用户具备
    # （仅 db 步骤；kb/web/llm 生成步骤由业务侧编排执行，其权限以数据步骤为门禁）
    # v6.65：支持通配权限（"*": True，admin 全权限），与 PermissionSystem ROLE_ADMIN 语义一致
    wildcard = perms.get("*", False) is True
    for s in steps:
        if not isinstance(s, dict):
            continue
        if s.get("type", "db") != "db":
            continue
        req = s.get("required_permission")
        if not req or not (perms.get(req) or wildcard):
            return {"success": False, "result": None, "steps": 0,
                    "error": f"无权限执行查询（需权限：{req or '未声明'}）",
                    "permission": False}

    executed = []
    # v6.65.4：step_results 与 query_steps 中 db 步骤一一对应（缺参跳过
    # 记 None），供业务侧按步骤索引渲染——多表流程中每个 db 步骤只渲染
    # 自己的结果，避免整表结果列表重复渲染导致数据错位。
    step_results: list = []
    # v6.65：缺参不中断——已提供的参数照查，缺失参数跳过该步骤查询，
    # 最终返回携带 missing 列表（业务侧渲染"缺少参数"说明）
    missing_params: list = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        # 仅执行 db 步骤（type 缺省视为 db）；kb/web/llm 步骤由业务侧
        # 知识助手编排（framework 保持零第三方依赖）
        if s.get("type", "db") != "db":
            continue
        step_results.append(None)
        table = s.get("table", "")
        key_field = s.get("key_field", "")
        source_key = s.get("source_key", "") or key_field
        fields = s.get("fields")
        mode = s.get("mode", "one")
        # v6.65.3：多表查询流程中，附加过滤条件仅保留当前表有效的字段
        # （fields 声明列 + key_field），避免跨表条件（如 total_amount
        # 应用到 customers 表）导致 UndefinedColumn 错误。
        _step_fields = set(fields or [])
        if key_field:
            _step_fields.add(key_field)
        _step_conds = [c for c in extra_conditions
                       if str(c.get("field", "")) in _step_fields] if _step_fields else list(extra_conditions)
        if table not in _QUERY_STEP_TABLES:
            return {"success": False, "result": None,
                    "steps": len(executed),
                    "error": f"查询表 {table} 不在允许范围",
                    "permission": True}
        value = params.get(source_key)
        if value is None:
            # v6.65：无主键但有附加过滤条件（如"库存大于100"）时，
            # 直接按附加条件查询多条（读取类，权限+表白名单+字段白名单已管控）；
            # 无主键且无附加条件 -> 缺参跳过（不中断），记录 missing 供业务侧说明
            if _step_conds:
                from prog.runtime.database import get_database as _gd
                _db = db if hasattr(db, "query_filtered") else None
                if _db is None:
                    _db = _gd()
                if _db is not None and hasattr(_db, "query_filtered"):
                    try:
                        res = _db.query_filtered(
                            table, list(_step_conds), fields, limit=50)
                        executed.append({
                            "step": s.get("step", len(executed) + 1),
                            "table": table, "mode": "many", "result": res})
                        step_results[-1] = res
                        continue
                    except Exception as e:
                        return {"success": False, "result": None,
                                "steps": len(executed),
                                "error": f"查库失败（{table}）：{e}",
                                "permission": True}
            # v6.65：缺参不中断——跳过该步骤查询，记录 missing 供业务侧说明
            if source_key not in missing_params:
                missing_params.append(source_key)
            continue
        try:
            if mode == "many":
                res = db.query_many(table, {key_field: value}, fields, limit=50)
            else:
                res = db.query_one(table, {key_field: value}, fields)
            # v6.65 附加过滤条件：主键等值命中后，再用 query_filtered
            # 补充比较/范围/模糊条件（同表再查一次，字段/操作符白名单）。
            # 仅当主键查询有结果且存在附加条件时执行，避免全表扫描。
            if _step_conds and res:
                _cur = res if isinstance(res, list) else [res]
                # 取主键值集合，用 IN 组合附加条件（防注入：字段白名单）
                from prog.runtime.database import get_database as _gd
                _db = db if hasattr(db, "query_filtered") else None
                if _db is None:
                    _db = _gd()
                if _db is not None and hasattr(_db, "query_filtered"):
                    _pk_vals = list({r.get(key_field) for r in _cur
                                     if r.get(key_field) is not None})
                    _cond = [{"field": key_field, "op": "in",
                              "value": _pk_vals}] if _pk_vals else []
                    _cond += list(_step_conds)
                    _filtered = _db.query_filtered(
                        table, _cond, fields, limit=50)
                    if mode == "one":
                        res = (_filtered[0] if _filtered else None)
                    else:
                        res = _filtered or []
            executed.append({"step": s.get("step", len(executed) + 1),
                             "table": table, "mode": mode, "result": res})
            step_results[-1] = res
        except Exception as e:
            return {"success": False, "result": None,
                    "steps": len(executed),
                    "error": f"查库失败（{table}）：{e}",
                    "permission": True}

    results = [e["result"] for e in executed]
    result = results[0] if len(results) == 1 else results
    return {"success": True, "result": result, "steps": len(executed),
            "error": None, "permission": True,
            "missing": missing_params, "step_results": step_results}


# 查询流程允许的步骤类型（db=查库 / kb=知识库 / web=联网 / llm=生成）
_QUERY_STEP_TYPES = ("db", "kb", "web", "llm")


def submit_query_flow(proposed: dict, user: Optional[dict] = None,
                      db: Any = None) -> dict:
    """提交查询流程（v6.64）：个人可免审批直接生效的查询流程。

    查询流程仅承载只读查询步骤（db/kb/web/llm 生成），无业务写副作用，
    故免审批直接生效（区别于业务流程的 workflow_def_change 审批链）。
    但必须通过规则校验（"仍需遵守规则"）：
        1. 必须含 gate_checks.query_steps（无则拒绝）
        2. 每步骤类型仅允许 db/kb/web/llm（拒绝未知/写类型）
        3. db 步骤 table ∈ _QUERY_STEP_TABLES 表白名单（防注入）
        4. 每步骤必须声明 required_permission（查询授权门禁，缺则拒绝）
    校验通过后直接 apply_workflow_def_change 写入生效定义行，
    并留痕 operation_logs（action=query_flow_create）。

    Args:
        proposed: 查询流程定义（与 submit_workflow_def_change 的 proposed 同构）
        user: 创建者 {"id"|"user_id", "name", "permissions"}; 可为空（系统）
        db: 可选数据库

    Returns:
        dict: {"success": bool, "workflow_type": str, "error": str|None}
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return {"success": False, "workflow_type": "", "error": "数据库不可用"}
    proposed = dict(proposed or {})
    wf_type = proposed.get("workflow_type", "")
    if not wf_type:
        return {"success": False, "workflow_type": "",
                "error": "缺少流程类型（workflow_type）"}
    gc = proposed.get("gate_checks") or {}
    if isinstance(gc, str):
        try:
            gc = json.loads(gc)
        except Exception:
            gc = {}
    steps = gc.get("query_steps") or []
    if not steps:
        return {"success": False, "workflow_type": wf_type,
                "error": "查询流程必须包含查库项目（gate_checks.query_steps）"}
    for s in steps:
        if not isinstance(s, dict):
            return {"success": False, "workflow_type": wf_type,
                    "error": "查库项目格式错误（须为对象）"}
        stype = s.get("type", "db")
        if stype not in _QUERY_STEP_TYPES:
            return {"success": False, "workflow_type": wf_type,
                    "error": f"不支持的步骤类型：{stype}"}
        if not s.get("required_permission"):
            return {"success": False, "workflow_type": wf_type,
                    "error": f"步骤 {s.get('step', '?')} 必须声明权限"
                             "（required_permission）"}
        if stype == "db" and s.get("table") not in _QUERY_STEP_TABLES:
            return {"success": False, "workflow_type": wf_type,
                    "error": f"查询表 {s.get('table')} 不在允许范围"}
    user = user or {}
    modifier = (user.get("id") or user.get("user_id") or "admin")[:64]
    ok = apply_workflow_def_change(proposed, db=db, modified_by=modifier)
    if not ok:
        return {"success": False, "workflow_type": wf_type,
                "error": "查询流程定义写入失败"}
    try:
        db.insert("operation_logs", {
            "user_id": modifier,
            "action": "query_flow_create",
            "details": {"workflow_type": wf_type,
                        "workflow_name": proposed.get("workflow_name", wf_type),
                        "steps": len(steps)},
        })
    except Exception as e:
        # W11：留痕失败记录日志（不阻断主流程，但审计追踪可见）
        try:
            import logging
            logging.getLogger(__name__).warning(
                "查询流程创建留痕失败 | workflow_type=%s | error=%s", wf_type, e,
            )
        except Exception:
            pass
    return {"success": True, "workflow_type": wf_type, "error": None}


# S8：训练审批并发保护锁（同 WorkflowEnforcer._advance_lock 思路）——
# 同一审批单的 read-modify-write 串行化，避免并发推进丢失签字/重复触发 apply
_TRAINING_APPROVAL_LOCK = threading.Lock()


def validate_sig_chain(steps_done, expected: int) -> Optional[str]:
    """审批链签字完整性校验（本会话加固，统一入口）。

    供 workflow_enforcer 与各 api 审批推进入口共用：前序签字数
    len(steps_done) 必须等于 expected，否则拒绝推进（steps_done 丢失/
    被篡改或 current_step 被直接修改跳级时防中间审批被绕过）。

    Args:
        steps_done: 前序签字列表（非 list 视为空）
        expected: 推进前应有的前序签字数（业务实例 = current_step-1；
                  训练/配置审批 = current_step）

    Returns:
        None 表示通过；否则返回错误文案（调用方以 4xx 拒绝推进）
    """
    if not isinstance(steps_done, list):
        steps_done = []
    if len(steps_done) != expected:
        return (f"审批链不完整：前序已审批 {len(steps_done)} 步，"
                f"当前应为 {expected} 步，请修正审批记录后重试")
    return None


def advance_training_approval(config_id: int, user: Optional[dict] = None,
                              db: Any = None) -> dict:
    """推进流程定义训练审批（v6.61）：workflow_configs 审批记录行逐步推进。

    与 apply_workflow_def_change 对称：提交走 submit_workflow_def_change，
    审批推进走本函数。审批链从审批记录行 approval_chain 读取（训练可定义），
    逐级校验当前步骤审批人角色（admin 代批豁免），签字记录写入
    thresholds.steps_done（{step, role, user_id, user_name, done_at}），
    全部通过后 apply_workflow_def_change 写入定义行生效并归档审批记录。

    S8：加锁串行化（with _TRAINING_APPROVAL_LOCK），并发安全。

    Args:
        config_id: workflow_configs 审批记录行 config_id
        user: 当前审批人 {"id"/"user_id", "name"/"username", "role"}
        db: 可选数据库

    Returns:
        dict: {"success": bool, "completed": bool, "current_step": int,
               "chain": list, "proposed": dict, "workflow_name": str,
               "steps_done": list, "error": str}
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return {"success": False, "completed": False, "error": "数据库不可用"}
    with _TRAINING_APPROVAL_LOCK:
        return _advance_training_approval_locked(config_id, user, db)


def _advance_training_approval_locked(config_id: int, user: Optional[dict],
                                      db: Any) -> dict:
    """advance_training_approval 加锁后的实际推进逻辑（S8 拆分）。"""
    import json as _json
    from datetime import datetime

    try:
        rec = db.query_one("workflow_configs", {"config_id": int(config_id)})
        if not rec:
            return {"success": False, "completed": False,
                    "error": f"审批单 {config_id} 不存在"}
        th = rec.get("thresholds")
        if isinstance(th, str):
            try:
                th = _json.loads(th)
            except Exception:
                th = {}
        if not isinstance(th, dict) or th.get("action") not in ("create", "update"):
            return {"success": False, "completed": False,
                    "error": "非流程定义训练审批记录"}

        proposed = th.get("proposed") or {}
        chain = rec.get("approval_chain")
        if isinstance(chain, str):
            try:
                chain = _json.loads(chain)
            except Exception:
                chain = []
        if not isinstance(chain, list) or not chain:
            chain = [{"step": 1, "role": "manager", "action": "审批"}]

        user = user or {}
        role = user.get("role", "")
        current_step = int(th.get("current_step", 0) or 0)

        # 完成守卫（W15：admin 豁免不得跳过——已完成的审批链不可重复追加签字）
        if current_step >= len(chain):
            return {"success": False, "completed": False,
                    "error": "审批链已完成，无需重复审批"}

        # 签字链完整性校验（本会话加固）：前序签字数须等于 current_step。
        # 上级审批签字全部到位才允许下级推进——steps_done 丢失/被篡改（或
        # current_step 被直接修改跳级）时拒绝推进，防中间审批被绕过。
        steps_done = th.get("steps_done") or []
        if not isinstance(steps_done, list):
            steps_done = []
        sig_err = validate_sig_chain(steps_done, current_step)
        if sig_err:
            return {"success": False, "completed": False, "error": sig_err}

        # 角色校验（v6.45 与 training approve 端点同规则；admin 代批豁免）
        if role != "admin":
            step_role = chain[current_step].get("role", "")
            if step_role and step_role != role:
                return {"success": False, "completed": False,
                        "error": f"当前步骤需「{step_role}」审批，"
                                 f"您的角色「{role}」无权限"}

        # 签字记录（审批痕迹，流程单据/训练申请单同模板展示"谁在何时审批"；
        # steps_done 已在签字链完整性校验处解析）
        steps_done.append({
            "step": current_step + 1,
            "role": role or (chain[current_step].get("role", "")
                             if current_step < len(chain) else ""),
            "user_id": user.get("id") or user.get("user_id") or "",
            "user_name": user.get("name") or user.get("username") or "",
            "action": "approved",
            "done_at": datetime.now().isoformat(),
        })

        next_step = current_step + 1
        completed = next_step >= len(chain)
        th["current_step"] = next_step
        th["steps_done"] = steps_done
        db.update("workflow_configs",
                  {"thresholds": _json.dumps(th, ensure_ascii=False)},
                  {"config_id": int(config_id)})

        if completed:
            ok = apply_workflow_def_change(
                proposed, db=db,
                modified_by=user.get("id") or user.get("user_id") or "")
            # W6：仅定义写入成功后才归档审批记录——失败保留 is_active 记录，
            # 审批人可重试（原实现无论成败均置 is_active=False，失败后无法重新审批）
            if ok:
                db.update("workflow_configs", {"is_active": False},
                          {"config_id": int(config_id)})
            return {
                "success": ok, "completed": True, "current_step": next_step,
                "chain": chain, "proposed": proposed,
                "steps_done": steps_done,
                "workflow_name": (proposed or {}).get("workflow_name", ""),
                "error": None if ok else "流程定义写入失败（审批记录已保留，可重试）",
            }

        return {
            "success": True, "completed": False, "current_step": next_step,
            "chain": chain, "proposed": proposed,
            "steps_done": steps_done,
            "workflow_name": (proposed or {}).get("workflow_name", ""),
            "error": None,
        }
    except Exception as e:
        return {"success": False, "completed": False, "error": str(e)}
