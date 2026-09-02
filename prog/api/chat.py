"""
Chat 对话API模块
================

文件用途：
    实现主对话API，作为用户与AI工厂管家交互的统一入口，
    调用CoordinatorAgent进行意图识别与分发。

技术规格章节：
    - §1.1.3 Coordinator Agent（本接口为Coordinator的HTTP入口）
    - §2 LLM安全门控（SSE流式响应携带门控结果）
    - §3.2~§3.8 各领域Agent（由Coordinator分发调用）

接口列表：
    - POST /api/chat: 主对话接口（同步响应）
    - POST /api/chat/stream: SSE流式对话接口
    - GET /api/chat/history: 会话历史查询（游标分页）

设计说明：
    - 主对话接口返回AgentResponse的JSON表示
    - 流式接口返回SSE流，前端通过EventSource消费
    - 高风险操作时，响应中 need_confirm=True，前端需二次确认后
      调用对应业务接口（如 orders）真正执行
    - 会话历史仅限归属当前用户（C-1 会话归属校验）
"""

import json
import logging
import time
import threading
import uuid
from typing import Any, Dict, Optional

from flask import Blueprint, request, jsonify, Response, g
from prog.utils.api_response import api_response, error_response

from prog.agents.base_agent import AgentResponse

_log = logging.getLogger(__name__)

try:
    from prog.core.debug import DEBUG
except Exception:
    DEBUG = False

# Blueprint定义（url_prefix在注册时统一设置）
chat_bp = Blueprint('chat', __name__, url_prefix='/api/chat')

# 会话级"待延续业务意图"（进程内快速路径）：多轮对话中，业务意图（如下单收集
# 产品/数量信息）需要多轮补充时，记录最近一次业务意图供 coordinator 延续，
# 避免补充信息（如"A-202，100套"）被误判为知识咨询。
# v6.46 D5：读/写统一走 _load_pending_intent / _save_pending_intent——
# 以 SessionManager（Redis 持久化 + 内存降级）为持久化后端，_PENDING_INTENTS
# 仅作进程内快速路径缓存，服务重启后可从 Redis 恢复多轮延续状态。
_PENDING_INTENTS: Dict[str, Dict[str, str]] = {}


def _load_pending_intent(session_id: str, sm_session_id: str) -> Optional[Dict[str, str]]:
    """读取会话待延续意图（v6.46 D5）。

    优先从 SessionManager（Redis 持久化）读取，命中时回填内存快速路径；
    读不到再回退进程内存（SessionManager 不可用的降级模式）。
    """
    if sm_session_id:
        try:
            sess = _session_mgr.get_session(sm_session_id)
            pending = (sess or {}).get("pending_intent")
            if isinstance(pending, dict) and pending:
                if session_id and pending != _PENDING_INTENTS.get(session_id):
                    _PENDING_INTENTS[session_id] = pending
                return pending
        except Exception:
            pass
    return _PENDING_INTENTS.get(session_id)


def _save_pending_intent(session_id: str, sm_session_id: str,
                         pending: Optional[dict]) -> None:
    """写/清会话待延续意图（v6.46 D5：SessionManager 持久化 + 内存快速路径双写）。"""
    if pending and isinstance(pending, dict):
        if session_id:
            _PENDING_INTENTS[session_id] = pending
        if sm_session_id:
            try:
                _session_mgr.update_session(
                    sm_session_id, {"pending_intent": pending})
            except Exception:
                pass
    else:
        if session_id:
            _PENDING_INTENTS.pop(session_id, None)
        if sm_session_id:
            try:
                # 置空即可（读取端 isinstance(dict) 判空），保留会话记录
                _session_mgr.update_session(
                    sm_session_id, {"pending_intent": None})
            except Exception:
                pass


# v6.80 业务上下文注入（d4）：将对话中已确认的业务实体（产品码/订单号/数量/
# 客户名等）合并进 recent_entities，供意图识别 prompt 参考——LLM 结合已确认
# 实体理解当前输入（如 pending 下单收集中"它的价格呢"结合 A-202/100套 消歧）。
# 数据源：body 槽位 + pending 延续槽位；仅保留实体白名单键。
_ENTITY_WHITELIST = (
    "product_code", "order_id", "quantity", "customer_name",
    "supplier", "work_order_id", "po_id", "line_id",
    "batch_no", "product", "amount",
)


def _inject_recent_entities(user_context: dict, pending: Optional[Dict]) -> None:
    entities: Dict[str, Any] = {}
    for src in (user_context.get("slots"), (pending or {}).get("slots")):
        if isinstance(src, dict):
            for k, v in src.items():
                if k in _ENTITY_WHITELIST and v not in (None, ""):
                    entities[k] = v
    if entities:
        user_context["recent_entities"] = entities

# v6.36：会话级对话记忆管理器（滑动窗口N=3 + 摘要压缩 + 相关性筛选）
from prog.runtime.conversation_memory import ConversationMemoryManager
_memory_mgr = ConversationMemoryManager()

# v6.38：SessionManager（Redis持久化 + 内存降级），作为对话历史的持久化后端。
# 与 _memory_mgr 并行运行：
#   - _memory_mgr 提供进程内增强特征（relevant_turns / intent_state 等），
#     格式为 turns 列表（coordinator / intent_recognition 当前消费此格式）；
#   - _session_mgr 提供 Redis 持久化、token 窗口裁剪、20条触发摘要压缩，
#     历史格式为标准 {role, content, ts}，通过独立字段 session_history 注入。
from prog.runtime.session_manager import SessionManager
_session_mgr = SessionManager()  # 默认内存降级模式；启动时若 Redis 可用则切换

# v6.80：意图漂移检测——pending 延续时区分"补充信息"与"新业务话题"
# （补充信息零延迟沿用原意图收敛；新业务话题脱离 pending 走强模型发散识别）
from prog.runtime.intent_recognition import looks_like_new_business_query


def _init_session_mgr() -> None:
    """尝试用 CacheManager 的 Redis 连接升级 SessionManager，失败保持内存模式。

    设计意图：
        CacheManager 已封装 Redis 探测与降级逻辑（_init_redis），
        这里复用其 Redis 客户端，避免重复连接配置；任何异常都不影响主流程。
    """
    global _session_mgr
    try:
        from prog.runtime.cache import get_cache
        cache = get_cache()
        redis_client = getattr(cache, "_redis", None)
        if redis_client is not None:
            _session_mgr = SessionManager(redis_client=redis_client)
    except Exception:
        # 任何失败均保持默认内存模式，确保不阻断对话主流程
        pass


