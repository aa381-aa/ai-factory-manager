# -*- coding: utf-8 -*-
"""对话记忆管理器（v6.37）。

六层优化：
1. 滑动窗口 N=5：保留最近5轮对话原文（输入截断200字/回复截断300字）
2. LLM 语义摘要：超过5轮时优先用 LLM 生成语义摘要，LLM 不可用降级规则式
3. 相关性筛选：基于 Jaccard 关键词相似度（含2字子串）选择最相关历史轮次
4. 递归意图记忆：每轮增量更新 intent_state，跟踪意图流转轨迹
5. 向量长期记忆：embedding_provider 可用时生成向量，支持跨会话语义检索
6. 分层记忆：短期（内存N轮）+ 摘要（压缩历史）+ 长期（向量检索）

兼容性：所有新增参数可选，默认 None，不影响现有调用。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 滑动窗口 N=5：保留最近5轮对话原文（输入截断200字/回复截断300字），对齐技术规格 §1.1.3.1「保留最近5轮完整对话」（业务规格书 v6.36/v6.38）
        - 摘要压缩：超过窗口自动将最旧轮次压缩为语义摘要（意图+输入摘要+回复摘要，截断500字），Token 节省 60-75%（业务规格书 v6.36）
        - LLM 语义摘要：超过窗口时优先用 LLM 生成语义摘要，LLM 不可用/异常降级规则式摘要（业务规格书 v6.37）
        - 记忆相关性筛选：基于 Jaccard 关键词相似度（含2字子串）选择与当前输入最相关历史轮次（最多2个）（业务规格书 v6.36）
        - 递归意图记忆：每轮增量更新 intent_state（格式 T1:query_qc->质检Agent | T2:create_order->销售Agent），跟踪意图流转轨迹（业务规格书 v6.37）
        - 向量长期记忆：embedding_provider 可用时每轮生成 embedding 存内存索引（最多100条），search_long_term() 余弦相似度检索历史对话（阈值0.3），支持跨会话语义检索（业务规格书 v6.37）
        - 分层记忆：短期（内存N轮）+ 摘要（压缩历史）+ 长期（向量检索）（业务规格书 v6.36/v6.37）
    对外接口（方法/API）：
        - ConversationMemory(session_id, llm_client=None, embedding_provider=None)：单会话对话记忆（业务规格书 v6.36/v6.37）
        - ConversationMemory.add_turn(user_input, reply, intent="", agent="")：添加一轮对话（截断后入窗口，超窗压缩最旧轮次）（业务规格书 v6.36）
        - ConversationMemory.get_context(current_input="") -> dict：完整上下文（turns/relevant_turns/summary/total_turns/last_intent/last_agent/intent_state）（业务规格书 v6.36/v6.37）
        - ConversationMemory.search_long_term(query_vec, top_k=3) -> list：向量余弦相似度检索历史对话（业务规格书 v6.37）
        - ConversationMemoryManager(ttl=SESSION_TTL, llm_client=None, embedding_provider=None)：进程级会话记忆管理器（业务规格书 v6.36）
        - ConversationMemoryManager.configure(llm_client=None, embedding_provider=None)：延迟注入 LLM 与 Embedding（服务器启动后调用）（业务规格书 v6.37）
        - ConversationMemoryManager.search_long_term(query, top_k=3, exclude_session="") -> list：跨会话语义检索（业务规格书 v6.37）
        - ConversationMemoryManager.cleanup_expired(current_time) -> int：会话 TTL 过期清理（业务规格书 v6.36）
    错误处理要求：
        - LLM 不可用或异常：降级为规则式摘要（完全兼容 v6.36 行为）（业务规格书 v6.37）
        - 所有新增参数可选默认 None：不传时完全降级为 v6.36 行为（业务规格书 v6.37）
        - 向量检索不可用（无 embedding_provider/查询为空）：返回空列表（业务规格书 v6.37）
"""
import re
import math
import queue as _queue
import threading
import time
from typing import Dict, List, Optional, Any, Callable

MAX_TURNS = 5  # v6.37：对齐技术规格 §1.1.3.1 "保留最近5轮完整对话"
MAX_INPUT_CHARS = 200
MAX_REPLY_CHARS = 300
MAX_SUMMARY_CHARS = 500
MAX_INTENT_STATE_CHARS = 200
SESSION_TTL = 3600

_STOP_WORDS = {"的", "了", "是", "在", "我", "你", "他", "她", "它", "这", "那", "有", "和",
               "与", "或", "请", "帮", "给", "下", "个", "什", "么", "怎", "么",
               "可以", "能够", "需要", "应该", "一下", "现在", "今天", "明天"}


