"""
规则注册表（规则引擎核心）
==========================
文件用途：
    定义 BaseRule 基类、RuleResult 结果结构与 RuleRegistry 规则注册表，
    实现规则注册模式：每个 Agent 拥有自己适用的规则集，运行时按需加载。

对应技术规格章节（原项目引用）：
    - §2.1  规则注册模式（规则校验层 rule_validation 依赖）
    - §2.2  RBAC+ABAC（规则按角色/属性生效）

设计说明：
    1. BaseRule 为所有业务规则的抽象基类，子类实现 check() 返回 RuleResult。
    2. RuleResult 携带 pass/warn/block 三态、违规原因与审批要求。
    3. RuleRegistry 维护「Agent 类型 -> 规则列表」映射，支持按 Agent 取规则集。
    4. 硬规则（hard=True）返回 block 时不可被任何角色 override（bypass=false）。

开源化说明：
    - 进程级缓存（ProcessLevelCache）原由 prog/rules/config_manager.py 提供，
      该模块不属于开源框架范围。此处提供内置最小实现作为降级；
      使用者可通过 runtime.config_manager 扩展更完整的实现。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 规则注册模式：每个 Agent 拥有适用规则集，运行时按需加载；RuleRegistry 维护「Agent 类型 -> 规则列表」映射（来源：SPEC §3.3 / 模块拆分方案 契约4）
        - RuleResult 三态结果结构（pass/warn/block）+ blocked/passed 属性 + to_dict（来源：SPEC §3.3 / 模块拆分方案 契约4）
        - BaseRule 抽象基类可训练性：rule_type 区分 hard_logic/parameter/approval_chain，参数经 load_config_from_db 从 business_rules 表加载（来源：SPEC §3.3）
        - 硬规则（bypass=false）不可绕过：is_hard=True 的 block 不可被任何角色 override（来源：SPEC §3.3 / 模块拆分方案 契约4）
        - 进程级缓存：所有 BaseRule 实例共享 _process_cache（内置最小实现降级），训练更新参数后 clear_config_cache 失效（来源：SPEC §3.3.2）
    对外接口（方法/API）：
        - RuleResult(status='pass', rule_name='', message='', requires_approval=False, approver_role=None, is_hard=False, extra=None)：规则校验结果（来源：SPEC §3.3）
        - BaseRule.check(*args, **kwargs)：执行规则校验（子类必须实现），返回 RuleResult（来源：SPEC §3.3）
        - BaseRule.load_config_from_db(rule_id=None)：加载优先级=进程级缓存 -> 实例级缓存（校验缓存代次）-> business_rules 表，失败返回 {}（来源：SPEC §3.3.2）
        - BaseRule.is_trainable：rule_type 为 parameter/approval_chain 时可训练（来源：SPEC §3.3）
        - RuleRegistry.register(agent_type, rule)：注册规则（同规则对同 Agent 去重）（来源：SPEC §3.3.3）
        - RuleRegistry.get_rule(rule_name) / get_all_rules() / get_rules_for_agent(agent_type)：按名获取 / 去重全部 / 按 Agent 取规则集（来源：SPEC §3.3.3）
        - RuleRegistry.get_shared() / reset_shared()：进程级共享注册表单例（业务侧模块加载时注册全部规则）（来源：模块拆分方案 契约4）
    错误处理要求：
        - 规则不存在或执行异常：BaseAgent._apply_rules 按规则名逐条执行，缺失/异常时跳过，不阻断其余规则（来源：SPEC §3.1.2）
        - 规则引擎不可用时：返回默认通过（_PassRuleResult），避免阻断业务流程（来源：SPEC §3.1.2）
        - 数据库查询失败/无数据库：load_config_from_db 返回 {}，子类提供降级默认值（来源：SPEC §3.3.2）
"""

import json
import threading
from typing import Optional, List, Dict, Any, Tuple


class _BuiltinProcessLevelCache:
    """内置最小版进程级缓存（降级实现）

    所有 BaseRule 实例共享同一份配置缓存，减少重复查询。
    提供 get / set / invalidate 接口，与 config_manager.ProcessLevelCache 兼容。
    """

    _cache: Dict[str, dict] = {}
    _cache_versions: Dict[str, int] = {}
    _hits: int = 0
    _misses: int = 0
    _lock = threading.Lock()

    def get(self, rule_id: str) -> Tuple[dict, bool]:
        """获取缓存：命中返回 (config, True)，未命中返回 ({}, False)"""
        with self._lock:
            if rule_id in self._cache:
                self._hits += 1
                return self._cache[rule_id], True
            self._misses += 1
            return {}, False

    def set(self, rule_id: str, config: dict) -> None:
        """设置缓存"""
        with self._lock:
            self._cache[rule_id] = config
            self._cache_versions[rule_id] = self._cache_versions.get(rule_id, 0) + 1

    def invalidate(self, rule_id: str = None) -> None:
        """失效缓存：rule_id 为 None 时清空全部"""
        with self._lock:
            if rule_id is None:
                self._cache.clear()
                self._cache_versions.clear()
            else:
                self._cache.pop(rule_id, None)
                self._cache_versions.pop(rule_id, None)

    @classmethod
    def get_stats(cls) -> dict:
        """获取缓存统计"""
        with cls._lock:
            total = cls._hits + cls._misses
            hit_rate = round(cls._hits / total, 4) if total > 0 else 0.0
            return {
                "hits": cls._hits,
                "misses": cls._misses,
                "hit_rate": hit_rate,
                "size": len(cls._cache),
                "versions": dict(cls._cache_versions),
            }