def _session_alias_key(session_id: str) -> str:
    return f"chat_session_alias:{session_id}"


# v6.67：前端 sessionId -> SessionManager UUID 的进程内存兜底映射。
# Redis 不可用（内存降级模式）时保证同浏览器同 sessionId 跨请求复用同一
# 持久化会话（多轮对话归档到同一 PG session，刷新后可恢复历史）。
_ALIAS_CACHE: Dict[str, str] = {}


def _save_session_alias(session_id: str, sm_session_id: str) -> None:
    """持久化 前端 sessionId -> SessionManager UUID 映射（Redis 1h TTL + 内存兜底）。"""
    _ALIAS_CACHE[session_id] = sm_session_id
    try:
        redis = getattr(_session_mgr, "_redis", None)
        if redis is not None:
            redis.set(_session_alias_key(session_id), sm_session_id, ex=3600)
    except Exception:
        pass


def _resolve_session_alias(session_id: str) -> str:
    """按前端 sessionId 解析持久化后端 session_id（Redis 优先，内存兜底）。"""
    try:
        redis = getattr(_session_mgr, "_redis", None)
        if redis is not None:
            v = redis.get(_session_alias_key(session_id))
            if v:
                return v.decode() if isinstance(v, bytes) else str(v)
    except Exception:
        pass
    cached = _ALIAS_CACHE.get(session_id)
    if cached:
        return cached
    return session_id or ""


def _ensure_session(session_id: str, user_id: str) -> str:
    """确保 SessionManager 中存在会话，返回可用于持久化的 session_id。

    参数：
        session_id: 请求体携带的 session_id（前端固定值，如 sess_<ts>）
        user_id: 用户ID（用于创建新会话）

    返回：
        str: SessionManager 内有效的 session_id（UUID 或已注册的原值）。
        首次映射时记录 前端sessionId -> UUID（Redis），后续请求可复用，
        保证多轮延续状态（pending_intent/历史）跨轮、跨重启可恢复。

    C-1 会话归属校验：复用已有会话前核对 user_id 归属——匿名/他人会话
    一律不复用（改为创建新会话隔离），防止盗用 session_id 延续他人会话
    （读取其历史/pending_intent）。
    """
    try:
        def _owner_ok(existing: dict) -> bool:
            owner = existing.get("user_id") or ""
            if user_id:
                return owner == user_id
            return owner in ("", "anonymous")

        # 前端 sessionId 若已有持久化映射，先校验归属再复用映射的 UUID
        resolved = _resolve_session_alias(session_id) if session_id else ""
        if resolved:
            existing = _session_mgr.get_session(resolved)
            if existing is not None:
                if _owner_ok(existing):
                    return resolved
                # 归属他人：不复用，落库新会话隔离
                _log.warning("会话 %s 归属他人（%s），拒绝复用，改新建会话",
                             resolved, existing.get("user_id", ""))
        if session_id:
            existing = _session_mgr.get_session(session_id)
            if existing is not None:
                if _owner_ok(existing):
                    return session_id
        # 不存在/归属不匹配 -> 创建新会话（SessionManager 内部生成 UUID），记录映射
        sm_id = _session_mgr.create_session(user_id or "anonymous")
        if session_id and sm_id:
            _save_session_alias(session_id, sm_id)
        return sm_id
    except Exception:
        # SessionManager 不可用时回退原值，确保不阻断对话主流程
        return session_id or ""


def _archive_conversation_pg(sm_session_id: str, user_id: str,
                             user_msg: str, reply_text: str,
                             intent_name: str = "", agent_name: str = "") -> None:
    """v6.62：会话归档到 PG 长期归档层（规格 §1.1.3.5 + L441）。

    Redis（SessionManager 快速层）之外的 PostgreSQL 归档：
      - conversation_sessions：会话元数据 + 摘要（upsert，last_active_at/context_summary）
      - conversation_messages：完整对话消息长期保存（user + assistant 两条）
    异步写入不阻断对话主流程；DB 不可用静默降级（仅 debug 状态使用降级路径）。
    """
    try:
        from datetime import datetime, timezone
        from prog.core.database import get_database
        db = get_database()
        if db is None:
            return

        def _write():
            try:
                now = datetime.now(timezone.utc).isoformat()
                summary = ""
                if reply_text:
                    summary = reply_text if len(reply_text) <= 2000 else reply_text[:2000] + "..."
                existed = db.query_one("conversation_sessions",
                                       {"session_id": sm_session_id})
                if existed:
                    db.update("conversation_sessions", {
                        "last_active_at": now,
                        "status": "active",
                        "context_summary": summary,
                    }, {"session_id": sm_session_id})
                else:
                    db.insert("conversation_sessions", {
                        "session_id": sm_session_id,
                        "user_id": user_id or "anonymous",
                        "channel": "erp",
                        "status": "active",
                        "context_summary": summary,
                    })
                if user_msg:
                    db.insert("conversation_messages", {
                        "session_id": sm_session_id,
                        "role": "user",
                        "content": user_msg[:2000],
                    })
                if reply_text:
                    db.insert("conversation_messages", {
                        "session_id": sm_session_id,
                        "role": "assistant",
                        "content": reply_text,
                        "intent": intent_name or "",
                        "agent": agent_name or "",
                    })
            except Exception:
                pass  # 归档失败不影响主流程（DB 不可用的 debug 降级）

        threading.Thread(target=_write, daemon=True).start()
    except Exception:
        pass


def _load_session_summary_pg(sm_session_id: str) -> str:
    """Redis 会话过期/进程重启后，从 PG 归档恢复会话摘要（规格 L441）。

    返回 conversation_sessions.context_summary；DB 不可用或查无返回空串。
    """
    try:
        from prog.core.database import get_database
        db = get_database()
        if db is None:
            return ""
        row = db.query_one("conversation_sessions",
                           {"session_id": sm_session_id})
        if row:
            return row.get("context_summary") or ""
    except Exception:
        pass
    return ""


# ============================================================
# 路由定义
# ============================================================