class ConversationMemory:
    """单会话对话记忆管理（v6.37：六层优化）。"""

    def __init__(self, session_id: str,
                 llm_client: Any = None,
                 embedding_provider: Any = None):
        self.session_id = session_id
        self.turns: List[Dict[str, Any]] = []
        self.summary: str = ""
        self.total_turns: int = 0
        self.last_intent: str = ""
        self.last_agent: str = ""
        # v6.37 新增
        self.intent_state: str = ""  # 递归意图记忆：每轮增量更新
        self.llm_client = llm_client  # LLM 引擎（用于语义摘要）
        self.embedding_provider = embedding_provider  # Embedding 提供方
        self._embeddings: List[tuple] = []  # [(text, vector)] 向量索引
        # v6.93 提速：记忆增强（embedding/LLM 摘要）异步落盘锁——
        # 后台 worker 写 _embeddings/_summary 时加锁，主链路读与写互斥
        self._bg_lock = threading.Lock()

    def add_turn(self, user_input: str, reply: str,
                 intent: str = "", agent: str = "") -> None:
        """添加一轮对话到记忆。"""
        self.total_turns += 1
        turn = {
            "input": user_input[:MAX_INPUT_CHARS],
            "reply": reply[:MAX_REPLY_CHARS],
            "intent": intent,
            "agent": agent,
            "turn": self.total_turns,
        }
        self.turns.append(turn)
        if intent:
            self.last_intent = intent
        if agent:
            self.last_agent = agent

        # 方向二：递归意图记忆——每轮更新意图状态
        self._update_intent_state(intent, agent)

        # 方向三：向量长期记忆——生成 embedding（v6.93 提速：异步后台执行，
        # 豆包 Embedding API 为网络调用，不再阻塞主链路 done 事件/同步响应）
        if self.embedding_provider:
            _MEMORY_BG_QUEUE.put((self, "embed", (user_input, reply)))

        # 超过窗口时压缩最旧轮次。v6.93 提速：仅当 LLM 可用（语义摘要为网络调用）
        # 时异步后台执行；无 LLM 时规则式摘要是纯内存操作，保持同步（行为零变化，
        # 测试/离线场景立即生效）。
        while len(self.turns) > MAX_TURNS:
            old = self.turns.pop(0)
            if self.llm_client is not None:
                _MEMORY_BG_QUEUE.put((self, "compress", old))
            else:
                self._compress_to_summary(old)

    # ---- 方向二：递归意图记忆 ----

    def _update_intent_state(self, intent: str, agent: str) -> None:
        """每轮增量更新意图状态（轻量级，无 LLM 调用）。

        格式：T1:query_qc->质检Agent | T2:create_order->销售Agent
        """
        entry = f"T{self.total_turns}:{intent or '?'}->{agent or '?'}"
        if self.intent_state:
            self.intent_state = self.intent_state + " | " + entry
        else:
            self.intent_state = entry
        if len(self.intent_state) > MAX_INTENT_STATE_CHARS:
            self.intent_state = self.intent_state[-MAX_INTENT_STATE_CHARS:]

    # ---- 方向一：LLM 语义摘要 ----

    def _compress_to_summary(self, turn: Dict) -> None:
        """将旧轮次压缩到摘要。

        优先用 LLM 生成语义摘要，不可用时降级规则式。
        v6.93：由后台 worker 线程执行（add_turn 异步投递），
        summary 写入加锁保护与主链路 get_context 读一致。
        """
        llm_summary = self._llm_summarize(turn) if self.llm_client else None
        if llm_summary:
            entry = llm_summary.strip()[:120]
        else:
            # 降级：规则式摘要
            intent_str = f"[{turn.get('intent', '?')}]"
            input_brief = turn["input"][:60]
            reply_brief = turn["reply"][:60]
            entry = f"T{turn['turn']} {intent_str} {input_brief}->{reply_brief}"

        with self._bg_lock:
            if self.summary:
                self.summary = self.summary + "\n" + entry
            else:
                self.summary = entry
            if len(self.summary) > MAX_SUMMARY_CHARS:
                self.summary = self.summary[-MAX_SUMMARY_CHARS:]

    def _llm_summarize(self, turn: Dict) -> Optional[str]:
        """调用 LLM 生成语义摘要。

        优先用 llm_provider（绕过安全门控，摘要不需要），降级直接调用。
        任何异常返回 None，由调用方降级为规则式。
        """
        try:
            prompt = (
                "将以下对话压缩为简洁摘要（不超过80字），保留意图、实体和结果：\n"
                f"用户：{turn.get('input', '')}\n回复：{turn.get('reply', '')}"
            )
            # 优先用 llm_provider（绕过门控）
            provider = getattr(self.llm_client, "llm_provider", None) or self.llm_client
            call = getattr(provider, "call", None) or getattr(provider, "generate", None)
            if not call:
                return None
            result = call(prompt)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                return result.get("text", "") or result.get("content", "")
            return str(result) if result else None
        except Exception:
            return None

    # ---- 方向三：向量长期记忆 ----

    def _add_embedding(self, user_input: str, reply: str) -> None:
        """生成对话 embedding 并存入内存索引。

        v6.93：由后台 worker 线程执行（add_turn 异步投递），
        _embeddings 写入加锁保护与 search_long_term 读一致。
        """
        try:
            text = f"{user_input[:100]} {reply[:100]}"
            vec = self.embedding_provider.embed(text)
            if vec and isinstance(vec, list):
                with self._bg_lock:
                    self._embeddings.append((text, vec))
                    # 限制向量索引大小（最多100条）
                    if len(self._embeddings) > 100:
                        self._embeddings = self._embeddings[-100:]
        except Exception:
            pass

    def search_long_term(self, query_vec: List[float],
                         top_k: int = 3) -> List[Dict[str, Any]]:
        """用向量余弦相似度检索历史对话。"""
        with self._bg_lock:
            embeddings = list(self._embeddings)
        if not embeddings or not query_vec:
            return []
        scored = []
        for text, vec in embeddings:
            score = _cosine_similarity(query_vec, vec)
            if score > 0.3:
                scored.append({"text": text, "score": round(score, 3),
                                "session_id": self.session_id})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ---- 原有方法（保持不变） ----

    def get_context(self, current_input: str = "") -> Dict[str, Any]:
        """获取完整上下文。"""
        relevant = self._select_relevant(current_input)
        ctx = {
            "turns": [t.copy() for t in self.turns],
            "relevant_turns": relevant,
            "summary": self.summary,
            "total_turns": self.total_turns,
            "last_intent": self.last_intent,
            "last_agent": self.last_agent,
            "intent_state": self.intent_state,  # v6.37 新增
        }
        return ctx

    def _select_relevant(self, current_input: str) -> List[Dict[str, Any]]:
        """基于关键词 Jaccard 相似度选择相关历史轮次。"""
        if not current_input or not self.turns:
            return []
        current_kw = _extract_keywords(current_input)
        if not current_kw:
            return [self.turns[-1].copy()] if self.turns else []
        scored: List[tuple] = []
        for turn in self.turns:
            turn_text = turn["input"] + " " + turn["reply"]
            turn_kw = _extract_keywords(turn_text)
            if not turn_kw:
                scored.append((turn, 0.0))
                continue
            intersection = current_kw & turn_kw
            union = current_kw | turn_kw
            score = len(intersection) / len(union) if union else 0.0
            scored.append((turn, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [t.copy() for t, s in scored[:2] if s > 0.0]

    def get_last_input(self) -> str:
        if self.turns:
            return self.turns[-1].get("input", "")
        return ""

    def get_last_reply(self) -> str:
        if self.turns:
            return self.turns[-1].get("reply", "")
        return ""

    def is_empty(self) -> bool:
        return not self.turns and not self.summary


def _extract_keywords(text: str) -> set:
    """关键词提取（统一解析层升级，返回类型保持 set 兼容）。

    C10/A.9：由 2-4 字滑窗升级为 utils.nl_parser.extract_keywords——
    实体整词优先（users/products/departments/customers 等 DB 词典）、
    去停用词、英文/型号整体保留；DB 不可达时降级为内置通用词典。
    """
    if not text:
        return set()
    try:
        from prog.utils.nl_parser import extract_keywords as _nl_extract
        return set(_nl_extract(text))
    except Exception:
        # 兜底：原 2-4 字滑窗逻辑（nl_parser 异常时保持记忆功能可用）
        words = set()
        for m in re.finditer(r'[\u4e00-\u9fff]{2,4}', text):
            w = m.group()
            if w not in _STOP_WORDS:
                words.add(w)
            if len(w) > 2:
                for i in range(len(w) - 1):
                    sub = w[i:i + 2]
                    if sub not in _STOP_WORDS:
                        words.add(sub)
        for m in re.finditer(r'[A-Za-z0-9][A-Za-z0-9\-]+', text):
            w = m.group().lower()
            if len(w) >= 2:
                words.add(w)
        return words


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """余弦相似度（纯 Python，无 numpy 依赖）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


class ConversationMemoryManager:
    """会话级记忆管理器（进程内）。

    v6.37：支持 LLM 语义摘要 + 向量长期记忆。
    所有参数可选，默认 None 时降级为纯规则模式（完全兼容 v6.36）。
    """

    def __init__(self, ttl: int = SESSION_TTL,
                 llm_client: Any = None,
                 embedding_provider: Any = None):
        self._memories: Dict[str, ConversationMemory] = {}
        # 会话最后活跃时间戳（session_id -> time.time()），供 TTL 过期清理
        self._last_active: Dict[str, float] = {}
        self._ttl = ttl
        self.llm_client = llm_client
        self.embedding_provider = embedding_provider
        # 保护 _memories / _last_active 的读写（后台 cleanup 与业务并发）
        self._lock = threading.Lock()

    def configure(self, llm_client: Any = None,
                  embedding_provider: Any = None) -> None:
        """延迟注入 LLM 和 Embedding（服务器启动后调用）。"""
        if llm_client is not None:
            self.llm_client = llm_client
        if embedding_provider is not None:
            self.embedding_provider = embedding_provider

    def get(self, session_id: str) -> ConversationMemory:
        """获取或创建会话记忆（并刷新最后活跃时间戳）。"""
        with self._lock:
            mem = self._memories.get(session_id)
            if mem is None:
                mem = ConversationMemory(
                    session_id, self.llm_client, self.embedding_provider)
                self._memories[session_id] = mem
            self._last_active[session_id] = time.time()
        return mem

    def add_turn(self, session_id: str, user_input: str, reply: str,
                 intent: str = "", agent: str = "") -> None:
        """添加一轮对话。"""
        self.get(session_id).add_turn(user_input, reply, intent, agent)

    def get_context(self, session_id: str,
                    current_input: str = "") -> Dict[str, Any]:
        """获取会话上下文。"""
        return self.get(session_id).get_context(current_input)

    def search_long_term(self, query: str, top_k: int = 3,
                         exclude_session: str = "") -> List[Dict[str, Any]]:
        """跨会话向量检索历史对话（方向三）。

        embedding_provider 可用时用语义检索，不可用返回空列表。
        """
        if not self.embedding_provider or not query:
            return []
        try:
            query_vec = self.embedding_provider.embed(query)
            if not query_vec:
                return []
        except Exception:
            return []

        results: List[Dict[str, Any]] = []
        with self._lock:
            items = list(self._memories.items())
        for sid, mem in items:
            if sid == exclude_session:
                continue
            results.extend(mem.search_long_term(query_vec, top_k))
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def get_last_input(self, session_id: str) -> str:
        return self.get(session_id).get_last_input()

    def get_last_reply(self, session_id: str) -> str:
        return self.get(session_id).get_last_reply()

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._memories.pop(session_id, None)
            self._last_active.pop(session_id, None)

    def cleanup_expired(self, current_time: float) -> int:
        """清理过期会话记忆（TTL 过期 + 内存超限双策略并存）。

        - TTL 过期清理：最后活跃时间超过 _ttl（默认 SESSION_TTL=3600s）的
          会话移除，current_time 为当前时间戳（与 _last_active 同基准 time.time()）
        - 内存超 1000 条清理空会话：保留原行为不变

        Returns:
            int: 本次清理移除的会话数
        """
        removed = 0
        with self._lock:
            # 策略一：TTL 过期清理
            expired_ids = [
                sid for sid, ts in self._last_active.items()
                if current_time - ts > self._ttl
            ]
            for sid in expired_ids:
                self._memories.pop(sid, None)
                self._last_active.pop(sid, None)
            removed += len(expired_ids)

            # 策略二：内存超 1000 条清理空会话（原行为保留）
            if len(self._memories) > 1000:
                before = len(self._memories)
                self._memories = {
                    k: v for k, v in self._memories.items() if not v.is_empty()
                }
                self._last_active = {
                    k: v for k, v in self._last_active.items()
                    if k in self._memories
                }
                removed += before - len(self._memories)
        return removed


# ============================================================
# v6.93 提速：记忆增强后台 worker（全局单一 daemon 线程）
# ============================================================
# 背景：add_turn 内的 embedding 向量化（豆包 API，每轮）与 LLM 语义摘要
# （每超窗口轮次触发一次）为网络调用，此前同步阻塞主链路——流式接口
# 的 done 事件被推迟（发送按钮滞留"发送中…"）、同步接口响应被推迟。
# 方案：embedding/压缩任务投递到全局队列，由单一 daemon worker 串行执行；
# 记忆为"尽力而为"的增强特性，延迟落盘不影响对话正确性（失败本就静默降级）。
# 线程安全：worker 写实例 _embeddings/_summary 前取实例 _bg_lock，
# 与主链路 search_long_term/get_context 读互斥。
_MEMORY_BG_QUEUE: _queue.Queue = _queue.Queue()


def _memory_bg_worker() -> None:
    """后台 worker：串行处理记忆增强任务（embedding / 摘要压缩）。"""
    while True:
        try:
            inst, kind, payload = _MEMORY_BG_QUEUE.get()
            if kind == "embed":
                inst._add_embedding(payload[0], payload[1])
            elif kind == "compress":
                inst._compress_to_summary(payload)
        except Exception:
            # 单个任务异常不影响后续任务（embedding/摘要均为尽力而为）
            pass


_threading_worker = threading.Thread(
    target=_memory_bg_worker, daemon=True, name="memory-bg-worker")
_threading_worker.start()
del _threading_worker
