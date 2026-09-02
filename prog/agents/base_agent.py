"""
Agent 基类（业务软件层 re-export）
==================================
框架能力：Agent 统一生命周期（process -> 构建提示词 -> LLM调用 -> 规则校验 ->
格式化响应）与 AgentResponse 统一响应契约由AI工厂管家框架运行时（prog/runtime）提供。
本文件仅作 re-export，业务 Agent 通过
`from prog.agents.base_agent import BaseAgent, AgentResponse` 继承使用。
"""
from prog.runtime.base_agent import AgentResponse, BaseAgent

# 开源版：业务规则包（prog.rules，商业 know-how）不在开源范围内，
# 无规则包时跳过规则注册，Agent 仍可运行（规则校验空转）。
try:
    from prog.rules import register_all_rules  # noqa: E402
    register_all_rules()
except Exception:
    pass

__all__ = ["BaseAgent", "AgentResponse"]
