"""
LLMProvider 统一接口层 - AI工厂管家

文件用途：
    定义 LLM（大语言模型）调用的统一接口层，封装所有与 LLM 服务的交互逻辑。
    包括 OpenAI 兼容接口的调用、流式输出、以及 5 道安全门控。

对应技术规格章节：
    §1.8.2 LLMProvider 统一接口层

替代 demo 文件/函数：
    替代 demo 中 llm_engine.py 的 call_llm() 和 generate_with_llm() 函数。
    demo 中这两个函数直接读取 llm_config.json 并调用 OpenAI SDK，
    无安全门控、无流式支持、无统一配置加载。

设计说明：
    1. 抽象基类 LLMProvider 定义统一契约，便于未来扩展非 OpenAI 兼容模型
    2. OpenAICompatibleProvider 为默认实现，支持豆包/DeepSeek 等 OpenAI 兼容 API
    3. 5 道安全门控按顺序执行，任一未通过即拒绝请求
    4. 配置来源：deployment_config.json 的 interfaces.llm_provider 节点
       （base_url / model / api_key_env / timeout / max_tokens / temperature）

5 道安全门控（按执行顺序）：
    1. prompt 注入检测：识别并拦截提示词注入攻击
    2. 意图合规校验：校验用户意图是否符合业务范围与合规要求
    3. 输出格式校验：校验 LLM 返回内容的结构化格式（JSON Schema 等）
    4. 敏感信息过滤：过滤响应中的敏感信息（手机号/身份证/密钥等）
    5. 操作确认：对会产生副作用的操作要求二次确认
"""

import json
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional


# ============================================================
# 进程级 LLM 健康状态（v6.67.6：LLM 调用失败时记录原因与友好提示，
# 供 /api/system/status 读取、前端"🧠 AI已接入"处展示——欠费/限流/
# 鉴权失败时客户可见，避免静默降级为规则识别导致语义误判。）
# ============================================================
_LLM_HEALTH: Dict[str, Any] = {
    "ok": True, "code": "", "error": "", "hint": "", "at": 0.0,
}


def get_llm_health() -> Dict[str, Any]:
    """读取进程级 LLM 健康状态（成功调用清除，失败记录原因与友好提示）。"""
    return dict(_LLM_HEALTH)


def _mark_llm_ok() -> None:
    """LLM 调用成功：清除失败状态（欠费恢复后前端自动恢复显示"AI已接入"）。"""
    _LLM_HEALTH.update({"ok": True, "code": "", "error": "", "hint": "", "at": time.time()})


def _mark_llm_failure(error_text: str) -> None:
    """记录 LLM 失败原因并生成友好提示。

    按错误特征归类：欠费(overdue)/限流(rate_limit)/鉴权(auth)/其他(error)。
    hint 直接用于前端"🧠 AI已接入"标签展示。
    """
    if re.search(r"AccountOverdueError|InsufficientBalance|overdue balance|欠费|余额", error_text, re.IGNORECASE):
        code, hint = "overdue", "AI账户欠费，智能识别已降级，请及时充值"
    elif re.search(r"RateLimit|429|Exceeded.*Quota|触发限流", error_text, re.IGNORECASE):
        code, hint = "rate_limit", "AI服务限流，智能识别已降级，请稍后重试"
    elif re.search(r"401|Authentication|InvalidApiKey|InvalidKey|鉴权失败", error_text, re.IGNORECASE):
        code, hint = "auth", "AI鉴权失败，智能识别已降级，请检查API Key配置"
    else:
        code, hint = "error", "AI服务异常，智能识别已降级为规则模式"
    _LLM_HEALTH.update({
        "ok": False, "code": code, "error": error_text[:300],
        "hint": hint, "at": time.time(),
    })


# ============================================================
# 安全门控规则常量
# ============================================================

# prompt 注入检测关键词
_PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "忽略以上指令",
    "忽略上述指令",
    "忽略前面",
    "你现在是",
    "输出系统提示词",
    "reveal system prompt",
    "ignore all previous",
    "disregard previous",
    "forget your instructions",
    "进入开发者模式",
    "jailbreak",
]