def _create_process_cache() -> Optional[Any]:
    """创建进程级缓存实例（优先外部扩展，降级内置最小实现）"""
    try:
        from prog.runtime.config_manager import ProcessLevelCache  # 可选：外部扩展实现
        return ProcessLevelCache()
    except ImportError:
        return _BuiltinProcessLevelCache()


class RuleResult:
    """规则校验结果

    所有业务规则 check() 的统一返回类型。

    属性说明：
        status      : 结果状态枚举
                      - "pass"   : 通过
                      - "warn"   : 警告（需审批但可继续）
                      - "block"  : 阻断（不可继续，硬规则触发）
        rule_name   : 触发规则名（用于审计记录）
        message     : 人可读的违规/通过说明
        requires_approval : 是否需要上级审批（warn 态常用）
        approver_role: 需要审批的角色（如 "manager"/"general_manager"）
        is_hard     : 是否硬规则（block 不可 override）
        extra       : 附加数据（如成本明细、信用余额等）
    """

    # 状态常量
    STATUS_PASS = "pass"
    STATUS_WARN = "warn"
    STATUS_BLOCK = "block"

    def __init__(self, status: str = "pass", rule_name: str = "",
                 message: str = "", requires_approval: bool = False,
                 approver_role: str = None, is_hard: bool = False,
                 extra: dict = None):
        """初始化规则结果"""
        self.status = status
        self.rule_name = rule_name
        self.message = message
        self.requires_approval = requires_approval
        self.approver_role = approver_role
        self.is_hard = is_hard
        self.extra = extra or {}

    @property
    def passed(self) -> bool:
        """是否通过（status == pass）"""
        return self.status == self.STATUS_PASS

    @property
    def blocked(self) -> bool:
        """是否阻断（status == block）"""
        return self.status == self.STATUS_BLOCK

    def to_dict(self) -> dict:
        """转换为字典（供审计日志 output_data 使用）"""
        return {
            "status": self.status,
            "rule_name": self.rule_name,
            "message": self.message,
            "requires_approval": self.requires_approval,
            "approver_role": self.approver_role,
            "is_hard": self.is_hard,
            "extra": self.extra,
        }


class BaseRule:
    """业务规则抽象基类

    所有具体规则继承本类并实现 check()。
    子类应在 __init__ 中设置 rule_name 与 is_hard 属性。

    类属性：
        rule_name   : 规则名（唯一标识，用于注册与审计）
        is_hard     : 是否硬规则（True 时 block 不可 override，bypass=false）
        spec_ref    : 对应技术规格章节引用
        rule_type   : 规则类型（"hard_logic" 不可变 / "parameter" 可训练参数 / "approval_chain" 审批链）
        rule_id     : 规则ID（用于从数据库加载可训练参数，可选用）
    """

    rule_name: str = "base_rule"
    is_hard: bool = False
    spec_ref: str = ""
    rule_type: str = "hard_logic"
    rule_id: str = ""
    # 进程级缓存引用（由框架初始化，所有 BaseRule 实例共享）
    _process_cache = None
    # 缓存代次（全局失效时递增，用于判断实例缓存是否过期）
    _cache_generation = 0

    def __init__(self):
        """初始化规则，子类应调用 super().__init__()"""
        self._cached_config: dict = None
        # 自动初始化进程级缓存（如果尚未初始化）
        if BaseRule._process_cache is None:
            try:
                BaseRule._process_cache = _create_process_cache()
            except ImportError:
                pass

    @property
    def is_trainable(self) -> bool:
        """是否可训练（rule_type 为 parameter 或 approval_chain 时可训练）"""
        return self.rule_type in ("parameter", "approval_chain")

    def check(self, *args, **kwargs) -> RuleResult:
        """执行规则校验（子类必须实现）

        Returns:
            RuleResult: 校验结果
        """
        raise NotImplementedError("子类必须实现 check() 方法")

    def load_config_from_db(self, rule_id: str = None) -> dict:
        """从规则配置表加载规则配置

        子类通过此方法从数据库读取可训练参数，替代硬编码。
        查询失败时返回空字典，子类应提供降级默认值。

        加载优先级：
            1. 进程级缓存（所有实例共享）
            2. 实例级缓存（_cached_config，单实例内有效）
            3. 数据库查询（命中后写入进程级缓存与实例缓存）

        Args:
            rule_id: 规则ID，默认使用 self.rule_id

        Returns:
            dict: 配置字典，查询失败返回 {}
        """
        rid = rule_id or self.rule_id
        if not rid:
            return {}

        # 1. 优先从进程级缓存获取
        if BaseRule._process_cache is not None:
            config, hit = BaseRule._process_cache.get(rid)
            if hit:
                # 同步到实例缓存
                self._cached_config = config
                self._cache_gen = BaseRule._cache_generation
                return config

        # 2. fallback 到实例缓存（检查代次一致性，避免使用过期缓存）
        if (self._cached_config is not None
                and getattr(self, "_cache_gen", 0) == BaseRule._cache_generation):
            return self._cached_config

        # 3. 查询数据库（可选依赖：无数据库层时跳过，返回默认配置）
        try:
            from prog.runtime.database import get_database  # 可选：外部数据库层
            db = get_database()
            row = db.query_one("business_rules", {"rule_id": rid})
            if row and row.get("config_json"):
                config = row["config_json"]
                if isinstance(config, str):
                    config = json.loads(config)
                # 写入进程级缓存（供其他实例共享）
                if BaseRule._process_cache is not None:
                    BaseRule._process_cache.set(rid, config)
                # 写入实例缓存
                self._cached_config = config
                self._cache_gen = BaseRule._cache_generation
                return config
        except Exception:
            pass

        self._cached_config = {}
        self._cache_gen = BaseRule._cache_generation
        return {}

    def clear_config_cache(self):
        """清除配置缓存（训练系统更新参数后调用）"""
        self._cached_config = None


