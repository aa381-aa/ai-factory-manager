"""
LLMEngine LLM生成引擎模块
=========================

文件用途：
    实现统一的LLM生成引擎，封装同步/流式生成接口，并强制执行5道安全门控。

技术规格章节：
    - §2 LLM安全门控（5道门控，硬性安全管控）
    - §1.1.3 Coordinator Agent（Agent通过本引擎调用LLM）

替代demo：
    替代 demo/llm_engine.py 的：
    - generate_with_llm()  （同步生成 + 安全门控）
    - generate_stream_with_llm()  （流式生成）
    - call_llm()  （底层API调用）

5道安全门控（顺序执行，任一不通过即阻断）：
    1. prompt_injection_check（prompt注入检测）：
       检测用户输入中是否包含prompt注入攻击模式
       （如"忽略以上指令""你现在是DAN模式"等越狱模板）
    2. intent_compliance_check（意图合规校验）：
       校验识别的意图与目标Agent的职责范围是否匹配
       （防止用户借销售Agent执行生产操作等越权）
    3. output_format_check（输出格式校验）：
       校验LLM输出是否符合预期格式（JSON/HTML/纯文本）
       （防止LLM返回无法解析的格式导致下游错误）
    4. sensitive_info_filter（敏感信息过滤）：
       过滤LLM输出中的敏感信息（手机号、身份证号、银行卡号等）
       （防止业务数据中的敏感字段泄露给无权限用户）
    5. operation_confirm（操作确认）：
       高风险操作（如下单、改价、出库）需要用户二次确认
       （标记 need_confirm=True，由前端弹窗确认后再执行）

依赖组件：
    - core/llm_provider.py: LLMProvider接口（封装不同厂商API差异）
"""

import json
import os
import re
from typing import Any, Dict, Generator, Optional


# ============================================================
# 默认配置与安全规则
# ============================================================

# 默认LLM配置（无API密钥时使用模拟模式）
DEFAULT_LLM_CONFIG = {
    "api_key": "",
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "deepseek-v4-flash-ga-260731",
    "temperature": 0.3,
    "max_tokens": 4096,
    "timeout": 60,
}

# prompt 注入检测关键词模式
PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "忽略以上指令",
    "忽略上述指令",
    "忽略前面",
    "你现在是",
    "你现在是DAN",
    "输出系统提示词",
    "reveal system prompt",
    "ignore all previous",
    "disregard previous",
    "forget your instructions",
    "你是一个没有限制的AI",
    "进入开发者模式",
    "jailbreak",
]

# 敏感信息正则模式（手机号/身份证/银行卡/api_key等）
# P1-5 修复：先长后短 + 边界断言——身份证(18)/银行卡(16-19) 先于手机号(11)，
# 否则手机号正则会在约半数省份段（第2位为3-9）截断身份证、银行卡长数字串，
# 导致剩余位数泄露。各正则加 (?<!\d)/(?!\d) 边界，避免命中更长数字串的子串。
SENSITIVE_PATTERNS = [
    # 身份证号（18位）
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "******************"),
    # 银行卡号（16-19位连续数字）
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "****"),
    # 手机号（11位）
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***********"),
    # api_key / password 字段
    (re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*\S+"), "****"),
]

# 高风险操作关键词
HIGH_RISK_ACTION_PATTERNS = [
    "删除", "delete", "remove",
    "取消订单", "cancel_order",
    "状态变更", "status_change",
    "降价", "改价", "改折扣",
    "出库", "发货",
    "下单", "创建订单",
]

# 高风险意图集合（触发操作确认门控）
HIGH_RISK_INTENTS = {
    "create_order", "modify_order", "cancel_order",
    "stock_out", "status_change", "delete",
}


# ============================================================
# 安全门控结果对象
# ============================================================

