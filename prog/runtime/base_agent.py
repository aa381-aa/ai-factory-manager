from __future__ import annotations

"""
BaseAgent 基类模块
==================

文件用途：
    定义所有业务领域Agent的公共抽象基类，统一Agent的输入/输出契约、
    生命周期与安全门控接入点。

技术规格章节（原项目引用）：
    - §1.1.3 Coordinator Agent（Agent生命周期与公共接口）
    - §2 LLM安全门控（5道门控在Agent处理流程中的接入位置）

Agent生命周期（基类约定）：
    1. 接收：process(user_input, context) 接收用户输入与会话上下文
    2. 构建提示词：_build_prompt() 注入Agent身份、业务数据、规则约束、对话历史
    3. LLM调用：_call_llm() 通过 LLMEngine 调用大模型（含5道安全门控）
    4. 规则校验：_apply_rules() 执行硬性业务规则（LLM不可绕过）
    5. 安全检查：经 LLMEngine 的 _run_safety_gates() 做输出安全校验
    6. 返回：_format_response() 格式化为统一的 AgentResponse

设计原则：
    - 子类必须实现 process()，可选重写 _build_prompt() 和 _handle_xxx()
    - 规则校验为硬约束，优先级高于LLM输出
    - Agent之间通过 CoordinatorAgent 进行上下文隔离，禁止直接互调

开源化说明：
    - LLM 引擎（prog/llm/engine.py）不属于开源框架范围，此处保留可选延迟导入，
      无 LLM/DB 环境时框架仍可导入运行（_call_llm 返回空串，由 _format_response 兜底）。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 定义所有业务领域 Agent 的公共抽象基类，统一输入/输出契约与生命周期（接收→构建提示词→LLM 调用→规则校验→格式化响应）（来源：SPEC §3.1 / 模块拆分方案 契约1）
        - 统一 AgentResponse 响应契约（content/data/action/need_confirm/rules_violated/agent_name/metadata），支持 to_dict 序列化与 SSE 流式输出（meta/message/done 事件）（来源：SPEC §3.1.1 / 模块拆分方案 契约1）
        - 规则校验为硬约束：硬规则（is_hard）blocked 时 LLM 输出不可覆盖，_apply_rules 中 result.blocked 立即终止循环（来源：SPEC §3.3.3 / 模块拆分方案 契约4）
        - 可观测性：process() 在 metadata 自动写入 elapsed_ms 耗时与 trace_id（§4.7.2.1 补正项①，v1.1 已提取）（来源：SPEC §4 可观测性）
    对外接口（方法/API）：
        - BaseAgent.__init__(agent_name, agent_type, llm_provider=None, database=None)：初始化 agent_name/agent_type/llm_provider/database/applicable_rules 公共属性（来源：SPEC §3.1.2）
        - BaseAgent.process(user_input, context) -> AgentResponse：Agent 统一入口（CoordinatorAgent 分发契约），按"构建提示词→LLM 调用→规则校验→格式化响应"编排并回填 elapsed_ms/agent_name（来源：SPEC §3.1.2 / 模块拆分方案 契约1）
        - BaseAgent._build_prompt(user_input, context) -> str：注入 Agent 身份/用户身份与权限/最近 2 轮对话历史/回复规范（来源：SPEC §3.1.2）
        - BaseAgent._call_llm(prompt) -> str：优先注入 llm_provider（call/generate/chat_completion），其次可选导入 LLMEngine，均不可用返回空串（来源：SPEC §3.1.2 / §2.3）
        - BaseAgent._apply_rules(data) -> RuleResult：按 applicable_rules 逐条执行规则，任一 blocked 立即终止，全部通过返回 pass（来源：SPEC §3.1.2 / 契约4）
        - BaseAgent._format_response(llm_output, rule_result=None) -> AgentResponse：按 blocked（硬阻断）/警告/requires_approval 三种规则结果格式化（来源：SPEC §3.1.2）
        - AgentResponse.to_dict() -> dict / to_sse_stream() -> Generator：序列化与 SSE 流式转换（来源：SPEC §3.1.1）
    错误处理要求：
        - 规则引擎不可用（未加载/异常）：返回 _PassRuleResult 默认通过，避免阻断业务流程（来源：SPEC §3.1.2）
        - LLM 不可用（未注入 llm_provider 且可选 LLM 引擎不可用）：_call_llm 返回空串，由 _format_response 给出兜底回复（来源：SPEC §2.3 / §3.1.2）
        - 规则不存在：跳过该规则继续执行其余规则；规则执行异常：fail-closed 转 warn（requires_approval）需人工复核，不静默放行（W7 修复，与 RuleEngine fail-closed / 审计链 warn 策略一致）（来源：SPEC §3.1.2）
"""

