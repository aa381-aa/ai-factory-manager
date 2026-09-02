"""
LLM 引擎模块
============

模块用途：
    AI工厂管家的LLM生成引擎层，承载提示词构建、LLM调用、
    安全门控、流式输出、知识库管理等核心能力。

技术规格章节：
    - §2 LLM安全门控（5道门控）
    - §3.7 Knowledge Assistant（知识库RAG）
    - 双通道架构中两通道共用的LLM基础设施

替代demo：
    替代 demo/llm_engine.py（810行）的全部职责，拆分为：
    - engine.py: 生成引擎 + 5道安全门控
      （替代 generate_with_llm / generate_stream_with_llm / call_llm）
    - prompt_builder.py: 动态提示词构建
      （替代 build_system_prompt）
    - streaming.py: SSE流式生成
      （替代 generate_stream_with_llm 的流式部分）
    - knowledge_base.py: 企业知识库管理
      （替代 build_enterprise_knowledge 的硬编码知识）

架构说明：
    本模块为所有Agent提供统一的LLM调用基础设施，Agent自身不直接
    持有API密钥或HTTP客户端，全部通过 LLMEngine 间接调用。
    5道安全门控在 LLMEngine 内统一执行，确保任何Agent都无法绕过。
"""

# LLM模块公开的对外接口（具体实现见各子模块）
# from .engine import LLMEngine, SafetyResult
# from .prompt_builder import PromptBuilder
# from .streaming import StreamingGenerator
# from .knowledge_base import KnowledgeBase, KnowledgeChunk

__all__ = [
    # "LLMEngine",
    # "SafetyResult",
    # "PromptBuilder",
    # "StreamingGenerator",
    # "KnowledgeBase",
    # "KnowledgeChunk",
]