def _load_history_pg(db: Any, sm_id: str, before: str, after: str,
                     limit: int) -> Optional[list]:
    """v6.67：从 PG 长期归档读取会话消息（游标分页）。

    conversation_messages.message_id 为 BIGSERIAL 递增主键，作分页游标：
      - before: message_id < before 的更早消息（向上滚动）
      - after:  message_id > after 的更新消息（向下滚动）
    返回列表始终按 message_id 升序（对话顺序），供前端直接渲染。

    Args:
        db: 数据库实例
        sm_id: SessionManager 后端会话 ID
        before: 上游标（取更早）
        after: 下游标（取更新）
        limit: 每页条数

    Returns:
        list: 消息字典列表；查询失败返回 None（调用方回退 Redis）
    """
    cond = "session_id = :sid"
    params: Dict[str, Any] = {"sid": sm_id}
    if before:
        cond += " AND message_id < :b"
        params["b"] = before
    if after:
        cond += " AND message_id > :a"
        params["a"] = after
    # 下游标（after）按 ASC 取更新消息；上游标/首页按 DESC 取最近后逆转为升序
    order = "ASC" if after else "DESC"
    sql = (
        "SELECT message_id, role, content, intent, agent, created_at "
        "FROM conversation_messages WHERE " + cond +
        f" ORDER BY message_id {order} LIMIT :lim"
    )
    try:
        rows = db.execute(sql, {**params, "lim": limit}).fetchall()
    except Exception:
        return None
    out = []
    for row in rows:
        r = row._mapping if hasattr(row, "_mapping") else row
        out.append({
            "message_id": r["message_id"],
            "role": r["role"],
            "content": r["content"],
            "intent": r["intent"] or "",
            "agent": r["agent"] or "",
            "created_at": str(r["created_at"]),
        })
    if not after:
        out.reverse()
    return out


def _count_pg(db: Any, sm_id: str, op: str, mid: Any) -> bool:
    """v6.67：判断游标方向是否仍有更多消息。"""
    try:
        r = db.execute(
            "SELECT COUNT(*) AS c FROM conversation_messages "
            "WHERE session_id = :sid AND message_id " + op + " :m",
            {"sid": sm_id, "m": mid})
        row = r.fetchone()
        return bool(row and (row[0] or 0) > 0)
    except Exception:
        return False


def _chat_history_handler() -> Any:
    """v6.67：会话历史读取入口（PG 长期归档优先，Redis 会话历史兜底）。"""
    session_id = (request.args.get("session_id") or "").strip()
    try:
        limit = int(request.args.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = min(max(limit, 1), 100)
    before = (request.args.get("before") or "").strip()
    after = (request.args.get("after") or "").strip()

    if not session_id:
        return error_response(400, "缺少 session_id"), 400

    user_id = g.get('user_id', '')

    try:
        sm_id = _resolve_session_alias(session_id) or session_id
        # C-1 会话归属校验：仅允许访问归属当前用户的会话（SessionManager 层）
        try:
            sess = _session_mgr.get_session(sm_id)
            if sess is not None:
                owner = sess.get("user_id") or ""
                if user_id:
                    if owner and owner != user_id:
                        return error_response(403, "无权访问该会话"), 403
                elif owner not in ("", "anonymous"):
                    return error_response(401, "未登录，无法访问该会话"), 401
        except Exception:
            pass
        from prog.core.database import get_database
        db = get_database()
        if db is not None:
            # C-1：PG 归档层归属校验（Redis 会话过期时兜底）
            try:
                sess_row = db.query_one("conversation_sessions",
                                        {"session_id": sm_id})
                if sess_row and sess_row.get("user_id"):
                    owner = sess_row.get("user_id") or ""
                    if user_id:
                        if owner != user_id:
                            return error_response(403, "无权访问该会话"), 403
                    elif owner not in ("", "anonymous"):
                        return error_response(401, "未登录，无法访问该会话"), 401
            except Exception:
                pass
            messages = _load_history_pg(db, sm_id, before, after, limit)
            if messages is not None:
                has_more_prev = False
                has_more_next = False
                if messages:
                    has_more_prev = _count_pg(db, sm_id, "<", messages[0]["message_id"])
                    has_more_next = _count_pg(db, sm_id, ">", messages[-1]["message_id"])
                return api_response(code=0, data={
                    "messages": messages,
                    "has_more_prev": has_more_prev,
                    "has_more_next": has_more_next,
                    "session_id": sm_id,
                })
    except Exception as e:
        return error_response(500, f"历史读取失败：{str(e) if DEBUG else '内部错误'}"), 500

    # Redis 会话历史兜底（PG 不可用/无归档时）
    sess = _session_mgr.get_session(sm_id)
    history = (sess or {}).get("history", [])
    return api_response(code=0, data={
        "messages": history,
        "has_more_prev": False,
        "has_more_next": False,
        "session_id": sm_id,
    })


def register_chat_routes(bp: Blueprint, coordinator: Any) -> None:
    """
    在chat Blueprint上注册对话路由。

    设计意图：
        将路由注册与Blueprint定义分离，便于注入CoordinatorAgent依赖。

    参数：
        bp: chat Blueprint实例
        coordinator: CoordinatorAgent实例

    注册的路由：
        - POST /api/chat
        - POST /api/chat/stream
    """
    # v6.37：延迟注入 LLM 和 Embedding（从 coordinator 获取）
    llm_engine = getattr(coordinator, '_llm_engine', None)
    if llm_engine:
        _memory_mgr.configure(llm_client=llm_engine)
    ka = getattr(coordinator, 'knowledge_assistant', None)
    if ka:
        emb = getattr(ka, 'embedding_provider', None)
        if emb:
            _memory_mgr.configure(embedding_provider=emb)

    # v6.38：尝试用 CacheManager 的 Redis 连接升级 SessionManager
    # 失败时 SessionManager 自动保持内存降级模式（见 _init_session_mgr）
    _init_session_mgr()

    # 使用闭包捕获 coordinator 引用
    @bp.route('', methods=['POST'])
    def chat():
        return _chat_handler(coordinator)

    @bp.route('/stream', methods=['POST'])
    def chat_stream():
        return _chat_stream_handler(coordinator)

    @bp.route('/history', methods=['GET'])
    def chat_history():
        """v6.67：会话历史查询（登录/刷新同步显示 + 滚动分页加载）。

        参数：
            session_id: 前端会话ID（必填）
            before: 游标 message_id，返回该条之前的更早消息（向上滚动）
            after:  游标 message_id，返回该条之后的更新消息（向下滚动）
            limit:  每页条数（默认 30，最大 100）

        返回：
            {code:0, data:{messages:[{message_id,role,content,intent,agent,created_at}],
                           has_more_prev, has_more_next, session_id}}
            优先读 PG 长期归档（conversation_messages，跨重启可恢复），
            无归档记录时回退 Redis 会话历史（SessionManager）。
        """
        return _chat_history_handler()


def _get_user_context() -> dict:
    """从请求上下文构造用户上下文。

    用户身份仅信任认证中间件注入的 g（由 Bearer token 解析），
    不允许读取请求头/请求体中的身份字段（可伪造）。

    返回：
        dict: 用户上下文
    """
    session_id = ''
    message = ''

    body = {}
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}

    session_id = body.get('session_id', '') or session_id
    message = body.get('message', '') or body.get('text', '')

    # v6.57：审批通知点击恢复——resume_workflow 携带待审批流程实例
    # （前端点击审批待办通知时记录，下一条消息随 body 回传）
    resume_workflow = body.get("resume_workflow")
    if not isinstance(resume_workflow, dict) or not resume_workflow.get("instance_id"):
        resume_workflow = None

    # 随消息上传的文件：按 file_ids 拉取解析文本作为附件上下文（供Agent引用）
    attachments = []
    file_ids = body.get('file_ids') or []
    if file_ids:
        try:
            from prog.api.files_api import get_file_texts
            attachments = get_file_texts(file_ids)
        except Exception:
            attachments = []

    # v6.44：随消息上传的文件合并入文件类槽位（attachment），
    # 使训练/发起流程时的文件槽位（报销单、模板等）可由实际上传文件满足
    slots = body.get("slots", {})
    if attachments:
        try:
            from prog.runtime.slot_engine import merge_uploaded_files
            slots = merge_uploaded_files(slots, attachments)
        except Exception:
            pass

    # 构造用户上下文（身份来自 token 解析结果，见认证中间件）
    user_context = {
        "user": {
            "id": g.get('user_id', ''),
            "role": g.get('user_role', ''),
            "name": g.get('user_name', ''),
            "title": g.get('user_title', ''),
            "department": g.get('user_department', ''),
            "permissions": g.get('permissions', {}),
        },
        "session_id": session_id,
        "history": body.get("history", []),
        "slots": slots,
        "attachments": attachments,
        # v6.57：审批通知点击恢复——随 body 回传的待审批流程实例
        # （下游 _chat_handler/_chat_stream_handler 读取此键注入 awaiting_approval pending）
        "resume_workflow": resume_workflow,
        # TG-09（v6.99.2）：客户端环境采集——client_ip/platform/request_id
        # 供跨部门临时授权申请/审计全链路使用（部署建议 P8，ProxyFix 已启用）
        "client_ip": (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                      or request.remote_addr or ""),
        "platform": (request.headers.get("User-Agent", "") or "")[:200],
        "request_id": request.headers.get("X-Request-ID") or str(uuid.uuid4()),
    }
    return user_context, message


