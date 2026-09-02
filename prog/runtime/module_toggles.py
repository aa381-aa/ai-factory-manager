"""
模块隔离机制
============
设计说明：
    1. module_toggles 为全局模块开关字典，控制各Agent模块的启用/禁用
    2. @require_module 装饰器：模块关闭时自动降级，不执行Agent逻辑
    3. 模块配置从外部 system_configs 表加载（可选依赖），无数据库时使用默认值

开源化说明：
    - 数据库层为可选依赖：无数据库时使用内置默认开关运行。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 模块独立性保障：未开通某一模块时系统仍正常运行，Agent 可独立启停，其缺失不影响其他 Agent 核心功能（SPEC §3.8 模块开关，来源映射 §4.3 模块独立性与容错机制）
        - DEFAULT_TOGGLES 8 个模块默认全部启用（sales/production/warehouse/technical/finance/knowledge_assistant/qc/hr_agent）（SPEC §3.8）
        - 模块配置从 system_configs 表加载（config_key='module_toggles'，config_value 为 JSON，仅更新已知模块且值为 bool），无数据库时使用内置默认开关（SPEC §3.8）
        - 支持运行时动态切换（需审批后生效）（SPEC §3.8）
        - @require_module 装饰器：模块关闭时被装饰方法自动降级返回统一响应（不执行实际 Agent 逻辑）（SPEC §3.8）
    对外接口（方法/API）：
        - ModuleToggleManager.get_instance() -> ModuleToggleManager：单例（SPEC §3.8）
        - ModuleToggleManager.is_enabled(module_name) -> bool：未知模块默认返回 True（SPEC §3.8）
        - ModuleToggleManager.enable(module_name) / disable(module_name)：运行时启用/禁用（SPEC §3.8）
        - ModuleToggleManager.get_all_toggles() -> dict：返回开关字典副本（SPEC §3.8）
        - ModuleToggleManager._load_from_db()：system_configs(config_key='module_toggles') 可选加载（SPEC §3.8）
        - require_module(module_name) -> Callable：模块开关装饰器，模块关闭时返回 AgentResponse(content=f"模块 {module_name} 已关闭，相关功能暂不可用。", action="module_disabled", agent_name=module_name)（SPEC §3.8）
    错误处理要求：
        - 无数据库或查询/解析失败：静默降级，使用内置默认开关运行（SPEC §3.8）
        - 未知模块名：is_enabled 默认返回 True（不阻断）（SPEC §3.8）
"""

import functools
import json
import threading
from typing import Any, Callable, Dict, Optional


# 默认模块开关（所有模块默认启用）
DEFAULT_TOGGLES = {
    "sales_agent": True,
    "production_agent": True,
    "warehouse_agent": True,
    "technical_agent": True,
    "finance_agent": True,
    "knowledge_assistant": True,
    "qc_agent": True,
    "hr_agent": True,
}


class ModuleToggleManager:
    """模块开关管理器（单例）

    管理各Agent模块的启用/禁用状态，
    支持运行时动态切换（需审批后生效）。

    属性：
        _toggles: 当前模块开关字典
    """

    _instance: Optional["ModuleToggleManager"] = None
    # 单例创建锁（保护 _instance 的创建，double-checked locking）
    _singleton_lock = threading.Lock()

    def __init__(self):
        # 模块开关字典操作的锁（保护 _toggles 的并发读写）
        self._toggle_lock = threading.Lock()
        self._toggles: Dict[str, bool] = dict(DEFAULT_TOGGLES)
        self._load_from_db()

    @classmethod
    def get_instance(cls) -> "ModuleToggleManager":
        """获取单例实例

        返回：
            ModuleToggleManager 单例
        """
        # double-checked locking：先无锁检查避免每次获取锁
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_enabled(self, module_name: str) -> bool:
        """检查模块是否启用

        参数：
            module_name: 模块名称（如 "qc_agent"）

        返回：
            True=启用，False=禁用；未知模块默认返回 True
        """
        with self._toggle_lock:
            return self._toggles.get(module_name, True)

    def enable(self, module_name: str) -> None:
        """启用模块

        参数：
            module_name: 模块名称
        """
        with self._toggle_lock:
            self._toggles[module_name] = True

    def disable(self, module_name: str) -> None:
        """禁用模块

        参数：
            module_name: 模块名称
        """
        with self._toggle_lock:
            self._toggles[module_name] = False

    def get_all_toggles(self) -> Dict[str, bool]:
        """获取所有模块开关状态

        返回：
            模块开关字典的副本
        """
        with self._toggle_lock:
            return dict(self._toggles)

    def _load_from_db(self) -> None:
        """从 system_configs 表加载模块开关配置（可选依赖）

        查询 config_key='module_toggles' 的记录，解析 config_value（JSON）
        更新 _toggles。无数据库或查询失败时静默降级，保留默认值。
        """
        try:
            from prog.runtime.database import get_database  # 可选：外部数据库层

            db = get_database()
            row = db.query_one(
                "system_configs", {"config_key": "module_toggles"}
            )
            if not row:
                return
            raw = row.get("config_value")
            if not raw:
                return
            # config_value 为 TEXT，存储 JSON 字符串
            toggles = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(toggles, dict):
                return
            # 仅更新已知模块的开关，确保值为布尔类型（字典更新在锁内完成）
            with self._toggle_lock:
                for key, value in toggles.items():
                    if key in DEFAULT_TOGGLES and isinstance(value, bool):
                        self._toggles[key] = value
        except Exception:
            # 无数据库或解析失败时静默降级，使用默认值
            pass


def require_module(module_name: str) -> Callable:
    """模块开关装饰器（@require_module）

    模块关闭时，被装饰的方法自动降级返回默认响应，
    不执行实际Agent逻辑。

    用法：
        class QCAgent(BaseAgent):
            @require_module("qc_agent")
            def process(self, user_input, context):
                ...

    参数：
        module_name: 模块名称（如 "qc_agent"）

    返回：
        装饰器函数
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            manager = ModuleToggleManager.get_instance()
            if not manager.is_enabled(module_name):
                # 模块关闭，返回降级响应
                from prog.runtime.base_agent import AgentResponse

                return AgentResponse(
                    content=f"模块 {module_name} 已关闭，相关功能暂不可用。",
                    action="module_disabled",
                    agent_name=module_name,
                )
            return func(*args, **kwargs)

        return wrapper

    return decorator
