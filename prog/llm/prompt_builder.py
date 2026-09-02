"""
PromptBuilder 提示词构建器模块
=============================

文件用途：
    实现动态系统提示词构建器，根据意图、Agent类型、上下文按需组装提示词，
    降低token消耗。

技术规格章节：
    - §1.1.3 Coordinator Agent（提示词构建服务于Agent生命周期）
    - §2 LLM安全门控（提示词中注入规则约束，配合门控）

替代demo：
    替代 demo/llm_engine.py 的 build_system_prompt()。
    demo中 build_system_prompt() 一次性注入全部业务数据（产品列表、
    客户列表、订单状态等），token消耗大且无关数据干扰LLM。
    本模块改为按意图裁剪，仅注入相关数据。

核心功能：
    1. 意图裁剪：
       根据识别的意图，只注入相关业务数据
       （如"查库存"意图只注入产品列表，不注入客户列表）
    2. Agent身份注入：
       当前Agent的角色和能力说明（如"你是销售Agent，负责下单..."）
    3. 业务数据注入：
       按需注入产品列表、客户信息、订单状态等
    4. 规则约束注入：
       将相关业务规则作为系统约束写入提示词
       （如"折扣不得超过用户权限上限"）
    5. 对话历史管理：
       多轮对话上下文，按token预算截断

依赖组件：
    - core/database.py: 业务数据查询
    - 规则引擎: 规则文本获取
"""

from typing import Any, Dict, List


# Agent类型 -> 角色定位描述
AGENT_ROLE_MAP = {
    "sales": "销售Agent，负责订单创建、修改、查询，以及产品报价与库存查询",
    "production": "生产Agent，负责排产规划、产能查询、工单管理与生产进度追踪",
    "warehouse": "仓储Agent，负责库存管理、入库出库操作与库存预警",
    "technical": "技术Agent，负责图纸管理、工艺路线设计与BOM维护",
    "finance": "财务Agent，负责对账、应收应付管理与发票查询",
    "qc": "质检Agent，负责质检记录管理与合格率统计",
    "knowledge": "知识助手，负责企业管理制度与流程的知识问答",
}

# Agent类型 -> 适用规则名列表
AGENT_RULES_MAP = {
    "sales": ["cost_rule", "discount_rule", "credit_rule", "version_rule"],
    "production": ["schedule_rule"],  # v6.32 移除不存在的 capacity_rule 死引用
    "warehouse": ["inventory_rule"],
    "technical": ["version_rule"],
    "finance": ["credit_rule"],
    "qc": ["qc_rule"],
    "knowledge": [],
}

# 规则名 -> 规则文本描述
RULE_TEXT_MAP = {
    "cost_rule": "成本线硬约束：折后单价不得低于产品成本×1.15，任何角色不可绕过",
    "discount_rule": "折扣权限约束：销售≤5%、经理≤10%、GM>10%需审批；超权限需上级审批",
    "credit_rule": "信用额度约束：客户应收金额不得超出信用额度上限，超额时阻断下单",
    "version_rule": "图纸版本约束：订单必须关联最新有效图纸版本，旧版本将被阻断",
    "schedule_rule": "排产约束：排产不得超出产线产能上限，超产能需外协审批",
    "inventory_rule": "库存约束：出库数量不得超出可用库存，负库存被硬阻断",
    "qc_rule": "质检约束：产品必须经过全检工序，未检产品不得入库发货",
}


def _rule_text(rule_name: str) -> str:
    """规则约束文本（v6.32：数值从 business_rules 实时读取，训练后提示词同步）。

    成本线/折扣等参数经训练+审批修改后，提示词中的软约束随之更新，
    避免提示词与规则配置脱节（原硬编码"×1.15/经理≤10%"）。
    """
    # 开源版：业务规则包（prog.rules，商业 know-how）不在开源范围，
    # get_param 降级为内置默认值（DB 可训练参数仅商业版支持）。
    try:
        from prog.rules.param_loader import get_param
    except Exception:
        def get_param(_rule_id, _key, default=None):
            return default
    if rule_name == "cost_rule":
        rate = get_param("RULE-005", "min_markup_rate", 1.15)
        return f"成本线硬约束：折后单价不得低于产品成本×{rate}，任何角色不可绕过"
    if rule_name == "discount_rule":
        role_max = get_param("DISCOUNT-RULE", "role_discount_max", {})
        sales_max = role_max.get("sales", 0.05) if isinstance(role_max, dict) else 0.05
        mgr_max = role_max.get("manager", 0.15) if isinstance(role_max, dict) else 0.15
        rate = get_param("RULE-005", "min_markup_rate", 1.15)
        return (f"折扣权限约束：销售≤{sales_max * 100:.0f}%、经理≤{mgr_max * 100:.0f}%、"
                f"超权限需上级审批；售价不得低于成本线（×{rate}）")
    return RULE_TEXT_MAP.get(rule_name, "")