# --------------------------------------------------------
# 主对话接口
# --------------------------------------------------------
def _chat_handler(coordinator: Any):
    """POST /api/chat 主对话接口内部实现。"""
    user_context, message = _get_user_context()

    if not message:
        # A3：错误响应统一 {code,msg,data:{content,...}}，与其它 API 契约一致
        return api_response(code=400, msg="message is required",
                            data={"content": "请输入您的消息。"}), 400

    try:
        session_id = user_context.get("session_id", "")
        # v6.38：确保 SessionManager 中存在会话（持久化后端）
        user_id = user_context.get("user", {}).get("id", "")
        sm_session_id = _ensure_session(session_id, user_id)
        # 注入会话级待延续意图（多轮：补充信息沿用原业务意图）
        # v6.46 D5：从 SessionManager 持久化读取（Redis 恢复重启前状态）
        pending = _load_pending_intent(session_id, sm_session_id)
        # v6.57：审批通知点击恢复——resume_workflow 覆盖待审批上下文（点击
        # 通知意图明确），即使会话刷新/pending 丢失也可定位待审批流程实例
        resume = user_context.get("resume_workflow")
        if resume:
            pending = {
                "name": "workflow_start",
                "target_agent": "knowledge",
                "slots": {},
                "action": "awaiting_approval",
                "phase": "awaiting_approval",
                "workflow_instance": {
                    "instance_id": resume.get("instance_id"),
                    "workflow_type": resume.get("workflow_type", ""),
                },
            }
            user_context["pending_intent"] = pending
        elif pending:
            user_context["pending_intent"] = pending
        # v6.80 业务上下文注入：pending/body 槽位实体供识别 prompt 参考
        _inject_recent_entities(user_context, pending)
        # v6.36：注入对话记忆上下文（3轮历史+摘要+相关轮次+last_input/last_reply）
        # v6.37：增加 intent_state（递归意图记忆）
        mem_ctx = _memory_mgr.get_context(session_id, message)
        if mem_ctx:
            user_context["last_input"] = mem_ctx.get("last_input", "") or _memory_mgr.get_last_input(session_id)
            user_context["last_reply"] = mem_ctx.get("last_reply", "") or _memory_mgr.get_last_reply(session_id)
            user_context["conversation_history"] = mem_ctx.get("turns", [])
            user_context["conversation_summary"] = mem_ctx.get("summary", "")
            user_context["relevant_turns"] = mem_ctx.get("relevant_turns", [])
            user_context["intent_state"] = mem_ctx.get("intent_state", "")
        # v6.38：注入 SessionManager 持久化上下文（标准 {role,content} 历史格式 + 摘要）
        # 注：使用独立字段 session_history / session_summary，避免覆盖 _memory_mgr
        # 的 turns 格式（coordinator / intent_recognition 仍消费 conversation_history）
        try:
            sm_ctx = _session_mgr.get_prompt_context(sm_session_id)
            if sm_ctx:
                user_context["session_history"] = sm_ctx.get("history", [])
                user_context["session_summary"] = sm_ctx.get("summary", "")
                user_context["session_total_tokens"] = sm_ctx.get("total_tokens", 0)
            else:
                # v6.62：Redis 会话过期/重启后从 PG 归档恢复摘要（规格 §1.1.3.5）
                pg_summary = _load_session_summary_pg(sm_session_id)
                if pg_summary:
                    user_context["session_summary"] = pg_summary
        except Exception:
            pass

        # v6.84.1：非流式接口预识别看门狗（与流式 SSE 接口 v6.79.1 对齐）--
        # 识别强模型（thinking）偶发推理不收敛时，route() 内 _recognize_intent
        # 无超时保护，非流式请求会挂死到客户端超时（实测"查看A-202的BOM结构"
        # 稳定 200s+）。后台线程预识别 + intent_timeout_sec 看门狗，超时回退
        # 规则层（skip_llm=True 零延迟），结果经 _pre_intent 供 route() 复用
        # （skip_llm 语义一致时避免二次强模型调用）。
        pending_now = user_context.get("pending_intent")
        skip_llm_now = bool(
            pending_now and isinstance(pending_now, dict)
            and not looks_like_new_business_query(message))
        _intent_box: Dict[str, Any] = {}

        def _pre_recognize_sync() -> None:
            try:
                _intent_box["intent"] = coordinator._recognize_intent(
                    message, user_context, skip_llm=skip_llm_now)
            except Exception:
                _intent_box["intent"] = None

        _rt_sync = threading.Thread(target=_pre_recognize_sync, daemon=True)
        _rt_sync.start()
        _pre_timeout = 25.0
        try:
            _iprov = getattr(
                getattr(coordinator, "_intent_recognizer", None),
                "_llm_client", None)
            _ip = getattr(_iprov, "llm_provider", None)
            if _ip is not None and getattr(_ip, "intent_timeout_sec", None):
                _pre_timeout = float(_ip.intent_timeout_sec)
        except Exception:
            pass
        _pre_t0 = time.time()
        while _rt_sync.is_alive():
            _rt_sync.join(timeout=0.5)
            if time.time() - _pre_t0 > _pre_timeout:
                # C-4：预识别超时——记录日志便于观测（daemon 线程由解释器回收）
                _log.warning("非流式预识别超时（%.1fs），回退规则层识别", _pre_timeout)
                break
        _pre_result = _intent_box.get("intent")
        if _pre_result is None:
            # 线程异常/超时兜底：回退规则层（不再触发可能再次卡住的强模型）
            _pre_result = coordinator._recognize_intent(
                message, user_context, skip_llm=True)
        user_context["_pre_intent"] = _pre_result
        user_context["_pre_intent_skip_llm"] = skip_llm_now

        # 调用 CoordinatorAgent 路由处理（v6.81：复合句多跳走 route_compound，
        # 单段输入内部透传 route，行为零变化）
        response = coordinator.route_compound(message, user_context)

        # 依据响应元数据维护多轮延续状态：
        # - metadata.pending_intent 有值 -> 写入（业务意图/request_info 状态）
        # - metadata.pending_intent 为 None -> 清除（任务完成/取消/确认）
        # v6.46 D5：双写 SessionManager(Redis 持久化) + 内存快速路径
        meta = response.metadata or {}
        pending_from_meta = meta.get("pending_intent", "__not_set__")
        if pending_from_meta != "__not_set__":
            _save_pending_intent(
                session_id, sm_session_id,
                pending_from_meta if isinstance(pending_from_meta, dict) else None)

        # v6.36：更新对话记忆（滑动窗口N=3 + 自动摘要压缩）
        reply_text = getattr(response, "content", "") or ""
        intent_name = meta.get("intent", "")
        agent_name = getattr(response, "agent_name", "")
        _memory_mgr.add_turn(session_id, message, reply_text, intent_name, agent_name)
        # v6.38：写入 SessionManager 持久化历史（user + assistant 两条消息）
        # 失败不阻断主流程（Redis 不可用时 SessionManager 自动降级内存模式）
        try:
            _session_mgr.add_message(sm_session_id, "user", message)
            _session_mgr.add_message(sm_session_id, "assistant", reply_text)
        except Exception:
            pass
        # v6.62：会话归档 PG 长期层（conversation_sessions/messages，规格 §1.1.3.5）
        _archive_conversation_pg(sm_session_id, user_id, message, reply_text,
                                 intent_name, agent_name)

        resp = response.to_dict()
        # v6.46：回传持久化后端 session_id，供前端下次请求复用（跨重启恢复多轮状态）
        if isinstance(resp, dict):
            resp["sm_session_id"] = sm_session_id
        return jsonify(resp)
    except Exception as e:
        _log.exception("chat 处理异常")
        # C-7：外部响应不回传内部异常详情（DEBUG 模式除外），避免信息泄露
        detail = str(e) if DEBUG else "内部错误"
        # A3：错误响应统一 {code,msg,data:{content,...}}，与其它 API 契约一致
        return api_response(code=500, msg=detail,
                            data={
                                "content": f"处理请求时发生错误：{detail}",
                                "agent_name": "系统",
                            }), 500


