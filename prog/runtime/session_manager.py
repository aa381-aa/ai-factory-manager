"""
会话管理器模块
==============

文件用途：
    提供 Agent 运行时多用户会话与对话历史管理，使用 Redis 持久化存储，
    支持会话创建、查询、更新、销毁与对话历史追加。

设计说明：
    - 会话数据使用 JSON 序列化存储，Redis 客户端通过鸭子类型注入
      （提供 get / set / delete / exists 接口即可），未注入时降级为内存字典模式；
    - 默认 TTL 30 分钟（1800 秒），每次 update_session / add_message 续期；
    - 对话历史默认保留最近 100 条，超出滚动淘汰旧消息；
    - 会话数据结构：user_id, created_at, last_active_at, current_agent,
      context(JSON), history(JSON list of {role, content, ts})。

token 窗口管理（§4.7.2.2 补正项②，v1.2 已提取）：
    estimate_tokens / trim_history_to_tokens / summarize_history /
    get_prompt_context 四方法，提供会话上下文的 token 估算、窗口裁剪与
    历史摘要压缩，避免长会话超出 LLM 上下文窗口。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 会话 CRUD + 对话历史 + token 级窗口管理，支持多进程/多实例部署（SPEC §5.1 会话上下文管理，§4.7.2.2 补正项②）
        - Redis 客户端鸭子类型注入（get/set/delete/exists），未注入时降级内存字典（带 TTL 清理）（SPEC §5.1）
        - 会话默认 TTL 30 分钟、每次 update_session/add_message 续期；历史默认保留 100 条滚动淘汰（SPEC §5.1）
        - 框架接入约定：BaseAgent._build_prompt() 仅注入最近 2 轮对话历史；业务层调用 get_prompt_context() 后把裁剪/摘要结果放回 context["history"] 注入 BaseAgent（SPEC §5.1）
    对外接口（方法/API）：
        - SessionManager.__init__(redis_client=None, prefix="session:", session_ttl=1800, history_limit=100, max_tokens=4096)（SPEC §5.1）
        - SessionManager.create_session(user_id, context=None) -> str：创建会话，返回新 session_id（SPEC §5.1）
        - SessionManager.get_session(session_id) -> Optional[dict]：获取会话，不存在/已过期返回 None（SPEC §5.1）
        - SessionManager.update_session(session_id, data) -> bool：合并更新并续期 TTL（SPEC §5.1）
        - SessionManager.destroy_session(session_id) -> bool：主动销毁会话（SPEC §5.1）
        - SessionManager.add_message(session_id, role, content) -> bool：追加历史，超 history_limit 滚动淘汰（SPEC §5.1）
        - SessionManager.get_history(session_id, limit=10) -> list：取最近 N 条历史，按时间正序（SPEC §5.1）
        - SessionManager.is_expired(session_id) -> bool：判断会话是否过期（SPEC §5.1）
        - SessionManager.estimate_tokens(text) -> int：估算 token 数，CJK 按 1 字 ≈ 1 token（SPEC §5.1 补正项②）
        - SessionManager.trim_history_to_tokens(history, max_tokens)：token 窗口裁剪，从最新向前累加保留最近消息（SPEC §5.1 补正项②）
        - SessionManager.summarize_history(history)：早期消息摘要压缩（替代直接丢弃，保留角色+要点）（SPEC §5.1 补正项②）
        - SessionManager.get_prompt_context(history, max_tokens=4096)：组装「摘要 + 最近完整消息」，历史超 20 条自动触发摘要（SPEC §5.1 补正项②）
    错误处理要求：
        - 未注入 Redis 客户端：降级内存字典模式（带 TTL 清理）（SPEC §5.1）
        - 会话不存在/已过期：读取类操作返回 None/空列表、写入类操作返回 False（SPEC §5.1 接口契约）
"""

