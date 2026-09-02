from __future__ import annotations

"""
KnowledgeAssistant 知识助手模块
==============================

文件用途：
    实现企业管理知识库的RAG问答Agent，承载管理咨询通道的全部交互。

技术规格章节：
    - §3.7 Knowledge Assistant（P2优先级）
    - 双通道架构：管理咨询通道（Milvus RAG）

替代demo：
    替代 demo/llm_engine.py 中 build_enterprise_knowledge() 的硬编码知识库。
    demo将企业管理知识以字符串硬编码拼接进系统提示词，本模块改为：
    知识文档 -> 向量化 -> Milvus存储 -> 语义检索 -> RAG提示词构建。

核心能力：
    1. 企业管理知识库RAG问答：
       用户提问 -> 向量检索相关知识片段 -> 注入提示词 -> LLM生成答案
    2. 制度查询：
       管理制度、流程规范、岗位职责等结构化查询
    3. 流程指导：
       操作指引、审批流程、表单填写说明等

通道区别（重要）：
    - 业务操作通道（其他Agent）：操作PostgreSQL，产生业务数据变更
    - 管理咨询通道（本Agent）：只读检索Milvus，不产生业务变更，
      仅回答管理类问题

依赖组件：
    - core/vector_store.py: Milvus向量库访问（Collection: ai_factory_kb）
    - core/embedding_provider.py: 文本向量化（embedding模型）
    - prog/llm/knowledge_base.py: 知识库管理（文档增删改查）

功能清单（规格/变更对照）：
    应实现功能（能力编号 → 规格书章节）：
        - K-01 咨询类问题识别与业务查询区分：is_consultation_query 双通道路由（规格书 §3.7.1）
        - K-02 知识文档存储与向量检索：RAG 检索 + 关键词检索兜底（规格书 §3.7.1；v6.26 中文分词修复）
        - K-03 基于上下文主动延伸回答问题：延伸阅读推荐（规格书 §3.7.1）
        - K-04 咨询场景中自然推荐产品：产品推荐链接（规格书 §3.7.1）
        - K-05 知识文档上传与自动向量化：upload_document（规格书 §3.7.1）
        - K-06 高频问题统计与知识缺口识别：analyze_knowledge_gaps（规格书 §3.7.1）
        - K-07 组织架构查询：已迁移至 §3.9 人力资源Agent HR-09，本模块不再承担（规格书 §3.7.1）
    子意图分发：
        - knowledge_query：_handle_knowledge_query —— RAG 问答（INT-13 知识查询，规格书 §A.8.2）
        - policy_consultation：_handle_policy_consultation —— 制度咨询（INT-11 管理咨询，规格书 §A.8.2）
        - process_guide：_handle_process_guide —— 流程指导/流程字段收集（INT-11，规格书 §A.8.2；INT-28 流程启动）
        - save_to_kb：_handle_save_to_kb —— 兜底回答「录入」入库（K-05，规格书 §3.7.1；v6.79 会话聚合）
        - kb_gap_analysis：_handle_kb_gap_analysis —— 知识缺口分析（K-06，规格书 §3.7.1；v6.63 补挂载）
        - greeting/thanks/farewell：_respond_chitchat —— 闲聊引导（INT-14，规格书 §A.8.3）
        - web_search（联网检索指令）：_web_search + _build_web_search_reply —— 真实执行必应检索直接展示（v6.87，规格书版本日志未收录）
        - workflow_query：_handle_workflow_query —— 流程实例查询（INT-31，规格书 §A.8.3；v6.60）
        - workflow_train：_handle_workflow_train —— 流程定义训练（INT-32，规格书 §A.8.3；v6.61）
        - query_flow：_handle_query_flow —— 查询流程编排（INT-33 analysis_query 等，规格书 §A.8.2；v6.64/v6.80）
    对外接口（方法/API）：
        - KnowledgeAssistant.process(user_input, context)：主处理入口 —— 按子意图分发（契约 1，模块拆分方案）
        - KnowledgeAssistant.process_stream(user_input, context)：流式处理 —— SSE 流式问答（v6.24）
        - KnowledgeAssistant.is_consultation_query(user_input)：K-01 双通道路由判断（规格书 §3.7.1）
        - KnowledgeAssistant.upload_document(title, content, source="", category="", tags=None)：K-05 文档上传向量化（规格书 §3.7.1）
        - KnowledgeAssistant.analyze_knowledge_gaps(conversations=None)：K-06 知识缺口分析（规格书 §3.7.1）
        - KnowledgeAssistant.render_training_doc(cfg_id, proposed, ...)：训练申请单渲染（v6.61，coordinator 审批推进后调用）
    错误处理要求：
        - 知识库未命中：LLM 自身知识回答 + 「企业知识库暂无此内容」标注 + 可选联网兜底（KB_WEB_SEARCH_ENABLED）+ 提示录入（v6.25/v6.83，规格书 §3.7.2）
        - 联网检索指令缺话题：引导用户提供具体内容，不退化到 LLM 误答"没有联网能力"（v6.87.1）
        - 能力疑问句（"你能联网吗"）：不触发真实检索，走常规知识问答（v6.87.1）
        - 查询流程缺参数：步骤跳过并标注"⚠️ 缺少查询参数"，不报错中断（v6.65.1，规格书版本日志）
"""

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from prog.agents.base_agent import BaseAgent, AgentResponse

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from prog.llm.knowledge_base import KnowledgeChunk


def _rag_stopwords() -> set:
    """知识检索停用词表（v6.31 从 system_configs.KEYWORD_STOPWORDS 读取，可配置扩展）。"""
    try:
        from prog.runtime.database import get_database
        import json
        db = get_database()
        row = db.query_one("system_configs", {"config_key": "KEYWORD_STOPWORDS"})
        if row and row.get("config_value"):
            val = json.loads(row["config_value"])
            if isinstance(val, list) and val:
                return set(val)
    except Exception:
        pass
    return {"查询", "查一下", "查看", "请问", "什么是", "什么", "怎么",
            "如何", "帮我", "一下", "知识库", "信息", "相关",
            "哪些", "怎样", "怎么样"}