class RuleRegistry:
    """规则注册表

    实现「Agent 类型 -> 适用规则列表」的注册与发现机制。
    每个 Agent 启动时向注册表注册其适用的规则，运行时按 Agent 取规则集
    顺序执行。

    设计意图：
        - 不同 Agent 适用不同规则集，避免全量加载。
        - 规则执行顺序由注册顺序决定（硬规则优先）。
        - 支持动态注册，便于扩展新规则。
    """

    # 模块级共享注册表（业务侧将全部规则注册到同一实例，_apply_rules 据此查找）
    _shared = None
    # 共享注册表创建/重置锁（保护 _shared，double-checked locking）
    _shared_lock = threading.Lock()

    def __init__(self):
        """初始化注册表

        内部维护 _rules: Dict[agent_type, List[BaseRule]] 与 _rule_map: Dict[rule_name, BaseRule]。
        """
        self._rules: Dict[str, List[BaseRule]] = {}
        self._rule_map: Dict[str, BaseRule] = {}
        # 注册表操作锁（保护 _rules / _rule_map 的并发读写）
        self._registry_lock = threading.Lock()

    @classmethod
    def get_shared(cls) -> "RuleRegistry":
        """获取进程级共享注册表实例（所有 Agent / _apply_rules 共用同一注册表）。

        业务侧在模块加载时通过 register() 将全部规则注册进共享实例，
        使 BaseAgent._apply_rules() 能按规则名找到规则并读取 engine_steps。
        """
        # double-checked locking：先无锁检查避免每次获取锁
        if cls._shared is None:
            with cls._shared_lock:
                if cls._shared is None:
                    cls._shared = cls()
        return cls._shared

    @classmethod
    def reset_shared(cls) -> None:
        """重置共享注册表（测试隔离用）。"""
        with cls._shared_lock:
            cls._shared = None

    def register(self, agent_type: str, rule: BaseRule) -> None:
        """为指定 Agent 类型注册一个规则

        Args:
            agent_type: Agent 类型标识（如 "sales_agent"）
            rule: BaseRule 子类实例
        """
        with self._registry_lock:
            if agent_type not in self._rules:
                self._rules[agent_type] = []
            # 避免同一规则重复注册到同一 Agent
            existing_names = {r.rule_name for r in self._rules[agent_type]}
            if rule.rule_name not in existing_names:
                self._rules[agent_type].append(rule)
            self._rule_map[rule.rule_name] = rule

    def get_rule(self, rule_name: str) -> BaseRule:
        """按规则名获取规则实例

        Args:
            rule_name: 规则名

        Returns:
            BaseRule | None: 找到返回实例，否则 None
        """
        return self._rule_map.get(rule_name)

    def get_all_rules(self) -> list:
        """获取全部已注册规则（去重）

        Returns:
            list[BaseRule]: 全部规则实例
        """
        with self._registry_lock:
            seen = set()
            result = []
            for rules in self._rules.values():
                for rule in rules:
                    if rule.rule_name not in seen:
                        seen.add(rule.rule_name)
                        result.append(rule)
            return result

    def get_rules_for_agent(self, agent_type: str) -> list:
        """获取指定 Agent 类型的适用规则集

        Agent 调用规则引擎时的主入口，返回按注册顺序排列的规则列表。

        Args:
            agent_type: Agent 类型标识

        Returns:
            list[BaseRule]: 该 Agent 的规则列表
        """
        return list(self._rules.get(agent_type, []))
