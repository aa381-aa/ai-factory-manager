"""
AI工厂管家 Runtime（AI Factory Manager）· 规则驱动的业务场景 Agent 运行时
=====================================================
自研轻量级 Agent 运行时框架，无第三方 Agent 框架依赖（LangChain/LangGraph/AutoGen 等），
面向制造业业务规则驱动场景。「AI工厂管家」以业务规则为纲，约束并驱动 Agent 行为，
让智能体像工厂里各司其职的岗位一样照章办事。原名 AI-Factory Agent Runtime。

本框架提取自「艾诺威·AI工厂管家」项目，仅包含通用框架能力，
不含具体业务逻辑与商业秘密。许可协议：Apache 2.0。

模块一览：
    - base_agent         : Agent 基类 + AgentResponse 统一响应契约
    - auth               : 认证能力（TokenSigner/Authenticator/MockUserSource，v1.3 提取）
    - coordinator        : 协调器（意图识别 -> 路由 -> 上下文隔离 -> 分发 -> 聚合）
    - rule_registry      : 规则引擎（RuleResult/BaseRule/RuleRegistry，bypass=false 硬规则）
    - rule_engine        : 数据驱动规则引擎（RuleEngine + 安全沙箱 DSL，v1.6.25 新增）
    - permission         : 权限系统（RBAC）
    - module_toggles     : 模块开关 + @require_module 装饰器
    - workflow_enforcer  : 流程约束引擎（三道校验 + 实例化）
    - intent_recognition : 意图识别
    - trace              : 统一追踪上下文（contextvars，v1.1 提取）
    - session_manager    : 会话管理 + token 窗口裁剪（v1.2 提取）
    - logger             : 结构化日志（JSON Lines + 脱敏，v1.2 提取）
    - debug              : DEBUG 开关与自检工具（v1.2 提取）
    - cache              : 缓存管理器（Redis/内存降级，v1.2 提取）
    - event_bus          : 事件总线（Redis Streams/内存降级，v1.2 提取）
    - file_storage       : 文件存储（S3/本地降级，v1.2 提取）
    - streaming          : SSE 流式输出（v1.2 提取）
"""

from prog.runtime.base_agent import BaseAgent, AgentResponse
from prog.runtime.coordinator import CoordinatorAgent
from prog.runtime.auth import Authenticator, TokenSigner, MockUserSource, verify_password
from prog.runtime.rule_registry import BaseRule, RuleRegistry, RuleResult
from prog.runtime.rule_engine import RuleEngine
from prog.runtime.permission import PermissionSystem
from prog.runtime.module_toggles import ModuleToggleManager, require_module
from prog.runtime.workflow_enforcer import WorkflowEnforcer
from prog.runtime.intent_recognition import IntentRecognizer
from prog.runtime.session_manager import SessionManager
from prog.runtime.logger import Logger
from prog.runtime.debug import DEBUG, set_debug, hello_world
from prog.runtime.cache import CacheManager, get_cache
from prog.runtime.event_bus import EventBus, RedisStreamBus, create_event_bus, get_event_bus
from prog.runtime.file_storage import FileStorageBase, S3Storage, get_file_storage
from prog.runtime.streaming import StreamingResponse, SSEHelper, create_streaming_response

__version__ = "1.6.64"  # 社区版：基于框架 1.6.64 精简，仅保留通用基础能力
__all__ = [
    "BaseAgent", "AgentResponse",
    "CoordinatorAgent",
    "Authenticator", "TokenSigner", "MockUserSource", "verify_password",
    "BaseRule", "RuleRegistry", "RuleResult",
    "RuleEngine",
    "PermissionSystem",
    "ModuleToggleManager", "require_module",
    "WorkflowEnforcer",
    "IntentRecognizer",
    "SessionManager",
    "Logger",
    "DEBUG", "set_debug", "hello_world",
    "CacheManager", "get_cache",
    "EventBus", "RedisStreamBus", "create_event_bus", "get_event_bus",
    "FileStorageBase", "S3Storage", "get_file_storage",
    "StreamingResponse", "SSEHelper", "create_streaming_response",
]