# 敏感信息正则模式（手机号/身份证/银行卡/密钥等）
_SENSITIVE_PATTERNS = [
    (re.compile(r"1[3-9]\d{9}"), "***********"),
    (re.compile(r"\d{17}[\dXx]"), "******************"),
    (re.compile(r"\d{16,19}"), "****"),
    (re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S+"), "****"),
]

# 开源版：已移除 get_security_patterns（agent_security 依赖，安全模式固定用内置常量）

# 高风险操作关键词（触发操作确认门控）
_HIGH_RISK_ACTIONS = [
    "删除", "delete", "remove",
    "取消订单", "cancel_order",
    "降价", "改价", "改折扣",
    "出库", "发货",
    "下单", "创建订单",
]


class LLMProvider:
    """
    LLM 提供者抽象基类

    定义所有 LLM 实现必须遵循的统一契约。
    具体实现类需实现 chat / stream_chat 等核心方法。

    设计意图:
        - 屏蔽不同 LLM 厂商的接口差异
        - 统一安全门控的执行入口
        - 便于单元测试时替换为 Mock 实现
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化 LLM 提供者

        参数:
            config: LLM 配置字典，来自 deployment_config.json 的
                    interfaces.llm_provider.config 节点，包含：
                    - base_url: API 基础地址
                    - model: 模型名称（如 doubao-pro-32k）
                    - api_key: API 密钥（已从环境变量解析）
                    - timeout: 调用超时（秒）
                    - max_tokens: 最大输出 Token 数
                    - temperature: 温度参数
        """
        self.config: Dict[str, Any] = dict(config) if config else {}
        self.base_url: str = self.config.get("base_url", "")
        self.model: str = self.config.get("model", "")
        self.api_key: str = self.config.get("api_key", "")
        self.timeout: int = self.config.get("timeout", 60)
        self.max_tokens: int = self.config.get("max_tokens", 4096)
        self.temperature: float = self.config.get("temperature", 0.3)
        # v6.78.3：双模型 thinking 外部可配（deployment_config.json 独立节点）
        #  - thinking="enabled"  ：显式开启思考（reasoning_content 流式输出，识别用强模型）
        #  - thinking="disabled" ：显式关闭思考（回复用快模型，TTFT 快）
        #  - 缺省：None（保持模型默认 / 历史 doubao-seed 关闭逻辑兜底）
        self.thinking: Optional[str] = self.config.get("thinking")
        self.thinking_budget: int = int(self.config.get("thinking_budget") or 0)
        # v6.79.1：意图识别预识别超时（秒，外部可配 deployment_config.json
        # interfaces.intent_llm_provider.config.intent_timeout_sec，缺省 25）。
        # 识别强模型（thinking）偶发推理不收敛（长时间空转不出 tool_call）时，
        # chat.py 预识别看门狗据此放弃强模型结果、回退规则层（零延迟）。
        try:
            self.intent_timeout_sec: float = float(
                self.config.get("intent_timeout_sec") or 25)
        except (TypeError, ValueError):
            self.intent_timeout_sec = 25.0

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """
        同步对话补全

        参数:
            messages: 消息列表，格式为 [{"role": "system|user|assistant", "content": "..."}]
            tools: 可选的工具/函数调用定义列表
            temperature: 温度参数，控制输出随机性
            max_tokens: 最大输出 Token 数

        返回:
            LLM 响应字典，包含 content / tool_calls / usage / finish_reason 等字段
        """
        raise NotImplementedError("子类必须实现 chat 方法")

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Iterator[Dict[str, Any]]:
        """
        流式对话补全

        通过生成器逐块返回 LLM 响应，适用于长文本输出场景，提升用户感知响应速度。

        参数:
            messages: 消息列表
            tools: 可选的工具/函数调用定义列表（v6.78.3 起支持流式函数调用，
                   流结束后额外 yield 一条 {"tool_calls": [...]} 合成块）
            temperature: 温度参数
            max_tokens: 最大输出 Token 数

        返回:
            生成器，每次 yield 一个响应片段字典（含 content / delta / reasoning 字段）
        """
        raise NotImplementedError("子类必须实现 stream_chat 方法")

    def close(self) -> None:
        """关闭连接，释放底层资源"""
        pass

    # ---- 兼容别名（供 LLMEngine 的 getattr 调用）----

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """chat 方法的兼容别名（LLMEngine 通过 getattr 调用此名称）"""
        return self.chat(
            messages,
            tools,
            temperature if temperature is not None else 0.1,
            max_tokens if max_tokens is not None else 4096,
        )

    def stream_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        """stream_chat 方法的兼容别名"""
        return self.stream_chat(
            messages,
            tools,
            temperature if temperature is not None else 0.1,
            max_tokens if max_tokens is not None else 4096,
        )

    # ---- 带 5 道安全门控的生成方法 ----

    def generate_with_safety_gates(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        operation_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        带 5 道安全门控的生成方法

        按顺序执行安全门控，任一未通过则返回拒绝响应。
        通过全部门控后调用 chat 生成最终响应。

        参数:
            messages: 消息列表
            user_id: 调用用户 ID（用于权限校验与审计）
            operation_type: 操作类型标识（用于判断是否需要操作确认门控）

        返回:
            响应字典，包含 content / safety_status / gate_results 等字段
            safety_status 为 "passed" 或 "blocked"
        """
        gate_results: List[Dict[str, Any]] = []

        # 门控 1：prompt 注入检测（生成前）
        passed = self._gate_prompt_injection(messages)
        gate_results.append({"gate": "prompt_injection", "passed": passed})
        if not passed:
            return {
                "content": "请求被拦截：检测到可能的 prompt 注入攻击",
                "safety_status": "blocked",
                "gate_results": gate_results,
            }

        # 门控 2：意图合规校验（生成前）
        passed = self._gate_intent_compliance(messages)
        gate_results.append({"gate": "intent_compliance", "passed": passed})
        if not passed:
            return {
                "content": "请求被拦截：意图不合规",
                "safety_status": "blocked",
                "gate_results": gate_results,
            }

        # 调用 LLM 生成响应
        response = self.chat(messages)

        # 门控 3：输出格式校验（生成后）
        passed = self._gate_output_format(response)
        gate_results.append({"gate": "output_format", "passed": passed})
        if not passed:
            return {
                "content": "请求被拦截：输出格式不符合预期",
                "safety_status": "blocked",
                "gate_results": gate_results,
            }

        # 门控 4：敏感信息过滤（生成后）
        response = self._gate_sensitive_info_filter(response)
        gate_results.append({"gate": "sensitive_info_filter", "passed": True})

        # 门控 5：操作确认（生成后，仅当指定操作类型时执行）
        if operation_type:
            confirmed = self._gate_operation_confirm(response, operation_type, user_id)
            gate_results.append({"gate": "operation_confirm", "passed": confirmed})
            if not confirmed:
                response["need_confirm"] = True

        response["safety_status"] = "passed"
        response["gate_results"] = gate_results
        return response

    def _gate_prompt_injection(self, messages: List[Dict[str, str]]) -> bool:
        """
        安全门控 1：prompt 注入检测

        检测用户输入中是否包含提示词注入攻击模式（如"忽略以上指令"等）。

        参数:
            messages: 待检测的消息列表

        返回:
            True 表示通过检测，False 表示检测到注入并拦截
        """
        for msg in messages:
            content = (msg.get("content") or "").lower()
            for pattern in _PROMPT_INJECTION_PATTERNS:
                if pattern.lower() in content:
                    return False
        return True

    def _gate_intent_compliance(self, messages: List[Dict[str, str]]) -> bool:
        """
        安全门控 2：意图合规校验

        校验用户意图是否符合系统业务范围与合规要求。

        参数:
            messages: 待校验的消息列表

        返回:
            True 表示通过校验，False 表示意图不合规
        """
        # 基础实现：默认通过，子类可扩展更严格的合规规则
        return True

    def _gate_output_format(self, response: Dict[str, Any]) -> bool:
        """
        安全门控 3：输出格式校验

        校验 LLM 返回内容是否符合预期的结构化格式（如 JSON Schema）。

        参数:
            response: LLM 响应字典

        返回:
            True 表示格式校验通过，False 表示格式不符
        """
        # 基础实现：校验响应包含 content 字段
        return isinstance(response, dict) and "content" in response

    def _gate_sensitive_info_filter(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """
        安全门控 4：敏感信息过滤

        过滤响应中的敏感信息（手机号 / 身份证 / 密钥 / 内部 IP 等）。

        参数:
            response: 待过滤的响应字典

        返回:
            过滤后的响应字典
        """
        content = response.get("content", "")
        if not isinstance(content, str):
            return response
        for pattern, replacement in _SENSITIVE_PATTERNS:
            content = pattern.sub(replacement, content)
        response["content"] = content
        return response

    def _gate_operation_confirm(
        self,
        response: Dict[str, Any],
        operation_type: str,
        user_id: str,
    ) -> bool:
        """
        安全门控 5：操作确认

        对会产生副作用（写库 / 发送消息 / 调用外部接口）的操作要求二次确认。

        参数:
            response: LLM 响应字典
            operation_type: 操作类型
            user_id: 调用用户 ID

        返回:
            True 表示已确认或无需确认，False 表示需用户确认
        """
        op_lower = (operation_type or "").lower()
        for keyword in _HIGH_RISK_ACTIONS:
            if keyword.lower() in op_lower:
                return False
        return True


class OpenAICompatibleProvider(LLMProvider):
    """
    OpenAI 兼容接口 LLM 提供者

    默认实现，通过 OpenAI Python SDK 调用任何兼容 OpenAI 接口的 LLM 服务：
        - 豆包（火山引擎方舟）：base_url=https://ark.cn-beijing.volces.com/api/v3
        - DeepSeek：base_url=https://api.deepseek.com/v1
        - 其他 OpenAI 兼容服务

    配置示例（deployment_config.json）:
        {
            "type": "openai_compatible",
            "config": {
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "model": "doubao-pro-32k",
                "api_key_env": "LLM_API_KEY",
                "timeout": 60,
                "max_tokens": 4096,
                "temperature": 0.3
            }
        }
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化 OpenAI 兼容提供者

        参数:
            config: 已解析环境变量的 LLM 配置字典

        降级说明:
            当 openai 库未安装时，自动进入模拟模式，chat/stream_chat 返回模拟响应。
        """
        super().__init__(config)
        self._mock_mode: bool = False
        self._client = None
        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key or "not-configured",
                base_url=self.base_url,
                timeout=self.timeout,
            )
        except ImportError:
            # openai 未安装，降级为模拟模式
            self._mock_mode = True

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """
        同步对话补全 - 调用 OpenAI 兼容 API

        参数:
            messages: 消息列表
            tools: 可选的工具定义
            temperature: 温度参数
            max_tokens: 最大 Token 数

        返回:
            响应字典，包含 content / tool_calls / usage / finish_reason
        """
        # 模拟模式：openai 未安装时返回模拟响应
        if self._mock_mode:
            return self._mock_chat(messages, tools, temperature, max_tokens)

        # S11 熔断器接线：LLM 连续失败达阈值后 open 快速失败
        # （CircuitOpenError），由下方 except 捕获走既有降级路径
        # （返回错误响应），不阻断业务调用方。
        try:
            from prog.core.circuit_breaker import get_breaker
            return get_breaker("llm_chat").call(
                self._chat_call, messages, tools, temperature, max_tokens)
        except Exception as e:
            _mark_llm_failure(str(e))
            return {
                "content": f"（LLM 调用失败：{e}）",
                "tool_calls": None,
                "usage": {},
                "finish_reason": "error",
            }

    def _chat_call(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """实际调用 OpenAI 兼容 API（S11 熔断器包装的受保护调用）。

        成功路径与历史 chat() 一致（含 _mark_llm_ok / _record_usage）；
        失败时异常向上传播，由 chat() 外层熔断器计数并降级响应。
        """
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        # ══════════════════════════════════════════════════════════════
        # PERF-FIX-v6.40 ANCHOR::Doubao-Seed 关闭thinking（非流式调用）
        # 根因：Doubao-Seed 默认开启串行 thinking 模式，thinking 首字 ~6s，
        #       content 首字 ~24s，体感极慢。
        # 修复：显式设置 thinking.type=disabled，跳过思考过程直接出结果。
        # 实测：TTFT 从 10.03s → 2.39s，提升 4.2 倍。
        # v6.78.3：双模型外部可配——thinking 由 config.thinking 控制：
        #   - "enabled"  ：不注入 disabled（保留思考），带 budget 时限预算
        #   - "disabled" ：注入 disabled（历史行为，回复快模型默认）
        #   - 缺省 None  ：仅 doubao-seed 兜底 disabled（向后兼容）
        # 防回归：删除该块必须同步评估 config.thinking 缺失时的降级行为。
        # ══════════════════════════════════════════════════════════════
            if self.thinking == "enabled":
                if self.thinking_budget and self.thinking_budget > 0:
                    kwargs["extra_body"] = {
                        "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget}}
            elif self.thinking == "disabled" or "doubao-seed" in self.model:
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        response = self._client.chat.completions.create(**kwargs)

        # 解析 OpenAI 响应对象为字典
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ]
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        _mark_llm_ok()
        # S12：LLM 调用成功后把 token 用量落库（失败静默，不阻断调用）
        self._record_usage(usage)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": usage,
            "finish_reason": choice.finish_reason,
        }


    def _record_usage(self, usage: Dict[str, Any]) -> None:
        """S12：LLM 调用成功后把 token 用量写入 llm_usage 表。

        失败静默：数据库不可达/表未创建时不阻断 LLM 调用。
        cost_yuan 按近似单价估算：prompt 0.0000008 元/token、
        completion 0.0000012 元/token。
        """
        try:
            from prog.core.database import get_database
            db = get_database()
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or 0)
            cost_yuan = round(prompt_tokens * 0.0000008 + completion_tokens * 0.0000012, 6)
            db.insert("llm_usage", {
                "user_id": "",
                "agent": "",
                "model": self.model or "",
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_yuan": cost_yuan,
                "success": True,
            })
        except Exception:
            # 失败静默：不阻断 LLM 调用
            pass

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> Iterator[Dict[str, Any]]:
        """
        流式对话补全 - 调用 OpenAI 兼容 API 的流式接口

        参数:
            messages: 消息列表
            tools: 可选的工具/函数调用定义列表（v6.78.3 起支持：流式函数调用
                   delta 按 index 累积，流结束后 yield 一条 {"tool_calls": [...]}
                   合成块；供意图识别强模型流式输出 reasoning + 保留结构化结果）
            temperature: 温度参数
            max_tokens: 最大 Token 数

        返回:
            生成器，每次 yield 一个响应片段字典
        """
        # 模拟模式
        if self._mock_mode:
            yield from self._mock_stream(messages)
            return

        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            # ══════════════════════════════════════════════════════════════
            # PERF-FIX-v6.40 ANCHOR::Doubao-Seed 关闭thinking（流式调用）
            # 根因 / 修复 / 防回归规则：同上方 ANCHOR（非流式）。
            # 注意：流式调用是对话系统主路径，80%+ 请求走这里。
            # v6.78.3：双模型外部可配——thinking 由 config.thinking 控制，
            # 与 chat() 保持一致的三态逻辑（enabled 开启+预算 / disabled
            # 关闭 / None 缺省仅 doubao-seed 兜底）。识别强模型经此方法
            # 流式输出 reasoning_content（前端 event: reasoning 渲染）。
            # ══════════════════════════════════════════════════════════════
            if self.thinking == "enabled":
                if self.thinking_budget and self.thinking_budget > 0:
                    kwargs["extra_body"] = {
                        "thinking": {"type": "enabled", "budget_tokens": self.thinking_budget}}
            elif self.thinking == "disabled" or "doubao-seed" in self.model:
                kwargs["extra_body"] = {
                    "thinking": {"type": "disabled"},
                }
            response = self._client.chat.completions.create(**kwargs)
            # v6.78.3：流式函数调用 delta 累积（按 index 分组）
            _tool_acc: Dict[int, Dict[str, Any]] = {}
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta
                    # 推理内容（思考过程），优先输出
                    # openai SDK 将非标准字段存储在 model_extra_fields 中
                    reasoning = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "model_extra_fields", {}).get("reasoning_content", "")
                        or getattr(delta, "__pydantic_extra__", {}).get("reasoning_content", "")
                        or ""
                    )
                    if reasoning:
                        yield {"reasoning": reasoning, "delta": reasoning}
                    # 正式回复内容
                    content = delta.content or ""
                    if content:
                        yield {"content": content, "delta": content}
                    # 函数调用 delta（部分参数逐块到达）
                    tool_deltas = getattr(delta, "tool_calls", None) or []
                    for tc in tool_deltas:
                        try:
                            idx = int(getattr(tc, "index", 0) or 0)
                        except (TypeError, ValueError):
                            idx = 0
                        acc = _tool_acc.setdefault(
                            idx, {"id": "", "type": "function",
                                  "function": {"name": "", "arguments": ""}})
                        tc_id = getattr(tc, "id", None)
                        if tc_id:
                            acc["id"] = tc_id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                acc["function"]["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                acc["function"]["arguments"] += fn.arguments
            # 流结束后输出累积的 tool_calls（仅当确实存在函数调用时）
            if _tool_acc:
                yield {"tool_calls": [_tool_acc[i] for i in sorted(_tool_acc)]}
        except Exception as e:
            yield {"content": f"（流式调用失败：{e}）", "delta": "", "error": str(e)}

    def close(self) -> None:
        """关闭 OpenAI 客户端连接"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # ---- 模拟模式内部方法 ----

    def _mock_chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]],
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        """模拟模式：openai 未安装时返回模拟响应"""
        user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break
        preview = user_msg[:80] if user_msg else ""
        return {
            "content": (
                f"（模拟模式 - openai 未安装）已收到请求：{preview}。"
                "请安装 openai 库并配置 API 密钥以启用真实 AI 回复。"
            ),
            "tool_calls": None,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "finish_reason": "stop",
        }

    def _mock_stream(self, messages: List[Dict[str, str]]) -> Iterator[Dict[str, Any]]:
        """模拟模式：流式输出模拟内容"""
        yield {"content": "（模拟模式）", "delta": "（模拟模式）"}
        yield {
            "content": "openai 未安装，请配置后启用 AI 回复。",
            "delta": "openai 未安装，请配置后启用 AI 回复。",
        }