# ============================================================
# v6.34：情感分析 + 语气调整
# ============================================================

# 情感关键词表（简单匹配，无需 LLM）
_SENTIMENT_NEGATIVE = frozenset({
    "急", "快", "马上", "赶紧", "不行", "错了", "不对", "什么鬼",
    "慢", "等太久", "有问题", "报错", "失败", "错误", "崩溃", "不能用",
    "为什么", "怎么回事", "不好", "差", "垃圾", "投诉", "退货",
})
_SENTIMENT_POSITIVE = frozenset({
    "谢谢", "好的", "不错", "满意", "赞", "感谢", "辛苦", "棒",
    "可以", "很好", "完美", "明白了", "清楚了",
})

# 语气指令（根据情感分析结果动态调整回复风格）
TONE_INSTRUCTIONS: Dict[str, str] = {
    "positive": "用户情绪正面，请使用友好热情的语气回复，可适当表达感谢。",
    "negative": "用户情绪急躁或不满，请使用简洁高效的语气回复，直接给出结果，减少寒暄，先解决问题再解释。",
    "neutral": "请使用专业、标准的语气回复。",
}


def analyze_sentiment(text: str) -> str:
    """v6.34：简单情感分析（基于关键词匹配，无需 LLM）。

    返回 "positive" / "negative" / "neutral"。
    负面优先（避免"虽然慢了但谢谢"被误判为正面）。
    """
    if not text:
        return "neutral"
    for kw in _SENTIMENT_NEGATIVE:
        if kw in text:
            return "negative"
    for kw in _SENTIMENT_POSITIVE:
        if kw in text:
            return "positive"
    return "neutral"