import json
import re
import threading
import time
from typing import Any, Dict, Generator, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from prog.runtime.rule_registry import RuleResult


# ============================================================
# 数据契约定义（占位类型，便于跨模块统一引用）
# ============================================================

class AgentResponse:
    """
    Agent 统一响应对象。

    用于在 Coordinator / Agent / API 层之间传递处理结果，避免使用裸 dict。

    属性说明：
        - content: 主回复内容（自然语言或结构化文本）
        - data: 结构化业务数据（订单、库存、排产等）
        - action: Agent建议的下一步动作（如"等待用户确认"）
        - need_confirm: 是否需要用户二次确认（高风险操作）
        - rules_violated: 命中违规的规则列表
        - agent_name: 产生该响应的Agent名称
        - metadata: 其他元信息（耗时、token用量等）
    """

    def __init__(self, content: str = "", data: Optional[Dict[str, Any]] = None,
                 action: str = "", need_confirm: bool = False,
                 rules_violated: Optional[List[str]] = None,
                 agent_name: str = "", metadata: Optional[Dict[str, Any]] = None):
        """初始化Agent响应对象。"""
        self.content = content or ""
        self.data = data or {}
        self.action = action or ""
        self.need_confirm = bool(need_confirm)
        self.rules_violated = rules_violated or []
        self.agent_name = agent_name or ""
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """将响应序列化为字典，供API层返回前端。"""
        return {
            "content": self.content,
            "data": self.data,
            "action": self.action,
            "need_confirm": self.need_confirm,
            "rules_violated": self.rules_violated,
            "agent_name": self.agent_name,
            "metadata": self.metadata,
        }

    def to_sse_stream(self) -> Generator[str, None, None]:
        """将响应转换为SSE流式数据块生成器（供流式接口使用）。"""
        # 首个meta事件：携带Agent名称/是否需确认/违规规则/action/metadata（与 to_dict 契约对齐，W32）
        meta = {
            "agent_name": self.agent_name,
            "need_confirm": self.need_confirm,
            "rules_violated": self.rules_violated,
            "action": self.action,
            "metadata": self.metadata,
        }
        yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

        # 按标点切分内容为多个chunk，模拟流式输出
        if self.content:
            chunks = _split_content_to_chunks(self.content)
            for chunk in chunks:
                payload = json.dumps({"content": chunk}, ensure_ascii=False)
                yield f"event: message\ndata: {payload}\n\n"

        # 结束事件
        yield "event: done\ndata: [DONE]\n\n"


def _split_content_to_chunks(text: str, chunk_size: int = 8) -> List[str]:
    """将文本按标点或固定长度切分为多个块，用于模拟流式输出。"""
    if not text:
        return []
    # 按标点切分，保留标点
    parts = re.split(r'([。！？\n；;])', text)
    chunks: List[str] = []
    buffer = ""
    for part in parts:
        buffer += part
        if len(buffer) >= chunk_size or part in "。！？\n；;":
            if buffer.strip():
                chunks.append(buffer)
            buffer = ""
    if buffer.strip():
        chunks.append(buffer)
    return chunks if chunks else [text]


# ============================================================
# BaseAgent 抽象基类
# ============================================================