def chat(coordinator: Any):
    """
    POST /api/chat 主对话接口。

    设计意图：
        接收用户消息，调用 CoordinatorAgent.route() 完成意图识别与
        Agent分发，返回AgentResponse。

    请求体（JSON）：
        {
            "message": "用户消息文本",
            "session_id": "会话ID（用于多轮对话）",
            "user_id": "用户ID（从JWT解析）"
        }

    响应（JSON）：
        {
            "content": "Agent回复文本",
            "data": {...},            # 结构化业务数据
            "need_confirm": false,    # 是否需要用户确认
            "agent_name": "销售Agent",
            "rules_violated": [...]   # 命中违规的规则
        }

    流程：
        1. 解析请求体，提取message/session_id/user_id
        2. 构造user_context（含身份、权限、对话历史）
        3. 调用 coordinator.route(message, user_context)
        4. 返回 AgentResponse.to_dict()
    """
    return _chat_handler(coordinator)


# --------------------------------------------------------
# SSE流式对话接口
# --------------------------------------------------------
def _chat_stream_handler(coordinator: Any):
    """POST /api/chat/stream SSE流式对话接口内部实现。"""
    user_context, message = _get_user_context()

    if not message:
        # A3：流式接口的流前错误同样统一 {code,msg,data:{content,...}}（SSE 事件流除外）
        return api_response(code=400, msg="message is required",
                            data={"content": "请输入您的消息。"}), 400

    def _fake_stream(content, delay=0.015):
        """假流式：按标点切分文本，逐块 yield SSE message 事件。

        v6.60：HTML 单据内容（流程实例查询等以 '<' 开头的渲染结果）
        一次性完整输出，避免被标点切碎导致前端拼接残缺。
        """
        if content and str(content).lstrip().startswith("<"):
            payload = json.dumps({"content": content}, ensure_ascii=False)
            yield f"event: message\ndata: {payload}\n\n"
            return
        chunks = []
        current = ""
        for char in content:
            current += char
            if char in '\n，。！？、；：' or len(current) >= 12:
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        for chunk in chunks:
            payload = json.dumps({"content": chunk}, ensure_ascii=False)
            yield f"event: message\ndata: {payload}\n\n"
            if delay > 0:
                time.sleep(delay)

    def generate():
        """SSE流式生成器。"""
        # ════════════════════════════════════════════════════════════════
        # PERF-FIX-v6.40 ANCHOR::首帧占位 — 必须放在任何DB/LLM/IO阻塞操作之前
        # 根因：首帧前3个阻塞点（session/memory/intent_recog）合计可达7s，
        #       用户在此期间只看到三点动画，体感卡死。
        # 修复：generate()首行立即yield meta+占位，后续阻塞在用户已看到文字后进行
        # 防回归：禁止把任何阻塞操作（DB/网络/模型调用）移到本ANCHOR块之前
        # ════════════════════════════════════════════════════════════════
        # 1/3 首包：meta（Agent栏"正在思考"）+ 首条占位
        thinking_meta = {"agent_name": "正在思考", "need_confirm": False, "rules_violated": []}
        yield f"event: meta\ndata: {json.dumps(thinking_meta, ensure_ascii=False)}\n\n"
        # PERF-FIX-v6.40：立即显示文字占位（替代三点动画，<100ms用户可见）
        yield f"event: message\ndata: {json.dumps({'content': '收到，正在帮您处理，请稍候~', 'placeholder': True}, ensure_ascii=False)}\n\n"

        # 以下DB/内存操作不影响首帧，在用户已看到占位文字后异步执行
        session_id = user_context.get("session_id", "")
        # v6.38：确保 SessionManager 中存在会话（持久化后端）
        user_id = user_context.get("user", {}).get("id", "")
        sm_session_id = _ensure_session(session_id, user_id)
        # 注入会话级待延续意图（多轮：补充信息沿用原业务意图）
        # v6.46 D5：从 SessionManager 持久化读取（Redis 恢复重启前状态）
        pending = _load_pending_intent(session_id, sm_session_id)
        # v6.57：审批通知点击恢复（与 _chat_handler 同步）——resume_workflow
        # 覆盖待审批上下文，即使会话刷新/pending 丢失也可定位待审批流程实例
        resume = user_context.get("resume_workflow")
        if resume:
            pending = {
                "name": "workflow_start",
                "target_agent": "knowledge",
                "slots": {},
                "action": "awaiting_approval",
                "phase": "awaiting_approval",
                "workflow_instance": {
                    "instance_id": resume.get("instance_id"),
                    "workflow_type": resume.get("workflow_type", ""),
                },
            }
            user_context["pending_intent"] = pending
        elif pending:
            user_context["pending_intent"] = pending
        # v6.80 业务上下文注入：pending/body 槽位实体供识别 prompt 参考
        _inject_recent_entities(user_context, pending)
        # v6.36：注入对话记忆上下文（3轮历史+摘要+相关轮次+last_input/last_reply）
        # v6.37：增加 intent_state（递归意图记忆）
        mem_ctx = _memory_mgr.get_context(session_id, message)
        if mem_ctx:
            user_context["last_input"] = mem_ctx.get("last_input", "") or _memory_mgr.get_last_input(session_id)
            user_context["last_reply"] = mem_ctx.get("last_reply", "") or _memory_mgr.get_last_reply(session_id)
            user_context["conversation_history"] = mem_ctx.get("turns", [])
            user_context["conversation_summary"] = mem_ctx.get("summary", "")
            user_context["relevant_turns"] = mem_ctx.get("relevant_turns", [])
            user_context["intent_state"] = mem_ctx.get("intent_state", "")
        # v6.38：注入 SessionManager 持久化上下文（标准 {role,content} 历史格式 + 摘要）
        # 注：使用独立字段 session_history / session_summary，避免覆盖 _memory_mgr
        # 的 turns 格式（coordinator / intent_recognition 仍消费 conversation_history）
        try:
            sm_ctx = _session_mgr.get_prompt_context(sm_session_id)
            if sm_ctx:
                user_context["session_history"] = sm_ctx.get("history", [])
                user_context["session_summary"] = sm_ctx.get("summary", "")
                user_context["session_total_tokens"] = sm_ctx.get("total_tokens", 0)
            else:
                # v6.62：Redis 会话过期/重启后从 PG 归档恢复摘要（规格 §1.1.3.5）
                pg_summary = _load_session_summary_pg(sm_session_id)
                if pg_summary:
                    user_context["session_summary"] = pg_summary
        except Exception:
            pass

        try:
            # PERF-FIX-v6.40 ANCHOR::意图识别前阶段占位
            # 根因：intent_recognition 在规则未命中时走LLM fallback≈6.7s，
            #       期间无任何UI更新→用户感知"卡死"。
            # 修复：意图识别前先yield"正在分析..."（规则命中时此帧和下一帧几乎同时出现，
            #       用户无感；规则未命中时用户能看到进度，避免以为系统死掉）
            # 防回归：禁止移除本yield，即使意图识别速度再快也要保留（陌生查询兜底）
            # ════════════════════════════════════════════════════════════════
            # 2/3 阶段：意图分析占位
            yield f"event: message\ndata: {json.dumps({'content': '🧭 让我先看一下您想做什么...', 'placeholder': True}, ensure_ascii=False)}\n\n"
            # v6.46：多轮延续（存在 pending_intent）时预识别仅规则匹配——
            # 补充信息（如"A-202，100套"）不应触发 LLM 语义识别（零延迟），
            # 且避免预识别与 route() 内识别重复调用 LLM（双识别延迟/双倍 token）
            pending_now = user_context.get("pending_intent")
            # v6.80 意图漂移检测（发散-收敛平衡）：pending 下若输入含明确业务名词
            # （新业务话题，如 pending 下单收集中问"咱库存…"），不跳过 LLM——
            # 交给强模型发散重新识别；纯补充信息（产品码/数量/订单号等，
            # looks_like_new_business_query=False）才零延迟沿用原意图（收敛）。
            skip_llm_now = bool(
                pending_now and isinstance(pending_now, dict)
                and not looks_like_new_business_query(message))
            # v6.78.3 双模型架构：预识别使用识别强模型（thinking 开启）在后台
            # 线程执行，reasoning 逐块经队列实时推前端（event: reasoning）——用户
            # 看到"思考中"而非静止占位，感知响应快；识别完成后 intent 供知识预判
            # 与 route() 复用（避免第二次强模型调用，见 coordinator.route L481）。
            # skip_llm（多轮补充信息）时无 LLM 调用，reasoning 队列为空，等价原行为。
            try:
                from queue import Empty as _QueueEmpty
                import queue as _queue
            except Exception:
                _queue = None
                _QueueEmpty = None
            if _queue is not None:
                _reasoning_q = _queue.Queue()
                _intent_holder: Dict[str, Any] = {}

                def _reasoning_sink(text: str) -> None:
                    _reasoning_q.put(text)

                def _pre_recognize() -> None:
                    try:
                        _intent_holder["intent"] = coordinator._recognize_intent(
                            message, user_context, skip_llm=skip_llm_now,
                            reasoning_callback=_reasoning_sink)
                    except Exception:
                        _intent_holder["intent"] = None

                _rt = threading.Thread(target=_pre_recognize, daemon=True)
                _rt.start()
                # v6.79.1：预识别超时看门狗——识别强模型（thinking）偶发推理
                # 不收敛（长时间空转不出 tool_call，如"帮我下个单"曾卡死 90s+），
                # 超过 intent_timeout_sec 即放弃强模型结果，回退规则层识别，
                # 避免 SSE 连接挂死（浏览器 150s 超时无 done 事件）。
                # 超时时长外部可配：deployment_config.json
                # interfaces.intent_llm_provider.config.intent_timeout_sec。
                _pre_timeout_sec = 25.0
                try:
                    _iprov = getattr(
                        getattr(coordinator, "_intent_recognizer", None),
                        "_llm_client", None)
                    _ip = getattr(_iprov, "llm_provider", None)
                    if _ip is not None and getattr(_ip, "intent_timeout_sec", None):
                        _pre_timeout_sec = float(_ip.intent_timeout_sec)
                except Exception:
                    pass
                _pre_t0 = time.time()
                _pre_timed_out = False
                while _rt.is_alive():
                    _rt.join(timeout=0.3)
                    if time.time() - _pre_t0 > _pre_timeout_sec:
                        # C-4：预识别超时——记录日志便于观测（daemon 线程回收）
                        _log.warning("流式预识别超时（%.1fs），回退规则层识别",
                                     _pre_timeout_sec)
                        _pre_timed_out = True
                        break
                    while True:
                        try:
                            _rc = _reasoning_q.get_nowait()
                        except _QueueEmpty:
                            break
                        if _rc:
                            yield f"event: reasoning\ndata: {json.dumps({'content': _rc}, ensure_ascii=False)}\n\n"
                # 线程结束后兜底清空剩余 reasoning 块（超时放弃后不再消费，
                # 避免被已放弃线程的迟到 reasoning 持续推送到前端）
                if not _pre_timed_out:
                    while True:
                        try:
                            _rc = _reasoning_q.get_nowait()
                        except _QueueEmpty:
                            break
                        if _rc:
                            yield f"event: reasoning\ndata: {json.dumps({'content': _rc}, ensure_ascii=False)}\n\n"
                intent = _intent_holder.get("intent")
                if intent is None or _pre_timed_out:
                    # 识别线程异常/超时兜底：回退规则层（skip_llm=True 零延迟，
                    # 且不再触发可能再次卡住的强模型调用；规则层可正确命中
                    # "帮我下个单"->create_order 等确定性输入）
                    intent = coordinator._recognize_intent(
                        message, user_context, skip_llm=True)
            else:
                intent = coordinator._recognize_intent(
                    message, user_context, skip_llm=skip_llm_now)
            # v6.78.3：预识别结果复用给 route()（同 skip_llm 语义时才生效）
            user_context["_pre_intent"] = intent
            user_context["_pre_intent_skip_llm"] = skip_llm_now
            # 多轮延续预判：存在 pending 且当前意图未命中新业务意图时，
            # 不走知识助手短路，交由 route() 沿用原业务意图（与同步接口一致）
            pending = user_context.get("pending_intent")
            continues = bool(
                pending and isinstance(pending, dict) and pending.get("target_agent")
                and intent.name in ("unknown", "knowledge_query", "greeting", "help")
            )
            is_knowledge = (not continues) and intent.name in (
                "knowledge_query", "chitchat", "unknown", "management_consulting"
            ) and coordinator.knowledge_assistant is not None

            if is_knowledge:
                # === 知识问答：LLM 真流式 ===
                agent_name = "知识助手"
                # 知识问答 -> 清除待延续意图（用户切换了话题）
                _save_pending_intent(session_id, sm_session_id, None)
                try:
                    ka = coordinator.knowledge_assistant

                    # 发送 meta（知识助手，无需确认）
                    meta = {"agent_name": agent_name, "need_confirm": False, "rules_violated": []}
                    yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

                    # ════════════════════════════════════════════════════════════
                    # PERF-FIX-v6.40 ANCHOR::RAG前阶段占位（3/3）
                    # 防回归：3条占位yield必须分别在：
                    #   ①generate()入口（首包）②意图识别前  ③RAG+LLM前
                    # ════════════════════════════════════════════════════════════
                    # 3/3 阶段：知识库检索占位
                    yield f"event: message\ndata: {json.dumps({'content': '🔍 正在知识库里帮您找相关资料...', 'placeholder': True}, ensure_ascii=False)}\n\n"

                    # 流式输出 LLM 内容（区分 reasoning 和 content）
                    stream_buf = []
                    for chunk in ka.process_stream(message, user_context):
                        if isinstance(chunk, tuple):
                            chunk_type, chunk_text = chunk
                            if chunk_text:
                                event_type = "reasoning" if chunk_type == "reasoning" else "message"
                                if event_type == "message":
                                    stream_buf.append(chunk_text)
                                payload = json.dumps({"content": chunk_text}, ensure_ascii=False)
                                yield f"event: {event_type}\ndata: {payload}\n\n"
                        elif chunk:
                            stream_buf.append(str(chunk))
                            payload = json.dumps({"content": chunk}, ensure_ascii=False)
                            yield f"event: message\ndata: {payload}\n\n"

                    # v6.62：知识问答会话归档 PG 长期层（规格 §1.1.3.5）
                    _archive_conversation_pg(sm_session_id, user_id, message,
                                             "".join(stream_buf),
                                             "knowledge_query", agent_name)
                    yield "event: done\ndata: [DONE]\n\n"
                    return
                except Exception as e:
                    # C-2 修复：知识分支失败直接向用户报错并结束流——
                    # 不再降级重跑 route_compound() 业务链（会重复识别/执行，
                    # 双倍 LLM 成本且可能重复执行业务副作用）
                    _log.exception("知识问答流式处理失败")
                    _err_detail = str(e) if DEBUG else "内部错误"
                    err_meta = {"agent_name": agent_name, "need_confirm": False,
                                "blocked": True, "error": _err_detail}
                    yield f"event: meta\ndata: {json.dumps(err_meta, ensure_ascii=False)}\n\n"
                    err_payload = json.dumps(
                        {"content": f"知识问答处理失败：{_err_detail}"},
                        ensure_ascii=False)
                    yield f"event: message\ndata: {err_payload}\n\n"
                    yield "event: done\ndata: [DONE]\n\n"
                    return

            # === 业务操作：route_compound() + 假流式（v6.81 多跳）===
            result_holder = {}
            def _route():
                result_holder["response"] = coordinator.route_compound(message, user_context)
            t = threading.Thread(target=_route, daemon=True)
            t.start()

            # ⚡ 优化：启动 route 线程后立即推送阶段占位文字，让用户知道在执行业务
            yield f"event: message\ndata: {json.dumps({'content': '⚙️ 好的，正在执行，请稍等...', 'placeholder': True}, ensure_ascii=False)}\n\n"

            # C-3 看门狗：route 线程最长等待 _route_timeout 秒，超时结束流式
            # 响应（后台 daemon 线程由解释器回收），避免 SSE 连接挂死无 done 事件
            _route_t0 = time.time()
            _route_timeout = 180.0
            while t.is_alive():
                t.join(timeout=2)
                if time.time() - _route_t0 > _route_timeout:
                    _log.error("route 线程执行超时（%.0fs），终止流式响应", _route_timeout)
                    break
                if t.is_alive():
                    yield ": heartbeat\n\n"

            response = result_holder.get("response")
            if response is None:
                raise RuntimeError("coordinator.route() 未返回结果")

            # 发送 meta（intent 以 route() 生效后的意图为准，多轮延续时非原始 unknown）
            resp_meta = response.metadata or {}
            meta = {
                "agent_name": response.agent_name or "Agent",
                "need_confirm": getattr(response, "need_confirm", False),
                "rules_violated": getattr(response, "rules_violated", []),
                "intent": resp_meta.get("intent") or intent.name,
                "sm_session_id": sm_session_id,
            }
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

            # 假流式输出内容
            content = getattr(response, "content", "") or "（无回复）"
            for sse_event in _fake_stream(content):
                yield sse_event

            # 更新多轮延续状态（v6.46 D5：SessionManager 持久化 + 内存双写）
            pending_from_meta = resp_meta.get("pending_intent", "__not_set__")
            if pending_from_meta != "__not_set__":
                _save_pending_intent(
                    session_id, sm_session_id,
                    pending_from_meta if isinstance(pending_from_meta, dict) else None)

            # v6.36：更新对话记忆（滑动窗口N=3 + 自动摘要压缩）
            intent_name = resp_meta.get("intent", "")
            agent_name = meta.get("agent_name", "")
            _memory_mgr.add_turn(session_id, message, content, intent_name, agent_name)
            # v6.38：写入 SessionManager 持久化历史（user + assistant 两条消息）
            # 失败不阻断主流程（Redis 不可用时 SessionManager 自动降级内存模式）
            try:
                _session_mgr.add_message(sm_session_id, "user", message)
                _session_mgr.add_message(sm_session_id, "assistant", content)
            except Exception:
                pass
            # v6.62：业务操作会话归档 PG 长期层（规格 §1.1.3.5）
            _archive_conversation_pg(sm_session_id, user_id, message, content,
                                     intent_name, agent_name)

            yield "event: done\ndata: [DONE]\n\n"

        except Exception as e:
            _log.exception("chat stream 处理异常")
            # C-7：外部 SSE 不回传内部异常详情（DEBUG 模式除外），避免信息泄露
            _sse_detail = str(e) if DEBUG else "内部错误"
            meta = {"need_confirm": False, "blocked": True, "error": _sse_detail}
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            err_msg = json.dumps(
                {"content": f"处理请求时发生错误：{_sse_detail}"},
                ensure_ascii=False)
            yield f"event: message\ndata: {err_msg}\n\n"
            yield "event: done\ndata: [DONE]\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


def chat_stream(coordinator: Any):
    """
    POST /api/chat/stream SSE流式对话接口。

    设计意图：
        与 /api/chat 功能一致，但以SSE流式返回，降低首字延迟。
        适用于长回复场景。

    请求体（JSON）：
        同 /api/chat

    响应（SSE流）：
        event: meta
        data: {"need_confirm": false, "agent_name": "销售Agent"}

        event: message
        data: {"content": "正在为您"}

        event: message
        data: {"content": "查询订单..."}

        event: done
        data: [DONE]

    流程：
        1. 解析请求体，构造user_context
        2. 调用 coordinator.route() 获取流式生成器
        3. 以Response(stream, mimetype='text/event-stream')返回
        4. 生成器逐块yield SSE事件

    说明：
        高风险操作时，首个meta事件中 need_confirm=True，
        前端确认后调用业务接口执行。
    """
    return _chat_stream_handler(coordinator)