class KnowledgeAssistant(BaseAgent):
    """
    知识助手Agent（§3.7，P2优先级）。

    设计意图：
        作为管理咨询通道的唯一入口，通过RAG检索企业知识库回答管理类问题。
        不操作业务数据库，仅做只读检索。

    替代demo：
        替代 demo/llm_engine.py build_enterprise_knowledge() 的硬编码知识。
        将静态知识字符串升级为可动态维护的向量知识库。

    属性：
        agent_name: "知识助手"
        agent_type: "knowledge"
        applicable_rules: []（知识助手不执行业务规则）
        vector_store: Milvus向量库访问实例
        embedding_provider: 文本向量化提供方
        llm_provider: LLM提供方（用于RAG答案生成）
        knowledge_base: 知识库管理实例（文档增删改查）

    处理流程（process）：
        1. 识别子意图（知识查询/制度咨询/流程指导）
        2. 路由到对应 _handle_xxx 方法
        3. 处理器内：RAG检索 -> 构建提示词 -> LLM生成 -> 返回答案+引用
    """

    def __init__(self, vector_store: Any = None,
                 embedding_provider: Any = None,
                 llm_provider: Any = None,
                 knowledge_base: Any = None,
                 web_search_enabled: bool = False):
        """
        初始化知识助手。

        参数：
            vector_store: Milvus向量库访问实例
            embedding_provider: 文本向量化提供方
            llm_provider: LLM提供方
            knowledge_base: 知识库管理实例
            web_search_enabled: 是否启用联网检索兜底（默认关闭，开启后知识库未命中时先联网检索）
        """
        super().__init__(
            agent_name="知识助手",
            agent_type="knowledge",
            llm_provider=llm_provider,
            database=None,  # 知识助手不操作业务数据库
        )
        self.applicable_rules: List[str] = []  # 知识问答无业务规则约束
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.knowledge_base = knowledge_base
        self.web_search_enabled = web_search_enabled
        # 会话级"最近一次知识库外回答"：用户在兜底回答后回复「录入」时入库
        self._last_qa: Dict[str, Dict[str, Any]] = {}
        # v6.30：注入知识库实例时加载已入库文档（图纸/工艺/训练内容等），
        # 使训练内容跨进程可被 RAG 检索（辅助质量分析、流程查询等）
        if knowledge_base is not None:
            try:
                knowledge_base.load_from_db()
            except Exception:
                pass

    # --------------------------------------------------------
    # 主处理入口
    # --------------------------------------------------------
    def process(self, user_input: str, context: Dict[str, Any]) -> AgentResponse:
        """
        知识助手主处理入口。

        设计意图：
            识别子意图后路由到对应处理器，执行RAG问答流程：
            检索 -> 构建提示词 -> LLM生成 -> 返回答案+引用。
            作为CoordinatorAgent在管理咨询通道的兜底Agent，
            意图识别失败时也会回退到本Agent。

        参数：
            user_input: 用户提问（如"报销流程是怎样的"）
            context: 会话上下文

        返回：
            AgentResponse: 包含答案文本与引用来源的响应

        分发逻辑：
            - knowledge_query      -> _handle_knowledge_query
            - policy_consultation  -> _handle_policy_consultation
            - process_guide        -> _handle_process_guide
        """
        start_time = time.time()
        # v6.45：coordinator 已启动流程实例（workflow_start）时，
        # 无论子意图为何均优先进入流程字段收集（多轮引导补全必填字段）
        wf_instance = context.get("workflow_instance")
        if wf_instance and isinstance(wf_instance, dict):
            response = self._handle_workflow_field_collection(
                user_input, context, wf_instance)
            elapsed = round((time.time() - start_time) * 1000, 2)
            response.metadata["elapsed_ms"] = elapsed
            response.metadata["sub_intent"] = "process_guide"
            return response

        # v6.46.1：发起流程但未匹配到流程定义（coordinator 标记）-> 提示新建
        if context.get("workflow_start_failed"):
            response = self._handle_workflow_not_found(user_input, context)
            elapsed = round((time.time() - start_time) * 1000, 2)
            response.metadata["elapsed_ms"] = elapsed
            response.metadata["sub_intent"] = "process_guide"
            return response

        # v6.60：流程实例查询（coordinator 已识别 workflow_query）——
        # 查看既有单据/进度（报销单样式 HTML），不进入知识检索/LLM，
        # 避免"显示刚才报销流程内容"被误当发起新流程或走慢速 LLM 兜底
        if context.get("intent") == "workflow_query":
            response = self._handle_workflow_query(user_input, context)
            elapsed = round((time.time() - start_time) * 1000, 2)
            response.metadata["elapsed_ms"] = elapsed
            response.metadata["sub_intent"] = "workflow_query"
            return response

        # v6.61：流程定义训练申请（coordinator 已识别 workflow_train）——
        # 文本描述/PDF 附件提取流程定义 → 提交训练审批 → 报销单样式 HTML 申请单
        if context.get("intent") == "workflow_train":
            response = self._handle_workflow_train(user_input, context)
            elapsed = round((time.time() - start_time) * 1000, 2)
            response.metadata["elapsed_ms"] = elapsed
            response.metadata["sub_intent"] = "workflow_train"
            # training_id 提升至 metadata（coordinator 据此挂起 workflow_train pending）
            _tid = (response.data or {}).get("training_id")
            if _tid:
                response.metadata["training_id"] = _tid
            return response

        # v6.64 查询流程（协调器注入 query_flow）：按流程定义 gate_checks.
        # query_steps 编排执行多步骤查库/知识库/网络/LLM 生成，返回结果卡片
        if context.get("query_flow"):
            response = self._handle_query_flow(user_input, context)
            elapsed = round((time.time() - start_time) * 1000, 2)
            response.metadata["elapsed_ms"] = elapsed
            response.metadata["sub_intent"] = "query_flow"
            return response

        # 从上下文中获取子意图，或通过输入识别
        sub_intent = context.get("sub_intent", "")
        if not sub_intent:
            sub_intent = self._recognize_sub_intent(user_input)

        # 分发到对应处理器
        if sub_intent in ("greeting", "thanks", "farewell"):
            # INT-14 闲聊：礼貌回应并引导，不进知识库检索
            response = self._respond_chitchat(sub_intent)
        elif sub_intent == "save_to_kb":
            # 兜底回答后的「录入」指令：将最近一次知识库外问答写入知识库
            response = self._handle_save_to_kb(user_input, context)
        elif sub_intent == "policy_consultation":
            response = self._handle_policy_consultation(user_input, context)
        elif sub_intent == "process_guide":
            response = self._handle_process_guide(user_input, context)
        elif sub_intent == "kb_gap_analysis":
            # K-06 高频问题统计与知识缺口识别（v6.63 补挂载）
            response = self._handle_kb_gap_analysis(user_input, context)
        else:
            # 默认走知识查询（兜底）
            response = self._handle_knowledge_query(user_input, context)

        # 记录耗时
        elapsed = round((time.time() - start_time) * 1000, 2)
        response.metadata["elapsed_ms"] = elapsed
        response.metadata["sub_intent"] = sub_intent or "knowledge_query"
        return response

    def process_stream(self, user_input: str, context: Dict[str, Any]):
        """
        流式处理知识查询，逐块 yield 内容文本。

        设计意图：
            与 process() 功能一致，但使用 LLM 流式 API，
            让用户在 LLM 生成过程中即可看到逐字输出。

        参数：
            user_input: 用户提问
            context: 会话上下文

        返回：
            generator: 逐块产出文本内容
        """
        sub_intent = self._recognize_sub_intent(user_input)

        # 闲聊类：直接 yield 完整回复
        if sub_intent in ("greeting", "thanks", "farewell"):
            response = self._respond_chitchat(sub_intent)
            yield ("content", response.content)
            return

        # 「录入」指令：将最近一次知识库外问答写入知识库
        if sub_intent == "save_to_kb":
            response = self._handle_save_to_kb(user_input, context)
            yield ("content", response.content)
            return

        # v6.87：联网检索指令（如"联网查找下/网上查下X"）→ 真实执行必应检索
        # 并直接展示结果（零LLM干预，防止误答"没有联网能力"）；
        # 空串表示指令确认但缺话题，引导用户提供具体内容
        _web_query = self._resolve_web_query(user_input, context)
        if _web_query is not None:
            if _web_query:
                _web_res = self._web_search(_web_query)
                content = self._build_web_search_reply(_web_query, _web_res)
                # v7.08：联网检索结果同样支持「录入」——记录待录入内容并提示，
                # 使知乎/全网检索到的知识可沉淀进企业知识库
                session_id = (context or {}).get("session_id", "")
                if session_id and _web_res:
                    self._last_qa[session_id] = {
                        "title": _web_query,
                        "content": content,
                        "source": "联网检索（对话录入）",
                        "category": "联网问答",
                    }
                    content += ("\n\n📥 本次联网检索结果尚未录入知识库，"
                                "是否需要将本次问答录入知识库？回复「录入」即可保存。")
                yield ("content", content)
            else:
                yield ("content", "好的，请告诉我要联网查询的具体内容或话题，例如：\"查一下精益生产的定义\"。")
            return

        # RAG 检索 + 构建提示词（即时）
        contexts = self._rag_search(user_input)

        # 知识库未命中兜底：联网检索（可选）+ LLM 自身知识回答 + 提示录入
        if not contexts:
            web_results = self._web_search(user_input)
            prompt = self._build_kb_gap_prompt(user_input, web_results)

            llm_streamed = False
            collected: List[str] = []
            if self.llm_provider is not None:
                stream_method = getattr(self.llm_provider, "_stream_llm_api", None)
                is_messages_api = False
                if stream_method is None:
                    stream_method = getattr(self.llm_provider, "stream_chat", None)
                    is_messages_api = stream_method is not None
                if stream_method is None:
                    stream_method = getattr(self.llm_provider, "stream", None)
                if stream_method is not None:
                    try:
                        stream_args = ([{"role": "user", "content": prompt}],) if is_messages_api else (prompt,)
                        for chunk in stream_method(*stream_args):
                            if isinstance(chunk, tuple):
                                yield chunk
                                llm_streamed = True
                                if chunk[0] == "content":
                                    collected.append(chunk[1])
                            elif isinstance(chunk, dict):
                                reasoning = chunk.get("reasoning", "")
                                if reasoning:
                                    yield ("reasoning", reasoning)
                                content = chunk.get("content", "") or chunk.get("delta", "")
                                if content:
                                    yield ("content", content)
                                    collected.append(content)
                                llm_streamed = True
                            elif chunk:
                                yield ("content", chunk)
                                collected.append(chunk)
                                llm_streamed = True
                    except Exception:
                        pass

            if llm_streamed:
                answer = "".join(collected)
                if not answer:
                    # 统一记录条件（与 _handle_knowledge_query 同步路径一致）：
                    # 流式未产出内容时回退兜底回答，保证「📥 提示录入」与可录入
                    # 内容始终一致，避免提示可录入但回复「录入」时"暂无可录入的内容"。
                    answer = self._build_fallback_answer(user_input, [])
                    yield ("content", answer)
            else:
                llm_output = self._call_llm(prompt)
                answer = llm_output or self._build_fallback_answer(user_input, [])
                yield ("content", answer)

            # v6.83：联网兜底回答尾部标注可点击的参考来源
            if web_results:
                yield ("content", self._build_source_footer(web_results))

            # 记录待录入内容（知识库外回答）
            if answer:
                session_id = (context or {}).get("session_id", "")
                if session_id:
                    self._last_qa[session_id] = {
                        "title": user_input,
                        "content": answer,
                        "source": "知识库外回答（对话录入）",
                        "category": "问答",
                    }
            yield ("content", "\n\n📥 企业知识库暂未收录该内容，是否需要将本次问答录入知识库？回复「录入」即可保存。")
            return

        prompt = self._build_rag_prompt(user_input, contexts,
                                        attachments=context.get("attachments"))

        # LLM 流式输出（yield 元组: ("reasoning", text) 或 ("content", text)）
        llm_streamed = False
        if self.llm_provider is not None:
            stream_method = getattr(self.llm_provider, "_stream_llm_api", None)
            is_messages_api = False
            if stream_method is None:
                stream_method = getattr(self.llm_provider, "stream_chat", None)
                is_messages_api = stream_method is not None
            if stream_method is None:
                stream_method = getattr(self.llm_provider, "stream", None)
            if stream_method is not None:
                try:
                    stream_args = ([{"role": "user", "content": prompt}],) if is_messages_api else (prompt,)
                    for chunk in stream_method(*stream_args):
                        if isinstance(chunk, tuple):
                            yield chunk
                            llm_streamed = True
                        elif isinstance(chunk, dict):
                            reasoning = chunk.get("reasoning", "")
                            if reasoning:
                                yield ("reasoning", reasoning)
                            content = chunk.get("content", "") or chunk.get("delta", "")
                            if content:
                                yield ("content", content)
                            llm_streamed = True
                        elif chunk:
                            yield ("content", chunk)
                            llm_streamed = True
                except Exception:
                    pass

        # LLM 不可用时降级为非流式
        if not llm_streamed:
            llm_output = self._call_llm(prompt)
            yield ("content", llm_output or self._build_fallback_answer(user_input, contexts))

        # 附加引用来源（即时）
        sources = self._extract_sources(contexts)
        if sources:
            yield ("content", "\n\n📚 参考来源：")
            for i, src in enumerate(sources, 1):
                yield ("content", f"\n  [{i}] {src}")

        # 主动延伸 + 产品推荐（即时）
        yield ("content", self._build_proactive_extension(user_input, contexts))
        yield ("content", self._build_product_recommendation(user_input))

    # --------------------------------------------------------
    # INT-14 闲聊：礼貌回应并引导（不进知识库检索）
    # --------------------------------------------------------
    def _respond_chitchat(self, sub_intent: str) -> AgentResponse:
        """对问候/致谢/告别做礼貌回应并引导到可用能力。

        参数：
            sub_intent: greeting / thanks / farewell

        返回：
            AgentResponse: 礼貌回复
        """
        replies = {
            "greeting": ("您好！我是AI工厂管家知识助手，可以为您解答企业管理知识、"
                         "制度流程等问题；也可以输入“下单”“查询库存”“查看排产”等指令进行业务操作。"),
            "thanks": "不客气！如需帮助，请随时告诉我。",
            "farewell": "再见！欢迎随时回来。",
        }
        return AgentResponse(
            content=replies.get(sub_intent, replies["greeting"]),
            agent_name=self.agent_name,
        )

    def _recognize_sub_intent(self, user_input: str) -> str:
        """从用户输入中识别知识子意图。

        参数：
            user_input: 用户输入文本

        返回：
            str: 子意图标签（knowledge_query/policy_consultation/process_guide/save_to_kb）

        v6.46 C4：process_guide/policy_consultation 关键词表迁入
        DB(SUB-INTENT-DEFS) 可训练；save_to_kb 含长度<=8 约束，保留在本地。
        """
        # 「录入」指令：将最近一次知识库外问答写入知识库（短指令优先匹配）
        # 长度约束为代码内逻辑（DB 关键词表无法表达），保持原位
        if ("录入" in user_input or "收录" in user_input or
                "存知识库" in user_input or "存入知识库" in user_input or
                "保存到知识库" in user_input) and len(user_input) <= 8:
            return "save_to_kb"
        from prog.runtime.sub_intent_engine import get_sub_intent_keywords as _gkw
        def _kw(sub: str) -> list:
            """子意图关键词查表辅助。

            参数：
                sub: 子意图键（如 process_guide/knowledge_query）
            返回：
                list: 该子意图的关键词列表；查表函数不可用或异常时返回 []
            """
            if _gkw is None:
                return []
            try:
                return _gkw(self.agent_type, sub)
            except Exception:
                return []

        # 流程指导关键词
        if any(k in user_input for k in _kw("process_guide")):
            return "process_guide"
        # 制度咨询关键词
        if any(k in user_input for k in _kw("policy_consultation")):
            return "policy_consultation"
        # 知识缺口分析关键词（K-06，v6.63）
        if any(k in user_input for k in _kw("kb_gap_analysis")):
            return "kb_gap_analysis"
        # 知识查询（默认）
        return "knowledge_query"

    # --------------------------------------------------------
    # 知识查询（模拟RAG）
    # --------------------------------------------------------
    def _handle_knowledge_query(self, user_input: str,
                                context: Dict[str, Any]) -> AgentResponse:
        """
        处理知识查询意图。

        设计意图：
            执行RAG问答流程：向量检索相关知识片段 -> 构建RAG提示词 ->
            LLM生成答案 -> 返回答案与引用来源。

        参数：
            user_input: 用户提问（如"精益生产的核心原则是什么"）
            context: 会话上下文

        返回：
            AgentResponse: 包含答案文本与引用来源的响应

        流程：
            1. _rag_search(query) 检索top_k相关知识片段
            2. _build_rag_prompt(query, contexts) 构建RAG提示词
            3. 调用LLM生成答案
            4. 格式化响应（答案 + 引用来源列表）
        """
        # v6.87：联网检索指令（同步路径，与 process_stream 一致）——
        # 真实执行必应检索并直接展示结果；空串表示缺话题，引导提供内容
        _web_query = self._resolve_web_query(user_input, context)
        if _web_query is not None:
            if _web_query:
                _web_res = self._web_search(_web_query)
                content = self._build_web_search_reply(_web_query, _web_res)
                # v7.08：联网检索结果支持「录入」（与 process_stream 一致）
                session_id = (context or {}).get("session_id", "")
                if session_id and _web_res:
                    self._last_qa[session_id] = {
                        "title": _web_query,
                        "content": content,
                        "source": "联网检索（对话录入）",
                        "category": "联网问答",
                    }
                    content += ("\n\n📥 本次联网检索结果尚未录入知识库，"
                                "是否需要将本次问答录入知识库？回复「录入」即可保存。")
            else:
                content = ("好的，请告诉我要联网查询的具体内容或话题，"
                           "例如：\"查一下精益生产的定义\"。")
            return AgentResponse(
                content=content,
                metadata={"sub_intent": "web_search", "web_query": _web_query},
            )

        # 1. 向量检索相关知识片段
        contexts = self._rag_search(user_input)

        # 知识库未命中兜底：联网检索（可选）+ LLM 自身知识回答 + 提示录入
        if not contexts:
            web_results = self._web_search(user_input)
            prompt = self._build_kb_gap_prompt(user_input, web_results)
            llm_output = self._call_llm(prompt)
            content = llm_output or self._build_fallback_answer(user_input, [])

            # 记录待录入内容（知识库外回答）——与 process_stream 流式路径
            # 行为统一：无论 LLM 是否可用（含 fallback 兜底），只要产生了
            # 回答 content 即记录 _last_qa，保证"📥 提示录入"与可录入内容
            # 始终一致，避免提示可录入但回复「录入」时"暂无可录入的内容"。
            # 记录内容纯净（先记录、后拼 footer），参考来源仅用于展示不入库。
            if content:
                session_id = (context or {}).get("session_id", "")
                if session_id:
                    self._last_qa[session_id] = {
                        "title": user_input,
                        "content": content,
                        "source": "知识库外回答（对话录入）",
                        "category": "问答",
                    }

            # v6.83：联网兜底回答尾部标注可点击的参考来源（仅展示，不入库）
            if web_results:
                content += self._build_source_footer(web_results)

            content += ("\n\n📥 企业知识库暂未收录该内容，是否需要将本次问答录入知识库？"
                        "回复「录入」即可保存。")
            return self._format_response(content, {
                "query": user_input,
                "contexts": [],
                "sources": [],
                "context_count": 0,
                "kb_gap": True,
            })

        # 2. 构建RAG提示词（含随消息上传的附件内容）
        prompt = self._build_rag_prompt(user_input, contexts,
                                        attachments=context.get("attachments"))

        # 3. 调用LLM生成答案
        llm_output = self._call_llm(prompt)

        # 4. 格式化响应（答案 + 引用来源）
        if llm_output:
            content = llm_output
        else:
            # LLM不可用时，直接返回检索到的知识片段
            content = self._build_fallback_answer(user_input, contexts)

        # 附加引用来源
        sources = self._extract_sources(contexts)
        if sources:
            content += "\n\n📚 参考来源："
            for i, src in enumerate(sources, 1):
                content += f"\n  [{i}] {src}"

        # K-03: 基于上下文主动延伸回答
        content += self._build_proactive_extension(user_input, contexts)
        # K-04: 咨询场景中自然推荐产品
        content += self._build_product_recommendation(user_input)

        return self._format_response(content, {
            "query": user_input,
            "contexts": contexts,
            "sources": sources,
            "context_count": len(contexts),
        })

    def _rag_search(self, query: str) -> List[Dict[str, Any]]:
        """
        向量检索相关知识片段。

        设计意图：
            将用户query通过embedding_provider向量化，在Milvus的
            ai_factory_kb Collection中做语义检索，返回top_k最相关片段。
            当向量库不可用时，回退到关键词匹配模拟检索。

        参数：
            query: 用户查询文本

        返回：
            list: 检索到的知识片段列表（按相似度降序）

        依赖：
            - embedding_provider: 生成query向量
            - vector_store: Milvus检索
        """
        # C10/A.9：检索前 query 清洗——剥口语虚词/疑问词、保留实体，
        # 避免"帮我查一下/请问有没有"等噪声影响向量检索召回率
        from prog.utils.nl_parser import clean_search_query
        query = clean_search_query(query) or query

        # v7.08：弱命中过滤——知识库会话沉淀量大后，任意问题都能靠词面重叠
        # 凑出弱相关片段（实测真命中≈0.66 vs 弱命中≈0.22~0.36），导致"未命中
        # →录入提示"分支永不触发。按检索模式区分分数语义（防止部署 Milvus
        # 时误伤）：
        #   - Milvus 真实模式：COSINE distance（越小越相似，0=完全相同）
        #   - 内存降级 / TF-IDF：cosine 相似度（越大越相似）
        # 阈值：KB_RAG_MIN_SCORE（相似度下限，默认 0.40）；Milvus 换算为
        # distance 上限 _MAX_DISTANCE = 1 - _MIN_SCORE = 0.60。
        _MIN_SCORE = float(os.environ.get("KB_RAG_MIN_SCORE", "0.40") or 0.40)
        _MAX_DISTANCE = 1.0 - _MIN_SCORE
        # 知识库文档数低于该值时不启用弱命中过滤：TF-IDF 分数受文档数 N
        # 影响（N 小时 IDF 稀疏、分数整体偏低），小库弱命中概率本身很低，
        # 过滤反而误杀真命中（如单文档库"精益生产"≈0.27 < 0.40）。
        _MIN_DOCS_FOR_FILTER = int(os.environ.get("KB_RAG_MIN_DOCS", "20") or 20)

        # 尝试使用向量库检索
        if self.vector_store is not None and self.embedding_provider is not None:
            try:
                # 生成查询向量
                query_vector = self.embedding_provider.embed(query)
                # 向量检索top_k
                results = self.vector_store.search(
                    collection_name="ai_factory_kb",
                    query_vector=query_vector,
                    top_k=5,
                )
                if results:
                    # P1-7 修复：向量库返回键为 doc_id/chunk_text/source/metadata，
                    # 统一映射为 _build_rag_prompt 消费的 content/title/source/score
                    formatted = []
                    for r in results:
                        meta = r.get("metadata", {})
                        if isinstance(meta, str):
                            try:
                                import json as _json
                                meta = _json.loads(meta)
                            except (ValueError, TypeError):
                                meta = {}
                        if not isinstance(meta, dict):
                            meta = {}
                        item = {
                            "doc_id": r.get("doc_id", ""),
                            "content": r.get("chunk_text", "") or r.get("content", ""),
                            "source": r.get("source", ""),
                            # v7.08：兼容 Milvus(distance 键) / 降级(score 键) 两种返回
                            "score": r.get("distance", r.get("score", 0.0)),
                            "metadata": meta,
                        }
                        if meta.get("title"):
                            item["title"] = meta["title"]
                        formatted.append(item)
                    # v7.08：弱命中过滤——按向量库模式区分分数语义：
                    #   milvus（真实部署）：distance 越小越相似 → 保留 <= 上限
                    #   其他（内存模拟降级）：distance 为 cosine 相似度，越大越相似
                    _mode = getattr(self.vector_store, "_mode", "") or ""
                    if _mode == "milvus":
                        strong = [r for r in formatted
                                  if float(r.get("score", 1.0)) <= _MAX_DISTANCE]
                    else:
                        strong = [r for r in formatted
                                  if float(r.get("score", 0.0)) >= _MIN_SCORE]
                    # P2 关键路径日志：向量检索命中（含过滤前/后数量）
                    logger.debug("RAG 向量检索命中 query=%r hits=%d strong=%d "
                                 "mode=%s", query, len(formatted),
                                 len(strong), _mode or "memory")
                    return strong
            except Exception as e:
                # P2 关键路径日志：向量检索异常降级，不再静默
                logger.warning("RAG 向量检索异常，降级关键词检索 query=%r: %s",
                               query, e)

        # v6.30：优先动态知识库（含图纸/工艺/训练文件/流程文档等训练内容，
        # 辅助质量分析、流程查询）；无命中不再走静态 mock_kb 兜底（v6.46 D2：
        # mock_kb 为代码内凭空捏造的"精益生产/5S"等文档，DB 无对应数据，
        # 命中即答非所问——移除静态兜底，空库由 process 层明确提示"未收录"）
        if self.knowledge_base is not None:
            try:
                results = self.knowledge_base.search(query, top_k=5)
                if results:
                    # v7.08：弱命中过滤（降级 TF-IDF 路径：score 越大越相似）。
                    # 仅当知识库文档数足够多时启用——小库 TF-IDF 分数整体偏低
                    # （受 N 影响），弱命中概率本身低，过滤反而误杀真命中。
                    _doc_count = len(getattr(
                        self.knowledge_base, "_memory_store", {}) or {})
                    if _doc_count >= _MIN_DOCS_FOR_FILTER:
                        strong = [r for r in results
                                  if r.get("score", 0.0) >= _MIN_SCORE]
                    else:
                        strong = results
                    logger.debug("RAG 关键词检索命中 query=%r hits=%d strong=%d "
                                 "docs=%d", query, len(results),
                                 len(strong), _doc_count)
                    if strong:
                        return strong
                    return []
            except Exception:
                pass

        # 无真实知识命中：返回空（触发 process 层"企业知识库暂未收录"提示）
        return []

    def _build_rag_prompt(self, query: str,
                          contexts: List[Dict[str, Any]],
                          attachments: Optional[List[Dict[str, Any]]] = None) -> str:
        """
        构建RAG提示词。

        设计意图：
            将检索到的知识片段与随消息上传的附件内容作为上下文注入提示词，
            引导LLM基于知识库内容回答，并在答案中标注引用来源。

        参数：
            query: 用户查询
            contexts: 检索到的知识片段列表
            attachments: 随消息上传的文件解析文本列表（可选）

        返回：
            str: 完整的RAG提示词（系统指令 + 知识上下文 + 用户问题）

        提示词结构：
            [系统指令] 你是企业知识助手，基于以下知识片段回答问题...
            [附件1] 图纸-xxx.pdf 内容: ...
            [知识片段1] 标题: xxx 内容: xxx
            [用户问题] xxx
            [输出要求] 答案需标注引用来源编号
        """
        # 构建附件上下文（用户随消息上传的文件，优先级最高）
        attach_text = ""
        if attachments:
            attach_lines = []
            for i, att in enumerate(attachments, 1):
                name = att.get("name", f"附件{i}")
                content = (att.get("text") or "")[:2000]
                attach_lines.append(
                    f"[附件{i}] {name}\n内容: {content}"
                )
            attach_text = "\n\n".join(attach_lines)

        # 构建知识上下文
        context_text = ""
        if contexts:
            context_lines = []
            for i, ctx in enumerate(contexts, 1):
                title = ctx.get("title", "")
                content = ctx.get("content", "")
                source = ctx.get("source", "")
                context_lines.append(
                    f"[知识片段{i}] 标题: {title}\n来源: {source}\n内容: {content}"
                )
            context_text = "\n\n".join(context_lines)
        else:
            context_text = "（未检索到相关知识片段）"

        # 拼接附件+知识上下文（避免在f-string表达式内使用反斜杠）
        sep = "\n\n" if attach_text and context_text else ""
        knowledge_block = attach_text + sep + context_text

        prompt = f"""你是企业知识助手，基于以下知识片段回答用户问题。

## 知识上下文
{knowledge_block}

## 用户问题
{query}

## 输出要求
1. 直接给出答案，第一句就开始回答用户问题；严禁出现"我将/我会/我们只需要/根据要求/按照要求/注意开头"等转述性或元描述式开场白
2. 优先基于附件与知识片段内容回答，不要编造信息
3. 如果知识片段中无相关信息，请如实告知
4. 答案中标注引用的附件/知识片段编号（如[附件1]/[知识片段1]）
5. 用自然、口语化的中文回复，像真人助手对话一样娓娓道来，不要像文档一样生硬罗列
6. 适当使用 Markdown（短标题、列表、加粗）让重点清晰，但保持对话感
7. 回复控制在500字以内
"""
        return prompt

    def _build_fallback_answer(self, query: str,
                               contexts: List[Dict[str, Any]]) -> str:
        """LLM不可用时的兜底回答：直接拼接检索到的知识片段。

        参数：
            query: 用户查询
            contexts: 检索到的知识片段列表

        返回：
            str: 兜底回答文本
        """
        if not contexts:
            return f"抱歉，未找到与「{query}」相关的知识内容。请尝试换个关键词提问。"

        lines = [f"关于「{query}」，检索到以下相关知识："]
        for i, ctx in enumerate(contexts, 1):
            lines.append(f"\n[{i}] {ctx.get('title', '?')}")
            lines.append(f"来源：{ctx.get('source', '?')}")
            lines.append(f"内容：{ctx.get('content', '?')}")
        return "\n".join(lines)

    def _web_search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """联网检索（可选，默认关闭）：必应网页搜索 HTML，国内可达，无需密钥。

        设计意图：
            知识库未命中时的第二层兜底。由 web_search_enabled 开关控制，
            未启用或联网失败时返回空列表，自动降级为纯 LLM 自身知识回答。
            （v6.86：检索源由 api.duckduckgo.com 替换为 bing.com——DDG 在国内
            网络连接超时不可达；Bing 自动重定向 cn.bing.com 实测 1~2s 可达。
            Bing 无公开免费 JSON API，抓取 /search HTML 后用 b_algo 结构提取。）

        参数：
            query: 用户查询
            top_k: 返回结果数量上限（同时作为 Bing count 请求参数）

        返回：
            list: 检索结果列表 [{title, snippet, url}]
        """
        if not self.web_search_enabled:
            return []
        # C10/A.9：多 query 并行检索（P0 实体短语 / P1 关键词堆叠），
        # 结果按 URL 去重合并；重写失败回退原始 query 单发
        # v7.04/v7.05：联网搜索改用知乎——配置 ZHIHU_ACCESS_SECRET 时
        # 双通道并行（知乎站内 zhihu_search + 全网 global_search）合并去重，
        # 未配置密钥回退 Bing（国内可达，无需密钥），保证功能不失效
        fetcher = (self._fetch_zhihu_both
                   if os.environ.get("ZHIHU_ACCESS_SECRET")
                   else self._fetch_bing_search)
        queries = self._build_search_queries(query)
        results: List[Dict[str, Any]] = []
        seen_urls: set = set()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(queries)) as ex:
            futures = {
                ex.submit(fetcher, q, top_k): q for q in queries
            }
            for fut in concurrent.futures.as_completed(futures):
                for r in fut.result() or []:
                    url = r.get("url", "")
                    if url and url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append(r)
        return results[:top_k]

    def _build_search_queries(self, query: str) -> List[str]:
        """基于统一解析层重写检索 query：P0 实体短语优先，P1 关键词堆叠
        补充；重写不可用时回退原始 query（保证既有行为不回退）。"""
        try:
            from prog.utils.nl_parser import rewrite_search_query
            qmap = rewrite_search_query(query)
            out: List[str] = []
            for level in ("P0", "P1"):
                for item in (qmap.get(level) or []):
                    if item and item != query and item not in out:
                        out.append(item)
            out.append(query)
            return out[:2]
        except Exception:
            return [query]

    # 知乎密钥轮换索引（进程级：30001 限频自动切下一个密钥，避免超限影响搜索）
    _ZHIHU_KEY_IDX = 0

    def _zhihu_secrets(self) -> List[str]:
        """主/备份密钥列表（ZHIHU_ACCESS_SECRET 主 + ZHIHU_ACCESS_SECRET_BACKUP 备份，
        均支持逗号分隔多密钥；去重保序）。"""
        secrets: List[str] = []
        for name in ("ZHIHU_ACCESS_SECRET", "ZHIHU_ACCESS_SECRET_BACKUP"):
            val = (os.environ.get(name) or "").strip()
            if not val:
                continue
            for s in val.split(","):
                s = s.strip()
                if s and s not in secrets:
                    secrets.append(s)
        return secrets

    def _zhihu_api_get(self, url: str, query: str, count: int,
                       secret: str):
        """知乎搜索 API 单次 GET 请求。返回 (json_data, limited)：
        limited=True 表示 30001 频率限制（触发密钥切换信号）。"""
        import requests
        resp = requests.get(
            url,
            params={"Query": query, "Count": count},
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Request-Timestamp": str(int(time.time())),
                "Content-Type": "application/json",
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        return data, data.get("Code") == 30001

    def _zhihu_fetch(self, url: str, query: str, top_k: int, count_cap: int,
                     parse_items) -> List[Dict[str, Any]]:
        """带密钥轮换的知乎搜索（v7.06：主密钥 30001 限频自动切换备份密钥）。

        轮换策略：按 _ZHIHU_KEY_IDX 起点顺序尝试全部密钥，命中 30001 限频
        即换下一个密钥重试；成功后记住当前可用密钥索引（进程级），下次
        请求直接使用，避免每次先打超限密钥。网络/解析异常与业务错误码
        （20001 鉴权/90001 内部）不触发切换。
        """
        secrets = self._zhihu_secrets()
        if not secrets:
            return []
        for attempt in range(len(secrets)):
            idx = (self._ZHIHU_KEY_IDX + attempt) % len(secrets)
            secret = secrets[idx]
            try:
                count = max(1, min(int(top_k), count_cap))
                data, limited = self._zhihu_api_get(url, query, count, secret)
            except Exception as e:
                logger.warning("知乎搜索异常（%s）：%s",
                               url.rsplit("/", 1)[-1], e)
                return []
            if limited:
                logger.warning("知乎密钥 %s 触发频率限制(30001)，切换备用密钥重试",
                               (secret[:6] + "***") if len(secret) > 6 else "***")
                continue
            if data.get("Code") != 0:
                logger.warning("知乎搜索失败 code=%s msg=%s（20001 鉴权/90001 内部）",
                               data.get("Code"), data.get("Message"))
                return []
            self._ZHIHU_KEY_IDX = idx  # 记住可用密钥，下次直达
            items = (data.get("Data") or {}).get("Items") or []
            return parse_items(items, count, top_k)
        logger.warning("知乎全部 %d 个密钥均触发频率限制，本轮放弃搜索",
                       len(secrets))
        return []

    def _fetch_zhihu_search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """单 query 知乎站内搜索 API（v7.04；v7.06 加备份密钥轮换）。

        接入：GET https://developer.zhihu.com/api/v1/content/zhihu_search
        （文档 https://developer.zhihu.com/docs?key=zhihu_search）
        Query 必填、Count 上限 10；Code=0 成功。
        """
        return self._zhihu_fetch(
            "https://developer.zhihu.com/api/v1/content/zhihu_search",
            query, top_k, 10, self._normalize_zhihu_items)

    def _fetch_zhihu_global_search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """单 query 知乎全网搜索 API（v7.05；v7.06 加备份密钥轮换）。

        接入：GET https://developer.zhihu.com/api/v1/content/global_search
        （文档 developer.zhihu.com/docs?key=global_search）
        Query 必填、Count 上限 20；可选 Filter/SearchDB（当前未启用）。
        """
        return self._zhihu_fetch(
            "https://developer.zhihu.com/api/v1/content/global_search",
            query, top_k, 20, self._normalize_zhihu_items)

    @staticmethod
    def _normalize_zhihu_items(items: List[Dict[str, Any]], count: int,
                               top_k: int) -> List[Dict[str, Any]]:
        """知乎 Items → 统一结果格式（站内/全网共用）。"""
        results: List[Dict[str, Any]] = []
        for it in items:
            title = (it.get("Title") or "").strip()
            url = (it.get("Url") or "").strip()
            if not title:
                continue
            snippet = (it.get("ContentText") or "").strip()
            author = (it.get("AuthorName") or "").strip()
            vote = int(it.get("VoteUpCount") or 0)
            comment = int(it.get("CommentCount") or 0)
            if author:
                head = f"（作者：{author}，赞同 {vote}，评论 {comment}）"
                snippet = (head + (("\n" + snippet) if snippet else ""))
            results.append({
                "title": title[:80],
                "snippet": snippet[:300],
                "url": url,
                "author": author,
                "vote_up": vote,
                "comment_count": comment,
            })
            if len(results) >= count:
                break
        return results[:top_k]

    def _fetch_zhihu_both(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """双通道并行：知乎站内（zhihu_search）+ 全网（global_search）合并去重。

        v7.05 设计：站内搜索返回知乎垂直高质量内容（问答/文章，Count≤10）；
        全网搜索覆盖全网时效信息（Count≤20）。并行取结果按 URL 去重合并，
        单通道失败不影响另一通道（子方法自带 try/except，此处再兜底一层）。
        """
        import concurrent.futures
        results: List[Dict[str, Any]] = []
        seen_urls: set = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_in = ex.submit(self._fetch_zhihu_search, query, top_k)
            f_global = ex.submit(self._fetch_zhihu_global_search, query, top_k)
            for fut in (f_in, f_global):
                try:
                    items = fut.result() or []
                except Exception as e:
                    logger.warning("知乎搜索单通道异常已隔离：%s", e)
                    items = []
                for r in items:
                    url = r.get("url", "")
                    if url and url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append(r)
        return results[:top_k]

    def _fetch_bing_search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """单 query 必应网页搜索（自原 _web_search 主体提取，供多 query 并行）。"""
        try:
            import html as _html
            import re as _re
            import requests
            resp = requests.get(
                "https://www.bing.com/search",
                params={"q": query, "count": top_k, "mkt": "zh-CN"},
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"),
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                timeout=6,
            )
            resp.raise_for_status()
            results: List[Dict[str, Any]] = []
            # Bing 结果块结构：<li class="b_algo"> 内 <h2><a href="url">标题</a></h2>
            # + <p>摘要</p>；逐块提取后按 top_k 截断
            for block in _re.findall(r'<li class="b_algo".*?</li>', resp.text, _re.S):
                m = _re.search(
                    r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</h2>',
                    block, _re.S)
                if not m:
                    continue
                url = m.group(1)
                title = _re.sub(r"<[^>]+>", "", m.group(2)).strip()
                pm = _re.search(r'<p[^>]*>(.*?)</p>', block, _re.S)
                snippet = _re.sub(r"<[^>]+>", "", pm.group(1)).strip() if pm else ""

                def _clean_ent(s: str) -> str:
                    # 清理 HTML 实体（Bing 双编码如 &amp;ensp;，循环 unescape 至稳定）
                    for _ in range(3):
                        nxt = _html.unescape(s)
                        if nxt == s:
                            break
                        s = nxt
                    return s

                title = _clean_ent(title)
                snippet = _clean_ent(snippet)
                if not title or not snippet:
                    continue
                results.append({
                    "title": title[:80],
                    "snippet": snippet[:200],
                    "url": url,
                })
                if len(results) >= top_k:
                    break
            return results[:top_k]
        except Exception:
            return []

    def _build_source_footer(self, web_results: List[Dict[str, Any]]) -> str:
        """构建联网检索"参考来源"标注（v6.83，追加到回答尾部）。

        设计意图：
            联网兜底回答需可溯源：回复末尾列出命中的网页标题与链接，
            用户可点击核验；无结果时返回空串不追加。
        """
        if not web_results:
            return ""
        lines = ["\n\n---\n🌐 参考来源："]
        for i, r in enumerate(web_results, 1):
            url = r.get("url", "") or ""
            title = (r.get("title", "") or "").strip() or url[:40]
            lines.append(f"{i}. [{title}]({url})" if url else f"{i}. {title}")
        return "\n".join(lines)

    _WEB_CMD_RE = re.compile(
        r"(请|麻烦|帮我|帮)?(联网|网络|网上|在线|互联网|web|上网)"
        r"(搜索|检索|查找|查询|搜|查|找|获取|看)?(下|一下)?"
    )

    def _resolve_web_query(self, user_input: str,
                           context: Dict[str, Any]) -> Optional[str]:
        """解析"联网查找X/从网上查一下X"类检索指令为真实检索词（v6.87.1）。

        设计意图：
            联网查询仅在两种情形执行：①用户单独要求联网（明确检索指令，
            如"联网查找下/网上查下XXX"）；②知识库未命中时的兜底检索
            （调用方 RAG 未命中路径）。能力疑问句（"你能联网吗/能从网络
            查找相关内容吗"）不触发真实检索，走常规知识问答兜底。

            本方法仅处理情形①，负责确定检索词，供调用方执行 _web_search()：
            1. 指令中自带话题（如"联网查下精益生产的定义"→"精益生产的定义"）；
            2. 指令无话题（如"联网查找下"）时，取会话历史中最近一条
               用户消息作为指代话题（如上一轮"郭德纲 红歌事件"）；
            3. 均不可得时返回空串""（调用方据此引导用户提供话题，避免
               退化到 LLM 兜底误答"没有联网能力"）；非检索指令返回 None。

        参数：
            user_input: 用户输入文本
            context: 会话上下文（含 history 列表，元素为 {role, content, ts}）

        返回：
            Optional[str]: 真实检索词；空串=指令确认但缺话题；None=非检索指令
        """
        if not user_input:
            return None
        text = user_input.strip()
        cap_word = r"(联网|网络|网上|在线|互联网|web|上网)"
        ask_word = r"(搜索|检索|查找|查询|搜|查|找|获取|看)"
        # 必须含能力词（联网/网上…），否则不视为联网检索指令
        if not re.search(cap_word, text):
            return None
        # v6.87.1：能力疑问句（"你能联网吗/能从网络查找相关内容吗"）不触发
        # 真实检索——联网查询仅在"单独要求联网"（明确检索指令）或知识库
        # 未命中兜底时执行，疑问句走常规知识问答兜底。
        # v7.05：仅当句尾问号且指令不是以能力词开头才判为能力疑问句——
        # "联网查一下精益生产是什么？"以能力词开头（明确检索指令）不得误判，
        # 而"能从网络查找相关内容吗？"不以能力词开头（能力疑问句）正常拦截。
        if re.search(r"[吗？?]$", text) and not self._WEB_CMD_RE.match(text):
            return None
        # 含查询词视为明确检索指令；否则不触发
        if not re.search(ask_word, text):
            return None

        # 1) 指令内自带话题：去掉指令词后剩余部分
        topic = self._WEB_CMD_RE.sub("", text)
        topic = topic.strip(" ，。？！?、：:;；.。\t \"'")
        topic = topic.lstrip("从请帮我麻烦为给按据")
        # v7.05：不再因话题含句中问号而拒绝（如"…的区别是什么？请总结要点"），
        # 句尾疑问词已由 clean_search_query 的 _QUESTION_TAIL_RE 剥离，能力
        # 疑问句已在入口按"句尾问号且无检索词"拦截，此处仅校验话题长度
        if len(topic) >= 2:
            # C10/A.9：话题提纯——统一解析层清洗（去口语虚词/疑问词、
            # 实体加引号保护），避免"郭德纲 红歌事件是否有新进展"原句
            # 直发搜索引擎被切错
            from prog.utils.nl_parser import clean_search_query
            cleaned = clean_search_query(topic)
            return cleaned or topic

        # 2) 会话历史最近一条用户消息（指代上一轮话题）
        #    优先 session_history（SessionManager 持久化 {role,content,ts}），
        #    兼容 history（前端 body 传入，可能为空）
        history = context.get("session_history") or context.get("history") or []
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "user":
                prev = (msg.get("content") or "").strip()
                if prev and prev != user_input:
                    return prev

        # 3) 确为联网检索指令但无话题/无历史可指代：
        #    返回空串标记（调用方据此引导用户提供话题，避免退化到
        #    LLM 兜底而误答"没有联网能力"）
        if re.search(ask_word, text):
            return ""
        return None

    _WEB_ANALYZE_MAX_ITEMS = 6
    _WEB_SNIPPET_CAP = 300

    def _analyze_web_results(self, query: str,
                             results: List[Dict[str, Any]]) -> str:
        """LLM 综合分析联网检索结果，提炼要点/结论（v7.07）。

        设计意图：
            检索结果（标题/摘要）为搜索引擎/知乎返回的原始片段，可能截断、
            内容不完整；将结果送 LLM 结合 query 综合分析，输出结论/要点/
            对比后再展示。LLM 不可用或调用失败返回空串，由调用方降级为
            直接展示（功能不失效）。

        参数：
            query: 用户检索词
            results: _web_search 返回的结果列表

        返回：
            str: LLM 分析文本；空串=不可用（触发降级）
        """
        if not results:
            return ""
        items = results[:self._WEB_ANALYZE_MAX_ITEMS]
        lines = [f"用户问题：{query}", "", "以下是联网检索到的结果："]
        for i, r in enumerate(items, 1):
            title = (r.get("title") or "无标题").strip()[:200]
            snippet = (r.get("snippet") or "").strip()[:self._WEB_SNIPPET_CAP]
            url = (r.get("url") or "").strip()
            lines.append(f"{i}. 标题：{title}")
            if snippet:
                lines.append(f"   摘要：{snippet}")
            if url:
                lines.append(f"   链接：{url}")
        lines.append("")
        lines.append(
            "请综合分析上述检索结果，直接回答用户问题，用简洁的中文分点"
            "提炼要点/结论/对比，引用检索结果中的具体信息；"
            "不要编造检索结果中不存在的内容；若结果不足以回答请如实说明。")
        return self._call_llm("\n".join(lines))

    def _build_web_search_reply(self, query: str,
                                results: List[Dict[str, Any]]) -> str:
        """构建"联网检索"回复：优先 LLM 分析总结展示，不可用降级直接展示。

        设计意图：
            v6.87 原为直接展示（标题/链接/摘要，零LLM干预，防"没有联网
            能力"误答）；v7.07 用户指示"搜索到内容后不直接展示，对结果
            进行分析处理后展示，否则内容不完整"——检索摘要为原始片段可能
            截断，先送 LLM 综合分析提炼要点，再附来源链接；LLM 不可用时
            降级原直接展示，功能不失效。

        参数：
            query: 实际检索词
            results: _web_search 返回的结果列表（可为空）

        返回：
            str: 展示文本
        """
        if not results:
            return (f"抱歉，联网检索「{query}」未获取到结果"
                    "（网络暂时不可达或超时），可稍后重试或换个话题。")
        analysis = ""
        try:
            analysis = self._analyze_web_results(query, results) or ""
        except Exception:
            analysis = ""
        analysis = analysis.strip()
        lines = []
        if analysis:
            # 分析后展示：综合结论 + 来源链接
            lines.append(f"已为你联网检索并分析「{query}」：")
            lines.append("")
            lines.append(analysis)
            lines.append("")
            lines.append("📎 参考来源：")
            for i, r in enumerate(results, 1):
                title = r.get("title") or "无标题"
                url = r.get("url") or ""
                lines.append(f"{i}. {title}")
                if url:
                    lines.append(f"   {url}")
        else:
            # 降级：LLM 不可用，直接展示标题/链接/摘要
            lines.append(f"已为你联网检索到「{query}」的相关内容：")
            lines.append("")
            for i, r in enumerate(results, 1):
                title = r.get("title") or "无标题"
                url = r.get("url") or ""
                snippet = r.get("snippet") or ""
                lines.append(f"{i}. {title}")
                if url:
                    lines.append(f"   {url}")
                if snippet:
                    lines.append(f"   {snippet}")
                lines.append("")
        return "\n".join(lines).rstrip()

    def _build_kb_gap_prompt(self, query: str,
                             web_results: List[Dict[str, Any]]) -> str:
        """构建"知识库未命中"兜底提示词。

        设计意图：
            知识库无相关内容时，让LLM基于自身知识（及可选的联网结果）
            回答，并明确标注回答来源为知识库外，提示用户可录入知识库。
            存在联网检索结果时，提示词声明"系统已联网检索"，避免LLM
            基于自身认知误答"没有联网能力"（v6.87）。

        参数：
            query: 用户查询
            web_results: 联网检索结果（可为空）

        返回：
            str: 兜底提示词
        """
        web_text = ""
        if web_results:
            web_lines = []
            for i, r in enumerate(web_results, 1):
                web_lines.append(
                    f"[网页{i}] {r.get('title', '')}\n摘要: {r.get('snippet', '')}\n链接: {r.get('url', '')}"
                )
            web_text = "\n\n".join(web_lines)

        # v6.87：存在检索结果时声明"系统已联网检索"，禁止LLM自称"没有联网能力"
        has_web = bool(web_results)
        open_line = ("「已为你联网检索到以下相关内容」" if has_web
                     else "「企业知识库中暂无此内容，以下为AI基于自身知识的回答，仅供参考」")

        return f"""你是AI工厂管家知识助手。企业知识库中未检索到与用户问题直接相关的内容。

## 联网检索结果{('（系统已通过必应执行联网检索，以下为命中的网页，供参考）' if has_web else '（未检索到相关外部网页）')}
{web_text or '无'}

## 用户问题
{query}

## 输出要求
1. 第一句直接以{open_line}开头，然后立即回答用户问题；严禁出现"我将/我会/我们只需要/根据要求/按照要求/注意开头"等转述性或元描述式开场白
2. 若上方联网检索结果中存在与问题相关的网页，请优先基于检索结果回答，并如实说明"已为你联网检索到相关内容"；严禁声称"我没有联网搜索能力/无法联网检索"——系统已具备联网检索能力
3. 请基于自身专业知识回答用户问题，回答要专业、条理清晰、像真人对话一样自然
4. 适当使用 Markdown（短标题、列表、加粗）让重点清晰，但保持对话感
5. 回答控制在500字以内
6. 不要输出录入/保存知识库的提示（由系统统一附加）
"""

    def _aggregate_kb_candidate(self, context: Dict[str, Any],
                                session_id: str,
                                qa: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """聚合会话最近 N 轮问答为待录入知识库内容（多人协作场景③，v6.79）。

        来源优先级：
            1. session_history（SessionManager 持久化 {role, content, ts}）：
               取最近 N=3 对"用户提问 + 助手回答"，title 取首问、content 合并；
            2. 回退 _last_qa（最近一次知识库外回答，仅 1 轮）。

        Returns:
            dict: {title, content, source, category, qa_count}
        """
        history = (context or {}).get("session_history") or []
        pairs = []
        if isinstance(history, list):
            buf_q, buf_a = None, ""
            for msg in reversed(history):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                text = (msg.get("content") or "").strip()
                if not text:
                    continue
                if role == "assistant":
                    buf_a = text
                elif role == "user":
                    if buf_a:
                        pairs.append({"q": text[:200], "a": buf_a})
                        buf_a = ""
                        if len(pairs) >= 3:
                            break
        if pairs:
            pairs.reverse()
            parts = []
            for i, p in enumerate(pairs, 1):
                parts.append(f"问{i}：{p['q']}\n答{i}：{p['a']}")
            return {
                "title": pairs[0]["q"],
                "content": "\n\n".join(parts),
                "source": "会话协作录入（知识库外问答聚合）",
                "category": "问答",
                "qa_count": len(pairs),
            }
        qa = qa or {}
        return {
            "title": qa.get("title", ""),
            "content": qa.get("content", ""),
            "source": qa.get("source", "对话录入"),
            "category": qa.get("category", "问答"),
            "qa_count": 1,
        }

    def _submit_l1_kb_entry(self, title: str, content: str, source: str,
                            category: str, qa_count: int) -> int:
        """提交 L1 会话学习样本留痕（多人协作场景③）：写入 training_data，
        approved=False，供 manager/admin 复核/纠正（复用现有 L1 审批机制）。

        Returns:
            int: training_data.id；DB 不可用返回 0（不阻断知识库入库）
        """
        try:
            from prog.runtime.database import get_database
            import json as _json
            db = get_database()
            if db is None:
                return 0
            return db.insert("training_data", {
                "agent_type": "knowledge",
                "intent": "kb_entry",
                "user_input": title[:500],
                "ai_output": content[:8000],
                "final_output": content[:8000],
                "approved": False,
                "metadata": _json.dumps({
                    "kb_entry": True,
                    "title": title, "source": source,
                    "category": category, "qa_count": qa_count,
                }, ensure_ascii=False),
            }) or 0
        except Exception:
            return 0

    def _handle_save_to_kb(self, user_input: str,
                           context: Dict[str, Any]) -> AgentResponse:
        """处理「录入」指令：将最近一次知识库外问答写入知识库。

        设计意图：
            用户在看到兜底回答后的「录入知识库」提示时，回复「录入」，
            将上一条知识库外问答（问题+答案）保存到企业知识库。
            v6.79（多人协作场景③）：录入来源由"最近一轮"升级为
            "会话最近 N 轮问答聚合"（多人讨论沉淀），并同步提交 L1
            会话学习样本（approved=False）供 manager/admin 复核。

        参数：
            user_input: 用户输入（如"录入"）
            context: 会话上下文（含session_id）

        返回：
            AgentResponse: 录入结果
        """
        session_id = (context or {}).get("session_id", "")
        qa = self._last_qa.get(session_id or "")
        cand = self._aggregate_kb_candidate(context, session_id, qa)
        if not cand.get("title") or not cand.get("content"):
            return AgentResponse(
                content=("暂无可录入的内容。请先向知识助手提问，AI给出知识库外回答后，"
                         "再回复「录入」即可保存到企业知识库。"),
                agent_name=self.agent_name,
            )
        # 1. 提交 L1 会话学习样本留痕（复用现有 L1 审批，manager/admin 可复核）
        l1_id = self._submit_l1_kb_entry(
            cand["title"], cand["content"],
            cand.get("source", "对话录入"), cand.get("category", "问答"),
            cand.get("qa_count", 1))
        # 2. 入库并向量化（即时可用，向量库不可用降级关键词检索）
        result = self.upload_document(
            title=cand["title"],
            content=cand["content"],
            source=cand.get("source", "对话录入"),
            category=cand.get("category", "问答"),
        )
        if session_id:
            self._last_qa.pop(session_id, None)
        if result.get("vectorized"):
            suffix = "已完成向量化，可直接语义检索。"
        else:
            suffix = "向量化降级，已入库可关键词检索。"
        l1_note = (f"（已提交 L1 会话学习样本 {l1_id}，待 manager/admin 复核）"
                   if l1_id else "（L1 样本留痕不可用，仅入库）")
        return AgentResponse(
            content=(f"✅ 已录入企业知识库（{result.get('doc_id', '')}），"
                     f"聚合会话 {cand.get('qa_count', 1)} 轮问答。{suffix}"
                     f"\n{l1_note}"),
            agent_name=self.agent_name,
            data={**result, "l1_sample_id": l1_id},
        )

    def _extract_sources(self, contexts: List[Dict[str, Any]]) -> List[str]:
        """从检索结果中提取引用来源。

        参数：
            contexts: 知识片段列表

        返回：
            list: 来源描述列表
        """
        sources = []
        for ctx in contexts:
            title = ctx.get("title", "")
            source = ctx.get("source", "")
            if source:
                sources.append(f"{title}（{source}）")
            elif title:
                sources.append(title)
        return sources

    # --------------------------------------------------------
    # 制度咨询
    # --------------------------------------------------------
    def _handle_policy_consultation(self, user_input: str,
                                    context: Dict[str, Any]) -> AgentResponse:
        """
        处理制度咨询意图。

        设计意图：
            解答公司管理制度、政策规范类问题。
            通过RAG检索制度文档，返回政策解读与适用说明。

        参数：
            user_input: 用户输入（如"公司的考勤制度是怎样的"）
            context: 会话上下文

        返回：
            AgentResponse: 制度咨询结果（含政策解读、引用来源）
        """
        # 检索制度相关文档
        contexts = self._rag_search(user_input)

        # 构建制度咨询专用提示词
        prompt = self._build_policy_prompt(user_input, contexts)
        llm_output = self._call_llm(prompt)

        if llm_output:
            content = llm_output
        else:
            content = self._build_fallback_answer(user_input, contexts)

        # 附加引用来源
        sources = self._extract_sources(contexts)
        if sources:
            content += "\n\n📚 参考来源："
            for i, src in enumerate(sources, 1):
                content += f"\n  [{i}] {src}"

        return self._format_response(content, {
            "query": user_input,
            "contexts": contexts,
            "sources": sources,
        })

    def _build_policy_prompt(self, query: str,
                             contexts: List[Dict[str, Any]]) -> str:
        """构建制度咨询专用提示词。

        参数：
            query: 用户查询
            contexts: 检索到的知识片段

        返回：
            str: 制度咨询提示词
        """
        context_text = ""
        if contexts:
            context_lines = []
            for i, ctx in enumerate(contexts, 1):
                context_lines.append(
                    f"[制度文档{i}] {ctx.get('title', '')}\n{ctx.get('content', '')}"
                )
            context_text = "\n\n".join(context_lines)
        else:
            context_text = "（未检索到相关制度文档）"

        return f"""你是企业制度咨询助手，基于以下制度文档回答用户问题。

## 制度文档
{context_text}

## 用户问题
{query}

## 输出要求
1. 严格基于制度文档内容回答，不做主观解读
2. 如有适用条件、例外情况需说明
3. 引用具体制度条款（如[制度文档1]）
4. 如未找到相关制度，告知用户联系人事部或管理部
5. 用专业、严谨的中文回复，像真人助手一样清晰解答，避免机械式输出
6. 适当使用 Markdown（短标题、列表、加粗）让重点清晰，但保持对话感
"""

    # --------------------------------------------------------
    # 流程指导
    # --------------------------------------------------------
    def _wf_required_fields(self, wf_type: str) -> list:
        """流程必填字段（v6.46：三层配置驱动，字段随训练动态增减）：
        1. SLOT-DEFS.required_rules（slot_engine 表驱动，DB 可训练）
        2. workflow_configs.gate_checks.required_fields（DB 定义行可训练）
        3. 内置兜底报销三字段（DB 不可用时的降级默认）
        """
        # 1) SLOT-DEFS 必填规则（可训练，含 or 表达式如 "a|b"）
        try:
            from prog.runtime.slot_engine import get_required_slots
            req = get_required_slots(wf_type)
            if req:
                return list(req)
        except Exception:
            pass
        # 2) workflow_configs.gate_checks.required_fields（可训练）
        try:
            import json as _json
            from prog.runtime.database import get_database
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            config = WorkflowEnforcer(database=get_database())._get_workflow_config(wf_type) or {}
            gc = config.get("gate_checks") or {}
            if isinstance(gc, str):
                try:
                    gc = _json.loads(gc)
                except Exception:
                    gc = {}
            rf = gc.get("required_fields") or {}
            if isinstance(rf, dict):
                out = [str(x) for v in rf.values()
                       if isinstance(v, list) for x in v if x]
                if out:
                    return out
        except Exception:
            pass
        # 3) 内置降级（与 migrations/019 训练定义的字段保持一致）
        return ["amount", "expense_type", "reason"]

    def _wf_field_missing(self, collected: dict, required: list) -> list:
        """计算缺失字段（支持 or 表达式 "a|b"：任一满足即视为已提供）。"""
        missing = []
        for f in required:
            if "|" in str(f):
                parts = [p for p in str(f).split("|") if p]
                if not any(str(collected.get(p, "") or "").strip() for p in parts):
                    missing.append(f)
            else:
                if not str(collected.get(f, "") or "").strip():
                    missing.append(f)
        return missing

    def _wf_trigger_keywords(self, wf_type: str) -> list:
        """当前流程触发关键词（DB workflow_configs.thresholds.trigger_keywords，
        可训练；DB 不可用时降级内置报销关键词）。用于整句兜底排除启动语
        （v6.46.1：替代硬编码"报销/流程"，新训练流程自动生效）。
        """
        try:
            import json as _json
            from prog.runtime.database import get_database
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            config = WorkflowEnforcer(database=get_database())._get_workflow_config(wf_type) or {}
            thresholds = config.get("thresholds") or {}
            if isinstance(thresholds, str):
                try:
                    thresholds = _json.loads(thresholds)
                except Exception:
                    thresholds = {}
            tk = thresholds.get("trigger_keywords")
            if isinstance(tk, list) and tk:
                return [str(t) for t in tk if t]
        except Exception:
            pass
        return ["费用报销", "报销"]

    @staticmethod
    def _wf_field_is_numeric(field: str) -> bool:
        """字段是否数值型（整句兜底跳过）：amount/quantity 等数值槽位
        不应被整句文本兜底误赋值（v6.46.1）。
        """
        try:
            from prog.runtime.slot_engine import get_slot_defs
            defn = get_slot_defs().get(str(field), {})
            vt = str(defn.get("value_type", ""))
            return vt in ("int", "float", "amount_wan", "discount", "period")
        except Exception:
            return str(field) in ("amount", "quantity", "price",
                                  "unit_price", "discount", "days")

    @staticmethod
    def _wf_field_free_text(field: str) -> bool:
        """字段是否自由文本（整句兜底优先赋值）：SLOT-DEFS 槽位定义
        free_text=True（如 reason/事由）优先接收任意整句；枚举类字段
        （expense_type 等）不优先截取（v6.46.1）。
        """
        try:
            from prog.runtime.slot_engine import get_slot_defs
            defn = get_slot_defs().get(str(field), {})
            return bool(defn.get("free_text"))
        except Exception:
            return str(field) in ("reason", "remark", "description", "memo")

    @staticmethod
    def _extract_wf_name(user_input: str) -> str:
        """从输入提取流程名（如"发起预算审批流程" -> "预算审批"）。

        C10/A.9：优先匹配 workflow 实体词典（workflow_configs 热加载的
        流程名，精确匹配不截断）。仅接受较长流程名（>=6 字）——短名
        （如"审批流程""报销审批"）多为泛化结构词且正则已能覆盖，避免
        子串误匹配（如"启动订单审批流程"被词典"审批流程"截断）。未命中
        回退原贪婪正则。
        """
        try:
            from prog.utils.nl_parser import extract_entities
            for e in extract_entities(user_input):
                if e["type"] == "workflow" and len(e["text"]) >= 6:
                    return e["text"]
        except Exception:
            pass
        m = re.search(
            r"(?:发起|启动|申请|提交|新建|创建|走|进行)\s*(.{1,10})(?:流程|报销|审批)",
            user_input)
        if m:
            name = m.group(1).strip()
            if name:
                return name
        m = re.search(r"(.{1,10}?)(?:流程|审批)", user_input)
        if m:
            return m.group(1).strip()
        return ""

    def _handle_workflow_not_found(self, user_input: str,
                                   context: Dict[str, Any]) -> AgentResponse:
        """流程不存在提示（v6.46.1）：用户发起流程但未匹配到流程定义时，
        提示确认名称或申请新建（训练建立：触发关键词/必填字段/审批链）。"""
        wf_name = self._extract_wf_name(user_input) or "该流程"
        content = (
            f"暂未找到「{wf_name}」的流程定义，无法启动。\n\n"
            "您可以：\n"
            "1. 确认流程名称是否正确（如「费用报销」「请假」「合同审批」等）；\n"
            "2. 如需新建流程，可提交训练申请——定义流程触发关键词、必填字段"
            "与审批链，审批通过后即可发起（无需改代码）。"
        )
        return self._format_response(content, {"query": user_input})

    # ────────────────────────────────────────────────────────────
    # v6.60：流程实例查询（workflow_query 意图）
    # 规格书：流程五段接线之"业务生效前可查询"——查看既有单据/进度，
    # 返回报销单样式 HTML（申请字段 + 审批签字链），不触发新流程实例。
    # ────────────────────────────────────────────────────────────
    _WF_TYPE_NAMES = {
        "expense_reimbursement": "费用报销审批",
        "order_approve": "订单确认审批",
        "return_process": "客户退货审批",
        "production_schedule": "生产排产审批",
        "drawing_change": "图纸变更审批",
        "customer_change": "客户信息变更",
        "product_change": "产品信息变更",
        "cost_markup_change": "成本加成率变更",
        "version_sm_change": "版本物料清单变更",
        "sched_constraint_change": "排产约束变更",
        "inv_stage_change": "库存阶段变更",
        "bom_check_change": "BOM校验规则变更",
        "drawing_field_change": "图纸字段变更",
        "rule_config_change": "规则配置变更",
    }
    _ROLE_NAMES = {
        "sales_manager": "销售经理",
        "production_manager": "生产经理",
        "finance_manager": "财务经理",
        "warehouse_manager": "仓储主管",
        "admin": "系统管理员",
        "manager": "部门经理",
        "sales": "销售专员",
        "production": "生产专员",
        "finance": "财务专员",
        "warehouse": "仓储专员",
        "qc": "质检专员",
        "qc_manager": "质量经理",
        "hr": "人事专员",
        "customer_service": "客服专员",
    }
    _WF_FIELD_NAMES = {
        "reason": "事由", "amount": "金额", "expense_type": "费用类型",
        "customer_name": "客户", "product_code": "产品编码", "quantity": "数量",
        "order_id": "订单号", "return_qty": "退货数量", "refund_amount": "退款金额",
        "remark": "备注", "attachment": "附件", "work_order_id": "工单号",
        "plan_qty": "计划数量", "priority": "优先级", "scheduled_date": "排产日期",
    }

    def _wf_query_type(self, user_input: str) -> str:
        """从查询输入解析流程类型（报销/退货/订单/排产/图纸/客户/产品等）。"""
        for pat, wf in (
            (r"报销|费用", "expense_reimbursement"),
            (r"退货|退换货|退款", "return_process"),
            (r"订单|下单", "order_approve"),
            (r"排产|排程|生产计划", "production_schedule"),
            (r"图纸|工艺", "drawing_change"),
            (r"客户变更|客户信息|客户资料", "customer_change"),
            (r"产品变更|产品信息", "product_change"),
        ):
            if re.search(pat, user_input):
                return wf
        return ""

    # ────────────────────────────────────────────────────────────
    # v6.61：流程定义训练——从文本/PDF 提取流程内容（流程名/审批链/必填字段/
    # 触发词/模板），生成训练申请 proposed，经 submit_workflow_def_change
    # 走 workflow_def_change 审批链，审批通过后 apply_workflow_def_change 生效。
    # ────────────────────────────────────────────────────────────
    _ROLE_CN_TO_KEY = {
        "销售经理": "sales_manager", "销售总监": "sales_manager",
        "生产经理": "production_manager", "生产总监": "production_manager",
        "财务经理": "finance_manager", "财务总监": "finance_manager",
        "仓储主管": "warehouse_manager", "仓储经理": "warehouse_manager",
        "质量经理": "qc_manager", "质检经理": "qc_manager",
        "部门经理": "manager", "总经理": "admin", "老板": "admin",
        "人事经理": "hr", "行政经理": "hr", "专员": "staff",
    }
    _ROLE_SUFFIX = ("经理", "主管", "总监", "管理员")

    def _extract_workflow_from_text(
            self, user_input: str,
            att_texts: Optional[List[str]] = None) -> tuple:
        """从用户描述 + PDF/文档附件文本提取流程定义（尽力提取）。

        解析规则（对 PDF 制度/模板文档与自然语言描述同样适用）：
            - 流程名：`流程名[:：]` 显式指定；或描述中的"XX流程/XX审批"
            - 流程类型：`workflow_type[:：]` 或由流程名 ASCII 化兜底
            - 审批链：按序匹配中文角色词（销售经理/财务经理/仓储主管等），
              映射为 approval_chain 步骤 [{step, role, action}]
            - 必填字段：`必填字段[:：]` 后逗号/顿号/空格分隔的列表
            - 触发词：`触发词|触发关键词[:：]` 后列表
            - 模板：附件（PDF）全文或用户描述保存为 thresholds.template

        Returns:
            (proposed dict | None, missing list)：missing 为缺失的必填项
            （workflow_type/workflow_name/approval_chain），供引导用户补充。
        """
        import unicodedata
        src = (user_input or "")
        pdf_text = "\n".join([t for t in (att_texts or []) if t]) or ""
        if pdf_text:
            src = src + "\n" + pdf_text

        missing = []
        # 1. 流程名（优先显式"流程名:"；其次句中"XX审批流程/XX流程"贪心整词提取）
        wf_name = ""
        m = re.search(r"(?:流程名|流程名称|workflow_name)\s*[:：]\s*(\S+)", src)
        if m:
            wf_name = m.group(1).strip()
        if not wf_name:
            # 框架词（训练/创建/把…做成…）之后贪心捕获整词流程名：
            # "训练一个采购审批流程"→"采购审批流程"；避免"训练一个"被吞入名称
            framing = (r"(?:请|帮我|麻烦)?(?:训练|创建|新建|定义|设计|定制|"
                       r"配置|制作|做成|做|生成|制定)\s*(?:一个|一份|这个|这份|"
                       r"该|一下)?")
            m = re.search(
                framing + r"([\u4e00-\u9fa5]{2,10}(?:审批流程|审批|流程))",
                user_input or "")
            if m:
                wf_name = m.group(1)
            else:
                # 文档驱动训练（"把这份PDF做成流程"）：流程名取自附件文档标题/首行，
                # 而非句式整句或文档末尾审批描述（如"5000元以下部门经理审批"）
                doc_construct = re.search(
                    r"(把|用|根据|按|依据).{0,8}(pdf|PDF|文档|文件|附件|制度|模板).{0,12}(训练|做成|定义|创建|生成|制定).{0,10}(流程|审批|工作流)",
                    user_input or "")
                if doc_construct:
                    # 优先：附件文档首行标题（XX制度/规范/标准/办法/规定/审批流程）
                    doc_lines = [l.strip() for l in (pdf_text or "").splitlines()
                                 if l.strip()]
                    title = ""
                    if doc_lines:
                        tm = re.match(
                            r"^[\u4e00-\u9fa5A-Za-z0-9]{2,20}?"
                            r"(?:制度|规范|标准|办法|规定|审批流程|审批单|流程|审批)$",
                            doc_lines[0])
                        if tm:
                            title = tm.group(0)
                    if title:
                        wf_name = title
                    else:
                        # 回退：文档正文首个候选（标题在文档头部，取首个而非最后）
                        doc_cand = re.findall(
                            r"[\u4e00-\u9fa5]{2,10}(?:审批流程|审批|流程|制度|规范|标准|办法|规定)",
                            pdf_text or "")
                        if doc_cand:
                            wf_name = doc_cand[0]
                else:
                    u_cand = re.findall(
                        r"[\u4e00-\u9fa5]{2,10}(?:审批流程|审批|流程)",
                        user_input or "")
                    cand = u_cand or re.findall(
                        r"[\u4e00-\u9fa5]{2,10}(?:审批流程|审批|流程|制度|规范|标准|办法|规定)",
                        src)
                    if cand:
                        wf_name = cand[-1]
            # 只剥一层尾部后缀：采购审批流程→采购审批；报销制度→报销
            core = re.sub(r"(流程|制度|规范|标准|办法|规定)$", "", wf_name)
            if not core:
                core = re.sub(r"审批$", "", wf_name)
            if core:
                wf_name = core + "流程"
        if not wf_name:
            missing.append("流程名称")

        # 2. 流程类型（workflow_type；用核心名保证每个流程唯一，避免训练碰撞）
        wf_type = ""
        m = re.search(r"(?:workflow_type|流程类型|类型)\s*[:：]\s*([\w-]+)", src)
        if m:
            wf_type = m.group(1).strip()
        if not wf_type and wf_name:
            slug = "".join(c for c in unicodedata.normalize(
                "NFKD", wf_name) if c.isascii() and c.isalnum()).lower()
            if slug:
                wf_type = slug[:50]
            else:
                # 全中文名：核心名（去"流程/审批"一层后缀）作为类型，保持唯一可读
                wf_type = re.sub(r"流程$", "", wf_name) or wf_name
                wf_type = re.sub(r"审批$", "", wf_type) or wf_type
                wf_type = wf_type[:50]

        # 3. 审批链：按序提取中文角色词（去重，保持顺序）
        chain = []
        role_hits = re.findall(
            r"([\u4e00-\u9fa5]{2,4}(?:经理|主管|总监|管理员))", src)
        seen = set()
        for i, r in enumerate(role_hits, 1):
            key = self._ROLE_CN_TO_KEY.get(r, "")
            if key and key not in seen:
                seen.add(key)
                chain.append({"step": i, "role": key, "action": "审批"})
        if not chain:
            chain = [{"step": 1, "role": "manager", "action": "审批"}]
            missing.append("审批链（已默认部门经理单级，可修改）")

        # 4. 必填字段（冒号或空格分隔均可；"/"也分隔；"触发词"等后续段落不混入）
        fields = []
        m = re.search(
            r"(?:必填字段|required_fields|必填|需要填写)[:：\s]+"
            r"([^\n]+?)(?=[，,]\s*(?:触发词|触发关键词|keywords|关键词)|\n|$)",
            src)
        if m:
            fields = [x.strip() for x in re.split(
                r"[,，、/\s]+", m.group(1)) if x.strip()]
        if not fields:
            fields = ["reason"]
            missing.append("必填字段（已默认事由，可修改）")

        # 5. 触发词（冒号或空格分隔均可）
        trigger_keywords = []
        m = re.search(
            r"(?:触发词|触发关键词|trigger_keywords|关键词|keywords)[:：\s]+([^\n]+)", src)
        if m:
            trigger_keywords = [x.strip() for x in re.split(
                r"[,，、\s]+", m.group(1)) if x.strip()]

        # 6. 查库项目（v6.64 查询流程：gate_checks.query_steps）
        # 格式：每行一组 key=value——表/键/字段/权限/类型/问题/提示/别名/模式/标签
        #   1. 表=inventory 键=product_code 字段=raw,finished,safety_stock 权限=can_inventory 别名=inv
        #   2. 类型=kb 问题=${inv.name} 知识 别名=kb
        #   3. 类型=web 问题=${inv.name} 别名=web
        #   4. 类型=llm 提示=请汇总库存、价格与知识库/网络信息 别名=ans
        # 类型缺省 db（查业务表）；kb=知识库检索 / web=联网检索 / llm=LLM 生成
        query_steps = []
        qm = re.search(
            r"(?:查库项目|查询项目|query_steps)\s*[:：]?\s*(.*?)"
            r"(?=(?:\n\s*)?(?:承接查询意图|query_intent_map|触发词|触发关键词|"
            r"必填字段|required_fields|审批链)|$)",
            src, re.S)
        if qm:
            # 步骤分隔兼容换行与分号（前端多行输入可能合并为一行、以分号分隔）
            for _seg in re.split(r"[；;\n]+", qm.group(1)):
                _line = _seg.strip()
                if not _line:
                    continue
                _line = re.sub(r"^\d+[.、)]\s*", "", _line)
                _kv = dict(re.findall(r"([\u4e00-\u9fa5a-zA-Z_]+)\s*=\s*(\S+)", _line))
                if not _kv:
                    continue
                _step = {"step": len(query_steps) + 1}
                _step["type"] = _kv.get("类型") or _kv.get("type") or "db"
                if _kv.get("表") or _kv.get("table"):
                    _step["table"] = _kv.get("表") or _kv.get("table")
                if _kv.get("键") or _kv.get("key_field"):
                    _step["key_field"] = _kv.get("键") or _kv.get("key_field")
                _fs = _kv.get("字段") or _kv.get("fields")
                if _fs:
                    _step["fields"] = [x.strip() for x in _fs.split(",") if x.strip()]
                if _kv.get("权限") or _kv.get("required_permission"):
                    _step["required_permission"] = (
                        _kv.get("权限") or _kv.get("required_permission"))
                if _kv.get("问题") or _kv.get("query"):
                    _step["query"] = _kv.get("问题") or _kv.get("query")
                if _kv.get("提示") or _kv.get("prompt"):
                    _step["prompt"] = _kv.get("提示") or _kv.get("prompt")
                if _kv.get("别名") or _kv.get("as"):
                    _step["as"] = _kv.get("别名") or _kv.get("as")
                if _kv.get("模式") or _kv.get("mode"):
                    _step["mode"] = _kv.get("模式") or _kv.get("mode")
                if _kv.get("标签") or _kv.get("label"):
                    _step["label"] = _kv.get("标签") or _kv.get("label")
                query_steps.append(_step)

        # 7. 承接查询意图（v6.64：thresholds.query_intent_map {意图: 本流程类型}，
        # 协调器据此把查询类意图分派到本查询流程执行）
        query_intent_map = {}
        qim = re.search(
            r"(?:承接查询意图|query_intent_map)\s*[:：]\s*([^\n；;]+)", src)
        if qim:
            for it in re.split(r"[,，、\s]+", qim.group(1).strip()):
                if it:
                    query_intent_map[it] = wf_type

        _thresholds = {
            "trigger_keywords": trigger_keywords,
            "biz_type": "custom",
            "biz_id": "auto",
            # v6.61：模板全文（PDF 附件或用户描述），训练申请单展示用
            "template": pdf_text or (user_input or "")[:2000],
        }
        if query_intent_map:
            _thresholds["query_intent_map"] = query_intent_map
        _gate_checks = {
            "required_fields": {"1": fields},
            "required_approvals": {
                str(s.get("step")): {"role": s.get("role"), "required": True}
                for s in chain},
        }
        if query_steps:
            _gate_checks["query_steps"] = query_steps

        proposed = {
            "workflow_type": wf_type,
            "workflow_name": wf_name,
            "owner_dept": "system",
            "approval_chain": chain,
            "notify_rules": [],
            "thresholds": _thresholds,
            "gate_checks": _gate_checks,
        }
        return proposed, missing

    def _build_train_doc_inst(self, cfg_id: Any, proposed: dict,
                              user: dict, att_texts: Optional[List[str]]) -> dict:
        """构造训练申请单伪实例（与流程实例同结构），供 _render_wf_doc 同模板渲染。"""
        import time as _time
        from datetime import datetime
        chain = proposed.get("approval_chain") or []
        fields = []
        gc = proposed.get("gate_checks") or {}
        rf = gc.get("required_fields") or {}
        if isinstance(rf, dict):
            for v in rf.values():
                if isinstance(v, list):
                    fields.extend(str(x) for x in v if x)
        tk = (proposed.get("thresholds") or {}).get("trigger_keywords") or []
        # v6.64 查询流程：查库项目与承接查询意图展示
        qs = (proposed.get("gate_checks") or {}).get("query_steps") or []
        qs_desc = "、".join(
            f"{s.get('type', 'db')}:{s.get('table') or s.get('label') or s.get('query') or ''}"
            for s in qs if isinstance(s, dict)) or "—"
        qim = (proposed.get("thresholds") or {}).get("query_intent_map") or {}
        qim_desc = "、".join(qim.keys()) or "—"
        # 目标流程审批链（作为单据内容字段展示；本次审批链为 workflow_def_change 链）
        chain_desc = " → ".join(
            self._ROLE_NAMES.get(s.get("role", ""), s.get("role", ""))
            for s in chain if isinstance(s, dict)) or "—"
        u_id = user.get("id") or user.get("user_id") or "system"
        u_name = user.get("name") or user.get("username") or u_id
        return {
            "instance_id": cfg_id,
            "workflow_type": proposed.get("workflow_type", ""),
            "status": "pending",
            "created_by": u_id,
            "created_at": datetime.now().isoformat(),
            "biz_type": "workflow_training",
            "biz_id": proposed.get("workflow_type", ""),
            "extra_data": {"biz_data": {
                "流程名称": proposed.get("workflow_name", ""),
                "流程类型": proposed.get("workflow_type", ""),
                "申请人": f"{u_name}（{u_id}）",
                "审批链": chain_desc,
                "必填字段": "、".join(fields) if fields else "—",
                "触发词": "、".join(tk) if tk else "（训练后对话可触发）",
                "查库项目": qs_desc,
                "承接查询意图": qim_desc,
                "模板来源": "PDF附件" if att_texts else "文本描述",
            }},
            "steps_done": [],
            "current_step": 1,
        }

    def _handle_workflow_train(self, user_input: str,
                               context: Dict[str, Any]) -> AgentResponse:
        """流程定义训练申请（v6.61）：文本描述或 PDF/文档附件提取流程定义 →
        提交训练审批 → 报销单样式 HTML 训练申请单（与流程单据同一模板）。

        v6.61.1 附件类型分流（不做单一解读）：
            - 图纸/工艺文件（PDF 名或内容含 图纸/图号/dwg/drawing/工艺/工序/
              作业指导书/SOP 等）→ 入知识库（RAG 可检索，辅助质量分析/工艺查询），
              不参与流程定义提取
            - 制度/模板/流程文档 → 提取流程定义 → 流程训练申请
            - 仅上传图纸/工艺文件时返回"已入知识库"单据；含制度类时叠加流程训练

        缺必填项时引导补充（流程名称/审批链/必填字段），不提交空申请。
        """
        import time as _time
        atts = context.get("attachments") or []

        # ── 1. 附件分类：图纸/工艺 → 知识库；制度/模板 → 流程训练提取源 ──
        kb_docs = []      # [(doc_id, title, doc_type)]
        train_texts = []  # 参与流程定义提取的文本
        for a in atts:
            name = (a.get("name", "") or "").strip()
            text = (a.get("text", "") or "").strip()
            if not text:
                continue
            d_type = self._classify_doc(name, text)
            if d_type in ("drawing", "process_route"):
                doc_id = f"TR-{d_type}-{int(_time.time() * 1000)}-{len(kb_docs)}"
                try:
                    from prog.llm.knowledge_base import KnowledgeBase
                    KnowledgeBase.get_instance().add_document(
                        doc_id, text, source="知识助手-训练上传",
                        metadata={"title": name or doc_id,
                                  "doc_type": d_type,
                                  "tags": [d_type]})
                    kb_docs.append((doc_id, name or doc_id, d_type))
                except Exception:
                    kb_docs.append((doc_id, name or doc_id, d_type))
            else:
                train_texts.append(text)

        # ── 2. 仅图纸/工艺：返回知识库入库单据（不触发流程训练） ──
        if kb_docs and not train_texts:
            return self._render_kb_ingest_doc(kb_docs)

        # ── 3. 流程定义训练（含制度/模板类附件文本或纯文本描述） ──
        try:
            from prog.runtime.database import get_database
            from prog.runtime.workflow_enforcer import submit_workflow_def_change
            db = get_database()
        except Exception:
            db = None
        if db is None:
            return self._format_response("数据库不可用，无法提交流程训练申请。")

        proposed, missing = self._extract_workflow_from_text(user_input, train_texts)
        # 审批链默认 manager 单级视为可接受（标注可修改）；缺流程名/类型必须引导
        if not proposed.get("workflow_name") or not proposed.get("workflow_type"):
            hint = "、".join(dict.fromkeys(missing))
            kb_line = (f"（已收到 {len(kb_docs)} 份图纸/工艺文件入库）" if kb_docs else "")
            return self._format_response(
                f"流程训练申请信息不全，缺少：{hint}。{kb_line}\n\n"
                "请补充流程描述，例如：\n"
                "- 训练一个「采购审批流程」：审批链 部门经理→财务经理，必填字段"
                "供应商/金额/事由，触发词 采购审批\n"
                "- 或上传 PDF 制度/模板文档后说「把这份PDF做成流程」")

        user = (context or {}).get("user", {}) or {}
        u_id = user.get("id") or user.get("user_id") or "system"

        # v6.64 查询流程：个人免审批直接生效（只读查询步骤，经表白名单 +
        # 权限声明校验，submit_query_flow 内强校验）；业务流程仍走
        # workflow_def_change 审批链（填表→审批→生效）。
        _gc_proposed = proposed.get("gate_checks") or {}
        if isinstance(_gc_proposed, str):
            import json as _json
            try:
                _gc_proposed = _json.loads(_gc_proposed)
            except Exception:
                _gc_proposed = {}
        if _gc_proposed.get("query_steps"):
            from prog.runtime.workflow_enforcer import submit_query_flow
            qf_result = submit_query_flow(proposed, user=user, db=db)
            if not qf_result.get("success"):
                return self._format_response(
                    f"查询流程创建失败：{qf_result.get('error', '未知错误')}")
            inst = self._build_train_doc_inst(
                f"QF-{qf_result.get('workflow_type')}", proposed, user,
                train_texts)
            inst["status"] = "approved"
            doc = self._render_wf_doc(
                inst, db, chain=[], wf_name=proposed.get("workflow_name"))
            note = ("\n\n<small style='color:#059669'>✅ 查询流程已创建并直接生效"
                    "（查询流程为只读性质，免审批；已按表白名单与权限声明校验）。"
                    "</small>")
            return self._format_response(doc + note, {
                "query": user_input,
                "workflow_type": proposed.get("workflow_type"),
                "query_flow_created": True,
            })

        cfg_id = submit_workflow_def_change(proposed, db=db, changed_by=u_id)
        if not cfg_id:
            return self._format_response("流程训练申请提交失败，请稍后重试。")

        inst = self._build_train_doc_inst(cfg_id, proposed, user, train_texts)
        # 本次审批的签字链 = workflow_def_change 链（训练可定义，兜底 manager 单级）
        try:
            from prog.runtime.approval_chain import get_approval_chain
            wfdc_chain = get_approval_chain("workflow_def_change", db=db)
        except Exception:
            wfdc_chain = None
        doc = self._render_wf_doc(
            inst, db,
            chain=wfdc_chain,
            wf_name=proposed.get("workflow_name"))
        kb_line = ""
        if kb_docs:
            kb_line = ("\n\n<small style='color:#059669'>另已收到 "
                       f"{len(kb_docs)} 份图纸/工艺文件并入知识库"
                       "（可直接咨询「XX 工艺要求/图纸信息」）。</small>")
        note = ("\n\n<small style='color:#6b7280'>流程训练申请已提交（审批单 "
                f"{cfg_id}），经审批链逐级「同意」后流程定义生效；"
                "回复「同意」推进审批。</small>")
        return self._format_response(doc + kb_line + note, {
            "query": user_input,
            "training_id": cfg_id,
            "workflow_type": proposed.get("workflow_type"),
        })

    def _classify_doc(self, name: str, text: str) -> str:
        """附件/PDF 文档类型识别（v6.61.1）：图纸/工艺文件 → 入知识库，
        制度/模板/流程文档 → 流程定义训练提取源。文件名与内容前 500 字双通道。"""
        head = (text or "")[:500]
        n = (name or "").lower()
        if (re.search(r"图纸|图号|dwg|drawing", n)
                or re.search(r"图纸|图号|dwg|drawing", head)):
            return "drawing"
        if (re.search(r"工艺|工序|作业指导书|sop|工艺卡|流程卡|process", n)
                or re.search(r"工艺|工序|作业指导书|sop|工艺卡", head)):
            return "process_route"
        return "policy"

    def _render_kb_ingest_doc(self, kb_docs: List[tuple]) -> AgentResponse:
        """图纸/工艺文件入知识库结果单据（v6.61.1，HTML 卡片展示）。"""
        esc = self._wf_esc
        rows = ""
        for doc_id, title, d_type in kb_docs:
            tname = {"drawing": "图纸", "process_route": "工艺文件"}.get(d_type, d_type)
            rows += (
                f"<tr><td style='padding:5px 8px;border:1px solid #e5e7eb;"
                f"font-size:13px'>📄 {esc(title)}</td>"
                f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>"
                f"{esc(tname)}</td>"
                f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:12px;"
                f"color:#6b7280'>{esc(doc_id)}</td></tr>")
        html = (
            f"<div style='font-family:&quot;Microsoft YaHei&quot;,sans-serif;"
            f"max-width:560px;background:#ffffff;border:1px solid #e5e7eb;"
            f"border-radius:8px;padding:14px 16px;margin:4px 0;color:#1f2937;"
            f"box-shadow:0 1px 3px rgba(0,0,0,.06)'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"border-bottom:2px solid #059669;padding-bottom:8px;margin-bottom:10px'>"
            f"<span style='font-size:15px;font-weight:700;color:#065f46'>📚 知识库入库</span>"
            f"<span style='background:#059669;color:#ffffff;border-radius:999px;"
            f"padding:2px 10px;font-size:12px'>已入库 {len(kb_docs)} 份</span></div>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<tr><td style='padding:5px 8px;background:#f8fafc;border:1px solid #e5e7eb;"
            f"color:#6b7280;font-size:12px'>文档标题</td>"
            f"<td style='padding:5px 8px;background:#f8fafc;border:1px solid #e5e7eb;"
            f"color:#6b7280;font-size:12px'>类型</td>"
            f"<td style='padding:5px 8px;background:#f8fafc;border:1px solid #e5e7eb;"
            f"color:#6b7280;font-size:12px'>文档ID</td></tr>"
            f"{rows}</table></div>"
            f"\n\n<small style='color:#6b7280'>图纸/工艺文件已入知识库，可直接咨询"
            f"「XX 的工艺要求」「XX 图纸信息」；如需基于制度/模板训练新流程，"
            f"可继续上传流程制度文档并说明流程要求。</small>")
        return self._format_response(html, {"kb_docs": kb_docs})

    def render_training_doc(self, cfg_id: Any, proposed: dict,
                            user: dict = None, steps_done: Optional[list] = None,
                            status: str = "pending",
                            current_step: int = 1,
                            att_texts: Optional[List[str]] = None,
                            db: Any = None) -> str:
        """训练申请单渲染（v6.61，供 coordinator 审批推进后同模板展示）。

        与流程单据共用 _render_wf_doc 同一模板：构造伪实例 →
        审批签字链由 proposed.approval_chain × steps_done（已批签字）组成。
        """
        if db is None:
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                db = None
        inst = self._build_train_doc_inst(cfg_id, proposed or {}, user or {}, att_texts)
        inst["steps_done"] = [s for s in (steps_done or []) if isinstance(s, dict)]
        inst["current_step"] = int(current_step or 1)
        inst["status"] = status
        # 本次审批签字链 = workflow_def_change 链（与训练申请单一致）
        wfdc_chain = None
        try:
            from prog.runtime.approval_chain import get_approval_chain
            wfdc_chain = get_approval_chain("workflow_def_change", db=db)
        except Exception:
            wfdc_chain = None
        return self._render_wf_doc(
            inst, db,
            chain=wfdc_chain,
            wf_name=(proposed or {}).get("workflow_name"))

    @staticmethod
    def _wf_esc(value: Any) -> str:
        """HTML 转义（单据字段值渲染用）。"""
        import html as _html
        return _html.escape(str(value if value is not None else ""), quote=True)

    @staticmethod
    def _md_inline(text: str) -> str:
        """行内 Markdown 标记（text 须已 HTML 转义；与前端 mdInline 对齐）：
        **bold** / [text](url) / `code` / *italic*——仅匹配闭合对，流式安全。"""
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                      r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"(^|[^*])\*([^*\n]+)\*", r"\1<em>\2</em>", text)
        return text

    @classmethod
    def _md_to_html(cls, text: str) -> str:
        """零依赖轻量 Markdown→HTML（与前端 index.html mdToHtml 行为对齐）。

        支持：代码块 / 表格 / 标题#~##### / 分隔线 / 引用 / 无序·有序列表 /
        普通段落；行内经 _md_inline。用于查询流程 LLM 汇总步骤渲染，
        使卡片内 MD 标记（**加粗**、- 列表等）按排版展示而非原文。
        """
        if not text:
            return ""
        esc = cls._wf_esc
        lines = text.split("\n")
        html: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            # 代码块（``` 闭合对；未闭合按 pre 输出渐进显示）
            if re.match(r"^```", line.strip()):
                lang = line.strip()[3:].strip()
                i += 1
                code = []
                while i < n and not re.match(r"^```", lines[i].strip()):
                    code.append(lines[i]); i += 1
                if i < n:
                    i += 1
                lang_attr = (f' class="lang-{esc(lang)}"'
                             if re.match(r"^[a-zA-Z0-9_+-]*$", lang) else "")
                html.append(f"<pre{lang_attr}><code>{esc(chr(10).join(code))}</code></pre>")
                continue
            # 表格：表头 + |---| 分隔行
            if (re.match(r"^\|.*\|$", line.strip())
                    and i + 1 < n
                    and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip())):
                header = line.strip().strip("|").split("|")
                head = "<tr>" + "".join(
                    f"<th>{cls._md_inline(esc(c.strip()))}</th>" for c in header) + "</tr>"
                i += 2
                body = []
                while i < n and re.match(r"^\|.*\|$", lines[i].strip()):
                    cells = lines[i].strip().strip("|").split("|")
                    body.append("<tr>" + "".join(
                        f"<td>{cls._md_inline(esc(c.strip()))}</td>" for c in cells) + "</tr>")
                    i += 1
                html.append("<table><thead>" + head + "</thead>"
                            + ("<tbody>" + "".join(body) + "</tbody>" if body else "")
                            + "</table>")
                continue
            # 标题 # ~ #####
            hm = re.match(r"^(#{1,5})\s+(.*)$", line)
            if hm:
                h = min(len(hm.group(1)), 4)
                html.append(f"<h{h}>{cls._md_inline(esc(hm.group(2)))}</h{h}>")
                i += 1
                continue
            # 分隔线
            if re.match(r"^(\s*[-*_]\s*){3,}$", line):
                html.append("<hr>")
                i += 1
                continue
            # 引用块（连续 > 行）
            if re.match(r"^>\s?", line):
                q = []
                while i < n and re.match(r"^>\s?", lines[i]):
                    q.append(cls._md_inline(esc(re.sub(r"^>\s?", "", lines[i]))))
                    i += 1
                html.append("<blockquote>" + "<br>".join(q) + "</blockquote>")
                continue
            # 无序列表（连续 - / * / + 行）
            if re.match(r"^\s*[-*+]\s+", line):
                ul = []
                while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                    ul.append("<li>" + cls._md_inline(
                        esc(re.sub(r"^\s*[-*+]\s+", "", lines[i]))) + "</li>")
                    i += 1
                html.append("<ul>" + "".join(ul) + "</ul>")
                continue
            # 有序列表（连续 1. / 1) 行）
            if re.match(r"^\s*\d+[.)]\s+", line):
                ol = []
                while i < n and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                    ol.append("<li>" + cls._md_inline(
                        esc(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))) + "</li>")
                    i += 1
                html.append("<ol>" + "".join(ol) + "</ol>")
                continue
            # 普通段落：收集到空行或下一个块级标记
            para = [line]
            i += 1
            while (i < n and lines[i].strip() != ""
                   and not re.match(r"^(#{1,5}\s|>\s?|```|\|.*\||\s*[-*+]\s+|\s*\d+[.)]\s+|(\s*[-*_]\s*){3,}$)",
                                    lines[i])):
                para.append(lines[i])
                i += 1
            html.append("<p>" + cls._md_inline(esc("\n".join(para))) + "</p>")
        return "".join(html)

    def _handle_workflow_query(self, user_input: str,
                               context: Dict[str, Any]) -> AgentResponse:
        """流程实例查询：按 实例号 / 流程类型+当前用户 定位既有单据，
        渲染报销单样式 HTML（含申请字段与审批签字进度）。

        返回：
            AgentResponse（content 为 HTML 单据，前端 innerHTML 直通渲染）
        """
        import json as _json
        try:
            from prog.runtime.database import get_database
            db = get_database()
        except Exception:
            db = None
        if db is None:
            return self._format_response("数据库不可用，无法查询流程单据。")

        # 1. 解析查询条件
        m = re.search(r"(?:实例|编号|单号)\s*[#]?(\d+)", user_input)
        instance_id = int(m.group(1)) if m else None
        wf_type = self._wf_query_type(user_input)
        user = (context or {}).get("user", {}) or {}
        u_id = user.get("id") or user.get("user_id") or ""
        u_role = user.get("role") or ""

        # 2. 定位实例：优先实例号；否则按流程类型 + 当前用户（admin 不限）查最近
        instances = []
        try:
            if instance_id is not None:
                row = db.query_one("workflow_instances", {"instance_id": instance_id})
                if row:
                    # P1-10 修复：按号直查同样校验归属（防 IDOR）——
                    # 非 admin 仅可查看本人创建的实例，与下方 created_by 过滤口径一致
                    if u_role == "admin" or row.get("created_by") == u_id:
                        instances = [row]
            else:
                filters = {}
                if wf_type:
                    filters["workflow_type"] = wf_type
                if u_id and u_role != "admin":
                    filters["created_by"] = u_id
                instances = db.query_many(
                    "workflow_instances", filters or None,
                    order_by="instance_id DESC", limit=5) or []
        except Exception:
            instances = []

        # 3. 无结果提示
        if not instances:
            hint = f"流程类型「{wf_type}」" if wf_type else "该条件"
            return self._format_response(
                f"未找到{hint}相关的流程实例。可尝试：\n"
                "- 指定实例号：显示实例12\n"
                "- 按流程类型：查看最近的报销流程\n"
                "- 查看本人全部：显示我的流程")

        # 4. 渲染单据（仅首条完整渲染，多条附计数提示，避免对话过长）
        doc = self._render_wf_doc(instances[0], db)
        # v1.6.57 审批意见展示（多人协作场景①）：附留言列表（时间正序）
        comments_html = ""
        try:
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            _wf = WorkflowEnforcer(database=db)
            cmts = _wf.list_comments(instances[0].get("instance_id")) or []
            if cmts:
                esc = self._wf_esc
                c_rows = []
                for c in cmts:
                    aid = c.get("author_id") or "?"
                    aname = aid
                    if aid and aid != "anonymous":
                        try:
                            au = db.query_one("users", {"user_id": aid})
                            if au and (au.get("name") or au.get("username")):
                                aname = f"{au.get('name') or au.get('username')}（{aid}）"
                        except Exception:
                            pass
                    c_rows.append(
                        f"<div style='margin-top:6px;padding:6px 8px;"
                        f"background:#fffbeb;border:1px solid #fde68a;"
                        f"border-radius:6px;font-size:12px'>"
                        f"<span style='color:#92400e;font-weight:700'>📌 {esc(aname)}"
                        f"（第{esc(c.get('step') or 1)}步）</span>："
                        f"{esc(c.get('content') or '')}</div>")
                comments_html = (
                    "\n\n<div style='font-size:13px;font-weight:700;"
                    "color:#1e3a8a;margin-top:10px'>💬 审批意见"
                    f"（{len(cmts)}条）</div>" + "".join(c_rows))
        except Exception:
            comments_html = ""
        doc = doc + comments_html
        note = ""
        if instance_id is None and len(instances) > 1:
            note = (f"\n\n<small style='color:#6b7280'>共匹配 {len(instances)} 条"
                    f"流程实例，已展示最新 1 条（实例 {instances[0].get('instance_id')}）；"
                    f"输入「显示实例N」可查看指定单据。</small>")
        return self._format_response(doc + note, {
            "query": user_input,
            "instance_id": instances[0].get("instance_id"),
        })

    def _render_wf_doc(self, inst: Dict[str, Any], db: Any,
                       chain: Optional[List[dict]] = None,
                       wf_name: Optional[str] = None) -> str:
        """渲染报销单样式单据 HTML：单据头 + 基础信息 + 申请字段 + 审批签字链。

        v6.61：流程单据与训练申请单共用同一模板——新增 chain/wf_name 可选参数：
        - chain：审批链步骤列表（缺省从 workflow_configs 定义行读取；训练申请单
          的审批链来自 proposed 或审批行，无需依赖定义行）
        - wf_name：流程名（缺省查定义行/_WF_TYPE_NAMES；训练新建流程无定义行时传入）
        """
        import json as _json
        from datetime import datetime
        esc = self._wf_esc

        iid = inst.get("instance_id")
        wf_type = inst.get("workflow_type") or ""
        status = inst.get("status") or "running"
        created_by = inst.get("created_by") or ""
        created_at = inst.get("created_at")
        biz_type = inst.get("biz_type") or ""
        biz_id = inst.get("biz_id") or ""
        current_step = int(inst.get("current_step") or 1)

        # 流程名（参数 > DB 定义行 > 内置兜底）
        wf_name = wf_name or self._WF_TYPE_NAMES.get(wf_type, wf_type)
        config = {}
        try:
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            config = WorkflowEnforcer(database=db)._get_workflow_config(wf_type) or {}
            wf_name = config.get("workflow_name") or wf_name
        except Exception:
            pass

        # 申请人姓名（users 表优先，失败显示工号）
        starter = esc(created_by)
        try:
            u = db.query_one("users", {"user_id": created_by}) if created_by else None
            if u:
                nm = u.get("name") or u.get("username") or ""
                starter = f"{esc(nm)}（{esc(created_by)}）" if nm else esc(created_by)
        except Exception:
            pass

        # 申请字段（v6.59 暂存于 extra_data.biz_data）
        extra = inst.get("extra_data") or {}
        if isinstance(extra, str):
            try:
                extra = _json.loads(extra)
            except Exception:
                extra = {}
        biz = extra.get("biz_data") or {}
        if isinstance(biz, str):
            try:
                biz = _json.loads(biz)
            except Exception:
                biz = {}
        biz_rows = ""
        for k, v in biz.items():
            if v is None or v == "" or isinstance(v, (dict, list)):
                continue
            label = self._WF_FIELD_NAMES.get(str(k), str(k))
            biz_rows += (f"<tr><td style='width:96px;padding:5px 8px;background:#f8fafc;"
                         f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>{esc(label)}</td>"
                         f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>{esc(v)}</td></tr>")

        # 审批签字链：approval_chain（各步角色）+ steps_done（已批签字）
        chain = chain if chain is not None else config.get("approval_chain") or []
        if isinstance(chain, str):
            try:
                chain = _json.loads(chain)
            except Exception:
                chain = []
        steps_done = inst.get("steps_done") or []
        if isinstance(steps_done, str):
            try:
                steps_done = _json.loads(steps_done)
            except Exception:
                steps_done = []
        done_map = {}
        for sd in steps_done:
            if isinstance(sd, dict):
                try:
                    done_map[int(sd.get("step", 0))] = sd
                except (TypeError, ValueError):
                    continue

        chain_html = ""
        if chain:
            for step in chain:
                if not isinstance(step, dict):
                    continue
                try:
                    s_no = int(step.get("step", 0))
                except (TypeError, ValueError):
                    continue
                role = step.get("role", "")
                role_name = self._ROLE_NAMES.get(role, role)
                done = done_map.get(s_no)
                if done:
                    approver = (done.get("user_name") or done.get("user_id")
                                or done.get("role") or "")
                    dt = ""
                    if done.get("done_at"):
                        try:
                            dt = str(done["done_at"])[:16].replace("T", " ")
                        except Exception:
                            dt = str(done["done_at"])[:16]
                    chain_html += (
                        f"<div style='display:flex;align-items:center;gap:8px;"
                        f"padding:5px 0;border-bottom:1px dashed #eef2f7'>"
                        f"<span style='min-width:22px;height:22px;border-radius:50%;"
                        f"background:#d1fae5;color:#047857;text-align:center;"
                        f"line-height:22px;font-size:12px'>✓</span>"
                        f"<span style='font-size:13px;color:#1f2937'>{esc(role_name)}</span>"
                        f"<span style='font-size:12px;color:#059669'>{esc(approver)} {esc(dt)}</span></div>")
                else:
                    is_next = (s_no == current_step and status not in ("completed", "approved"))
                    chain_html += (
                        f"<div style='display:flex;align-items:center;gap:8px;"
                        f"padding:5px 0;border-bottom:1px dashed #eef2f7'>"
                        f"<span style='min-width:22px;height:22px;border-radius:50%;"
                        f"background:#eef2f7;color:#9ca3af;text-align:center;"
                        f"line-height:22px;font-size:12px'>◌</span>"
                        f"<span style='font-size:13px;color:#4b5563'>{esc(role_name)}</span>"
                        f"<span style='font-size:12px;color:#f59e0b'>"
                        f"{'待审批（当前步骤）' if is_next else '待审批'}</span></div>")
        else:
            chain_html = "<div style='font-size:12px;color:#9ca3af'>未配置审批链</div>"

        status_text = {
            "running": "审批中", "completed": "已完成", "approved": "已通过",
            "rejected": "已驳回", "draft": "草稿", "pending": "待审批",
        }.get(status, status)
        status_color = {"running": "#2563eb", "completed": "#059669",
                        "approved": "#059669", "rejected": "#dc2626",
                        "draft": "#9ca3af", "pending": "#f59e0b"}.get(status, "#6b7280")

        ts = ""
        if created_at:
            try:
                ts = datetime.fromisoformat(str(created_at)).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = str(created_at)[:16]

        return (
            f"<div style='font-family:&quot;Microsoft YaHei&quot;,sans-serif;"
            f"max-width:560px;background:#ffffff;border:1px solid #e5e7eb;"
            f"border-radius:8px;padding:14px 16px;margin:4px 0;color:#1f2937;"
            f"box-shadow:0 1px 3px rgba(0,0,0,.06)'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"border-bottom:2px solid #2563eb;padding-bottom:8px;margin-bottom:10px'>"
            f"<span style='font-size:15px;font-weight:700;color:#1e3a8a'>📋 {esc(wf_name)}</span>"
            f"<span style='background:{status_color};color:#ffffff;border-radius:999px;"
            f"padding:2px 10px;font-size:12px'>{esc(status_text)}</span></div>"
            f"<table style='width:100%;border-collapse:collapse'>"
            f"<tr><td style='width:96px;padding:5px 8px;background:#f8fafc;"
            f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>单据编号</td>"
            f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>"
            f"WF-{esc(iid)}</td>"
            f"<td style='width:96px;padding:5px 8px;background:#f8fafc;"
            f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>发起时间</td>"
            f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>{esc(ts)}</td></tr>"
            f"<tr><td style='width:96px;padding:5px 8px;background:#f8fafc;"
            f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>流程类型</td>"
            f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>{esc(wf_type)}</td>"
            f"<td style='width:96px;padding:5px 8px;background:#f8fafc;"
            f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>申请人</td>"
            f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>{starter}</td></tr>"
            + (f"<tr><td style='padding:5px 8px;background:#f8fafc;"
               f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>业务单号</td>"
               f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>"
               f"{esc(biz_type)}:{esc(biz_id)}</td>"
               f"<td style='padding:5px 8px;background:#f8fafc;border:1px solid #e5e7eb;"
               f"color:#6b7280;font-size:12px'>当前步骤</td>"
               f"<td style='padding:5px 8px;border:1px solid #e5e7eb;font-size:13px'>"
               f"第 {esc(current_step)} 步</td></tr>" if (biz_type and biz_id) else "")
            + biz_rows
            + "</table>"
            f"<div style='margin-top:10px'><div style='font-size:13px;font-weight:700;"
            f"color:#1e3a8a;margin-bottom:4px'>审批签字</div>{chain_html}</div>"
            "</div>")

    def _handle_query_flow(self, user_input: str,
                           context: Dict[str, Any]) -> AgentResponse:
        """查询流程执行（v6.64）：按流程定义 gate_checks.query_steps 编排
        多步骤查库/知识库/网络/LLM 生成，返回结果卡片。

        步骤类型（step.type，缺省 db）：
            db  : 查业务表——framework execute_workflow_query（权限+表白名单）
            kb  : 知识库检索（_rag_search）
            web : 联网检索（_web_search，web_search_enabled 开关控制）
            llm : LLM 生成（_call_llm，prompt 支持 ${步骤别名.字段} 引用前步骤结果）

        权限管控：每步 required_permission 须显式声明且当前用户具备，
        缺失即拒绝执行（查询授权门禁；与流程审批同级别）。
        """
        import json as _json
        qf = context.get("query_flow") or {}
        wf_type = qf.get("workflow_type", "")
        if not wf_type:
            return self._format_response("查询流程类型缺失，无法执行。",
                                         {"query": user_input})
        try:
            from prog.runtime.database import get_database
            from prog.runtime.workflow_enforcer import execute_workflow_query
            db = get_database()
        except Exception:
            db = None
        if db is None:
            return self._format_response("数据库不可用，无法执行查询流程。",
                                         {"query": user_input,
                                          "workflow_type": wf_type})

        row = None
        try:
            row = db.query_one("workflow_configs",
                               {"workflow_type": wf_type, "is_active": True})
        except Exception:
            pass
        if not row:
            return self._format_response(
                f"查询流程「{wf_type}」不存在或未生效（须先经流程训练审批生效）。",
                {"query": user_input, "workflow_type": wf_type})
        wf_name = row.get("workflow_name") or wf_type
        gc = row.get("gate_checks") or {}
        if isinstance(gc, str):
            try:
                gc = _json.loads(gc)
            except Exception:
                gc = {}
        steps = gc.get("query_steps") or []
        if not steps:
            return self._format_response(
                f"查询流程「{wf_name}」无查库项目（gate_checks.query_steps）。",
                {"query": user_input, "workflow_type": wf_type})

        # 合并查询参数：协调器注入槽位 + 本次输入提取（如产品码 A-202）
        params = dict(qf.get("slots") or {})
        try:
            from prog.runtime.slot_engine import extract_slots
            params.update({k: v for k, v in extract_slots(user_input).items()
                           if v})
        except Exception:
            pass

        # v6.65 查询附加词：规则解析（产品名称/参数/日期/状态，零延迟）；
        # 解析结果注入 _filters 供 db 步骤附加过滤；模糊片段走 LLM 补全；
        # 规则完全无法解析时，整体调用 LLM 生成查询参数（兜底通道）。
        qf_filters: List[Dict[str, Any]] = []
        qf_notes: List[str] = []
        _db_steps = [s for s in steps
                     if isinstance(s, dict) and s.get("type", "db") == "db"]
        try:
            from prog.runtime.query_param_parser import (
                parse_query_filters, llm_complete_filters, filters_to_human,
                llm_generate_query_params)
            _parsed = parse_query_filters(user_input)
            qf_filters = list(_parsed.get("filters") or [])
            qf_notes = list(_parsed.get("notes") or [])
            _fuzzy = _parsed.get("fuzzy")
            # 模糊片段 LLM 补全：规则未覆盖的附加词（如"铝外壳的"）；
            # 补全结果也注入 _filters，并记录说明
            if _fuzzy:
                _db_tables = [str(s.get("table", "")) for s in _db_steps]
                _db_fields: List[str] = []
                for s in _db_steps:
                    for f in (s.get("fields") or []):
                        if f not in _db_fields:
                            _db_fields.append(f)
                _table_hint = ",".join(_db_tables[:3])
                _filled = llm_complete_filters(
                    _fuzzy, self._call_llm, table_hint=_table_hint,
                    fields=_db_fields)
                if _filled:
                    qf_filters.extend(_filled)
                    _human = filters_to_human(_filled)
                    if _human:
                        qf_notes.append(f"（LLM识别）{_human}")
            # 规则解析未产出任何条件、且缺所有 db 步骤主键参数时，
            # 整体交由 LLM 生成查询参数（如"查一下上周库存偏高的物料"）
            _need_key = [str(s.get("source_key") or s.get("key_field") or "")
                         for s in _db_steps]
            _need_key = [k for k in _need_key if k]
            _has_key = any(params.get(k) for k in _need_key)
            if not qf_filters and not _has_key and _db_steps:
                _llm_qp = llm_generate_query_params(
                    user_input, _db_steps, self._call_llm)
                if _llm_qp.get("params") or _llm_qp.get("filters"):
                    # 槽位已捕获的参数优先，LLM 只补缺
                    for _k, _v in (_llm_qp.get("params") or {}).items():
                        if not params.get(_k):
                            params[_k] = _v
                    if _llm_qp.get("filters"):
                        qf_filters = list(_llm_qp.get("filters") or [])
                    qf_notes.extend(_llm_qp.get("notes") or [])
            if qf_filters:
                # v6.65.1：字段适配——LLM/规则生成的字段若不属于目标表
                # （如 inventory 无产品名列，"铝合金外壳"→product_name），
                # 反查 products 转 product_code IN(...)，避免 SQL UndefinedColumn
                _target_table = (str(_db_steps[0].get("table", ""))
                                 if _db_steps else "")
                try:
                    from prog.runtime.query_param_parser import (
                        adapt_filters_to_table)
                    _adapted, _adapt_notes = adapt_filters_to_table(
                        qf_filters, _target_table, db)
                    qf_filters = _adapted
                    qf_notes.extend(_adapt_notes)
                except Exception:
                    pass
            if qf_filters:
                params["_filters"] = qf_filters
        except Exception:
            pass

        # 权限管控：每步 required_permission 须显式声明且用户具备
        user = (context or {}).get("user") or {}
        perms = user.get("permissions") if isinstance(user, dict) else None
        perms = perms if isinstance(perms, dict) else {}
        # v6.65：支持通配权限（"*": True，admin 全权限）
        _wf_wildcard = perms.get("*", False) is True
        for s in steps:
            if not isinstance(s, dict):
                continue
            req = s.get("required_permission")
            if not req or not (perms.get(req) or _wf_wildcard):
                # 审计：查询被权限拒绝同样留痕（操作人/时间/原因）
                try:
                    _uid = (user.get("id") or user.get("user_id") or "system")[:64]
                    db.insert("operation_logs", {
                        "user_id": _uid,
                        "action": "query_flow_denied",
                        "details": {"workflow_type": wf_type,
                                    "workflow_name": wf_name,
                                    "reason": f"需权限 {req or '未声明'}",
                                    "params": dict(qf.get("slots") or {})},
                    })
                except Exception:
                    pass
                return self._format_response(
                    f"无权限执行查询流程「{wf_name}」（需权限：{req or '未声明'}）。",
                    {"query": user_input, "workflow_type": wf_type,
                     "permission": False})

        # 审计概览：记录每步骤类型/数据源/结果行数
        audit_steps: List[Dict[str, Any]] = []

        # 执行 db 步骤（framework：权限+表白名单+查库）
        # v6.65：缺参不中断——db_result.missing 记录缺失参数，
        # 已提供的参数照查；缺失参数在结果卡片中给出说明（不报错）
        db_result = None
        if any((s or {}).get("type", "db") == "db" for s in steps):
            db_result = execute_workflow_query(
                wf_type, params=params, user=user, db=db)
            if not db_result.get("success"):
                return self._format_response(
                    f"查询流程「{wf_name}」执行失败："
                    f"{db_result.get('error', '未知错误')}",
                    {"query": user_input, "workflow_type": wf_type,
                     **db_result})
        db_result = db_result or {}
        qf_missing = db_result.get("missing") or []

        # 步骤变量：后步骤经 ${步骤别名.字段} 引用前步骤结果
        step_vars: Dict[str, Any] = {}

        def _resolve(text: str) -> str:
            """解析字符串中的 ${步骤别名.字段} 占位符为前序步骤结果值。

            参数：
                text: 含 ${...} 占位符的模板文本（如步骤提示/汇总文案）
            返回：
                str: 占位符替换后的文本；路径不存在时保留原样 ${...}
            """
            def _rep(m):
                """单个 ${...} 占位符的替换回调。

                参数：
                    m: 正则匹配对象（group(1)="别名.字段"路径）
                返回：
                    str: 沿 step_vars 逐级取到的值；路径缺失时返回原占位符
                """
                path = m.group(1).split(".")
                node = step_vars
                for p in path:
                    if isinstance(node, dict) and p in node:
                        node = node[p]
                    else:
                        return m.group(0)
                return str(node)
            return re.sub(r"\$\{([\w.]+)\}", _rep, str(text))

        sections: List[str] = []
        step_i = 0
        esc = self._wf_esc
        # db 步骤渲染
        # v6.65.4：按步骤索引取 step_results（execute_workflow_query 返回，
        # 与 query_steps 中 db 步骤一一对应，缺参跳过为 None）——多表流程
        # 中每个 db 步骤只渲染自己的结果，不再整表结果列表重复渲染。
        db_step_results = (db_result or {}).get("step_results") or []
        db_idx = 0
        if db_result and db_result.get("success"):
            for s in steps:
                if not isinstance(s, dict) or s.get("type", "db") != "db":
                    continue
                step_i += 1
                _alias = s.get("as") or f"db{step_i}"
                _res = (db_step_results[db_idx]
                        if db_idx < len(db_step_results) else None)
                db_idx += 1
                step_vars[_alias] = _res
                _lbl = s.get("label") or s.get("table") or f"步骤{step_i}"
                sections.append(self._render_query_result(
                    f"步骤{step_i} · {esc(_lbl)}（数据库）", _res))
                audit_steps.append({
                    "step": s.get("step", step_i), "type": "db",
                    "table": s.get("table", ""),
                    "rows": (len(_res) if isinstance(_res, list)
                             else (1 if isinstance(_res, dict) else 0))})
        # kb / web / llm 步骤
        for s in steps:
            if not isinstance(s, dict):
                continue
            stype = s.get("type", "db")
            if stype == "db":
                continue
            step_i += 1
            _alias = s.get("as") or f"s{step_i}"
            _lbl = s.get("label") or stype.upper()
            if stype == "kb":
                _q = _resolve(s.get("query") or s.get("问题") or user_input)
                _res = self._rag_search(_q)
                step_vars[_alias] = _res
                sections.append(self._render_query_result(
                    f"步骤{step_i} · {esc(_lbl)}（知识库）", _res))
                audit_steps.append({"step": s.get("step", step_i),
                                    "type": "kb", "query": _q,
                                    "rows": len(_res or [])})
            elif stype == "web":
                _q = _resolve(s.get("query") or s.get("问题") or user_input)
                _res = self._web_search(_q)
                step_vars[_alias] = _res
                sections.append(self._render_query_result(
                    f"步骤{step_i} · {esc(_lbl)}（联网）", _res))
                audit_steps.append({"step": s.get("step", step_i),
                                    "type": "web", "query": _q,
                                    "rows": len(_res or [])})
            elif stype == "llm":
                _prompt = _resolve(s.get("prompt") or s.get("提示") or user_input)
                _out = self._call_llm(_prompt)
                step_vars[_alias] = _out or ""
                sections.append(self._render_query_result(
                    f"步骤{step_i} · {esc(_lbl)}（生成）",
                    _out or "（生成失败）"))
                audit_steps.append({"step": s.get("step", step_i),
                                    "type": "llm",
                                    "prompt_len": len(_prompt),
                                    "output_len": len(_out or "")})

        head = (f"<div style='font-family:&quot;Microsoft YaHei&quot;,sans-serif;"
                f"max-width:560px;background:#ffffff;border:1px solid #e5e7eb;"
                f"border-radius:8px;padding:14px 16px;margin:4px 0;color:#1f2937;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.06)'>"
                f"<div style='display:flex;justify-content:space-between;"
                f"align-items:center;border-bottom:2px solid #2563eb;"
                f"padding-bottom:8px;margin-bottom:10px'>"
                f"<span style='font-size:15px;font-weight:700;color:#1e3a8a'>"
                f"🔍 {esc(wf_name)}</span>"
                f"<span style='background:#059669;color:#ffffff;border-radius:999px;"
                f"padding:2px 10px;font-size:12px'>查询流程</span></div>"
                + (f"<div style='font-size:12px;color:#6b7280;"
                   f"background:#f0f9ff;border-radius:6px;padding:6px 8px;"
                   f"margin-bottom:8px'>🔎 附加条件：{esc('；'.join(qf_notes))}"
                   f"</div>" if qf_notes else "")
                + (f"<div style='font-size:12px;color:#b45309;"
                   f"background:#fffbeb;border-radius:6px;padding:6px 8px;"
                   f"margin-bottom:8px'>⚠️ 缺少查询参数："
                   f"{esc('、'.join(str(m) for m in qf_missing))}"
                   f"（该步骤未查询；可补充参数后重试）</div>"
                   if qf_missing else "")
                + "".join(sections)
                + "</div>")
        # 审计：查询流程执行留痕（操作人/时间/内容/各步骤结果概览）
        try:
            _uid = (user.get("id") or user.get("user_id") or "system")[:64]
            db.insert("operation_logs", {
                "user_id": _uid,
                "action": "query_flow_execute",
                "details": {
                    "workflow_type": wf_type,
                    "workflow_name": wf_name,
                    "params": {k: v for k, v in params.items()},
                    "steps": audit_steps,
                },
            })
        except Exception:
            pass
        return self._format_response(head, {
            "query": user_input, "workflow_type": wf_type,
            "steps": step_i, "params": params,
        })

    def _render_query_result(self, title: str, data: Any) -> str:
        """渲染查询步骤结果为 HTML 片段（表格行 / 列表 / 生成文本）。"""
        esc = self._wf_esc
        rows: List[str] = []
        if isinstance(data, dict):
            for k, v in data.items():
                rows.append(
                    f"<tr><td style='width:120px;padding:4px 8px;background:#f8fafc;"
                    f"border:1px solid #e5e7eb;color:#6b7280;font-size:12px'>"
                    f"{esc(k)}</td>"
                    f"<td style='padding:4px 8px;border:1px solid #e5e7eb;"
                    f"font-size:13px'>{esc(v)}</td></tr>")
        elif isinstance(data, list) and data:
            for item in data[:10]:
                if isinstance(item, dict):
                    # 检索结果样式（title/snippet 字段）
                    if item.get("title") or item.get("snippet") or item.get("content"):
                        _t = item.get("title") or ""
                        _s = item.get("snippet") or item.get("content") or ""
                        rows.append(
                            f"<tr><td style='padding:4px 8px;border:1px solid #e5e7eb;"
                            f"font-size:13px'><b>{esc(_t)}</b>"
                            f"<div style='color:#6b7280;font-size:12px'>"
                            f"{esc(str(_s)[:120])}</div></td></tr>")
                    else:
                        # 业务行（如 inventory 行）：首字段加粗为主键，其余 k=v 紧凑
                        _kv = " · ".join(
                            f"{esc(k)}={esc(v)}" for k, v in item.items()
                            if v is not None)
                        _first = next(iter(item.values()), "")
                        rows.append(
                            f"<tr><td style='padding:4px 8px;border:1px solid #e5e7eb;"
                            f"font-size:13px'><b>{esc(_first)}</b>"
                            f"<div style='color:#6b7280;font-size:12px'>{_kv}</div></td></tr>")
                else:
                    rows.append(
                        f"<tr><td style='padding:4px 8px;border:1px solid #e5e7eb;"
                        f"font-size:13px'>{esc(item)}</td></tr>")
        elif isinstance(data, str) and data:
            # v6.67.2：LLM 生成文本先经 _md_to_html 渲染 Markdown
            # （**加粗**/- 列表等按排版展示，不再显示 MD 原文标记）
            return (f"<div style='margin-top:8px'><div style='font-size:13px;"
                    f"font-weight:700;color:#1e3a8a;margin-bottom:4px'>"
                    f"{esc(title)}</div>"
                    f"<div class='md-body' style='font-size:13px;color:#374151;"
                    f"background:#f9fafb;border-radius:6px;padding:8px'>"
                    f"{self._md_to_html(data)}</div></div>")
        else:
            rows.append(
                f"<tr><td style='padding:4px 8px;border:1px solid #e5e7eb;"
                f"font-size:13px;color:#9ca3af'>（无数据）</td></tr>")
        return (f"<div style='margin-top:8px'><div style='font-size:13px;"
                f"font-weight:700;color:#1e3a8a;margin-bottom:4px'>"
                f"{esc(title)}</div>"
                f"<table style='width:100%;border-collapse:collapse'>"
                + "".join(rows) + "</table></div>")

    def _handle_workflow_field_collection(self, user_input: str,
                                          context: Dict[str, Any],
                                          wf_instance: Dict[str, Any]) -> AgentResponse:
        """流程字段收集（v6.46：三层配置驱动，字段随训练动态增减）。

        - 必填字段：SLOT-DEFS.required_rules → workflow_configs.gate_checks
          .required_fields → 内置降级（均 DB 可训练，字段不固定）
        - 槽位提取/引导语：复用 slot_engine（SLOT-DEFS 可训练，含文件类 attachment）
        - 整句兜底：输入未被任何必填槽位消费时，赋给首个缺失的非文件 str 字段
          （通用，不绑定具体字段名）
        - 已收集字段经 metadata.wf_slots 交由 coordinator 合并进
          pending_intent.slots 跨轮收集；集齐后置 __done__ 清除延续
        """
        from prog.runtime.slot_engine import extract_slots, get_prompt_hints

        wf_type = wf_instance.get("workflow_type", "expense_reimbursement")
        instance_id = wf_instance.get("instance_id")
        wf_name = "费用报销审批"
        try:
            from prog.runtime.database import get_database
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            _cfg = WorkflowEnforcer(database=get_database())._get_workflow_config(wf_type) or {}
            wf_name = _cfg.get("workflow_name") or wf_type
        except Exception:
            pass

        required = self._wf_required_fields(wf_type)

        # 已收集字段：上一轮 pending_intent.slots + 本轮提取（仅保留必填字段）
        pending = context.get("pending_intent") or {}
        collected = {}
        _ps = pending.get("slots") if isinstance(pending, dict) else None
        if isinstance(_ps, dict):
            collected.update({k: v for k, v in _ps.items() if k in required})
        new_slots = extract_slots(user_input)
        for f in required:
            if f in new_slots and str(new_slots[f]).strip():
                collected[f] = new_slots[f]

        # 整句兜底（通用）：输入未被任何必填槽位消费时，赋给首个缺失的
        # 非文件 str 字段（不绑定具体字段名，字段可训练增减；v6.46.1：
        # 启动语排除改为动态触发关键词 + 跳过数值型字段，不再硬编码"报销/流程"）
        missing = self._wf_field_missing(collected, required)
        if missing:
            txt = (user_input or "").strip()
            # 本轮已消费：仅看本轮提取的槽位（多轮下累计 collected 非空
            # 会错误阻止后续整句兜底——"出差参加客户现场验收"无法赋给 reason）
            consumed = set(k for k in required if str(new_slots.get(k, "")).strip())
            wf_triggers = self._wf_trigger_keywords(wf_type)
            is_trigger = any(t and t in txt for t in wf_triggers)
            if 2 <= len(txt) <= 60 and not consumed and not is_trigger:
                # 候选：非 or 表达式 / 非文件 / 非数值型字段；优先 free_text
                # 字段（reason/事由），再回退其他字符串字段
                candidates = [f for f in missing
                              if "|" not in str(f) and "attachment" not in str(f)
                              and "file" not in str(f)
                              and not self._wf_field_is_numeric(f)]
                free = [f for f in candidates if self._wf_field_free_text(f)]
                target = (free or candidates)[0] if (free or candidates) else None
                if target:
                    collected[target] = txt

        missing = self._wf_field_missing(collected, required)
        detail_lines = [f"- {f}：{collected[f]} ✓" for f in required
                        if str(collected.get(f, "") or "").strip()]
        if missing:
            hints = get_prompt_hints(missing)
            hint_lines = [f"  {hints.get(f) or f'请提供字段 {f}。'}" for f in missing]
            content = (f"📋 {wf_name}（实例 {instance_id}）信息收集：\n"
                       + ("\n".join(detail_lines) if detail_lines else "  （暂无）")
                       + "\n\n还需补充以下必填信息：\n"
                       + "\n".join(hint_lines))
            resp = self._format_response(content, {
                "query": user_input, "workflow_instance": wf_instance,
            })
            resp.metadata["wf_slots"] = {
                "fields": dict(collected), "instance_id": instance_id,
                "workflow_type": wf_type,
            }
            return resp

        # 全部必填字段齐备：完成收集（流程实例由 WorkflowEnforcer 管理）
        content = (f"✅ {wf_name}申请信息已齐备（实例 {instance_id}）：\n"
                   + "\n".join(detail_lines)
                   + "\n\n流程已提交进入审批链，等待审批人处理。"
                   + "\n审批人可回复「同意」逐级推进审批。")
        # v6.59：业务数据暂存——收集字段写入 workflow_instances.extra_data.biz_data，
        # 审批全部通过后由 coordinator._apply_workflow_effect 读取执行业务生效
        # （order_approve/return_process/production_schedule/product_change/customer_change）。
        # 发起人信息一并暂存，供审批通知"流程全文"展示。
        try:
            from prog.runtime.database import get_database
            _db = get_database()
            if _db is not None:
                _biz = dict(collected)
                _user = context.get("user") or {}
                _biz.setdefault("requester",
                                _user.get("name") or _user.get("title") or _user.get("id") or "")
                try:
                    inst = _db.query_one("workflow_instances",
                                         {"instance_id": instance_id})
                    _extra = inst.get("extra_data") or {}
                    if isinstance(_extra, str):
                        import json as _json
                        try:
                            _extra = _json.loads(_extra)
                        except Exception:
                            _extra = {}
                    _extra = dict(_extra) if isinstance(_extra, dict) else {}
                    _extra["biz_data"] = _biz
                    _db.update("workflow_instances", {"extra_data": _extra},
                               {"instance_id": instance_id})
                except Exception:
                    pass
        except Exception:
            pass
        # v6.57：审批待办通知（通用流程流转）——如实反映流程具体内容与风险提示，
        # 通知第 1 步审批角色用户（users.role_id 匹配）+ 发起人；通知持久化 DB，
        # 不因会话刷新丢失；点击通知后随 chat resume_workflow 恢复审批上下文。
        try:
            from prog.runtime.event_bus import (
                EVENT_NOTIFY_APPROVAL, publish_event)
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            from prog.runtime.database import get_database
            _cfg = WorkflowEnforcer(database=get_database())._get_workflow_config(wf_type) or {}
            _chain = _cfg.get("approval_chain") or []
            if isinstance(_chain, str):
                import json as _json
                try:
                    _chain = _json.loads(_chain)
                except Exception:
                    _chain = []
            step_role = ""
            if isinstance(_chain, list) and _chain:
                step_role = (_chain[0] or {}).get("role", "")
            creator = (context.get("user") or {}).get("id") or ""
            # v6.59：通知内容注入发起人（"报销人/申请人"），审批人点击后对话展示流程全文
            _detail = dict(collected)
            _u2 = context.get("user") or {}
            _detail["申请人"] = (_u2.get("name") or _u2.get("title")
                                 or _u2.get("id") or "未知")
            publish_event(EVENT_NOTIFY_APPROVAL,
                          {"workflow_type": wf_type,
                           "instance_id": instance_id,
                           "workflow_name": wf_name,
                           "step_role": step_role, "target_user": creator,
                           "biz_detail": _detail},
                          source="knowledge_assistant")
        except Exception:
            pass
        resp = self._format_response(content, {
            "query": user_input, "workflow_instance": wf_instance,
        })
        resp.metadata["wf_slots"] = {
            "fields": dict(collected), "__done__": True,
            "instance_id": instance_id, "workflow_type": wf_type,
        }
        return resp

    def _handle_process_guide(self, user_input: str,
                              context: Dict[str, Any]) -> AgentResponse:
        """
        处理流程指导意图。

        设计意图：
            提供业务流程指导、操作步骤说明。
            通过RAG检索流程文档，返回结构化的操作指引。

        参数：
            user_input: 用户输入（如"报销流程是怎样的"）
            context: 会话上下文

        返回：
            AgentResponse: 流程指导结果（含操作步骤、注意事项）
        """
        # v6.45：coordinator 已通过 WorkflowEnforcer 启动流程实例时，
        # 进入流程字段收集（多轮引导补全必填字段，字段列表来自
        # workflow_configs.gate_checks.required_fields，DB 可训练）
        wf_instance = context.get("workflow_instance")
        if wf_instance and isinstance(wf_instance, dict):
            return self._handle_workflow_field_collection(
                user_input, context, wf_instance)

        # 检索流程相关文档
        contexts = self._rag_search(user_input)

        # 构建流程指导专用提示词
        prompt = self._build_process_prompt(user_input, contexts)
        llm_output = self._call_llm(prompt)

        if llm_output:
            content = llm_output
        else:
            content = self._build_fallback_answer(user_input, contexts)

        # 附加引用来源
        sources = self._extract_sources(contexts)
        if sources:
            content += "\n\n📚 参考来源："
            for i, src in enumerate(sources, 1):
                content += f"\n  [{i}] {src}"

        return self._format_response(content, {
            "query": user_input,
            "contexts": contexts,
            "sources": sources,
        })

    def _build_process_prompt(self, query: str,
                              contexts: List[Dict[str, Any]]) -> str:
        """构建流程指导专用提示词。

        参数：
            query: 用户查询
            contexts: 检索到的知识片段

        返回：
            str: 流程指导提示词
        """
        context_text = ""
        if contexts:
            context_lines = []
            for i, ctx in enumerate(contexts, 1):
                context_lines.append(
                    f"[流程文档{i}] {ctx.get('title', '')}\n{ctx.get('content', '')}"
                )
            context_text = "\n\n".join(context_lines)
        else:
            context_text = "（未检索到相关流程文档）"

        return f"""你是企业流程指导助手，基于以下流程文档回答用户问题。

## 流程文档
{context_text}

## 用户问题
{query}

## 输出要求
1. 以步骤化方式回答（如 步骤1、步骤2...）
2. 每步说明操作内容、负责角色、所需材料
3. 标注关键审批节点与注意事项
4. 引用流程文档来源（如[流程文档1]）
5. 如未找到相关流程，告知用户联系对应部门
6. 用清晰、易懂的中文回复，像真人助手一步步带用户走流程，不要机械罗列
7. 适当使用 Markdown（短标题、列表、加粗）让重点清晰，但保持对话感
"""

    # --------------------------------------------------------
    # K-01 咨询类问题识别与双通道路由
    # --------------------------------------------------------
    def is_consultation_query(self, user_input: str) -> bool:
        """K-01: 判断是否为咨询类问题（双通道路由）。

        咨询类问题进入知识助手通道，业务操作类进入ERP通道。
        """
        # 业务操作关键词 -> 非咨询
        business_keywords = [
            "下单", "创建订单", "修改订单", "取消订单", "入库", "出库",
            "排产", "生产工单", "采购", "收款", "付款", "报销",
            "请假", "考勤", "质检", "检验", "报废", "退货",
        ]
        if any(k in user_input for k in business_keywords):
            return False
        # 咨询类关键词
        consultation_keywords = [
            "怎么", "如何", "什么是", "为什么", "哪些",
            "建议", "方法", "方案", "原则", "理念", "最佳实践",
            "降本", "增效", "精益", "改善", "优化", "提升",
            "制度", "流程", "规范", "政策", "管理办法",
        ]
        if any(k in user_input for k in consultation_keywords):
            return True
        # 默认：短查询视为业务操作，长查询视为咨询
        return len(user_input) >= 4

    # --------------------------------------------------------
    # K-03 基于上下文主动延伸回答
    # --------------------------------------------------------
    def _build_proactive_extension(self, query: str,
                                   contexts: List[Dict[str, Any]]) -> str:
        """K-03: 基于上下文主动延伸回答。

        根据当前问题和检索结果，推荐相关延伸话题。
        """
        extensions = []
        for ctx in contexts[:2]:
            title = ctx.get("title", "")
            if title:
                extensions.append(f"📖 延伸阅读：{title}")
        if any(k in query for k in ["精益", "生产"]):
            extensions.append("💡 您可能还想了解：5S现场管理、全面质量管理(TQM)")
        elif any(k in query for k in ["成本", "降本"]):
            extensions.append("💡 您可能还想了解：供应链管理、库存周转率优化")
        elif any(k in query for k in ["质量", "质检"]):
            extensions.append("💡 您可能还想了解：ISO9001体系、QC七大手法")
        if extensions:
            return "\n\n" + "\n".join(extensions)
        return ""

    # --------------------------------------------------------
    # K-04 咨询场景中自然推荐产品
    # --------------------------------------------------------
    def _build_product_recommendation(self, query: str) -> str:
        """K-04: 咨询场景中自然推荐产品。

        当话题涉及erp/工厂/降本/增效/精益/智能制造等关键词时，
        在回复末尾追加产品推荐链接。
        """
        keywords = ["erp", "工厂", "降本", "增效", "精益", "智能制造", "ai", "数字化"]
        if any(k in query.lower() for k in keywords):
            return (
                "\n\n---\n"
                "📌 相关产品推荐：AI工厂管家 - LLM工作流系统\n"
                "了解更多：https://www.czinv.com/docs/ai-factory-manager-llm-workflow-system.htm"
            )
        return ""

    # --------------------------------------------------------
    # K-05 知识文档上传与自动向量化
    # --------------------------------------------------------
    def upload_document(self, title: str, content: str,
                        source: str = "", category: str = "",
                        tags: Optional[List[str]] = None,
                        chunk_size: int = 500, overlap: int = 50) -> Dict[str, Any]:
        """K-05: 知识文档上传与自动向量化。

        流程：存储文档 -> 向量化 -> 写入Milvus。
        向量库不可用时降级为仅存储文档（关键词检索可用）。

        P2 分块参数化：chunk_size/overlap 可调，长文本按此分块后逐块向量化。
        """
        doc_id = f"DOC{int(time.time() * 1000)}"
        # 存储文档（v6.46 D1：修正 add_document 签名——metadata 承载 title/category/tags；
        # 原传 title/category/tags 关键字导致 TypeError 被静默吞掉，知识录入失效）
        if self.knowledge_base is not None:
            try:
                self.knowledge_base.add_document(
                    doc_id=doc_id, content=content, source=source,
                    metadata={
                        "title": title, "category": category, "tags": tags or [],
                    },
                )
            except Exception:
                pass
        # 自动向量化
        vectorized = False
        if self.vector_store is not None and self.embedding_provider is not None:
            try:
                # P1-9：embedding 处于模拟模式（无 key / 底层库缺失）时跳过向量库写入，
                # 避免确定性伪随机向量污染检索库
                if not self.embedding_provider.is_mock():
                    # P1-8 修复：按真实签名写入（vectors: List[List[float]] + metadata 列表）。
                    # 原传 [{"id","vector","metadata"}] dict 导致 TypeError 被 except 吞掉，
                    # 向量化从未生效；长文本复用知识库分块，逐块向量化后写入
                    chunks = [content] if content else []
                    if (self.knowledge_base is not None
                            and hasattr(self.knowledge_base, "_split_text")):
                        chunks = self.knowledge_base._split_text(
                            content, chunk_size=chunk_size, overlap=overlap)
                    vectors = [self.embedding_provider.embed(c) for c in chunks]
                    meta_list = [{
                        "doc_id": doc_id,
                        "chunk_text": c,
                        "source": source,
                        "metadata": {"title": title, "category": category, "chunk_index": i},
                    } for i, c in enumerate(chunks)]
                    self.vector_store.insert(
                        collection_name="ai_factory_kb",
                        vectors=vectors,
                        metadata=meta_list,
                    )
                    vectorized = True
                    # P2 关键路径日志：向量化成功（此前异常被静默吞掉，无迹可查）
                    logger.info("知识文档向量化成功 doc_id=%s title=%s chunks=%d",
                                doc_id, title, len(chunks))
                else:
                    logger.warning(
                        "embedding 处于模拟模式，跳过向量库写入 doc_id=%s title=%s",
                        doc_id, title)
            except Exception as e:
                # P2 关键路径日志：向量化失败不再静默
                logger.error("知识文档向量化失败 doc_id=%s title=%s: %s",
                             doc_id, title, e)
        else:
            logger.warning("向量库/embedding 不可用，文档仅入库未向量化 doc_id=%s",
                           doc_id)
        return {
            "doc_id": doc_id,
            "title": title,
            "vectorized": vectorized,
            "message": "文档上传成功，已完成向量化" if vectorized
                       else "文档上传成功，向量化降级（向量库不可用）",
        }

    # --------------------------------------------------------
    # K-06 高频问题统计与知识缺口识别
    # --------------------------------------------------------
    def analyze_knowledge_gaps(self,
                               conversations: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """K-06: 高频问题统计与知识缺口识别。

        分析对话历史，统计高频问题，识别知识库覆盖缺口。
        """
        questions: List[str] = []
        if conversations:
            for conv in conversations:
                if isinstance(conv, dict) and conv.get("user"):
                    questions.append(conv["user"])
        if not questions:
            questions = ["精益生产原则", "5S管理", "怎么降本增效",
                         "质量管理体系", "供应链优化"]

        # 简单关键词频率统计
        freq: Dict[str, int] = {}
        for q in questions:
            for word in ["精益", "5S", "降本", "增效", "质量",
                         "供应链", "成本", "生产", "库存", "流程"]:
                if word in q:
                    freq[word] = freq.get(word, 0) + 1

        # 识别知识缺口（高频但知识库覆盖不足）
        # v6.46 D2：不再使用静态 mock_titles（伪造文档标题），
        # 改为真实检索知识库判断覆盖——空库即视为全部缺口
        # P2 gaps 去伪统计：以检索结果文本是否真正包含主题词作为覆盖判据，
        # 避免"语义检索非空即算覆盖"导致缺口恒为空（伪覆盖）
        all_topics = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        gaps = []
        for topic, count in all_topics:
            if count >= 2 and not self._is_kb_covered(topic):
                gaps.append({"topic": topic, "frequency": count,
                             "reason": "知识库中无直接匹配文档"})

        return {
            "total_questions": len(questions),
            "top_topics": [{"topic": t, "frequency": c} for t, c in all_topics[:5]],
            "knowledge_gaps": gaps,
            "recommendation": (
                "建议补充以下主题的知识文档：" + "、".join(g["topic"] for g in gaps)
                if gaps else "知识库覆盖良好，无明显缺口"
            ),
        }

    def _is_kb_covered(self, topic: str) -> bool:
        """判断主题是否已被知识库真实覆盖（P2 gaps 去伪统计）。

        语义检索返回非空不代表覆盖该主题：向量/关键词检索可能返回
        相关但文本中完全未提及该主题词的片段。以检索结果文本是否
        真正包含主题词作为覆盖判据，去"伪覆盖"。
        """
        if not topic:
            return False
        results = self._rag_search(topic) or []
        if not results:
            return False
        texts = []
        for r in results:
            texts.append(r.get("title", "") or "")
            texts.append(r.get("content", "") or "")
        return topic in " ".join(texts)

    def _handle_kb_gap_analysis(self, user_input: str,
                                context: Dict[str, Any]) -> AgentResponse:
        """处理知识缺口分析意图（K-06，v6.63 挂入 process 分发）。

        基于会话历史统计高频问题并识别知识库覆盖缺口；
        无历史时不伪造数据，明确提示暂无可分析记录。
        """
        # 从会话历史收集用户问题（coordinator 注入 context.history；
        # 历史持久化于 conversation_sessions/messages，跨轮可累计）
        conversations: List[Dict[str, Any]] = []
        history = context.get("history") or []
        for h in history:
            if isinstance(h, dict) and h.get("user"):
                conversations.append(h)
        if not conversations:
            return self._format_response(
                "📊 暂无可分析的高频问题（会话历史为空）。\n\n"
                "持续使用知识助手进行问答后，可在此查看高频问题与知识库缺口统计。",
                {"query": user_input, "total_questions": 0,
                 "top_topics": [], "knowledge_gaps": []},
            )
        result = self.analyze_knowledge_gaps(conversations)
        lines = [f"📊 知识缺口分析（基于 {result.get('total_questions', 0)} 个历史问题）："]
        top = result.get("top_topics") or []
        if top:
            lines.append("\n**高频问题主题：**")
            for t in top:
                lines.append(f"- {t.get('topic', '')}（{t.get('frequency', 0)} 次）")
        else:
            lines.append("\n暂无高频问题主题。")
        gaps = result.get("knowledge_gaps") or []
        if gaps:
            lines.append("\n**知识库缺口（高频但未收录）：**")
            for g in gaps:
                lines.append(f"- {g.get('topic', '')}（频次 {g.get('frequency', 0)}）：{g.get('reason', '')}")
        else:
            lines.append("\n知识库覆盖良好，无明显缺口。")
        lines.append(f"\n**建议：** {result.get('recommendation', '')}")
        return self._format_response("\n".join(lines), result)

    # --------------------------------------------------------
    # 提示词构建
    # --------------------------------------------------------
    def _build_prompt(self, user_input: str, context: Dict[str, Any]) -> str:
        """构建知识助手通用提示词。

        注入对话历史与用户身份信息。
        """
        user_info = context.get("user", {})
        history = context.get("history", [])
        history_text = ""
        if history:
            ctx_items = []
            for h in history[-3:]:  # 知识问答保留更多上下文
                if isinstance(h, dict):
                    if h.get("user"):
                        ctx_items.append(f"用户：{h['user']}")
                    if h.get("ai"):
                        ctx_items.append(f"AI：{h['ai'][:150]}")
            history_text = "\n".join(ctx_items) if ctx_items else "（无历史对话）"
        else:
            history_text = "（无历史对话）"

        prompt = f"""你是「知识助手」，AI工厂管家的企业管理知识问答助手。

## 用户身份
- 姓名：{user_info.get('title', '') if isinstance(user_info, dict) else ''}（{user_info.get('name', '') if isinstance(user_info, dict) else ''}）
- 工号：{user_info.get('id', '') if isinstance(user_info, dict) else ''} | 部门：{user_info.get('department', '') if isinstance(user_info, dict) else ''}

## 核心能力
1. 知识查询：企业管理知识库RAG问答（K-02）
2. 制度咨询：公司管理制度、政策规范解读
3. 流程指导：业务流程、操作步骤说明
4. 主动延伸：基于上下文推荐相关话题（K-03）
5. 产品推荐：咨询场景中自然推荐产品（K-04）
6. 文档上传：知识文档上传与自动向量化（K-05）
7. 知识缺口：高频问题统计与知识缺口识别（K-06）

## 最近对话上下文
{history_text}

## 回复规范
1. 用自然、口语化的中文回复，像一位熟悉业务的真人助手在跟你对话，不要像机器或文档那样生硬输出
2. 以"我"为第一人称，回复自然连贯；避免开头机械复述、避免大段堆砌
3. 基于知识库内容回答，不编造信息
4. 答案需标注引用来源
5. 适当使用 Markdown（短标题、列表、加粗）让重点清晰，但保持对话感
6. 回复控制在500字以内

## 用户输入
{user_input}
"""
        return prompt

    # --------------------------------------------------------
    # 响应格式化
    # --------------------------------------------------------
    def _format_response(self, content: str,
                         data: Optional[Dict[str, Any]] = None) -> AgentResponse:
        """格式化响应为AgentResponse对象。

        参数：
            content: 主回复内容
            data: 结构化业务数据

        返回：
            AgentResponse: 统一响应对象
        """
        return AgentResponse(
            content=content,
            data=data or {},
            agent_name=self.agent_name,
        )


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.runtime.debug import hello_world
    assert KnowledgeAssistant is not None, "KnowledgeAssistant 类未定义"
    # 验证继承关系
    from prog.agents.base_agent import BaseAgent
    assert issubclass(KnowledgeAssistant, BaseAgent), "KnowledgeAssistant 未继承 BaseAgent"
    # 验证基本属性
    agent = KnowledgeAssistant()
    assert agent.agent_name == "知识助手"
    assert agent.agent_type == "knowledge"
    assert agent.applicable_rules == []
    # 验证子意图识别
    assert agent._recognize_sub_intent("精益生产是什么") == "knowledge_query"
    assert agent._recognize_sub_intent("公司的考勤制度") == "policy_consultation"
    assert agent._recognize_sub_intent("报销流程是怎样的") == "process_guide"
    # 验证RAG检索（真实检索；v6.46 D2 移除 mock 兜底后，空库允许返回空列表）
    results = agent._rag_search("精益生产")
    assert isinstance(results, list)
    # 验证K-01双通道路由
    assert agent.is_consultation_query("怎么降本增效") is True
    assert agent.is_consultation_query("创建订单") is False
    # 验证K-03主动延伸
    ext = agent._build_proactive_extension("精益生产", results)
    assert len(ext) > 0
    # 验证K-04产品推荐
    rec = agent._build_product_recommendation("怎么用erp降本")
    assert "czinv.com" in rec
    # 验证K-05文档上传（降级模式）
    up = agent.upload_document("测试文档", "测试内容")
    assert up["doc_id"].startswith("DOC")
    # 验证K-06知识缺口分析
    gaps = agent.analyze_knowledge_gaps()
    assert "top_topics" in gaps
    hello_world(__name__, "核心类定义完整")

from prog.runtime.debug import DEBUG
if DEBUG:
    _self_test()