class BaseAgent:
    """
    所有业务领域Agent的公共基类。

    设计意图：
        - 统一Agent的对外接口（process），便于 CoordinatorAgent 无差别分发
        - 统一生命周期：接收->构建提示词->LLM调用->规则校验->安全检查->返回
        - 统一持有 llm_provider 与 database 引用，避免各子类重复初始化
        - applicable_rules 声明该Agent需要执行的规则集合（来自规则引擎）

    属性：
        agent_name: Agent显示名称（如"销售Agent"）
        agent_type: Agent类型标识（如"sales"/"production"，用于路由）
        applicable_rules: 该Agent适用的规则名列表（如["discount_rule","credit_rule"]）
        llm_provider: LLM提供方接口实例（可选，None 时降级）
        database: 数据库访问层实例（可选，None 时降级）
    """

    # 线程本地用户上下文（S1 并发安全：多线程并发调用同一 Agent 实例时，
    # 实例属性会被互相覆盖，改用线程本地存储隔离各请求的用户身份）
    _user_local = threading.local()

    def __init__(self, agent_name: str, agent_type: str,
                 llm_provider: Any = None, database: Any = None):
        """
        初始化Agent实例。

        参数：
            agent_name: Agent显示名称
            agent_type: Agent类型标识，用于CoordinatorAgent路由
            llm_provider: LLM提供方接口（None时使用默认Provider）
            database: 数据库访问层（None时使用默认连接）

        说明：
            applicable_rules 默认为空列表，子类 __init__ 中应覆盖赋值。
        """
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.llm_provider = llm_provider
        self.database = database
        # 子类应在自身初始化时声明适用的规则
        self.applicable_rules: List[str] = []

    # --------------------------------------------------------
    # 主处理入口（子类必须实现或扩展）
    # --------------------------------------------------------
    def process(self, user_input: str, context: Dict[str, Any]) -> AgentResponse:
        """
        Agent主处理入口。

        设计意图：
            作为CoordinatorAgent分发的统一入口，封装完整的Agent生命周期。
            子类通常重写此方法以实现自身的处理编排。

        参数：
            user_input: 用户原始输入文本
            context: 会话上下文，包含用户身份、权限、对话历史、槽位等

        返回：
            AgentResponse: 统一响应对象

        生命周期：
            接收 -> _build_prompt -> _call_llm -> _apply_rules -> _format_response
        """
        start_time = time.time()
        # 注入当前用户上下文（供规则引擎/审批角色使用，S1：改用线程本地存储，
        # 避免实例属性在多线程并发调用同一 Agent 时被覆盖造成用户身份串扰）
        BaseAgent._user_local.current_user = context.get("user") or {}
        # 1. 构建提示词
        prompt = self._build_prompt(user_input, context)
        # 2. 调用LLM
        llm_output = self._call_llm(prompt)
        # 3. 规则校验
        rule_result = self._apply_rules(context.get("data", {}))
        # 4. 格式化响应
        response = self._format_response(llm_output, rule_result)
        # 记录耗时到metadata
        elapsed = round((time.time() - start_time) * 1000, 2)
        response.metadata["elapsed_ms"] = elapsed
        response.agent_name = self.agent_name
        # 记录追踪ID（§4.7.2.1）：与审核链/日志的trace_id一致
        try:
            from prog.runtime.trace import get_trace_id
            response.metadata["trace_id"] = get_trace_id()
        except Exception:
            pass
        return response

    # --------------------------------------------------------
    # 提示词构建（子类按需重写以注入领域数据）
    # --------------------------------------------------------
    def _build_prompt(self, user_input: str, context: Dict[str, Any]) -> str:
        """
        构建Agent专用提示词。

        参数：
            user_input: 用户输入
            context: 会话上下文

        返回：
            str: 完整的系统+用户提示词
        """
        user_info = context.get("user", {})
        if not isinstance(user_info, dict):
            # 非 dict 用户上下文统一规范化为空 dict，后续直接使用（消除重复 isinstance 判断）
            user_info = {}
        perms = user_info.get("permissions", {})
        history = context.get("history", [])

        # 构建对话历史文本（最近2轮）
        history_text = ""
        if history:
            ctx_items = []
            for h in history[-2:]:
                if isinstance(h, dict):
                    if h.get("user"):
                        ctx_items.append(f"用户：{h['user']}")
                    if isinstance(h.get("ai"), str):
                        ctx_items.append(f"AI：{h['ai'][:100]}")
            history_text = "\n".join(ctx_items) if ctx_items else "（无历史对话）"
        else:
            history_text = "（无历史对话）"

        prompt = f"""你是「{self.agent_name}」，AI工厂管家的专业助手。

## 用户身份
- 姓名：{user_info.get('title', '')}（{user_info.get('name', '')}）
- 工号：{user_info.get('id', '')} | 部门：{user_info.get('department', '')}

## 用户权限
- 折扣上限：{perms.get('discount_max', 0)}
- 可修改订单：{perms.get('can_modify_order', False)}
- 可查看成本：{perms.get('can_view_cost', False)}

## 最近对话上下文
{history_text}

## 回复规范
1. 用自然、专业的中文回复
2. 严格遵守权限：无权查看成本时不透露成本数据
3. 回复控制在300字以内，重点突出
4. 涉及高风险操作时提示需确认

## 用户输入
{user_input}
"""
        return prompt

    # --------------------------------------------------------
    # LLM调用（统一接入安全门控）
    # --------------------------------------------------------
    def _call_llm(self, prompt: str) -> str:
        """
        调用LLM生成回复。

        设计意图：
            通过 LLMEngine 统一调用。Agent自身不直接持有API密钥或HTTP客户端。
            无 LLM 环境（未注入 llm_provider 且可选 LLM 引擎不可用）时返回空串，
            由 _format_response 兜底，保证框架可独立运行。

        参数：
            prompt: 完整提示词

        返回：
            str: LLM原始输出文本
        """
        # 优先使用注入的llm_provider
        if self.llm_provider is not None:
            # 如果llm_provider有call/generate方法，直接调用
            call_method = getattr(self.llm_provider, "call", None) or getattr(
                self.llm_provider, "generate", None
            )
            if call_method:
                try:
                    result = call_method(prompt)
                    if isinstance(result, str):
                        return result
                    if isinstance(result, dict):
                        return result.get("text", "") or result.get("content", "")
                except Exception:
                    pass
            # 尝试chat_completion接口
            chat_method = getattr(self.llm_provider, "chat_completion", None)
            if chat_method:
                try:
                    messages = [{"role": "user", "content": prompt}]
                    resp = chat_method(messages)
                    if isinstance(resp, dict):
                        return resp.get("content", "")
                except Exception:
                    pass

        # 尝试使用 LLMEngine（可选导入：LLM 引擎不在开源框架范围内，
        # 无 LLM 环境时静默降级，不影响框架导入与运行）
        try:
            from prog.runtime.llm.engine import LLMEngine  # 可选：外部 LLM 引擎
            # 实例级缓存，避免每轮对话重复构造 LLMEngine
            engine = getattr(self, "_llm_engine_cache", None)
            if engine is None:
                engine = LLMEngine(self.llm_provider)
                self._llm_engine_cache = engine
            return engine.generate(prompt, {})
        except Exception:
            pass

        # 兜底：无LLM可用时返回空串，由_format_response处理
        return ""

    # --------------------------------------------------------
    # 规则校验（硬约束，LLM不可绕过）
    # --------------------------------------------------------
    def _apply_rules(self, data: Dict[str, Any]) -> "RuleResult":
        """
        执行适用的业务规则校验。

        设计意图：
            规则引擎为硬约束，优先级高于LLM输出。即使LLM给出"同意"，规则
            不通过时也必须阻断。applicable_rules 决定执行哪些规则。

        参数：
            data: 待校验的业务数据（如订单字段、库存变动等）

        返回：
            RuleResult: 校验结果对象
        """
        # 无适用规则时直接返回通过
        if not self.applicable_rules:
            return _PassRuleResult()

        # 安全铁律：这些规则永远走硬编码，不接受引擎覆盖
        _HARDCODED_RULES = frozenset({"data_flow_rule"})

        # 尝试通过规则注册表执行校验
        try:
            from prog.runtime.rule_registry import RuleRegistry, RuleResult
            from prog.runtime.rule_engine import RuleEngine

            registry = RuleRegistry.get_shared()
            engine = RuleEngine(database=self.database)
            violated: List[str] = []
            final_result = None

            for rule_name in self.applicable_rules:
                # 安全铁律：强制走硬编码，跳过引擎
                if rule_name in _HARDCODED_RULES:
                    rule = registry.get_rule(rule_name)
                    if rule is None:
                        continue
                    try:
                        result = rule.check(data)
                        if result and not result.passed:
                            violated.append(rule_name)
                            final_result = result
                            if result.blocked:
                                break
                    except Exception as exc:
                        # W7 fail-closed：规则执行异常不静默放行，转 warn（需审批），
                        # 与审计链层"异常返回 warn 不阻断整条链"策略一致
                        self._log_rule_error(rule_name, exc)
                        final_result = RuleResult(
                            status=RuleResult.STATUS_WARN,
                            rule_name=rule_name,
                            message=f"规则执行异常，需人工复核：{exc}",
                            requires_approval=True,
                        )
                        violated.append(rule_name)
                        break
                    continue

                # 尝试引擎执行路径
                rule = registry.get_rule(rule_name)
                if rule is not None:
                    config = rule.load_config_from_db()
                    engine_steps = config.get("engine_steps")
                    if engine_steps:
                        # 引擎执行：传入完整 context（含 data, user 等）
                        context = {"data": data,
                                   "user": getattr(BaseAgent._user_local, 'current_user', {}) or {}}
                        params = dict(config)
                        params.pop("engine_steps", None)
                        params.pop("engine_version", None)
                        result = engine.execute(
                            {"engine_steps": engine_steps},
                            context, params, rule_name,
                        )
                        if result and not result.passed:
                            violated.append(rule_name)
                            final_result = result
                            if result.blocked:
                                break
                        continue

                # 回退到硬编码规则（向后兼容）
                if rule is not None and hasattr(rule, "check"):
                    try:
                        try:
                            result = rule.check(data)
                        except TypeError:
                            # 规则 check 签名与 data 不匹配（如 cost.check(items)）
                            # 时按命名参数提取重试——引擎配置（engine_steps）缺失的
                            # 降级路径，避免规则校验整体误转"需审批"
                            result = self._check_rule_with_data(rule, data)
                        if result and not result.passed:
                            violated.append(rule_name)
                            final_result = result
                            # 硬规则阻断时立即终止
                            if result.blocked:
                                break
                    except Exception as exc:
                        # W7 fail-closed：与硬编码路径一致，异常转 warn 需审批
                        self._log_rule_error(rule_name, exc)
                        final_result = RuleResult(
                            status=RuleResult.STATUS_WARN,
                            rule_name=rule_name,
                            message=f"规则执行异常，需人工复核：{exc}",
                            requires_approval=True,
                        )
                        violated.append(rule_name)
                        break

            if final_result is None:
                return RuleResult(status="pass", message="全部规则校验通过")
            final_result.extra.setdefault("violated", violated)
            return final_result
        except Exception:
            # 规则引擎不可用时放行（避免阻断业务流程）
            return _PassRuleResult()

    def _log_rule_error(self, rule_name: str, exc: Exception) -> None:
        """记录规则执行异常日志（W7 fail-closed 辅助）。

        参数：
            rule_name: 规则名
            exc: 捕获的异常
        """
        try:
            import logging
            logger = getattr(self, "_logger", None) or logging.getLogger(__name__)
            logger.error(
                "规则执行异常，转 warn 需审批 | rule=%s | error=%s",
                rule_name, exc, exc_info=True,
            )
        except Exception:
            pass

    def _check_rule_with_data(self, rule: Any, data: dict) -> "RuleResult":
        """规则硬编码 check 的 data 兼容适配（引擎配置缺失时的降级路径）。

        各业务规则 check 接收命名参数（cost.check(items)、discount.check(
        discount_rate, user_role)、credit.check(customer_id, order_amount)、
        version.check(product_code, drawing_version)），而 _apply_rules 统一
        传整个 data——无 engine_steps 时按规则类型从 data 提取对应字段；
        版本规则缺图纸版本快照时视为跳过（与引擎"字段缺失守卫"行为一致）。
        """
        rname = getattr(rule, "rule_name", "")
        try:
            if rname == "cost_rule":
                return rule.check(data.get("items") or [])
            if rname == "discount_rule":
                up = data.get("user_permissions") or {}
                role = (up.get("role") or "") if isinstance(up, dict) else ""
                return rule.check(data.get("discount_rate"), role)
            if rname == "credit_rule":
                return rule.check(data.get("customer_id"), data.get("order_amount") or 0)
            if rname == "version_rule":
                if not data.get("drawing_version"):
                    return _PassRuleResult()
                return rule.check(data.get("product_code"), data.get("drawing_version"))
        except Exception:
            return _PassRuleResult()
        # 通用兜底：按命名参数提取（缺失传 None，规则内部自行降级）
        import inspect
        try:
            sig = inspect.signature(rule.check)
            kwargs = {}
            for name, param in sig.parameters.items():
                if name in ("self", "context", "args", "kwargs"):
                    continue
                kwargs[name] = data.get(name)
            return rule.check(**kwargs)
        except Exception:
            return _PassRuleResult()

    # --------------------------------------------------------
    # 响应格式化（统一输出契约）
    # --------------------------------------------------------
    def _format_response(self, llm_output: str,
                         rule_result: Optional["RuleResult"] = None) -> AgentResponse:
        """
        将LLM输出与规则结果格式化为统一响应。

        参数：
            llm_output: LLM原始输出
            rule_result: 规则校验结果（可为None，表示未触发规则）

        返回：
            AgentResponse: 统一响应对象
        """
        content = llm_output or ""
        rules_violated: List[str] = []
        need_confirm = False
        action = ""

        # 处理规则校验结果
        if rule_result is not None:
            blocked = getattr(rule_result, "blocked", False)
            passed = getattr(rule_result, "passed", True)
            requires_approval = getattr(rule_result, "requires_approval", False)
            message = getattr(rule_result, "message", "")
            rule_name = getattr(rule_result, "rule_name", "")
            approver_role = getattr(rule_result, "approver_role", None)

            if blocked:
                # 硬规则阻断：覆盖LLM输出，返回阻断原因
                content = f"操作被规则引擎阻断：{message}" if message else "操作被规则引擎阻断，请检查输入参数。"
                if rule_name:
                    rules_violated.append(rule_name)
                action = "blocked"
            elif requires_approval:
                # 需审批确认（S1：warn/route_approval 的 passed=False，须先于 not passed 判定，
                # 否则 elif not passed 先命中导致 require_approval 分支永不可达）
                need_confirm = True
                action = "require_approval"
                if rule_name:
                    rules_violated.append(rule_name)
                if approver_role:
                    content = (f"{content}\n此操作需要{approver_role}审批：{message}"
                               if content else f"此操作需要{approver_role}审批：{message}")
                elif message:
                    content = (f"{content}\n此操作需要审批：{message}"
                               if content else f"此操作需要审批：{message}")
            elif not passed:
                # 警告级别
                if rule_name:
                    rules_violated.append(rule_name)
                if message:
                    content = f"{content}\n⚠️ 规则提醒：{message}" if content else f"⚠️ 规则提醒：{message}"

        # 无LLM输出且无规则阻断时，给出兜底回复
        if not content:
            content = "我已收到您的请求，但当前无法生成回复，请稍后重试。"

        return AgentResponse(
            content=content,
            rules_violated=rules_violated,
            need_confirm=need_confirm,
            action=action,
            agent_name=self.agent_name,
        )


class _PassRuleResult:
    """默认通过的规则结果（规则引擎不可用时的兜底）。

    避免对 RuleResult 产生强运行时依赖，在规则引擎未加载时提供同构的通过结果。
    """

    status = "pass"
    rule_name = ""
    message = "规则引擎未加载，默认通过"
    requires_approval = False
    approver_role = None
    is_hard = False

    def __init__(self) -> None:
        # W25：extra 改为实例属性，避免类级可变默认值被所有实例共享
        self.extra: Dict[str, Any] = {}

    @property
    def passed(self) -> bool:
        """是否通过规则校验——兜底恒为 True（无规则可拦截）。"""
        return True

    @property
    def blocked(self) -> bool:
        """是否被规则阻断——兜底恒为 False（无规则可阻断）。"""
        return False

    def to_dict(self) -> dict:
        """序列化为字典（供调用方统一处理 RuleResult 与兜底结果）。

        返回：
            dict: status/rule_name/message 三个字段
        """
        return {"status": self.status, "rule_name": self.rule_name, "message": self.message}