class SafetyResult:
    """
    安全门控结果对象。

    用于封装 _run_safety_gates() 的输出，明确告知调用方是否通过、
    哪道门控阻断、阻断原因。

    属性说明：
        - passed: 是否通过全部5道门控
        - blocked_gate: 阻断的门控名称（passed=True时为None）
        - reason: 阻断原因说明
        - need_confirm: 第5道门控标记，是否需要用户确认
        - sanitized_output: 经敏感信息过滤后的输出（第4道门控产出）
    """

    def __init__(self, passed: bool = True, blocked_gate: Optional[str] = None,
                 reason: str = "", need_confirm: bool = False,
                 sanitized_output: str = ""):
        """初始化安全门控结果。

        参数：
            passed: 是否通过全部门控
            blocked_gate: 阻断的门控名称
            reason: 阻断原因
            need_confirm: 是否需要用户确认
            sanitized_output: 过滤后的安全输出
        """
        self.passed = bool(passed)
        self.blocked_gate = blocked_gate
        self.reason = reason
        self.need_confirm = bool(need_confirm)
        self.sanitized_output = sanitized_output

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "passed": self.passed,
            "blocked_gate": self.blocked_gate,
            "reason": self.reason,
            "need_confirm": self.need_confirm,
        }


# ============================================================
# LLMEngine 生成引擎
# ============================================================