import json
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class SessionManager:
    """会话管理器。

    设计意图：
        将会话状态从应用进程移至 Redis，支持多进程/多实例部署，
        并提供对话历史追加与续期机制。

    属性：
        _redis: Redis 客户端连接（鸭子类型，None 时内存降级）
        _prefix: Redis 键前缀（默认 'session:'）
        _session_ttl: 会话 TTL 秒数（默认 1800=30 分钟）
        _history_limit: 历史保留条数（默认 100）
    """

    DEFAULT_TTL = 1800  # 30 分钟
    DEFAULT_HISTORY_LIMIT = 100
    DEFAULT_MAX_TOKENS = 4096  # 提示词上下文 token 窗口上限
    DEFAULT_SUMMARY_LIMIT = 20  # 超过该条数后触发历史摘要压缩

    def __init__(self, redis_client: Any = None,
                 prefix: str = "session:",
                 session_ttl: int = DEFAULT_TTL,
                 history_limit: int = DEFAULT_HISTORY_LIMIT,
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        # Redis 客户端连接（为 None 时降级为内存字典模式）
        self._redis = redis_client
        self._prefix = prefix
        self._session_ttl = session_ttl
        self._history_limit = history_limit
        self._max_tokens = max_tokens

        # 内存降级模式的存储：session_id -> 会话数据字典
        self._memory_store: Dict[str, dict] = {}
        # 内存降级模式的 TTL 记录：session_id -> 过期时间戳
        self._memory_ttls: Dict[str, float] = {}
        # 内存降级模式的锁（保护 _memory_store / _memory_ttls 的并发读写）
        self._memory_lock = threading.Lock()

    # --------------------------------------------------------
    # 内部辅助方法
    # --------------------------------------------------------

    def _make_key(self, session_id: str) -> str:
        """构建 Redis 键名（前缀 + session_id）"""
        return f"{self._prefix}{session_id}"

    def _cleanup_expired_memory(self) -> None:
        """清理内存降级模式中已过期的会话"""
        if not self._memory_ttls:
            return
        now = time.time()
        expired = [sid for sid, exp in self._memory_ttls.items() if exp <= now]
        for sid in expired:
            self._memory_store.pop(sid, None)
            self._memory_ttls.pop(sid, None)

    def _is_memory_mode(self) -> bool:
        """是否运行在内存降级模式（无 Redis 连接）"""
        return self._redis is None

    # --------------------------------------------------------
    # 核心会话操作
    # --------------------------------------------------------

    def create_session(self, user_id: str,
                       context: Optional[Dict[str, Any]] = None) -> str:
        """创建会话。

        参数：
            user_id: 用户标识
            context: 初始上下文数据

        返回：
            str: 新生成的 session_id（UUID），并写入 Redis 带 TTL。
        """
        session_id = str(uuid.uuid4())
        now = time.time()
        now_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))

        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now_str,
            "last_active_at": now_str,
            "current_agent": "",
            "context": context or {},
            "history": [],
        }

        if self._is_memory_mode():
            # 内存降级模式
            self._memory_store[session_id] = session_data
            self._memory_ttls[session_id] = now + self._session_ttl
        else:
            # Redis 模式：JSON 序列化后写入，设置 TTL
            key = self._make_key(session_id)
            serialized = json.dumps(session_data, ensure_ascii=False, default=str)
            self._redis.set(key, serialized, ex=self._session_ttl)

        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """获取会话。

        参数：
            session_id: 会话 ID

        返回：
            会话数据字典；会话不存在或已过期返回 None。
        """
        if self._is_memory_mode():
            # 内存降级模式
            self._cleanup_expired_memory()
            if session_id not in self._memory_store:
                return None
            # 返回深拷贝避免外部修改影响内部存储
            return json.loads(json.dumps(
                self._memory_store[session_id], ensure_ascii=False, default=str))

        # Redis 模式
        key = self._make_key(session_id)
        value = self._redis.get(key)
        if value is None:
            return None
        # Redis 返回的可能是 str 或 bytes，统一处理
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return json.loads(value)

    def update_session(self, session_id: str, data: Dict[str, Any]) -> bool:
        """更新会话（合并写入并续期 TTL）。

        参数：
            session_id: 会话 ID
            data: 待合并的字段字典

        返回：
            bool: 会话存在且更新成功返回 True，否则 False。
        """
        session = self.get_session(session_id)
        if session is None:
            return False

        # 合并字段（history 字段特殊处理，不允许直接覆盖）
        for key, value in data.items():
            if key == "history":
                # history 通过 add_message 方法维护，不在此直接覆盖
                continue
            session[key] = value

        # 更新最后活跃时间
        session["last_active_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime())

        if self._is_memory_mode():
            # 内存降级模式：直接写回并续期
            self._memory_store[session_id] = session
            self._memory_ttls[session_id] = time.time() + self._session_ttl
        else:
            # Redis 模式：序列化写回并续期 TTL
            key = self._make_key(session_id)
            serialized = json.dumps(session, ensure_ascii=False, default=str)
            self._redis.set(key, serialized, ex=self._session_ttl)

        return True

    def destroy_session(self, session_id: str) -> bool:
        """销毁会话（主动删除，不等 TTL 过期）。

        参数：
            session_id: 会话 ID

        返回：
            bool: 删除成功返回 True。
        """
        if self._is_memory_mode():
            # 内存降级模式
            with self._memory_lock:
                existed = session_id in self._memory_store
                self._memory_store.pop(session_id, None)
                self._memory_ttls.pop(session_id, None)
                return existed

        # Redis 模式
        key = self._make_key(session_id)
        deleted = self._redis.delete(key)
        return deleted > 0

    def add_message(self, session_id: str, role: str, content: str) -> bool:
        """追加对话历史消息。

        参数：
            session_id: 会话 ID
            role: 消息角色（'user'/'assistant'/'system'）
            content: 消息内容

        返回：
            bool: 追加成功返回 True；并自动续期会话 TTL。
            超过 history_limit 时滚动淘汰最旧消息。
        """
        session = self.get_session(session_id)
        if session is None:
            return False

        # 构建消息记录
        message = {
            "role": role,
            "content": content,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }

        # 追加到历史列表
        history = session.get("history", [])
        history.append(message)

        # 滚动淘汰：超过上限时丢弃最旧的消息
        if len(history) > self._history_limit:
            history = history[-self._history_limit:]

        session["history"] = history
        session["last_active_at"] = message["ts"]

        if self._is_memory_mode():
            # 内存降级模式：直接写回并续期
            self._memory_store[session_id] = session
            self._memory_ttls[session_id] = time.time() + self._session_ttl
        else:
            # Redis 模式：序列化写回并续期 TTL
            key = self._make_key(session_id)
            serialized = json.dumps(session, ensure_ascii=False, default=str)
            self._redis.set(key, serialized, ex=self._session_ttl)

        return True

    def get_history(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取对话历史。

        参数：
            session_id: 会话 ID
            limit: 返回最近 N 条（默认 10）

        返回：
            历史消息列表，按时间正序排列；会话不存在返回空列表。
        """
        session = self.get_session(session_id)
        if session is None:
            return []

        history = session.get("history", [])
        # 取最近 limit 条，按时间正序返回
        recent = history[-limit:] if limit > 0 else history
        return list(recent)

    def is_expired(self, session_id: str) -> bool:
        """判断会话是否过期（不存在即视为过期）。"""
        if self._is_memory_mode():
            # 内存降级模式
            with self._memory_lock:
                self._cleanup_expired_memory()
                return session_id not in self._memory_store

        # Redis 模式：key 不存在即过期
        key = self._make_key(session_id)
        return not self._redis.exists(key)

    # --------------------------------------------------------
    # token 窗口管理与历史摘要压缩（补正项②，v1.2 已提取）
    # --------------------------------------------------------

    @staticmethod
    def estimate_tokens(text: Any) -> int:
        """估算文本 token 数。

        中文按 1 字≈1 token、英文按 4 字符≈1 token 估算；
        数字与标点按 1 token/2 字符估算。用于 token 窗口裁剪。
        """
        if text is None:
            return 0
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return 0
        # 简单估算：CJK 字符计 1，其余按 4 字符计 1
        cjk_count = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
        other_count = len(text) - cjk_count
        return cjk_count + other_count // 4 + 1

    def trim_history_to_tokens(self, history: List[Dict[str, Any]],
                               max_tokens: int = None) -> List[Dict[str, Any]]:
        """按 token 窗口裁剪历史，保留最近消息。

        从最新消息向前累加，直到达到 max_tokens 上限。
        超出窗口的早期消息被丢弃（保留最近消息保证连续性）。

        Args:
            history: 历史消息列表（按时间正序）
            max_tokens: token 上限，默认使用构造参数 _max_tokens

        Returns:
            裁剪后的历史消息列表（时间正序）
        """
        if not history:
            return []
        limit = max_tokens if max_tokens is not None else self._max_tokens
        kept = []
        total = 0
        # 从最新向前累加
        for msg in reversed(history):
            msg_tokens = self.estimate_tokens(msg.get("content", ""))
            if total + msg_tokens > limit:
                break
            kept.append(msg)
            total += msg_tokens
        kept.reverse()
        return kept

    def summarize_history(self, history: List[Dict[str, Any]],
                          max_summary_tokens: int = 200) -> str:
        """生成历史摘要（摘要压缩）。

        将早期消息压缩为一条摘要文本，替代直接丢弃——
        保留关键信息（角色、操作要点），显著降低 token 占用。

        Args:
            history: 待摘要的历史消息列表
            max_summary_tokens: 摘要最大 token 数

        Returns:
            摘要文本字符串；空历史返回空串
        """
        if not history:
            return ""
        parts = []
        budget = max_summary_tokens
        for msg in history:
            role = msg.get("role", "user")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(role, role)
            # 单条摘要：截断长消息保留要点
            truncated = content
            if self.estimate_tokens(truncated) > 30:
                truncated = truncated[:60] + "…"
            snippet = f"{label}:{truncated}"
            snippet_tokens = self.estimate_tokens(snippet)
            if budget - snippet_tokens <= 0:
                break
            parts.append(snippet)
            budget -= snippet_tokens
        return "；".join(parts)

    def get_prompt_context(self, session_id: str,
                           max_tokens: int = None) -> Dict[str, Any]:
        """组装提示词上下文（摘要 + 最近完整消息）。

        长会话处理策略（补正项②）：
            1. 若历史条数 <= SUMMARY_LIMIT：直接返回最近消息（按 token 窗口裁剪）
            2. 若历史条数 > SUMMARY_LIMIT：早期消息生成摘要，最近消息保留完整

        Args:
            session_id: 会话 ID
            max_tokens: token 窗口上限

        Returns:
            dict: {"summary": 摘要, "history": 最近完整消息列表, "total_tokens": 估算 token 总数}
        """
        session = self.get_session(session_id)
        if session is None:
            return {"summary": "", "history": [], "total_tokens": 0}

        history = session.get("history", [])
        limit = max_tokens if max_tokens is not None else self._max_tokens

        if len(history) <= self.DEFAULT_SUMMARY_LIMIT:
            # 短会话：仅 token 窗口裁剪
            trimmed = self.trim_history_to_tokens(history, limit)
            total = sum(self.estimate_tokens(m.get("content", "")) for m in trimmed)
            return {"summary": "", "history": trimmed, "total_tokens": total}

        # 长会话：摘要早期 + 保留最近
        summary_count = len(history) - self.DEFAULT_SUMMARY_LIMIT
        early = history[:summary_count]
        recent = history[summary_count:]
        summary = self.summarize_history(early)
        trimmed = self.trim_history_to_tokens(recent, limit)
        total = self.estimate_tokens(summary) + sum(
            self.estimate_tokens(m.get("content", "")) for m in trimmed)
        return {"summary": summary, "history": trimmed, "total_tokens": total}