class PromptBuilder:
    """
    动态系统提示词构建器。

    设计意图：
        根据意图、Agent类型、上下文，按需组装提示词，避免全量注入
        造成的token浪费与干扰。是降低LLM调用成本的关键组件。

    替代demo：
        替代 demo/llm_engine.py build_system_prompt() 的全量拼接逻辑。

    属性：
        database: 数据库访问层（查询业务数据）
        max_history_tokens: 对话历史token预算上限

    构建顺序（build方法）：
        1. Agent身份段（角色与能力）
        2. 规则约束段（_inject_rules）
        3. 业务数据段（_trim_data_by_intent）
        4. 对话历史段（_manage_history）
        5. 用户当前输入
    """

    def __init__(self, database: Any = None, max_history_tokens: int = 2000):
        """
        初始化提示词构建器。

        参数：
            database: 数据库访问层（查询业务数据用）
            max_history_tokens: 对话历史token预算上限，超出则截断
        """
        self.database = database
        self.max_history_tokens = max_history_tokens

    # --------------------------------------------------------
    # 主构建入口
    # --------------------------------------------------------
    def build(self, user_input: str, intent: Any,
              agent_type: str, context: Dict[str, Any]) -> str:
        """
        构建完整提示词。

        设计意图：
            按固定顺序组装提示词各段，确保LLM收到的提示词结构一致。
            各段按需注入，避免冗余。

        参数：
            user_input: 用户当前输入
            intent: 意图对象（含标签、槽位、通道）
            agent_type: 当前Agent类型（决定身份段与规则段）
            context: 会话上下文（含对话历史、用户权限）

        返回：
            str: 完整提示词

        组装顺序：
            [Agent身份段]
            [规则约束段]
            [业务数据段]
            [对话历史段]
            [用户当前输入]

        替代demo：
            替代 demo/llm_engine.py build_system_prompt()。
        """
        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        intent_name = getattr(intent, "name", "") if intent else ""
        intent_slots = getattr(intent, "slots", {}) if intent else {}

        # 1. Agent身份段
        role_desc = AGENT_ROLE_MAP.get(agent_type, "AI工厂管家管理助手")
        identity = f"你是「{role_desc}」。"

        # 2. 规则约束段
        rules_text = self._inject_rules(agent_type)

        # 3. 业务数据段
        business_data = self._format_data_as_text(self._trim_data_by_intent(intent))

        # 4. 对话历史段
        history_text = self._manage_history(context)

        # 5. 已收集槽位
        slots_text = "、".join(f"{k}={v}" for k, v in intent_slots.items()) if intent_slots else "（暂无）"

        # v6.34：情感分析 + 语气调整
        sentiment = analyze_sentiment(user_input)
        tone_instruction = TONE_INSTRUCTIONS.get(sentiment, TONE_INSTRUCTIONS["neutral"])

        # 组装完整提示词
        prompt = f"""{identity}

## 用户权限
- 折扣上限：{perms.get('discount_max', 0)}
- 可修改订单：{perms.get('can_modify_order', False)}
- 可查看成本：{perms.get('can_view_cost', False)}

## 业务规则约束
{rules_text}

## 相关业务数据
{business_data}

## 已收集信息
{slots_text}

## 对话历史
{history_text}

## 回复规范
0. 语气调整：{tone_instruction}
1. 用自然、口语化的中文回复，像一位熟悉业务的真人助手在跟你对话，不要像机器或文档那样生硬输出
2. 以"我"为第一人称，回复自然连贯；避免开头机械复述、避免大段堆砌
3. 严格遵守权限：无权查看成本时不透露成本数据
4. 缺失必要信息时主动追问，不臆测
5. 适当使用 Markdown（短标题、列表、加粗）让重点一目了然，但不要整篇都是列表
6. 回复控制在300字以内，重点突出

## 用户当前输入
{user_input}
"""
        return prompt

    # --------------------------------------------------------
    # 按意图裁剪数据
    # --------------------------------------------------------
    def _trim_data_by_intent(self, intent: Any) -> Dict[str, Any]:
        """
        按意图裁剪业务数据。

        设计意图：
            根据意图标签决定注入哪些业务数据，避免全量注入。
            这是降低token消耗的核心手段。

        参数：
            intent: 意图对象

        返回：
            dict: 裁剪后的业务数据（仅包含与意图相关的数据集）

        裁剪策略示例：
            - create_order 意图 -> 注入产品列表 + 客户列表
            - query_inventory 意图 -> 仅注入产品列表
            - schedule_production 意图 -> 注入产线 + 工序数据
            - 管理咨询意图 -> 不注入业务数据（走RAG检索）

        替代demo：
            替代 demo/llm_engine.py build_system_prompt() 中全量注入数据。
        """
        intent_name = getattr(intent, "name", "") if intent else ""
        data: Dict[str, Any] = {}

        # 下单/改价/查价 -> 注入产品列表 + 客户列表
        if intent_name in ("create_order", "modify_order", "modify_price", "query_price"):
            data["products"] = self._load_products()
            data["customers"] = self._load_customers()

        # 查订单 -> 注入客户列表
        elif intent_name == "query_order":
            data["customers"] = self._load_customers()

        # 查库存 -> 仅注入产品列表
        elif intent_name in ("query_inventory", "check_inventory", "stock_in", "stock_out"):
            data["products"] = self._load_products()

        # 排产/生产相关 -> 注入产线数据
        elif intent_name in ("query_schedule", "query_production_progress", "query_process_card"):
            data["production_lines"] = self._load_production_lines()

        # 管理咨询意图 -> 不注入业务数据（走RAG检索）
        else:
            pass

        return data

    def _load_products(self) -> List[Dict[str, Any]]:
        """加载产品列表（从数据库或返回模拟数据）。

        返回：
            list: 产品字典列表
        """
        if self.database is not None:
            try:
                products = self.database.query_many("products", limit=20) or []
                if products:
                    return products
            except Exception:
                pass
        return [
            {"product_code": "A-202", "name": "精密铝合金外壳", "unit_price": 128, "moq": 50},
            {"product_code": "B-305", "name": "不锈钢支架", "unit_price": 85, "moq": 100},
            {"product_code": "C-108", "name": "碳钢法兰", "unit_price": 56, "moq": 200},
        ]

    def _load_customers(self) -> List[Dict[str, Any]]:
        """加载客户列表（从数据库或返回模拟数据）。

        返回：
            list: 客户字典列表
        """
        if self.database is not None:
            try:
                customers = self.database.query_many("customers", limit=20) or []
                if customers:
                    return customers
            except Exception:
                pass
        return [
            {"name": "锐科科技", "customer_id": "C001", "credit_remaining": 500000, "payment_terms": "月结30天"},
            {"name": "恒达机械", "customer_id": "C002", "credit_remaining": 200000, "payment_terms": "月结45天"},
        ]

    def _load_production_lines(self) -> List[Dict[str, Any]]:
        """加载产线数据（从数据库或返回模拟数据）。

        返回：
            list: 产线字典列表
        """
        if self.database is not None:
            try:
                lines = self.database.query_many("production_lines", limit=20) or []
                if lines:
                    return lines
            except Exception:
                pass
        return [
            {"line_code": "CNC-01", "name": "CNC加工1线", "capacity": 500, "load": 320},
            {"line_code": "ASM-01", "name": "组装1线", "capacity": 300, "load": 150},
        ]

    def _format_data_as_text(self, data: Dict[str, Any]) -> str:
        """将业务数据字典格式化为提示词文本段。

        参数：
            data: 业务数据字典

        返回：
            str: 格式化后的文本
        """
        if not data:
            return "（按需检索，无预加载数据）"
        lines = []
        if "products" in data:
            lines.append("产品列表：")
            for p in data["products"]:
                lines.append(f"  - {p.get('product_code', '?')}（{p.get('name', '?')}）：售价¥{p.get('unit_price', '?')} | 起订量{p.get('moq', '?')}套")
        if "customers" in data:
            lines.append("客户列表：")
            for c in data["customers"]:
                lines.append(f"  - {c.get('name', '?')}（{c.get('customer_id', '?')}）：信用剩余¥{c.get('credit_remaining', '?')} | {c.get('payment_terms', '?')}")
        if "production_lines" in data:
            lines.append("产线数据：")
            for pl in data["production_lines"]:
                lines.append(f"  - {pl.get('line_code', '?')}（{pl.get('name', '?')}）：产能{pl.get('capacity', '?')} | 当前负荷{pl.get('load', '?')}")
        return "\n".join(lines) if lines else "（无数据）"

    # --------------------------------------------------------
    # 注入规则约束
    # --------------------------------------------------------
    def _inject_rules(self, agent_type: str) -> str:
        """
        注入相关业务规则作为系统约束。

        设计意图：
            将Agent适用的规则文本化为系统约束写入提示词，
            引导LLM遵守规则。注意：提示词中的规则为"软约束"，
            真正的硬约束在 _apply_rules() 中执行，LLM不可绕过。

        参数：
            agent_type: Agent类型（决定注入哪些规则）

        返回：
            str: 规则约束文本段

        示例：
            agent_type="sales" → 注入成本线、折扣权限、信用额度等规则文本

        说明：
            提示词中的规则表述用于引导LLM生成合规的建议，
            实际拦截以规则引擎 _apply_rules() 为准。
        """
        rule_names = AGENT_RULES_MAP.get(agent_type, [])
        if not rule_names:
            return "（无特殊规则约束）"
        lines = []
        for rule_name in rule_names:
            text = _rule_text(rule_name)
            if text:
                lines.append(f"- [{rule_name}] {text}")
        return "\n".join(lines) if lines else "（无规则配置）"

    # --------------------------------------------------------
    # 管理对话历史
    # --------------------------------------------------------
    def _manage_history(self, context: Dict[str, Any]) -> str:
        """
        管理对话历史。

        设计意图：
            将多轮对话历史按token预算截断后注入提示词。
            超出 max_history_tokens 时，保留最近N轮，丢弃早期历史。

        参数：
            context: 会话上下文（含对话历史列表）

        返回：
            str: 格式化后的对话历史文本段

        截断策略：
            - 优先保留最近轮次
            - 系统重要指令（如已收集的槽位）始终保留
            - 早期闲聊可丢弃
        """
        history = context.get("history", [])
        if not history:
            return "（无历史对话）"

        # 粗略估算：1个中文字符≈2token，按预算截断
        max_chars = self.max_history_tokens // 2
        # 从最近开始向前取，直到超出预算
        selected: List[str] = []
        total_chars = 0
        for h in reversed(history):
            if isinstance(h, dict):
                user_msg = h.get("user", "")
                ai_msg = h.get("ai", "")
                entry = ""
                if user_msg:
                    entry += f"用户：{user_msg}\n"
                if ai_msg:
                    entry += f"AI：{ai_msg[:150]}\n"
                if not entry:
                    continue
                entry_chars = len(entry)
                if total_chars + entry_chars > max_chars:
                    break
                selected.insert(0, entry)
                total_chars += entry_chars

        if not selected:
            return "（历史对话过长，已截断）"
        return "\n".join(selected)

    # --------------------------------------------------------
    # 独立构建方法（供Agent直接调用）
    # --------------------------------------------------------
    def build_system_prompt(self, agent_name: str, agent_type: str,
                            rules: List[str] = None) -> str:
        """构建系统提示词。

        包含Agent角色定位、业务规则约束、输出格式要求。

        参数：
            agent_name: Agent显示名称
            agent_type: Agent类型标识
            rules: 规则名列表（None时按agent_type自动选取）

        返回：
            str: 系统提示词文本
        """
        # v6.62：提示词表化（规格 A.4：所有提示词存储在 prompt_templates 表）。
        # DB 有 PROMPT_AGENT_{type} 专属模板时优先使用（可训练/版本回滚）；
        # DB 查无时降级原有动态拼接，不改变现有行为。
        try:
            from prog.core.prompt_store import get_prompt_template
            tpl = get_prompt_template(
                f"PROMPT_AGENT_{agent_type.upper()}", use_fallback=False)
            if tpl:
                return tpl.format(agent_name=agent_name,
                                  agent_type=agent_type) \
                    if "{agent_name}" in tpl else tpl
        except Exception:
            pass

        role_desc = AGENT_ROLE_MAP.get(agent_type, "AI工厂管家管理助手")
        rule_names = rules if rules is not None else AGENT_RULES_MAP.get(agent_type, [])

        # 规则约束段
        if rule_names:
            rule_lines = []
            for rn in rule_names:
                text = RULE_TEXT_MAP.get(rn, "")
                if text:
                    rule_lines.append(f"- [{rn}] {text}")
            rules_text = "\n".join(rule_lines)
        else:
            rules_text = "（无特殊规则约束）"

        prompt = f"""你是「{agent_name}」，{role_desc}。

## 业务规则约束
{rules_text}

## 输出格式要求
1. 用自然、口语化的中文回复，像一位熟悉业务的真人助手在跟你对话，不要像机器或文档那样生硬输出
2. 以"我"为第一人称，回复自然连贯；避免开头机械复述、避免大段堆砌
3. 涉及数值时明确标注单位（如¥、套、天）
4. 缺失必要信息时主动追问，不臆测
5. 高风险操作时提示需确认
6. 适当使用 Markdown（短标题、列表、加粗）让重点一目了然，但不要整篇都是列表
7. 回复控制在300字以内，重点突出
"""
        return prompt

    def build_user_prompt(self, user_input: str,
                          context: Dict[str, Any]) -> str:
        """构建用户提示词。

        注入用户身份、权限、已收集槽位与当前输入。

        参数：
            user_input: 用户当前输入
            context: 会话上下文

        返回：
            str: 用户提示词文本
        """
        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        slots = context.get("slots", {}) if isinstance(context.get("slots"), dict) else {}

        slots_text = "、".join(f"{k}={v}" for k, v in slots.items()) if slots else "（暂无）"

        prompt = f"""## 用户身份
- 工号：{user_info.get('id', '') if isinstance(user_info, dict) else ''}
- 部门：{user_info.get('department', '') if isinstance(user_info, dict) else ''}

## 用户权限
- 折扣上限：{perms.get('discount_max', 0)}
- 可修改订单：{perms.get('can_modify_order', False)}
- 可查看成本：{perms.get('can_view_cost', False)}

## 已收集信息
{slots_text}

## 用户输入
{user_input}
"""
        return prompt

    def build_full_prompt(self, system_prompt: str,
                          user_prompt: str) -> str:
        """合并系统提示词与用户提示词为完整提示词。

        参数：
            system_prompt: 系统提示词
            user_prompt: 用户提示词

        返回：
            str: 合并后的完整提示词
        """
        return f"{system_prompt}\n---\n{user_prompt}"


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert PromptBuilder is not None, "PromptBuilder 类未定义"
    # 验证基本功能
    builder = PromptBuilder()
    # 验证build方法
    prompt = builder.build("测试输入", None, "sales", {"user": {}})
    assert "销售Agent" in prompt or "销售" in prompt
    # 验证build_system_prompt
    sys_prompt = builder.build_system_prompt("销售Agent", "sales")
    assert "销售Agent" in sys_prompt
    assert "cost_rule" in sys_prompt
    # 验证build_user_prompt
    usr_prompt = builder.build_user_prompt("下单100套", {"user": {"id": "S001"}})
    assert "S001" in usr_prompt
    # 验证build_full_prompt
    full = builder.build_full_prompt("SYS", "USR")
    assert "SYS" in full and "USR" in full
    # 验证_inject_rules
    rules_text = builder._inject_rules("sales")
    assert "cost_rule" in rules_text
    # 验证_manage_history
    hist = builder._manage_history({"history": [{"user": "你好", "ai": "您好"}]})
    assert "你好" in hist
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