class LLMEngine:
    """
    LLM生成引擎。

    设计意图：
        作为所有Agent调用LLM的统一入口，强制执行5道安全门控，
        确保任何Agent都无法绕过安全管控。同时屏蔽不同LLM厂商的
        API差异（通过 LLMProvider 接口）。

    替代demo：
        替代 demo/llm_engine.py 的 generate_with_llm() /
        generate_stream_with_llm() / call_llm()。

    属性：
        llm_provider: LLM提供方接口实例（封装API调用）
        safety_gates: 已注册的安全门控处理器列表（按顺序执行）

    安全门控执行时机：
        - 门控1（注入检测）：在生成前，对用户输入执行
        - 门控2（意图合规）：在生成前，结合意图与Agent类型执行
        - 门控3（格式校验）：在生成后，对LLM输出执行
        - 门控4（敏感过滤）：在生成后，对LLM输出执行
        - 门控5（操作确认）：在生成后，结合意图风险等级执行
    """

    def __init__(self, llm_provider: Any = None):
        """
        初始化LLM引擎。

        参数：
            llm_provider: LLM提供方接口实例（None时使用默认Provider）

        说明：
            safety_gates 按门控1~5顺序初始化，执行时严格依序。
        """
        self.llm_provider = llm_provider
        # 加载LLM配置
        self.config = self._load_config()
        # 初始化5道安全门控处理器（按顺序）
        self.safety_gates = [
            ("prompt_injection_check", self._gate_prompt_injection),
            ("intent_compliance_check", self._gate_intent_compliance),
            ("output_format_check", self._gate_output_format),
            ("sensitive_info_filter", self._gate_sensitive_info),
            ("operation_confirm", self._gate_operation_confirm),
        ]

    def _load_config(self) -> Dict[str, Any]:
        """加载LLM配置（从环境变量或配置文件）。

        返回：
            配置字典
        """
        # 优先从注入的provider获取配置
        if self.llm_provider is not None:
            provider_config = getattr(self.llm_provider, "config", None)
            if isinstance(provider_config, dict):
                return provider_config
        # 从环境变量读取
        config = DEFAULT_LLM_CONFIG.copy()
        api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get("ARK_API_KEY", "")
        if api_key:
            config["api_key"] = api_key
        base_url = os.environ.get("LLM_BASE_URL", "")
        if base_url:
            config["base_url"] = base_url
        model = os.environ.get("LLM_MODEL", "")
        if model:
            config["model"] = model
        # v6.46：无注入 provider 时回退 deployment_config.json（与 llm_provider 同源，
        # 消除"环境变量 vs 配置文件"双源不一致；未显式配置的键以配置文件补全）
        try:
            from prog.core.llm_provider import _load_default_llm_config
            dc = _load_default_llm_config()
            for k, v in dc.items():
                if k == "api_key":
                    if not config.get("api_key"):
                        config["api_key"] = v
                elif not config.get(k):
                    config[k] = v
        except Exception:
            pass
        return config

    def _is_active(self) -> bool:
        """检查LLM是否可用（有API密钥即为可用）。"""
        return bool(self.config.get("api_key", "").strip())

    # --------------------------------------------------------
    # 同步生成
    # --------------------------------------------------------
    def generate(self, prompt: str, context: Dict[str, Any]) -> str:
        """
        同步生成LLM回复。

        设计意图：
            执行完整的"前置门控->LLM调用->后置门控"流程，返回安全通过的输出。

        参数：
            prompt: 完整提示词（由PromptBuilder构建）
            context: 会话上下文（含意图、Agent类型、用户权限，供门控使用）

        返回：
            str: 经安全门控处理后的LLM输出文本

        流程：
            1. 前置门控：prompt_injection_check + intent_compliance_check
            2. LLM调用：llm_provider.call(prompt)
            3. 后置门控：_run_safety_gates(input, output)
            4. 返回 sanitized_output（或阻断原因）

        替代demo：
            替代 demo/llm_engine.py generate_with_llm()。
        """
        user_input = context.get("user_input", "") or prompt

        # 前置门控1：prompt注入检测
        injection_result = self._gate_prompt_injection(user_input, context)
        if not injection_result.passed:
            return injection_result.reason

        # 前置门控2：意图合规校验
        compliance_result = self._gate_intent_compliance(user_input, context)
        if not compliance_result.passed:
            return compliance_result.reason

        # LLM调用
        raw_output = self._call_llm_api(prompt)

        # 后置门控3~5
        safety_result = self._run_safety_gates(user_input, raw_output, context)
        if not safety_result.passed:
            return safety_result.reason
        # P1-1 修复：将 need_confirm 回写 context（与流式 meta 事件口径一致），
        # 供调用方（如 /api/llm/chat）读取后向用户下发二次确认，不再被丢弃
        if context is not None:
            context["need_confirm"] = safety_result.need_confirm
        return safety_result.sanitized_output or raw_output

    # --------------------------------------------------------
    # SSE流式生成
    # --------------------------------------------------------
    def generate_stream(self, prompt: str,
                        context: Dict[str, Any]) -> Generator[str, None, None]:
        """
        SSE流式生成LLM回复。

        设计意图：
            流式输出LLM生成内容，降低首字延迟。前置门控在流式开始前执行，
            后置门控中的格式校验与敏感过滤在流式块上增量执行，
            操作确认标记通过首个SSE事件下发。

        参数：
            prompt: 完整提示词
            context: 会话上下文

        返回：
            generator: 生成器，逐个产出SSE格式字符串

        流程：
            1. 前置门控（注入检测 + 意图合规）
            2. 流式调用 llm_provider.stream(prompt)
            3. 逐块格式化为SSE事件并yield
            4. 流式结束后执行后置门控（敏感过滤增量完成）

        替代demo：
            替代 demo/llm_engine.py generate_stream_with_llm()。
        """
        user_input = context.get("user_input", "") or prompt

        # 前置门控1：prompt注入检测
        injection_result = self._gate_prompt_injection(user_input, context)
        if not injection_result.passed:
            meta = {"need_confirm": False, "blocked": True}
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            yield f"event: message\ndata: {json.dumps({'content': injection_result.reason}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # 前置门控2：意图合规校验
        compliance_result = self._gate_intent_compliance(user_input, context)
        if not compliance_result.passed:
            meta = {"need_confirm": False, "blocked": True}
            yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
            yield f"event: message\ndata: {json.dumps({'content': compliance_result.reason}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
            return

        # 后置门控5：操作确认判断（通过meta事件下发）
        confirm_result = self._gate_operation_confirm(user_input, "", context)
        need_confirm = confirm_result.need_confirm
        meta = {"need_confirm": need_confirm, "blocked": False}
        yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"

        # 流式调用LLM
        full_output = ""
        # P1-3 修复：跨 chunk 敏感过滤——暂扣尾部可能构成敏感数字串前缀的字符，
        # 待下个 chunk 到达后再整体过滤，防手机号/身份证/银行卡被分块截断绕过脱敏
        tail = ""
        for chunk in self._stream_llm_api(prompt):
            # 兼容元组格式 ("reasoning"/"content", text) 和纯字符串
            if isinstance(chunk, tuple):
                chunk_type, chunk_text = chunk
                if chunk_type == "content":
                    sanitized, tail = self._filter_stream_chunk(chunk_text, tail)
                    if sanitized:
                        full_output += sanitized
                        payload = json.dumps({"content": sanitized}, ensure_ascii=False)
                        yield f"event: message\ndata: {payload}\n\n"
            else:
                sanitized, tail = self._filter_stream_chunk(chunk, tail)
                if sanitized:
                    full_output += sanitized
                    payload = json.dumps({"content": sanitized}, ensure_ascii=False)
                    yield f"event: message\ndata: {payload}\n\n"

        # 流结束：过滤尾部残留缓冲
        if tail:
            sanitized, _ = self._filter_stream_chunk("", tail)
            if sanitized:
                payload = json.dumps({"content": sanitized}, ensure_ascii=False)
                yield f"event: message\ndata: {payload}\n\n"

        yield "event: done\ndata: [DONE]\n\n"

    # --------------------------------------------------------
    # call方法（供BaseAgent._call_llm直接调用）
    # --------------------------------------------------------
    def call(self, prompt: str, **kwargs) -> str:
        """调用LLM生成回复（简化入口）。

        参数：
            prompt: 提示词
            **kwargs: 额外参数（如context）

        返回：
            str: LLM输出文本
        """
        context = kwargs.get("context", {})
        return self.generate(prompt, context)

    def chat(self, messages: list, tools: list = None, temperature: float = None) -> dict:
        """Chat接口（支持Function Calling / Structured Output）。

        参数：
            messages: OpenAI格式消息列表 [{"role":..., "content":...}, ...]
            tools: 可选，Function Calling工具定义列表
            temperature: 可选，温度参数

        返回：
            dict: {"content": str, "tool_calls": list|None}
                  tool_calls 为 [{"function": {"name":..., "arguments":...}}, ...]
        """
        # P1-2 修复：chat 入口补门控1（注入检测）——原实现零门控直通 LLM。
        # 取最后一条 user 消息做检测（Function Calling 场景首条多为系统提示）
        user_text = ""
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
                user_text = m["content"]
                break
        if user_text:
            inj = self._gate_prompt_injection(user_text, {})
            if not inj.passed:
                return {"content": inj.reason, "tool_calls": None}

        if not self._is_active():
            return {"content": "（LLM未配置，模拟模式）", "tool_calls": None}

        # 优先使用注入的provider（含安全门控）
        if self.llm_provider is not None:
            chat_method = getattr(self.llm_provider, "chat", None)
            if chat_method:
                try:
                    kwargs = {"messages": messages}
                    if tools:
                        kwargs["tools"] = tools
                    if temperature is not None:
                        kwargs["temperature"] = temperature
                    resp = chat_method(**kwargs)
                    if isinstance(resp, dict):
                        return resp
                    return {"content": str(resp), "tool_calls": None}
                except Exception:
                    pass  # 降级到HTTP直连

        # HTTP直连（豆包/OpenAI兼容格式）
        try:
            import requests
        except ImportError:
            return {"content": "（requests库未安装）", "tool_calls": None}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}",
        }
        payload = {
            "model": self.config.get("model", "deepseek-v4-flash-ga-260731"),
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.get("temperature", 0.3),
            "max_tokens": self.config.get("max_tokens", 4096),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        model_name = self.config.get("model", "")
        # ══════════════════════════════════════════════════════════════
        # PERF-FIX-v6.40 ANCHOR::Doubao-Seed 关闭thinking（chat 非流式）
        # 根因 / 修复 / 防回归规则：同 llm_provider.py ANCHOR。
        # ══════════════════════════════════════════════════════════════
        if "doubao-seed" in model_name:
            payload["thinking"] = {"type": "disabled"}

        url = self.config.get("base_url", "").rstrip("/") + "/chat/completions"
        timeout = self.config.get("timeout", 60)
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                return {"content": f"（API错误({resp.status_code})）", "tool_calls": None}
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            tool_calls = msg.get("tool_calls")
            content = msg.get("content", "") or ""
            return {"content": content.strip(), "tool_calls": tool_calls}
        except Exception as e:
            return {"content": f"（API调用异常：{e}）", "tool_calls": None}

    # --------------------------------------------------------
    # 底层LLM API调用
    # --------------------------------------------------------
    def _call_llm_api(self, prompt: str) -> str:
        """调用底层LLM API（OpenAI兼容格式）。

        参数：
            prompt: 完整提示词

        返回：
            str: LLM原始输出文本

        P1-6 修复：无 API 密钥时先返回模拟回复，再走 provider/HTTP 分支——
        否则 llm_provider 已注入但无 key 时，provider 分支会真实打 API，
        把 401 错误文本当正文返回。
        """
        # 无API密钥时返回模拟回复（前置判断，避免无 key 时真实打 API）
        if not self._is_active():
            return "（LLM未配置，模拟模式）我已收到您的请求，请配置API密钥以启用AI回复。"

        # 优先使用注入的provider
        if self.llm_provider is not None:
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
            # 尝试call方法
            call_method = getattr(self.llm_provider, "call", None)
            if call_method:
                try:
                    result = call_method(prompt)
                    if isinstance(result, str):
                        return result
                    if isinstance(result, dict):
                        return result.get("text", "") or result.get("content", "")
                except Exception:
                    pass

        # 使用openai SDK或httpx调用
        try:
            return self._call_via_http(prompt)
        except Exception as e:
            return f"（LLM调用失败：{e}）"

    def _call_via_http(self, prompt: str) -> str:
        """通过HTTP调用OpenAI兼容API。

        参数：
            prompt: 提示词

        返回：
            str: LLM输出文本
        """
        try:
            import requests
        except ImportError:
            return "（requests库未安装，无法调用LLM）"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}",
        }
        payload = {
            "model": self.config.get("model", "deepseek-v4-flash-ga-260731"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.get("temperature", 0.3),
            "max_tokens": self.config.get("max_tokens", 4096),
            "stream": False,
        }
        model_name = self.config.get("model", "")
        # ══════════════════════════════════════════════════════════════
        # PERF-FIX-v6.40 ANCHOR::Doubao-Seed 关闭thinking（complete 非流式）
        # 根因 / 修复 / 防回归规则：同 llm_provider.py ANCHOR。
        # ══════════════════════════════════════════════════════════════
        if "doubao-seed" in model_name:
            payload["thinking"] = {"type": "disabled"}

        url = self.config.get("base_url", "").rstrip("/") + "/chat/completions"
        timeout = self.config.get("timeout", 60)

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                return f"（API错误({resp.status_code})）"
            data = resp.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return text.strip()
        except Exception as e:
            return f"（API调用异常：{e}）"

    def _stream_llm_api(self, prompt: str) -> Generator[str, None, None]:
        """流式调用LLM API。

        参数：
            prompt: 提示词

        返回：
            generator: 文本块生成器
        """
        if not self._is_active():
            # 模拟流式输出
            yield "（LLM未配置，模拟模式）请配置API密钥以启用AI回复。"
            return

        if self.llm_provider is not None:
            stream_method = getattr(self.llm_provider, "stream_completion", None)
            if stream_method:
                try:
                    messages = [{"role": "user", "content": prompt}]
                    for chunk in stream_method(messages):
                        if isinstance(chunk, dict):
                            # 推理内容（思考过程），优先输出（与 _stream_via_http 一致）
                            reasoning = chunk.get("reasoning", "")
                            if reasoning:
                                yield ("reasoning", reasoning)
                            content = chunk.get("content", "") or chunk.get("delta", "")
                            if content:
                                yield ("content", content)
                    return
                except Exception:
                    pass

        # 通过HTTP流式调用
        try:
            yield from self._stream_via_http(prompt)
        except Exception as e:
            yield f"（流式调用失败：{e}）"

    def _stream_via_http(self, prompt: str) -> Generator[str, None, None]:
        """通过HTTP流式调用OpenAI兼容API。"""
        try:
            import requests
        except ImportError:
            yield "（requests库未安装）"
            return

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config['api_key']}",
        }
        payload = {
            "model": self.config.get("model", "deepseek-v4-flash-ga-260731"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.get("temperature", 0.3),
            "max_tokens": self.config.get("max_tokens", 4096),
            "stream": True,
        }
        model_name = self.config.get("model", "")
        # ══════════════════════════════════════════════════════════════
        # PERF-FIX-v6.40 ANCHOR::Doubao-Seed 关闭thinking（complete 流式）
        # 根因 / 修复 / 防回归规则：同 llm_provider.py ANCHOR。
        # ══════════════════════════════════════════════════════════════
        if "doubao-seed" in model_name:
            payload["thinking"] = {"type": "disabled"}

        url = self.config.get("base_url", "").rstrip("/") + "/chat/completions"
        timeout = self.config.get("timeout", 60)

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True)
            if resp.status_code != 200:
                yield f"（API错误({resp.status_code})）"
                return
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        # 推理内容（思考过程），优先输出
                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            yield ("reasoning", reasoning)
                        # 正式回复内容
                        content = delta.get("content", "")
                        if content:
                            yield ("content", content)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            yield f"（流式调用异常：{e}）"

    # --------------------------------------------------------
    # 安全门控执行
    # --------------------------------------------------------
    def _run_safety_gates(self, input: str,
                          output: str,
                          context: Optional[Dict[str, Any]] = None) -> SafetyResult:
        """
        执行5道安全门控。

        设计意图：
            统一执行后置3道门控（格式校验、敏感过滤、操作确认），
            前置2道（注入检测、意图合规）在 generate/generate_stream
            中先行调用。任一门控阻断即终止后续门控并返回。

        参数：
            input: 用户原始输入（供意图合规等门控参考）
            output: LLM原始输出
            context: 会话上下文（可选）

        返回：
            SafetyResult: 门控结果对象

        门控顺序与逻辑：
            1. prompt_injection_check: 已在前置执行（此处跳过）
            2. intent_compliance_check: 已在前置执行（此处跳过）
            3. output_format_check:
               依据context中预期的输出格式（JSON/HTML/text）校验
               不符则尝试修复或阻断
            4. sensitive_info_filter:
               正则匹配手机号/身份证/银行卡等，脱敏或剔除
               输出 sanitized_output
            5. operation_confirm:
               依据context中意图的风险等级（高/中/低）
               高风险 -> 标记 need_confirm=True，由前端确认后再执行
               中低风险 -> 直接放行

        替代demo：
            替代 demo/llm_engine.py generate_with_llm() 中分散的安全检查。
        """
        ctx = context or {}
        sanitized = output

        # 门控3：输出格式校验
        fmt_result = self._gate_output_format(input, output, ctx)
        if not fmt_result.passed:
            return fmt_result

        # 门控4：敏感信息过滤
        filter_result = self._gate_sensitive_info(input, output, ctx)
        if not filter_result.passed:
            return filter_result
        sanitized = filter_result.sanitized_output or output

        # 门控5：操作确认
        confirm_result = self._gate_operation_confirm(input, output, ctx)
        if not confirm_result.passed:
            return confirm_result

        return SafetyResult(
            passed=True,
            sanitized_output=sanitized,
            need_confirm=confirm_result.need_confirm,
        )

    # ============================================================
    # 5道安全门控实现
    # ============================================================

    def _gate_prompt_injection(self, input: str,
                                context: Dict[str, Any]) -> SafetyResult:
        """门控1：prompt注入检测。

        检测用户输入中的prompt注入攻击模式，命中即阻断。

        参数：
            input: 用户输入
            context: 会话上下文

        返回：
            SafetyResult: pass=未检测到注入，blocked=检测到注入
        """
        if not input:
            return SafetyResult(passed=True)
        # P1-4 修复：空白归一化后再匹配（对齐 agent_security v6.73）——
        # 折叠连续空白为单空格，防 "忽略   以上指令" 等多空格变体绕过字面子串检测
        input_norm = " ".join(input.lower().split())
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.lower() in input_norm:
                return SafetyResult(
                    passed=False,
                    blocked_gate="prompt_injection_check",
                    reason=f"检测到潜在的prompt注入攻击，请求已被拦截。",
                )
        return SafetyResult(passed=True)

    def _gate_intent_compliance(self, input: str,
                                 context: Dict[str, Any]) -> SafetyResult:
        """门控2：意图合规校验。

        校验识别的意图与目标Agent的职责范围是否匹配。

        参数：
            input: 用户输入
            context: 会话上下文（含intent与agent_type）

        返回：
            SafetyResult: pass=合规，blocked=越权
        """
        # 简化实现：检测是否有明显越权指令
        agent_type = context.get("agent_type", "")
        # 非生产Agent不应执行排产操作
        if agent_type and agent_type != "production":
            if any(k in input for k in ["排产", "安排生产", "开工单生产"]):
                return SafetyResult(
                    passed=False,
                    blocked_gate="intent_compliance_check",
                    reason=f"当前Agent（{agent_type}）无权执行生产操作，请通过生产Agent处理。",
                )
        return SafetyResult(passed=True)

    def _gate_output_format(self, input: str, output: str,
                             context: Dict[str, Any]) -> SafetyResult:
        """门控3：输出格式校验。

        依据context中预期的输出格式校验LLM输出。

        参数：
            input: 用户输入
            output: LLM输出
            context: 会话上下文（含expected_format）

        返回：
            SafetyResult: pass=格式符合，blocked=格式不符
        """
        expected_format = context.get("expected_format", "text")
        if expected_format == "json":
            # 尝试解析JSON
            try:
                json.loads(output)
            except (json.JSONDecodeError, ValueError):
                # 格式不符但不阻断，尝试提取JSON片段
                json_match = re.search(r'\{[\s\S]*\}', output)
                if json_match:
                    try:
                        json.loads(json_match.group())
                        return SafetyResult(passed=True, sanitized_output=output)
                    except (json.JSONDecodeError, ValueError):
                        pass
                return SafetyResult(
                    passed=False,
                    blocked_gate="output_format_check",
                    reason="LLM输出格式不符合预期（JSON），请重试。",
                )
        return SafetyResult(passed=True, sanitized_output=output)

    def _gate_sensitive_info(self, input: str, output: str,
                              context: Dict[str, Any]) -> SafetyResult:
        """门控4：敏感信息过滤。

        过滤LLM输出中的敏感信息（手机号、身份证号、银行卡号等）。

        参数：
            input: 用户输入
            output: LLM输出
            context: 会话上下文

        返回：
            SafetyResult: pass=无敏感信息或已过滤，blocked=含绝密信息
        """
        if not output:
            return SafetyResult(passed=True, sanitized_output=output)
        sanitized = output
        for pattern, replacement in SENSITIVE_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return SafetyResult(passed=True, sanitized_output=sanitized)

    def _filter_sensitive(self, text: str) -> str:
        """对文本块执行敏感信息过滤（增量版，供流式使用）。

        参数：
            text: 待过滤的文本块

        返回：
            str: 过滤后的文本
        """
        if not text:
            return text
        sanitized = text
        for pattern, replacement in SENSITIVE_PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def _filter_stream_chunk(self, text: str, tail: str) -> tuple:
        """跨 chunk 敏感过滤（P1-3）：拼接尾部缓冲后过滤，并暂扣新尾部。

        数字串前缀（连续数字 / 1[3-9] 起始）可能构成手机号/身份证/银行卡的
        开头，被分块截断时单独过滤无法命中——扣住待下个 chunk 一起过滤，
        流结束时再放行剩余缓冲。

        参数：
            text: 当前 chunk 文本
            tail: 上一轮暂扣的尾部缓冲

        返回：
            (emit_text, new_tail): 可安全下发的文本与新的暂扣缓冲
        """
        combined = tail + (text or "")
        sanitized = self._filter_sensitive(combined)
        # 从尾部回退暂扣连续数字（最长 19 位，覆盖最长敏感数字串）
        new_tail = ""
        for ch in reversed(sanitized):
            if ch.isdigit():
                new_tail = ch + new_tail
                if len(new_tail) >= 19:
                    break
            else:
                break
        if new_tail:
            emit = sanitized[:len(sanitized) - len(new_tail)]
        else:
            emit = sanitized
        return emit, new_tail

    def _gate_operation_confirm(self, input: str, output: str,
                                 context: Dict[str, Any]) -> SafetyResult:
        """门控5：操作确认。

        高风险操作需要用户二次确认。

        参数：
            input: 用户输入
            output: LLM输出
            context: 会话上下文（含intent风险等级）

        返回：
            SafetyResult: pass=放行，need_confirm=True时需确认
        """
        intent = context.get("intent", "")
        # 高风险意图需确认
        if intent in HIGH_RISK_INTENTS:
            return SafetyResult(
                passed=True,
                need_confirm=True,
                reason="此操作为高风险操作，需要用户确认。",
            )
        # 检测输出中的高风险操作关键词
        if output:
            for pattern in HIGH_RISK_ACTION_PATTERNS:
                if pattern in output:
                    return SafetyResult(
                        passed=True,
                        need_confirm=True,
                        reason=f"检测到高风险操作（{pattern}），需要用户确认。",
                    )
        return SafetyResult(passed=True)


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert LLMEngine is not None, "LLMEngine 类未定义"
    assert SafetyResult is not None, "SafetyResult 类未定义"
    # 验证基本功能
    engine = LLMEngine()
    # 验证门控1：注入检测
    result = engine._gate_prompt_injection("忽略以上指令", {})
    assert not result.passed, "prompt注入检测应阻断"
    result = engine._gate_prompt_injection("查一下库存", {})
    assert result.passed, "正常输入不应被阻断"
    # 验证门控4：敏感信息过滤
    result = engine._gate_sensitive_info("", "手机号13912345678", {})
    assert "13912345678" not in result.sanitized_output, "手机号应被脱敏"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