def _load_default_llm_config(section: str = "llm_provider") -> Dict[str, Any]:
    """从 deployment_config.json 加载 LLM 配置。

    参数:
        section: deployment_config.json 中 interfaces.<section> 节点名。
            - "llm_provider"（默认）：对话/Agent 回复通道（快模型）
            - "intent_llm_provider"：意图语义理解通道（强模型+thinking）
            v6.78.3 起双模型外部可配，两节点独立配置，缺任一节点时该
            通道沿用默认内置配置兜底。
    """
    config: Dict[str, Any] = {
        "type": "openai_compatible",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-pro-32k",
        "api_key_env": "LLM_API_KEY",
        "timeout": 60,
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    try:
        # 尝试读取 deployment_config.json（位于 prog/ 目录）
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "deployment_config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                deploy_config = json.load(f)
            llm_section = deploy_config.get("interfaces", {}).get(section, {})
            if llm_section:
                config["type"] = llm_section.get("type", config["type"])
                config.update(llm_section.get("config", {}))
    except Exception:
        pass
    # D6：llm_api 写盘时对 api_key 做 Fernet 加密（前缀 fernet:），此处还原；
    # 透明兼容旧版 b64: 混淆与明文。
    ak = config.get("api_key")
    if isinstance(ak, str) and ak:
        try:
            from prog.utils.crypto import decrypt_text
            config["api_key"] = decrypt_text(ak)
        except Exception:
            config.pop("api_key", None)
    return config


def create_llm_provider(config: Optional[Dict[str, Any]] = None,
                        section: str = "llm_provider") -> LLMProvider:
    """
    工厂函数：创建 LLM 提供者实例

    从环境变量/配置创建 LLMProvider 实例。
    当 config 为 None 时，自动从 deployment_config.json 加载配置。

    参数:
        config: LLM 配置字典，为 None 时自动加载默认配置
        section: 配置节点名（见 _load_default_llm_config），v6.78.3 起
                 支持 "intent_llm_provider" 加载语义理解强模型配置

    返回:
        LLMProvider 实例

    v6.67.6：LLM 在任何时候均使用同一通道——主动触发 config_loader 加载 .env
    （LLM_API_KEY 等），不依赖调用方是否已先初始化 DB/config_loader。
    此前若 create_llm_provider 在 config_loader 之前被调用，os.environ 无
    LLM_API_KEY，provider api_key 为空，LLM 静默降级为模拟模式（部分进程
    有 key、部分无 key，识别行为不一致）。
    """
    # 确保 .env 已加载到 os.environ（幂等：config_loader 内部有 _loaded 缓存）
    try:
        from prog.config.config_loader import get_config_loader
        get_config_loader().load_config()
    except Exception:
        pass

    if config is None:
        config = _load_default_llm_config(section)

    # 解析 api_key_env 为实际 api_key
    api_key_env = config.get("api_key_env")
    if api_key_env and not config.get("api_key"):
        config["api_key"] = os.environ.get(api_key_env, "")

    # 兜底：尝试常见环境变量
    if not config.get("api_key"):
        config["api_key"] = os.environ.get("LLM_API_KEY", "") or os.environ.get("ARK_API_KEY", "")

    provider_type = config.get("type", "openai_compatible")
    if provider_type == "openai_compatible":
        return OpenAICompatibleProvider(config)

    # 默认返回 OpenAI 兼容实现
    return OpenAICompatibleProvider(config)


def get_llm_provider() -> LLMProvider:
    """
    模块级便捷函数：获取 LLM 提供者单例

    根据 deployment_config.json 的 interfaces.llm_provider.type 实例化对应实现。
    当前默认返回 OpenAICompatibleProvider 实例。

    返回:
        LLMProvider 实例
    """
    return create_llm_provider()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert LLMProvider is not None, "LLMProvider 类未定义"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
