"""
意图识别器模块
==============
文件用途：
    提供用户意图识别能力，将自然语言输入映射为结构化Intent，
    供各Agent路由分发。采用"DB规则优先 + 内置规则兜底 + LLM兜底"的三层识别机制。

三层识别机制说明（v6.29 意图规则"数据库+代码"化）：
    1. DB规则匹配（_rule_based_match）：
       从 intent_rules 表加载启用规则（enabled=TRUE，按 priority 升序），
       命中即返回，并携带规则自带的 target_agent/target_channel 路由信息。
       规则内容（正则/路由/优先级）可通过训练变更：
       - L1 反馈（add_feedback）：用户显式纠正，写库直接生效（priority=1 高优先级精确匹配）
       - L2 训练产出/LLM建议（add_rule）：写库 enabled=FALSE 待审批，审批后生效
       - 管理API修改：经 workflow_configs 审批链，审批通过后 version+1 生效
       热更新：按 refresh_interval 轮询重载，变更生效无需重启。
    2. 内置规则兜底（_builtin_rules = DEFAULT_RULES）：
       DB 表为空、DB 不可用或 DB 规则未命中时使用，保证离线可用。
    3. LLM辅助识别（_llm_based_match）：
       规则均未命中时调用LLM进行意图分类，支持开放业务表达。
       LLM返回结构化JSON（intent + 提取的参数），并由安全门校验后返回。
       失败时回退为 unknown 意图，避免误路由。

开源化说明：
    - LLM 客户端通过构造参数注入（可选）：无 LLM 时仅规则匹配，其余回退 unknown。
    - 数据库层（intent_rules 表加载/训练数据回填/规则写入）为可选依赖，
      无数据库时降级为内置规则 + 内存修正。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 三层识别机制："DB 规则优先 + 内置规则兜底 + LLM 兜底"，规则未命中时调用 LLM 分类，失败安全回退 unknown 避免误路由（来源：SPEC §3.11 / 业务规格书 v6.29 / 模块拆分方案 契约2）
        - 确定性规则预检：LLM 步骤前 _deterministic_rule_precheck 复用规则匹配（含消歧），高频动作/查询句式零延迟命中；动作类意图命中且句首为查询动词时跳过预检（守卫）——规避强模型对确定性句式的推理延迟（来源：业务规格书 v6.85 / CHANGELOG v41）
        - 意图规则"数据库+代码"化：intent_rules 表加载（enabled=TRUE 按 priority 升序，携带 target_agent/target_channel 路由）+ 内置 DEFAULT_RULES 兜底 + refresh_interval 轮询热更新（reload_rules）（来源：SPEC §3.11 / 业务规格书 v6.29）
        - 消歧逻辑 _disambiguate：多规则命中按语义优先级选真实意图；动作/查询分类集合可训练（DISAMBIG-CFG.action_intents/query_intents 覆盖 _DEFAULT_ACTION_INTENTS/_DEFAULT_QUERY_INTENTS）（来源：SPEC §3.11.3 / 业务规格书 v6.46 C5）
        - 反馈训练机制：add_feedback（L1 纠错，v1.6.17 起审批制）、add_rule（L2 产出/LLM 建议待审批）、load_trained_rules（approved 样本加载）、get_feedback_stats（来源：SPEC §3.11.4 / 业务规格书 v6.29）
        - LLM 识别增强：_build_llm_prompt 注入 INTENT_DESCRIPTIONS 与操作上下文/对话历史；Function Calling/Structured Output 工具 schema 约束到 _known_intents() 动态白名单（来源：业务规格书 v6.33 / SPEC §3.11.3.1）
        - 意图参数提取统一委托 slot_engine.extract_slots（_extract_params 删除前 9 步硬编码正则，全链路同源可训练）（来源：业务规格书 v6.46 C1 / 模块拆分方案 契约3）
    对外接口（方法/API）：
        - Intent（@dataclass）：name/params/confidence/source/raw_input 结构——与 coordinator.py 的 Intent（name/channel/confidence/slots/target_agent）结构不同，本模块用于独立意图分类服务（来源：SPEC §3.11.1）
        - IntentRecognizer.__init__(rules=None, llm_client=None, database=None, refresh_interval=None)：构造识别器（来源：SPEC §3.11.1 / 业务规格书 v6.29）
        - IntentRecognizer.recognize(user_input, session_context=None, skip_llm=False, reasoning_callback=None) -> Intent：识别主入口（来源：SPEC §3.11.1 / 业务规格书 v6.78.3）
        - IntentRecognizer.add_rule(pattern, intent_name, ...) / add_feedback(user_input, recognized_intent, correct_intent, session_id=None) -> bool：训练反馈接口（来源：SPEC §3.11.1/3.11.4）
        - IntentRecognizer.load_trained_rules() -> int / get_feedback_stats() -> dict：加载 approved 训练规则与反馈统计（来源：SPEC §3.11.4）
        - IntentRecognizer.load_rules_from_db() -> int / reload_rules() -> int / get_rules_summary() / get_db_rules(include_disabled=False)：DB 规则加载/热更新/摘要（来源：业务规格书 v6.29）
        - looks_like_new_business_query(text) -> bool：pending 延续时区分"补充信息"与"新业务话题"（发散-收敛判定）（来源：业务规格书 v6.80 / 模块拆分方案 契约2 多轮状态契约）
        - KNOWN_INTENTS / INTENT_DESCRIPTIONS / DEFAULT_RULES / _DEFAULT_ACTION_INTENTS / _DEFAULT_QUERY_INTENTS：意图白名单/描述/内置规则/消歧分类默认集合（来源：SPEC §3.11.2/3.11.3 / 业务规格书 v6.46）
    错误处理要求：
        - LLM 调用失败或返回非法意图（不在已知列表内）：降级 unknown 意图，避免误路由（来源：SPEC §3.11.1）
        - DB 不可用或 intent_rules 表为空：降级内置 DEFAULT_RULES，保证离线可用（来源：SPEC §3.11 / 模块 docstring）
        - LLM 客户端未注入：仅规则匹配，其余回退 unknown（来源：模块 docstring 开源化说明）
        - 训练数据回填/规则写入无数据库：仅内存记录（add_feedback 无 DB 时仅记日志）（来源：SPEC §3.11.4 / 模块 docstring）
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


KNOWN_INTENTS = {
    "create_order", "query_order", "modify_order", "order_cancel",
    "query_inventory", "inventory_adjust", "stock_in", "stock_out",
    "financial_query", "financial_operation", "knowledge_query",
    "knowledge_management", "management_consulting",
    "data_analysis", "chitchat", "system_op", "unknown",
    "report_issue", "query_price", "work_report",
    "payroll", "attendance", "onboarding", "resignation", "org_query",
    "purchase", "return_order",
    "complaint", "workflow_start", "workflow_guide",
    # v6.13：扩充 DB 种子规则（migrations/009）已存在但 KNOWN_INTENTS 缺失的意图，
    # 避免 LLM Function Calling 返回这些意图时被强制改成 unknown（置信度降至 0.3）
    "contract",                  # 合同生成/查询（DB RULE-INT-004/005，路由 sales）
    "confirm", "cancel",         # 通用确认/取消（DB RULE-INT-050/051）
    "greeting", "thanks", "farewell", "help",  # 寒暄类（DB RULE-INT-052~055）
    "system",                    # 系统操作（DB RULE-INT-056，与 system_op 别名）
    "query_customer",            # 客户查询（DB RULE-INT-042）
    "query_overview",            # 数据总览（DB RULE-INT-045）
    "query_audit",               # 内审查询（DB RULE-INT-044）
}

# v6.46 C5：消歧"动作>查询"优先级分类的默认集合（DB 不可用时降级；
# 训练可通过 DISAMBIG-CFG.action_intents/query_intents 覆盖）
_DEFAULT_ACTION_INTENTS = {
    # 业务写操作
    "create_order", "modify_order", "order_cancel", "stock_in", "stock_out",
    "inventory_adjust", "purchase",
    "financial_operation", "work_report", "payroll",
    "onboarding", "resignation", "workflow_start",
    # 管理动作（非查询）
    "complaint",
    "return_order", "attendance", "org_query",
    "contract", "knowledge_management",
    # 通用确认/取消
    "confirm", "cancel",
    # v6.61：流程定义训练（写操作，优先级高于下单/采购裸词）
    "workflow_train",
}
_DEFAULT_QUERY_INTENTS = {
    "query_inventory", "query_order",
    "query_price", "query_customer", "knowledge_query",
    "management_consulting", "data_analysis",
    "financial_query", "query_overview", "query_audit",
    "report_issue",
    "workflow_guide",
    # 寒暄/系统
    "chitchat", "system_op", "greeting", "thanks", "farewell", "help", "system",
}

# v6.33：意图定义字典，注入 LLM prompt 提升分类准确率
INTENT_DESCRIPTIONS: Dict[str, str] = {
    "create_order": "用户想要下单、采购或订购产品",
    "query_order": "用户想要查询订单状态、进度或详情",
    "modify_order": "用户想要修改或变更订单",
    "order_cancel": "用户想要取消或退回订单",
    "query_inventory": "用户想要查询库存数量、现货情况",
    "inventory_adjust": "用户想要调整库存、盘盈盘亏",
    "stock_in": "用户想要入库或收货",
    "stock_out": "用户想要出库或发货",
    "financial_query": "用户想要对账、查应收应付、成本、利润、财务报表、欠款（财务口径核算与经营分析）",
    "financial_operation": "用户想要执行付款、收款操作",
    "knowledge_query": "用户想要查询资料、说明书、文档",
    "knowledge_management": "用户想要管理知识库、文档",
    "management_consulting": "用户想要管理咨询、制度建议",
    "data_analysis": "用户想要数据分析、报表、趋势",
    "chitchat": "用户在闲聊、问候",
    "system_op": "用户想要登录、切换用户、退出",
    "unknown": "无法识别用户意图",
    "report_issue": "用户想要报告问题、设备故障",
    "query_price": "用户想要报价、询价、查价格",
    "work_report": "用户想要报工、提交报工记录",
    "payroll": "用户想要查询工资、薪酬",
    "attendance": "用户想要查询考勤、打卡",
    "onboarding": "用户想要办理入职",
    "resignation": "用户想要办理离职",
    "org_query": "用户想要查询组织架构、人员列表",
    "purchase": "用户想要下采购单、管理供应商",
    "return_order": "用户想要退货",
    "complaint": "用户想要投诉、客诉",
    "workflow_start": "用户想要发起或启动流程",
    "workflow_guide": "用户想要查看流程列表、流程引导",
    "workflow_query": "用户想要查看既有流程单据或进度",
    "workflow_train": "用户想要训练/定义一个新流程（文本或PDF文档提取流程定义）",
    # v6.13：新增意图描述（与 KNOWN_INTENTS 扩充同步，注入 LLM prompt 提升分类准确率）
    "contract": "用户想要生成、起草、签订或查询合同",
    "confirm": "用户确认、同意或批准某操作",
    "cancel": "用户取消、放弃或不要某操作",
    "greeting": "用户在问候、打招呼",
    "thanks": "用户在表达感谢",
    "farewell": "用户在告别、离开",
    "help": "用户询问系统功能、求助或想了解能做什么",
    "system": "用户想要登录、切换用户、退出（system_op 别名）",
    "query_customer": "用户想要查询客户档案信息、信用额度、账期（客户基础资料维度，不含财务核算与欠款对账）",
    "query_overview": "用户想要查看数据总览、经营概况、工厂概况",
    "query_audit": "用户想要查询审计、内审、合规、操作记录",
}

# v6.67.5：确定性短语白名单——保留简短、确定、无歧义的词句作零延迟快速通道。
# 原则（用户指示：完全基于正则的语义识别不可用，移除；保留简短确定无歧义词句）：
#   1) 整句精确匹配（^...$）：仅当输入完全等于白名单短语时命中，杜绝
#      "查看下订单"含"下订单"子串这类主谓宾歧义（子串匹配一律不进入白名单）；
#   2) 仅收录语义完整、无歧义的词句（寒暄/系统/确认/明确业务短语）；
#   3) 其余开放表达全部交由 LLM + 训练数据主导识别。
_FAST_PHRASES: Dict[str, str] = {
    # 寒暄类（确定无歧义）
    "你好": "greeting", "您好": "greeting", "hi": "greeting", "hello": "greeting",
    "早上好": "greeting", "下午好": "greeting", "晚上好": "greeting", "在吗": "greeting",
    "哈喽": "greeting", "你们好": "greeting",
    "谢谢": "thanks", "感谢": "thanks", "多谢": "thanks", "thanks": "thanks",
    "辛苦了": "thanks", "谢谢你们": "thanks",
    "再见": "farewell", "拜拜": "farewell", "bye": "farewell", "回见": "farewell",
    "走了": "farewell", "下次见": "farewell",
    "你是谁": "help", "你能做什么": "help", "功能": "help", "帮助": "help",
    "怎么用": "help", "有什么用": "help", "你们能做什么": "help", "你能帮我什么": "help",
    # 系统操作（确定无歧义）
    "登录": "system_op", "退出": "system_op", "登出": "system_op", "注销": "system_op",
    "切换用户": "system_op", "我是谁": "system_op", "当前用户": "system_op",
    # 通用确认/取消（确定无歧义）
    "确认": "confirm", "同意": "confirm", "批准": "confirm", "没问题": "confirm",
    "就这样": "confirm", "可以": "confirm", "好的": "confirm", "行": "confirm",
    "取消": "cancel", "放弃": "cancel", "不要了": "cancel", "算了": "cancel",
    "不了": "cancel", "不用了": "cancel", "不需要": "cancel",
    # 订单：完整明确短语（整句匹配，避免"查看下订单"歧义）
    "下订单": "create_order", "下单": "create_order", "创建订单": "create_order",
    "新建订单": "create_order", "下一个订单": "create_order",
    "查订单": "query_order", "查看订单": "query_order", "查询订单": "query_order",
    "订单状态": "query_order", "订单进度": "query_order", "订单情况": "query_order",
    "订单详情": "query_order", "订单列表": "query_order", "所有订单": "query_order",
    "我的订单": "query_order", "订单编号": "query_order", "查一下订单": "query_order",
    # 库存（确定无歧义）
    "查库存": "query_inventory", "查一下库存": "query_inventory",
    "库存查询": "query_inventory", "查询库存": "query_inventory",
    "库存多少": "query_inventory", "库存情况": "query_inventory",
    "还有多少库存": "query_inventory", "还有多少现货": "query_inventory",
    # 主谓宾查询句（查看入库记录/查看排产计划等）由训练数据动态加入白名单
    # （v6.67.6：_load_trained_fast_phrases 从 training_data 加载查询意图样本），
    # 不硬编码——训练新增样本自动生效，规则配置不写死代码。
    # 报工/工资/考勤（确定无歧义）
    "报工": "work_report", "报工记录": "work_report", "提交报工": "work_report",
    "工资": "payroll", "薪酬": "payroll", "发工资": "payroll", "工资单": "payroll",
    "考勤": "attendance", "打卡": "attendance", "出勤": "attendance",
    # 财务（确定无歧义）
    "对账": "financial_query", "应收款": "financial_query", "应收账款": "financial_query",
    "财务报表": "financial_query", "利润": "financial_query",
    # 流程（确定无歧义）
    "发起流程": "workflow_start", "发起审批": "workflow_start", "启动流程": "workflow_start",
    "报销": "workflow_start", "申请报销": "workflow_start", "我要报销": "workflow_start",
    # 管理咨询高频词（整句=咨询意图，确定无歧义）
    "精益生产": "management_consulting", "怎么降本": "management_consulting",
    "如何降本": "management_consulting", "怎么提效": "management_consulting",
    "如何提效": "management_consulting", "管理建议": "management_consulting",
    "管理优化": "management_consulting", "流程制度": "management_consulting",
    # 知识查询（确定无歧义）
    "查询资料": "knowledge_query", "查资料": "knowledge_query", "看文档": "knowledge_query",
    "操作规程": "knowledge_query", "作业指导书": "knowledge_query",
}

# v6.67.6：训练数据动态白名单收录的查询意图。
# 仅查询意图（query_* 等）整句收录进白名单——执行意图样本
# （如补充信息"A-202"在下单流程中标注为 create_order）不收录：命中白名单会
# 打断 pending 多轮延续（skip_llm 时 fast 通道仍执行，误将槽位补充当新意图）。
# 查询句（查看入库记录/查看排产计划等）整句匹配无歧义，进白名单 100% 正确。
_TRAINED_FAST_INTENTS = frozenset({
    "query_inventory", "query_order",
    "financial_query",
    "query_price",
    "query_customer", "query_audit", "query_overview",
})

# v6.33：Function Calling 工具定义（Structured Output）
# reasoning 在前：LLM 先分析再给结论，准确率更高
_CLASSIFY_INTENT_TOOL = [{
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": "分析用户输入，识别其意图并返回分类结果",
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {
                    "type": "string",
                    "description": "分析过程：用户输入与上下文的关系、意图判断依据",
                },
                "intent": {
                    "type": "string",
                    "enum": sorted(KNOWN_INTENTS),
                    "description": "识别出的意图名称",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "置信度（0-1）",
                },
                "params": {
                    "type": "object",
                    "description": "从用户输入中提取的参数",
                    "properties": {},
                },
            },
            "required": ["reasoning", "intent", "confidence"],
        },
    },
}]


def _get_db(database: Any = None) -> Any:
    """获取数据库访问层实例（可选依赖，DB 不可用时降级返回 None）。

    prog.runtime 为主路径；备用裸 runtime 路径为原 agent-runtime-os 独立副本
    （该副本已取消，仅存历史兼容）。
    DB 不可用时降级返回 None（调用方需处理 None 跳过 DB 操作）。
    """
    if database is not None:
        return database
    get_db_fn = None
    try:
        from prog.runtime.database import get_database  # prog 项目
        get_db_fn = get_database
    except Exception:
        # W12：捕获 Exception（而非仅 ImportError）——prog.runtime.database 模块
        # 存在但导入时内部抛非 ImportError（如依赖缺失）也应降级尝试第二条路径
        try:
            from runtime.database import get_database  # 备用路径（历史 agent-runtime-os）
            get_db_fn = get_database
        except Exception:
            get_db_fn = None
    if get_db_fn is None:
        return None
    try:
        return get_db_fn()
    except Exception:
        return None


# ============================================================
# v6.80 意图漂移检测：pending 延续时区分"补充信息"与"新业务话题"
# ============================================================
# 多轮延续（pending_intent）仅应吞掉补充信息/追问（产品码、订单号、数量、
# "为什么/怎么"等零业务信号输入）；用户切换业务话题（含明确业务名词的新
# 查询，如 pending 下单收集中问"咱库存是不是快见底了"）必须脱离原意图、
# 交由强模型重新识别——否则新话题被 pending 吞并误路由（发散-收敛平衡：
# 发散=新话题走强模型，收敛=补充信息零延迟沿用原意图）。
_BUSINESS_TOPIC_WORDS = (
    "库存", "订单", "下单", "排产", "生产", "设备", "质量", "质检", "价格",
    "客户", "财务", "成本", "合同", "图纸", "工艺", "物料", "采购", "退货",
    "进度", "工单", "保养", "故障", "工资", "考勤", "报销", "流程",
    "效率", "OEE", "产能", "供应商", "产品", "在途", "合格率", "不良",
    "审计", "培训", "制度", "工单号", "交货",
    # v6.83：意图漂移覆盖补全——明确业务动作/名词（规则未命中时也能脱离 pending）。
    # 刻意排除：分析/原因（追问词）、检验/巡检/维修（字段值）、数据（泛化低信号）。
    "入库", "出库", "发货", "收货", "盘点", "离职", "入职",
    "报工", "排班", "产量", "单据", "总览", "检验记录",
)
# 字段值组合排除（v6.83/v6.84）："生产日期是2026-01-01"、"排产日期是8月15日"、
# "订单号是SOxxx" 等实为字段值回答（补充信息句式），不判为新话题——沿用 pending
# （wf_collecting 另有 explicit 保护，此处兜底非收集状态的 request_info 追问场景）。
# 须带"是/："（答案句式）才排除，避免把真实查询（"查看订单状态"）误判为补充信息。
_FIELD_VALUE_RE = re.compile(
    r"(?:生产|排产|订单|质检|合同|工单|采购|交货|发货|收货|入库|出库|报工|报销|交期|设备|报修|检验|测试|单)"
    r"(?:日期|批次|批号|数量|规格|型号|编号|时间|班组|线|工位|单号|金额|单价|状态|名称|结果|号)"
    r"\s*(?:是|:|：)"
)
# 纯槽位值（补充信息）：数量+单位 / SO/WO/PO+数字 / 产品码(A-202) / 日期 /
# 时间区间（3月到5月）/ 短标识
_SLOT_VALUE_RE = re.compile(
    r"^(?:\d+[套个件台元万元千元公斤张条]?|(?:SO|WO|PO)\d{6,}|"
    r"[A-Z]{1,3}-\d{2,}|[A-Za-z0-9]{2,16}|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]|\d{4}年\d{1,2}月|"
    r"\d{1,2}月到\d{1,2}月|\d{1,2}月\d{1,2}日到\d{1,2}月\d{1,2}日)$"
)
# v6.84：动词引导的分析请求——"帮我分析下这段数据/请统计一下产量"为 data_analysis
# 新话题，脱离 pending（"分析/统计"裸词是追问词刻意不列入 _BUSINESS_TOPIC_WORDS，
# 但带"帮我/请"等动作引导的完整请求句应判为新话题发散识别）。
_ANALYZE_VERB_RE = re.compile(r"(?:帮我|请|麻烦|给我|来|帮我做|帮我整).{0,8}(?:分析|统计|核算|汇总|算一下|算算)")


def looks_like_new_business_query(text: str) -> bool:
    """判断输入是否为切换业务话题的新查询（区别于 pending 补充信息/追问）。

    规则：
        1. 纯槽位值（A-202 / SO20260801001 / 100套 / 2026-08-15 / 3月到5月）
           → False（补充信息，沿用 pending）
        2. 字段值组合答案句式（"生产日期是…"等带是/：）→ False（补充信息）
        3. 动词引导的分析请求（"帮我分析…/请统计…"）→ True（新话题，脱离 pending）
        4. 含明确业务名词（库存/订单/排产/设备/质量/成本…）→ True（新话题）
        5. 其余短追问（"为什么失败"/"那结果呢"/"继续"）→ False（沿用 pending）

    供 chat.py 预识别 skip_llm 判定与 coordinator.route 延续复用判定使用。
    """
    t = (text or "").strip()
    if not t:
        return False
    if _SLOT_VALUE_RE.match(t):
        return False
    if _FIELD_VALUE_RE.search(t):
        return False
    if _ANALYZE_VERB_RE.search(t):
        return True
    return any(w in t for w in _BUSINESS_TOPIC_WORDS)


@dataclass
class Intent:
    """识别出的意图。

    属性：
        name: 意图名称（如 'create_order'）
        params: 从输入中提取的参数（如产品型号、数量、客户名）
        confidence: 置信度0~1
        source: 识别来源（'rule' 或 'llm'）
        raw_input: 原始用户输入（用于训练数据回填）
        channel: 路由通道（business/consulting/system，DB规则命中时携带）
        target_agent: 目标Agent（DB规则命中时携带，训练可调整路由）
    """
    name: str = "unknown"
    params: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    source: str = "rule"
    raw_input: str = ""
    channel: str = ""
    target_agent: str = ""


@dataclass
class _IntentRule:
    """内部编译后的意图规则。

    属性：
        compiled: 编译后的正则对象
        intent_name: 意图名称
        priority: 优先级（数字越小优先级越高）
        target_agent: 目标Agent（DB规则携带，内置规则为空）
        target_channel: 路由通道
        rule_id: DB规则ID（内置规则为空）
        source: 来源（db/builtin/memory）
    """
    compiled: Any
    intent_name: str
    priority: int = 50
    target_agent: str = ""
    target_channel: str = "business"
    rule_id: str = ""
    source: str = "builtin"


class IntentRecognizer:
    """意图识别器。

    设计意图：
        封装DB规则匹配、内置规则兜底与LLM辅助三条路径，对外暴露统一recognize入口。
        规则匹配延迟低、零成本；LLM兜底提升覆盖率。
        规则内容存储于 intent_rules 表，支持训练变更（L1反馈/L2产出/LLM建议，
        经审批后生效），DB不可用时降级为内置规则。

    属性：
        _rules: 有效规则列表（DB规则 + 内置兜底规则，按优先级排序）
        _db_rules: DB加载规则（enabled=TRUE，按 priority 升序）
        _builtin_rules: 内置兜底规则（DEFAULT_RULES 编译）
        _llm_client: LLM客户端（延迟注入，避免循环依赖）
    """

    # 高频意图正则规则（内置兜底，按业务覆盖优先级排序）
    # 注意：避免"库存"等单名词宽泛匹配导致的误识别问题，
    # 库存类规则仅匹配明确的查询意图短语，而非单独"库存"字样。
    DEFAULT_RULES = [
        # 动作意图优先（下单/采购等强动词）
        (r"(下[个张笔]?单|创建订单|新建订单|采购|订购|订货|想买|要买|下一笔订单|下个订单|下笔订单|下一个订单|订个货|开单|帮我订|要订货)", "create_order"),
        # v6.41：中缀产品型号的下单（"帮我下一笔A-202的订单"）
        (r"(下一笔|下一个|下个|帮我下|我要下|给我下).{0,10}订单", "create_order"),
        (r"(查订单|查看订单|查询订单|订单状态|订单进度|订单情况|订单详情|所有订单|订单列表|订单看板|我的订单|现有订单|有哪些订单|订单编号|全部订单|查一下订单)", "query_order"),
        # 查询某人订单（多轮语境：查一下张三的订单 / 看看李四的订单）
        (r"(查一下|查询|查查|看看).{0,10}的订单", "query_order"),
        # v6.41：按单号查状态（"查一下SO20260801001的状态"，SO/WO/PO + 数字）
        (r"(查一下|查询|查看|看看).{0,6}(SO|WO|PO|单号|订单号)?[A-Z]{0,2}\d{4,}.*(状态|进度|详情|情况)", "query_order"),
        # v6.67.3：纯订单号输入（无查询动词，如直接输入 SO20260801001）——
        # 规则层直接路由 query_order，避免走 LLM 兜底被幻觉为下单。
        # 边界：前后为非字母数字（^ 或非 [A-Za-z0-9]），防 SO 前缀混入普通词。
        (r"(?:^|[^A-Za-z0-9])(SO|WO|PO)\d{6,}(?:$|[^A-Za-z0-9])", "query_order"),
        # v6.67.4：主谓宾——句首查询动词 + "下订单"类动作短语 = 查询订单。
        # 根因："查看下订单/查看一下下订单/查一下下订单"中"下订单"是宾语
        #   （要查看的订单），不是下单动作；create_order 的"下订单"子串误匹配。
        #   句首查询动词（查/查看/查询/看看…）为谓语 → query_order。
        (r"(查一下|查询|查看|看看|查查|查看一下|查一查|查找|看下).{0,6}(下订单|订订单|已下订单|下过的订单|订过的订单|下单)", "query_order"),
        (r"(修改订单|改单|变更|追加|加单|加数量|改成|修改数量)", "modify_order"),
        (r"(取消.{0,2}订单|取消单子|退单|撤销订单|不要这个订单|取消这个单子|取消这笔订单)", "order_cancel"),
        # 库存查询：仅匹配明确表达查询库存的短语，
        # 避免单独"库存"字样命中（如"库存够不够，我想下单"不应判定为查库存）
        (r"(查库存|查一下库存|库存查询|库存多少|库存情况|现货情况|多少现货|还有多少现货|还有多少库存)", "query_inventory"),
        # v6.65.3：数值条件查询（"最多300的产品"/"不超过100的库存"）
        (r"(查|查询|查一下|查下|查查|查一查|查看|看看).{0,6}(最多|不超过|小于|低于|少于|大于|高于|超过|最少|至少).{0,4}\d+.{0,8}(产品|库存|物料|存货|商品)", "query_inventory"),
        (r"(库存调整|调整库存|盘盈|盘亏|库存修正|库存盘点)", "inventory_adjust"),
        (r"(入库|收货)", "stock_in"),
        (r"(出库|发货)", "stock_out"),
        (r"(对账|应收|财务|回款|应付|发票|账龄|财务报表|资金流)", "financial_query"),
        # v6.90：补联网向动词（搜一下/搜索/上网查/网上查/查资料…），"网上查一下XX"命中后走 process_stream 联网前置分支
        (r"(问一下|搜一下|搜索|上网查|网上查|查询资料|查资料|查一下资料|查查资料|说明书|文档|怎么办|怎么解决|有什么建议|怎么改善)", "knowledge_query"),
        # v6.41：成本优化咨询（"如何降低生产成本"）
        (r"(如何降低|怎么降低|降低成本|如何降本|怎么降本|降本增效|如何提高|怎么提升|如何提升|怎么提高|如何改善|怎么优化|如何优化|降本|提效)", "knowledge_query"),
        # v6.41：操作规程/作业指导书类文档查询（"查一下设备操作规程"）
        (r"(操作规程|作业指导书|操作手册|作业规范|工艺规范|安全规程|操作规范)", "knowledge_query"),

        (r"(停机|设备故障|维修|TPM|保养)", "report_issue"),  # 设备问题
        (r"(在制品|WIP|在制|半成品)", "query_inventory"),  # 在制品查询
        (r"(物料齐套|备料|齐套率)", "query_inventory"),  # 物料齐套
        (r"(交期|交付|交付期|准时交付|OTD)", "query_order"),  # 交期查询

        # 仓储物流类
        (r"(盘点|库存盘点|循环盘点)", "query_inventory"),  # 盘点
        (r"(安全库存|最低库存|再订货点)", "query_inventory"),  # 安全库存
        (r"(追溯|批次追溯|序列号追溯|Traceability)", "query_inventory"),  # 追溯

        # 财务成本类
        (r"(报价|询价|价格查询|多少钱|什么价格|价格是多少|售价多少)", "query_price"),  # 报价询价
        (r"(利润|利润率|毛利率|净利率)", "financial_query"),  # 利润分析
        (r"(付款条件|账期|信用期)", "financial_query"),  # 付款条件

        # 财务操作类（付款/收款等具体操作，与financial_query消歧）
        (r"(付款|收款|财务操作|付钱|收钱|开票|开发票)", "financial_operation"),  # 财务操作

        # 人力资源类
        (r"(报工|报工记录|提交报工|工时)", "work_report"),  # 报工管理
        (r"(工资|薪酬|计件工资|工资单|发工资)", "payroll"),  # 工资管理
        (r"(考勤|打卡|出勤|迟到|请假|加班)", "attendance"),  # 考勤管理
        (r"(入职|新员工|建档)", "onboarding"),  # 入职管理
        (r"(离职|辞职|交接|离职手续)", "resignation"),  # 离职管理
        (r"(组织架构|部门|人员列表|员工列表|组织结构)", "org_query"),  # 组织架构查询
        # v6.41：按部门查人数（"生产部有几个人"）
        (r"([\u4e00-\u9fa5]{1,4}部).{0,8}(几个人|多少人|人员名单|有几个人|人数)", "org_query"),

        # 采购/退货/客诉类
        # v6.47：purchase 规则增强——"采购X原料/材料/物料"属采购申请（INT-20->warehouse），
        #        与 create_order 的裸"采购"（客户下单语境）区分
        (r"(采购单|下采购单|供应商|采购订单|采购.{0,12}(原料|材料|物料|物资|耗材)|采购申请|请购|申购)", "purchase"),  # 采购管理
        (r"(退货|退货申请|退货单)", "return_order"),  # 退货管理
        (r"(客诉|投诉|客户投诉|质量问题投诉)", "complaint"),  # 客诉管理

        # 合同相关（v6.13：新增 contract 意图兜底，DB RULE-INT-004/005 优先）
        (r"(生成合同|起草合同|拟合同|签合同|签订合同|合同管理|合同模板|查合同|查询合同|我的合同|合同列表|合同状态|合同详情)", "contract"),
        (r"(生成|起草|拟|签).{0,20}合同", "contract"),
        # v6.41：查客户名下的合同（"查一下锐科的合同"）
        (r"(查一下|查看|看看|查询|有).{0,10}的合同", "contract"),

        # v6.59：费用报销流程直接动词触发——"我要报销X元差旅费"等表达
        # 优先于 query_customer 泛词（"客户现场验收"含"客户"两字会误判），
        # 句首位置权重（规则1）与 priority 双保险保证命中 workflow_start
        (r"(我要?报销|帮我报销|请帮我报销|申请报销|提交报销).{0,40}?(?:元|差旅|餐费|交通|住宿|招待|出差|会议|办公|费)", "workflow_start"),
        (r"(报销|费用报销).{0,8}(?:元|差旅费|餐费|交通费|住宿费|招待费|办公费|会议费)", "workflow_start"),

        # v6.60：流程实例查询——"显示刚才报销流程内容/实例12/报销进度"
        # 查询动词+流程词（优先于纯流程名触发 workflow_start 的误判），
        # "实例N"、"流程/审批+内容/进度/详情" 均为查看既有单据而非发起新流程
        (r"(显示|查看|查询|查一下|看看|查查|打开|调出|翻出|找一下|找找|请显示|帮我查).{0,12}(?:流程|审批|报销|实例|工作流|单据).{0,10}(?:内容|详情|信息|进度|状态|记录|历史)?", "workflow_query"),
        (r"(?:实例|编号|单号)\s*[#]?(\d+)", "workflow_query"),
        (r"(流程|审批|报销|工单|单据)(?:内容|详情|进度|状态|记录|历史|信息)", "workflow_query"),

        # v6.61：流程定义训练申请——"训练/创建/定义流程"或"把PDF做成流程"
        # 由知识助手收集流程定义（文本描述或 PDF 附件提取）并提交训练审批
        (r"(训练|创建|新建|定义|设计|定制|配置|制作).{0,10}(流程|审批流程|工作流|审批单)", "workflow_train"),
        (r"(流程|审批流程|工作流).{0,8}(训练|定义|创建|新建|定制|设计)", "workflow_train"),
        (r"(把|用|根据|按|依据).{0,8}(这份|这个|该|一下)?(pdf|PDF|文档|文件|附件|制度|模板).{0,12}(训练|做成|定义|创建|生成|制定).{0,10}(流程|审批|工作流)", "workflow_train"),

        # 客户查询类（v6.13：新增 query_customer 兜底）
        (r"(客户|信用|额度|账期|欠款|应收|客户信息)", "query_customer"),

        # 知识管理类
        (r"(知识管理|知识库|文档管理|经验库)", "knowledge_management"),  # 知识管理

        # 流程启动与引导类
        (r"(发起流程|启动流程|发起审批|开始流程|提交申请|发起一个.*流程|发起.*审批流程|发起.{0,12}?(?:流程|审批)|申请.{0,12}?(?:报销|流程|审批)|提交.{0,12}?(?:报销|审批|流程申请))", "workflow_start"),  # 流程启动（v6.43：泛化任意流程名）
        (r"(流程列表|可发起什么|有哪些流程|流程引导|能发起什么流程)", "workflow_guide"),
        (r"(管理咨询|管理制度|流程制度|管理建议|管理优化|如何管理|怎么管理)", "management_consulting"),
        (r"(数据分析|数据统计|数据报表|经营分析|报表分析|趋势分析)", "data_analysis"),
        # v6.41：经营数据总览（"这个月经营数据怎么样"）
        (r"(经营数据|经营情况|经营状况|本月数据|月度数据|经营数据怎么样|数据总览|经营概况|经营指标|销售数据|订单数据|产值数据|产量数据|库存数据|财务数据)", "query_overview"),
        # 寒暄类细分（v6.13：与 DB RULE-INT-052~055 对齐，chitchat 仍作兜底）
        (r"(你好|您好|hi|hello|早上好|下午好|晚上好|在吗|哈喽)", "greeting"),
        (r"(谢谢|感谢|多谢|thanks|辛苦了)", "thanks"),
        (r"(再见|拜拜|bye|走了|回见)", "farewell"),
        (r"(你是谁|你能做什么|功能|帮助|怎么用|有什么用)", "help"),
        (r"(登录|切换|谁在用|退出|我是谁|当前用户|登出|注销)", "system_op"),
        # 通用确认/取消（v6.13：与 DB RULE-INT-050/051 对齐）
        (r"(确认|同意|批准|执行|提交|没问题|就这样)", "confirm"),
        (r"(取消|放弃|不要了|算了|不了|不用了)", "cancel"),
    ]

    # DB降级种子规则（镜像 migrations/009_intent_rules.sql 的56条种子）
    # DB不可用时作为兜底，确保降级模式仍有完整路由信息（target_agent/target_channel/priority）
    # 格式: (pattern, intent_name, target_agent, target_channel, priority)
    SEED_RULES_FALLBACK = [
        # 销售：订单/合同（动作优先, priority 10~13）
        (r"(下[个张笔]?单|(?<![查看找询一])下订单|下一笔订单|下个订单|下笔订单|创建订单|新建订单|订个货|帮我订|要订货|采购|订购|订货|开单|想买|要买)", "create_order", "sales", "business", 10),
        (r"(下一笔|下一个|下个|帮我下|我要下|给我下).{0,10}订单", "create_order", "sales", "business", 10),
        (r"(修改订单|改单|变更|追加|加单|加数量|改成|修改数量)", "modify_order", "sales", "business", 11),
        (r"(取消.{0,2}订单|取消单子|退单|撤销订单|不要这个订单)", "order_cancel", "sales", "business", 12),
        (r"(生成合同|起草合同|拟合同|签合同|签订合同|合同管理|合同模板|查合同|查询合同|我的合同|合同列表|合同状态|合同详情)", "contract", "sales", "business", 13),
        (r"(生成|起草|拟|签).{0,20}合同", "contract", "sales", "business", 13),
        (r"(查一下|查看|看看|查询|有).{0,10}的合同", "contract", "sales", "business", 13),
        # 仓储：出入库/库存调整（priority 15）
        (r"(入库|收货)", "stock_in", "warehouse", "business", 15),
        (r"(出库|发货)", "stock_out", "warehouse", "business", 15),
        (r"(库存调整|调整库存|盘盈|盘亏|库存修正|库存盘点)", "inventory_adjust", "warehouse", "business", 15),
        (r"(停机|设备故障|维修|TPM|保养)", "report_issue", "production", "business", 16),
        # 采购/退货/客诉（priority 17）
        # v6.47：增强"采购X原料/材料/物料"->purchase（INT-20），与 create_order 裸"采购"区分
        (r"(采购单|下采购单|供应商|采购订单|采购.{0,12}(原料|材料|物料|物资|耗材)|采购申请|请购|申购)", "purchase", "warehouse", "business", 17),
        (r"(退货|退货申请|退货单)", "return_order", "sales", "business", 17),
        (r"(客诉|投诉|客户投诉|质量问题投诉)", "complaint", "qc", "business", 17),
        # 财务操作（priority 18）
        (r"(付款|收款|开票|开发票|财务操作|付钱|收钱)", "financial_operation", "finance", "business", 18),
        # HR（priority 19）
        (r"(报工|工时|报工记录|提交报工)", "work_report", "hr", "business", 19),
        (r"(工资|薪酬|计件工资|工资单|发工资)", "payroll", "hr", "business", 19),
        (r"(考勤|打卡|出勤|迟到|请假|加班)", "attendance", "hr", "business", 19),
        (r"(入职|新员工|建档)", "onboarding", "hr", "business", 19),
        (r"(离职|辞职|交接|离职手续)", "resignation", "hr", "business", 19),
        (r"(组织架构|部门|人员列表|员工列表|组织结构)", "org_query", "hr", "business", 19),
        (r"([\u4e00-\u9fa5]{1,4}部).{0,8}(几个人|多少人|人员名单|有几个人|人数)", "org_query", "hr", "business", 19),
        # 流程启动/引导（priority 20）
        # v6.59：费用报销直接动词触发（与 DEFAULT_RULES 同步），优先于 query_customer 泛词
        (r"(我要?报销|帮我报销|请帮我报销|申请报销|提交报销).{0,40}?(?:元|差旅|餐费|交通|住宿|招待|出差|会议|办公|费)", "workflow_start", "knowledge", "business", 20),
        (r"(报销|费用报销).{0,8}(?:元|差旅费|餐费|交通费|住宿费|招待费|办公费|会议费)", "workflow_start", "knowledge", "business", 20),
        (r"(发起流程|启动流程|发起审批|开始流程|提交申请|发起一个.*流程|发起.*审批流程|发起.{0,12}?(?:流程|审批)|申请.{0,12}?(?:报销|流程|审批)|提交.{0,12}?(?:报销|审批|流程申请))", "workflow_start", "knowledge", "business", 20),
        # v6.60：流程实例查询（priority 25，优先于"进度"类裸词
        # 与 query_customer 泛词；与 DEFAULT_RULES 同步，路由到知识助手查既有单据）
        (r"(显示|查看|查询|查一下|看看|查查|打开|调出|翻出|找一下|找找|请显示|帮我查).{0,12}(?:流程|审批|报销|实例|工作流|单据).{0,10}(?:内容|详情|信息|进度|状态|记录|历史)?", "workflow_query", "knowledge", "business", 25),
        (r"(?:实例|编号|单号)\s*[#]?(\d+)", "workflow_query", "knowledge", "business", 25),
        (r"(流程|审批|报销|工单|单据)(?:内容|详情|进度|状态|记录|历史|信息)", "workflow_query", "knowledge", "business", 25),
        # v6.61：流程定义训练申请（priority 8 高于 create_order 的"采购"裸词，
        # 避免"训练一个采购审批流程"被下单意图抢占；路由知识助手；与 DEFAULT_RULES 同步）
        (r"(训练|创建|新建|定义|设计|定制|配置|制作).{0,10}(流程|审批流程|工作流|审批单)", "workflow_train", "knowledge", "business", 8),
        (r"(流程|审批流程|工作流).{0,8}(训练|定义|创建|新建|定制|设计)", "workflow_train", "knowledge", "business", 8),
        (r"(把|用|根据|按|依据).{0,8}(这份|这个|该|一下)?(pdf|PDF|文档|文件|附件|制度|模板).{0,12}(训练|做成|定义|创建|生成|制定).{0,10}(流程|审批|工作流)", "workflow_train", "knowledge", "business", 8),
        (r"(流程列表|可发起什么|有哪些流程|流程引导|能发起什么流程)", "workflow_guide", "knowledge", "consulting", 20),
        # 查询意图（priority 30~36）
        (r"(查订单|查看订单|查询订单|订单状态|订单进度|订单情况|订单详情|所有订单|订单列表|订单看板|我的订单|我下的单|查一下我的订单|现有订单|现在的订单|有哪些订单|订单有哪些|订单编号|全部订单|查一下订单)", "query_order", "sales", "business", 30),
        (r"(查一下.+的订单|查询.+的订单|查.+的订单|看看.+的订单)", "query_order", "sales", "business", 30),
        (r"(查一下|查询|查看|看看).{0,6}(SO|WO|PO|单号|订单号)?[A-Z]{0,2}\d{4,}.*(状态|进度|详情|情况)", "query_order", "sales", "business", 30),
        # v6.67.3：纯订单号输入（无查询动词，如直接输入 SO20260801001）→ query_order
        (r"(?:^|[^A-Za-z0-9])(SO|WO|PO)\d{6,}(?:$|[^A-Za-z0-9])", "query_order", "sales", "business", 30),
        # v6.67.4：主谓宾——句首查询动词 + "下订单"类动作短语 = 查询订单
        (r"(查一下|查询|查看|看看|查查|查看一下|查一查|查找|看下).{0,6}(下订单|订订单|已下订单|下过的订单|订过的订单|下单)", "query_order", "sales", "business", 30),
        (r"(查库存|查一下库存|库存查询|查询库存|库存多少|库存情况|现货情况|还有多少|剩多少|有没有货|备货情况|看看库存|有没有库存)", "query_inventory", "warehouse", "business", 31),
        (r"(查.*库存|库存.*多少|库存.*情况)", "query_inventory", "warehouse", "business", 31),
        # v6.65.3：数值条件查询（"最多300的产品"/"不超过100的库存"/"大于1000的物料"）
        (r"(查|查询|查一下|查下|查查|查一查|查看|看看).{0,6}(最多|不超过|小于|低于|少于|大于|高于|超过|最少|至少).{0,4}\d+.{0,8}(产品|库存|物料|存货|商品)", "query_inventory", "warehouse", "business", 31),
        (r"(在制品|WIP|在制|半成品|物料齐套|备料|齐套率|盘点|循环盘点|安全库存|最低库存|再订货点|追溯|批次追溯|序列号追溯|Traceability)", "query_inventory", "warehouse", "business", 31),
        # v6.93.1 T18：口语化库存状态问句（与 DEFAULT_RULES 同步，降级模式带路由）
        (r"库存.{0,6}(快见底|见底了|快没|快用完|快用光|还够吗|还够不够|够不够|够用吗|还多吗|充足吗|缺货|断货|会不会断|能撑|剩多少|还有多少)", "query_inventory", "warehouse", "business", 31),
        (r"(多少钱|价格多少|报价|售价|单价|询价|价格查询|什么价格|价格是多少|售价多少)", "query_price", "sales", "business", 34),
        (r"(客户|信用|额度|账期|欠款|应收|客户信息)", "query_customer", "sales", "business", 34),
        (r"(对账|应收|应付|财务|回款|发票|利润|毛利|净利润|账龄|财务报表|资金流|利润率|毛利率|净利率|付款条件|信用期)", "financial_query", "finance", "business", 35),
        (r"(审计|内审|查账|审核记录|合规|日志|操作记录|违规|越权)", "query_audit", "audit", "business", 36),
        (r"(数据总览|数据看板|经营概况|工厂概况|整体情况|数据汇总|总览|概览)", "query_overview", "sales", "business", 36),
        (r"(经营数据|经营情况|经营状况|本月数据|月度数据|经营数据怎么样|数据总览|经营概况|经营指标|销售数据|订单数据|产值数据|产量数据|库存数据|财务数据)", "query_overview", "sales", "business", 36),
        # 咨询/知识/系统（priority 40~47）
        (r"(知识管理|知识库|文档管理|经验库)", "knowledge_management", "knowledge", "consulting", 40),
        (r"(问一下|搜一下|搜索|上网查|网上查|查询资料|查资料|查一下资料|查查资料|说明书|文档|怎么办|怎么解决|有什么建议|怎么改善)", "knowledge_query", "knowledge", "consulting", 41),
        (r"(如何降低|怎么降低|降低成本|如何降本|怎么降本|降本增效|如何提高|怎么提升|如何提升|怎么提高|如何改善|怎么优化|如何优化|降本|提效)", "knowledge_query", "knowledge", "consulting", 41),
        (r"(操作规程|作业指导书|操作手册|作业规范|工艺规范|安全规程|操作规范)", "knowledge_query", "knowledge", "consulting", 41),
        (r"(管理咨询|管理制度|流程制度|管理建议|管理优化|如何管理|怎么管理)", "management_consulting", "knowledge", "consulting", 42),
        # v6.41：管理咨询高频词镜像（与 DEFAULT_RULES v6.40 ANCHOR 同步，降级模式带路由）
        (r"(精益|精益生产|丰田生产方式|TPS|JIT|准时化|拉动生产|看板生产|一个流|单件流|连续流)", "management_consulting", "knowledge", "consulting", 42),
        (r"(5S|6S|7S|8S|整理|整顿|清扫|清洁|素养|安全|节约|学习)", "management_consulting", "knowledge", "consulting", 42),
        (r"(全面质量管理|TQM|QC七大手法|新QC七大手法|质量屋|QFD|质量机能展开)", "management_consulting", "knowledge", "consulting", 42),
        (r"(PDCA|戴明环|SDCA|持续改善|Kaizen|改善提案|小集团活动|QCC|品管圈)", "management_consulting", "knowledge", "consulting", 42),
        (r"(ISO|ISO9000|ISO9001|ISO14001|IATF16949|ISO.*体系|体系认证|内审|外审|管理评审)", "management_consulting", "knowledge", "consulting", 42),
        (r"(看板|Kanban|安灯|Andon|安东|快速换模|SMED|全员生产维护|TPM|防错|Pokayoke|5Why|五个为什么|鱼骨图|特性要因图|帕累托|柏拉图|VSM|价值流图|价值流分析|OEE|设备综合效率|稼动率|线平衡|生产节拍|Takt Time|标准作业|SOP|作业标准|标准工时|平准化|混流生产|多能工|细胞生产|Cell|工序分割|搬运改善|超市|水蜘蛛|Mizusumashi)", "management_consulting", "knowledge", "consulting", 42),
        (r"(六西格玛|6σ|6Sigma|DMAIC|DFSS|黑带|绿带|SBTI|精益六西格玛)", "management_consulting", "knowledge", "consulting", 42),
        (r"(数据分析|数据统计|数据报表|经营分析|报表分析|趋势分析)", "data_analysis", "knowledge", "consulting", 42),
        (r"(确认|同意|批准|执行|提交|没问题|就这样)", "confirm", "", "business", 45),
        (r"(取消|放弃|不要了|算了|不了|不用了)", "cancel", "", "business", 45),
        (r"(你好|您好|hi|hello|早上好|下午好|晚上好|在吗|哈喽)", "greeting", "knowledge", "consulting", 46),
        (r"(谢谢|感谢|多谢|thanks|辛苦了)", "thanks", "knowledge", "consulting", 46),
        (r"(再见|拜拜|bye|走了|回见)", "farewell", "knowledge", "consulting", 46),
        (r"(你是谁|你能做什么|功能|帮助|怎么用|有什么用)", "help", "knowledge", "consulting", 46),
        (r"(登录|切换|谁在用|退出|我是谁|当前用户|登出|注销)", "system", "knowledge", "business", 47),
    ]

    def __init__(self, rules: Optional[list] = None, llm_client: Any = None,
                 database: Any = None, refresh_interval: float = 5.0) -> None:
        """初始化意图识别器（四层规则 + LLM 客户端装配）。

        参数：
            rules: 自定义规则列表（[(pattern, intent_name, ...)]）；传入时
                   覆盖内置 DEFAULT_RULES 兜底且禁用确定性短语快速通道
                   （自定义规则与默认规则集语义互斥，白名单属默认集）；
                   为 None 时加载 内置兜底 + DB 降级种子规则 + DB 规则。
            llm_client: LLM 客户端（延迟注入，供 LLM 主导识别/few-shot/
                   Function Calling 使用；None 时纯规则离线识别）
            database: 数据库访问层（启动时 load_rules_from_db 加载 DB 规则；
                   DB 规则含 L1/L2 训练规则与 priority，表空/不可用时静默
                   降级种子规则+内置兜底）
            refresh_interval: DB 规则/训练数据动态白名单的刷新间隔（秒），
                   进程内热更新（超时窗口内不再重复查库）
        """
        # 编译内置兜底规则：每条规则为 _IntentRule 对象
        raw_builtin = rules if rules is not None else self.DEFAULT_RULES
        self._builtin_rules = []
        for rule in raw_builtin:
            pattern, intent_name = rule[0], rule[1]
            self._builtin_rules.append(_IntentRule(
                compiled=re.compile(pattern, re.IGNORECASE),
                intent_name=intent_name,
                source="builtin",
            ))

        # v6.67.5：自定义规则（rules 参数传入）时禁用确定性短语快速通道，
        # 与"自定义规则时默认规则不再生效"语义保持一致（白名单属于默认规则集）。
        self._fast_phrases_enabled = rules is None

        # 编译DB降级种子规则（含完整路由信息，镜像009_intent_rules.sql）
        # DB不可用时替代DB规则，确保降级模式仍有target_agent/target_channel/priority
        # 仅使用默认规则时启用种子降级；自定义rules时不加载（避免测试/定制场景干扰）
        self._seed_fallback_rules = []
        if rules is None:
            for rule in self.SEED_RULES_FALLBACK:
                pattern, intent_name = rule[0], rule[1]
                target_agent = rule[2] if len(rule) > 2 else ""
                target_channel = rule[3] if len(rule) > 3 else "business"
                priority = rule[4] if len(rule) > 4 else 50
                self._seed_fallback_rules.append(_IntentRule(
                    compiled=re.compile(pattern, re.IGNORECASE),
                    intent_name=intent_name,
                    priority=priority,
                    target_agent=target_agent,
                    target_channel=target_channel,
                    source="seed",
                ))

        # DB规则（DB优先，启动时加载；失败降级为种子规则+内置兜底）
        self._db_rules: list = []
        self._rules: list = list(self._seed_fallback_rules) + list(self._builtin_rules)
        self._db_rules_ok = False
        self._database = database
        self._refresh_interval = float(refresh_interval)
        self._db_last_check = 0.0

        # LLM客户端（延迟注入，避免循环依赖）
        self._llm_client = llm_client

        # v6.67.6：训练数据动态白名单（approved 查询意图样本整句匹配，
        # 训练加入白名单——不硬编码规则，训练新增样本自动生效）
        self._trained_fast_phrases: Dict[str, str] = {}
        self._trained_phrases_at = 0.0

        # 启动加载 DB 规则（表空/DB不可用时静默降级内置兜底）
        self.load_rules_from_db()

    def recognize(self, user_input: str,
                 session_context: Optional[Dict[str, Any]] = None,
                 skip_llm: bool = False,
                 reasoning_callback: Optional[Any] = None) -> Intent:
        """主识别方法（v6.67.5：LLM+训练数据主导）。

        参数：
            user_input: 用户自然语言输入
            session_context: 会话上下文（用于多轮指代消解，如"它""那个订单"）
            skip_llm: 跳过LLM回退（多轮延续时仅做规则匹配，避免补充信息触发LLM延迟）
            reasoning_callback: v6.78.3 可选回调 callable(str)——LLM 思考过程
                （reasoning_content）逐块回调，用于前端实时展示"思考中"（双模型
                架构：识别强模型 thinking 开启）。为 None 时不使用流式调用，
                保持原有非流式 chat 行为。

        返回：
            Intent对象；规则与LLM均未命中时返回 name='unknown'。

        识别链路（v6.67.5 重构：移除"正则语义识别"主导，改为）：
            1. 确定性短语快速通道（_FAST_PHRASES 整句精确匹配，零延迟、零歧义）
            2. LLM + 训练数据主导（few-shot 注入 approved 训练样本，Function Calling
               结构化输出；主谓宾等语义歧义在此层解决）
            3. 规则兜底（DB 规则 + 内置规则，仅 LLM 不可用/失败时降级，保证离线可用）
        """
        if not user_input or not user_input.strip():
            return Intent(name="unknown", confidence=0.0, raw_input=user_input or "")

        # 多轮指代消解：检测代词并用会话上下文中的实体替换
        resolved_input = self._resolve_pronouns(user_input, session_context)

        # 输入归一化
        normalized = self._normalize(resolved_input)

        # 第一步：DB 高优先级训练规则（priority<10，L1反馈/L2产出/显式训练，
        # 用户显式训练的正确意图最高优先，不受白名单/LLM 抢占）
        high_pri = self._db_high_pri_match(normalized)
        if high_pri is not None:
            high_pri.raw_input = user_input
            high_pri.params = self._extract_params(user_input)
            return high_pri

        # 第二步：确定性短语快速通道（整句精确匹配，零延迟、零歧义）
        fast_intent = self._fast_phrase_match(normalized) if self._fast_phrases_enabled else None
        if fast_intent is not None:
            # 白名单命中时补充 DB 同意图路由（DB 规则优先原则，训练可调整路由）；
            # DB 规则同样命中该输入时视为规则命中（source='rule'）
            db_route = self._db_route_for(fast_intent.name)
            if db_route:
                fast_intent.target_agent, fast_intent.channel = db_route
                fast_intent.source = "rule"
            fast_intent.raw_input = user_input
            # 尝试从原始输入中提取参数
            fast_intent.params = self._extract_params(user_input)
            return fast_intent

        # 第三步：LLM + 训练数据主导（有 LLM 客户端时）
        if self._llm_client is not None and not skip_llm:
            # v6.85 性能：确定性规则预检先于 LLM——
            # 高频动作/查询句式（"帮我下个单"/"查一下B-305的库存"）规则层
            # 零延迟命中，避免走意图识别强模型（thinking enabled）造成
            # 秒级延迟（实测"帮我下个单"LLM 推理 25s+）。守卫：动作类意图
            # 且句首为查询动词时跳过（"查看一下下订单"主谓宾歧义保留 LLM
            # 主导，v6.67.4/5 修复不变）；查询类/无歧义动作类直接命中。
            pre_intent = self._deterministic_rule_precheck(normalized)
            if pre_intent is not None:
                pre_intent.raw_input = user_input
                pre_intent.params = self._extract_params(user_input)
                return pre_intent
            intent = self._llm_based_match(user_input, session_context,
                                           reasoning_callback=reasoning_callback)
            if intent.name != "unknown":
                intent.raw_input = user_input
                # W1：合并——slot_engine 规则提取优先，LLM 结构化输出（params）
                # 补充缺失字段（原实现直接覆盖，LLM 提取的参数全部丢失）
                intent.params = self._merge_params(
                    self._extract_params(user_input), intent.params)
                return intent
            # LLM 识别为 unknown 时继续走规则兜底，保证离线/异常降级可用

        # v6.67.5.1：skip_llm（多轮延续）时纯订单号视为补充信息——
        # 下单流程要求订单号时用户回复 SO20260801001 应沿用 pending 意图
        # （如下单流程收集订单号），不被纯订单号规则误判为
        # query_order 而中断多轮流程；无 pending（skip_llm=False）时该规则
        # 仍生效（直接输入订单号查询订单）。与 coordinator 降级路径 L1057
        # `if not skip_llm` 跳过 INTENT_REGEXES 的语义保持一致。
        # 注意：normalized 已将输入小写化，此处须 IGNORECASE 匹配（SO→so）。
        if skip_llm and re.search(
                r"(?:^|[^a-z0-9])(SO|WO|PO)\d{6,}(?:$|[^a-z0-9])",
                normalized, re.IGNORECASE):
            return Intent(name="unknown", confidence=0.0, raw_input=user_input)

        # 第四步：规则兜底（DB规则 + 内置规则，离线可用；不再作为语义识别主导）
        # v6.67.5 修复：skip_llm（多轮补充信息）也须先走规则匹配——补充信息如
        # "查一下SO20260801001的状态"含明确查询动词，应命中 query_order 规则，
        # 使 coordinator 不沿用上一轮 pending（create_order 待 quantity）误下单；
        # 补充信息（A-202/100套 等纯槽位）不命中规则 → unknown → pending 延续（零延迟）
        intent = self._rule_based_match(normalized)
        if intent is not None:
            intent.raw_input = user_input
            intent.params = self._extract_params(user_input)
            return intent

        # skip_llm：多轮延续时规则未命中直接返回unknown，由coordinator沿用pending_intent
        if skip_llm:
            return Intent(name="unknown", confidence=0.0, raw_input=user_input)

        return Intent(name="unknown", confidence=0.0, raw_input=user_input)

    def _db_high_pri_match(self, text: str) -> Optional[Intent]:
        """DB 高优先级训练规则匹配（priority<10，L1反馈/L2产出/显式训练）。

        v6.67.5：用户显式训练/反馈产生的精确规则（priority<10）具有最高优先级，
        不受确定性短语白名单与 LLM 抢占——训练修正必须即时生效。

        Args:
            text: 已归一化的输入

        Returns:
            Intent（source='rule'，携带 DB 路由）；未命中返回 None。
        """
        for rule in self._rules:
            # W9：priority int 归一防御（兼容 str 类型 priority）
            if getattr(rule, "source", "") != "db" or int(rule.priority) >= 10:
                continue
            if rule.compiled.search(text):
                return Intent(
                    name=rule.intent_name,
                    confidence=0.95,
                    source="rule",
                    channel=rule.target_channel,
                    target_agent=rule.target_agent,
                )
        return None

    def _db_route_for(self, intent_name: str) -> Optional[tuple]:
        """查询 DB 规则中指定意图的路由（target_agent, target_channel）。

        白名单/快速通道命中时补充 DB 训练路由，保持"DB 规则优先、训练可调整路由"。

        Args:
            intent_name: 意图名

        Returns:
            (target_agent, target_channel) 或 None
        """
        for rule in self._rules:
            if getattr(rule, "source", "") == "db" and rule.intent_name == intent_name:
                if rule.target_agent:
                    return rule.target_agent, rule.target_channel or "business"
        return None

    def _load_trained_fast_phrases(self, force: bool = False) -> None:
        """从训练数据加载查询意图样本为整句白名单（v6.67.6：白名单由训练加入）。

        从 training_data 表读取 approved=TRUE 的意图识别样本，仅收录查询意图
        （_TRAINED_FAST_INTENTS）——执行意图样本不收录：补充信息（如"A-202"在
        下单流程中标注为 create_order）不应命中白名单打断 pending 多轮延续；
        查询句（查看入库记录/查看排产计划等）整句匹配无歧义，进白名单 100% 正确，
        绕开 LLM 分类随机性。带刷新间隔缓存（_refresh_interval），训练数据
        变化后自动热更新，无需重启。

        Args:
            force: 强制重新加载（忽略刷新间隔）
        """
        now = time.time()
        if (self._trained_fast_phrases and not force
                and now - self._trained_phrases_at < self._refresh_interval):
            return
        phrases: Dict[str, str] = {}
        try:
            db = _get_db(self._database)
            rows = db.query_many(
                "training_data",
                filters={"agent_type": "intent_recognizer", "approved": True},
                order_by="id DESC",
            ) or []
            for row in rows:
                intent = row.get("intent")
                user_input = (row.get("user_input") or "").strip()
                if not intent or not user_input or intent == "unknown":
                    continue
                if intent not in _TRAINED_FAST_INTENTS:
                    continue
                phrases.setdefault(user_input, intent)
        except Exception:
            return
        self._trained_fast_phrases = phrases
        self._trained_phrases_at = now

    def _fast_phrase_match(self, text: str) -> Optional[Intent]:
        """确定性短语快速通道：整句精确匹配白名单，零延迟、零歧义。

        v6.67.5：用户指示"完全基于正则的语义识别不可用，移除；保留简短的、
        确定无歧义的正则词句"。白名单仅整句匹配（^...$），杜绝"查看下订单"
        含"下订单"子串被误判为主语动作的主谓宾歧义。
        v6.67.6：白名单由训练加入——静态 _FAST_PHRASES（基础无歧义词）之外，
        从 training_data 加载 approved 查询意图样本作为动态整句白名单，
        训练新增查询样本自动生效（查看入库记录/查看排产计划等 100% 正确）。

        Args:
            text: 已归一化的输入（全角转半角、小写、去空白）

        Returns:
            Intent（source='fast'）；未命中返回 None。
        """
        key = text.strip()
        intent_name = _FAST_PHRASES.get(key)
        if intent_name is None:
            # v6.67.6：训练数据动态白名单（查询意图样本）
            self._load_trained_fast_phrases()
            intent_name = self._trained_fast_phrases.get(key)
        if intent_name is None:
            return None
        # I19：寒暄/查询类意图默认走 consulting 通道（greeting/thanks/help 等
        # 原硬编码 business 无意义；DB 规则命中时会由 _db_route_for 覆盖为训练路由）
        channel = "consulting" if intent_name in _DEFAULT_QUERY_INTENTS else "business"
        return Intent(
            name=intent_name,
            confidence=0.95,
            source="fast",
            channel=channel,
        )

    def _resolve_pronouns(self, user_input: str, session_context: Optional[Dict]) -> str:
        """多轮指代消解：检测代词并用会话上下文中的实体替换。

        当用户输入包含"它/那个/这个/上面的/刚才的"等代词时，
        从 session_context 中提取上一轮的槽位（产品型号/订单号/客户名等），
        将代词替换为具体实体，使规则匹配能正确命中。

        Args:
            user_input: 原始用户输入
            session_context: 会话上下文（含 slots/history/last_intent）

        Returns:
            str: 消解后的输入（无代词或无上下文时原样返回）
        """
        if not session_context or not isinstance(session_context, dict):
            return user_input

        # 代词检测（仅短输入或含明确代词时触发）
        pronouns = ["它", "那个", "这个", "上面的", "刚才的", "之前那个", "之前的", "该"]
        has_pronoun = any(p in user_input for p in pronouns)
        if not has_pronoun:
            return user_input

        # 从上下文中提取可用实体
        slots = session_context.get("slots", {})
        if not slots or not isinstance(slots, dict):
            # 从历史记录中提取最近的实体
            history = session_context.get("history", [])
            if history and isinstance(history, list):
                for turn in reversed(history):
                    if isinstance(turn, dict):
                        turn_slots = turn.get("slots", {})
                        if turn_slots and isinstance(turn_slots, dict):
                            slots = turn_slots
                            break
            if not slots:
                return user_input

        # 按优先级选择替换实体
        replacements = []
        for key in ("product_code", "order_id", "work_order_id", "po_id",
                     "customer_name", "supplier"):
            val = slots.get(key)
            if val:
                replacements.append((key, str(val)))

        if not replacements:
            return user_input

        # 用第一个可用实体替换代词
        entity = replacements[0][1]
        resolved = user_input
        for pronoun in pronouns:
            if pronoun in resolved:
                resolved = resolved.replace(pronoun, entity)
                break  # 只替换第一个匹配的代词

        return resolved

    def _rule_based_match(self, text: str) -> Optional[Intent]:
        """规则匹配（DB优先 + 内置兜底）。

        参数：
            text: 已归一化的用户输入（去标点、统一大小写）

        返回：
            命中返回Intent（source='rule'），否则None。
        """
        # 热更新检查：周期重载 intent_rules 表（训练变更生效无需重启）
        self._maybe_refresh_rules()

        # 收集所有命中的规则（DB规则按priority在前，内置兜底在后）
        # v6.13：记录匹配位置（match.start），供 _disambiguate 实现"句首意图词优先"权重
        matched = []  # [(rule, match_start)]
        for rule in self._rules:
            m = rule.compiled.search(text)
            if m:
                matched.append((rule, m.start()))

        if not matched:
            return None

        # 多规则命中时进行意图消歧，选出真实意图
        # W9：priority int 归一（DB 规则加载处已 int，此处防御兼容直接构造的规则）
        candidates = [(r.intent_name, s, int(r.priority)) for r, s in matched]
        final_intent = self._disambiguate(text, candidates)
        rule = next((r for r, _ in matched if r.intent_name == final_intent), matched[0][0])
        return Intent(
            name=final_intent,
            confidence=0.9,
            source="rule",
            channel=rule.target_channel,
            target_agent=rule.target_agent,
        )

    # v6.85 性能：确定性动作/查询类意图白名单（句首查询动词守卫见
    # _deterministic_rule_precheck；仅用于预检层，不影响 _disambiguate）
    # W3：与 _DEFAULT_ACTION_INTENTS/_DEFAULT_QUERY_INTENTS 对齐——
    #   移除幽灵意图 version_change（不在 KNOWN_INTENTS，死代码）。
    _ACTION_INTENTS: frozenset = frozenset({
        "create_order", "modify_order", "order_cancel",
        "stock_in", "stock_out", "inventory_adjust",
        "work_report", "payroll", "attendance",
        "onboarding", "resignation", "report_issue",
        "complaint", "purchase",
    })
    _QUERY_VERB_PREFIX = re.compile(
        r"^(查一下|查询|查看|看看|查查|查看一下|查一查|查找|看下|查下|帮我查|请查|查)")

    def _deterministic_rule_precheck(self, text: str) -> Optional[Intent]:
        """确定性规则预检（v6.85 性能）：LLM 之前规则层先行。

        设计：
            - 复用 _rule_based_match（DB 优先 + 内置兜底 + _disambiguate 消歧）；
            - 守卫：动作类意图命中且句首为查询动词时跳过——"查看一下下订单"
              中"下订单"是宾语而非动作，主谓宾歧义保留 LLM 主导判断
              （v6.67.4/5 修复不变）；查询类及无歧义动作类直接命中返回。
            - 零 token / 零延迟；规则未命中返回 None 继续走 LLM 兜底。
        """
        intent = self._rule_based_match(text)
        if intent is None:
            return None
        if (intent.name in self._ACTION_INTENTS
                and self._QUERY_VERB_PREFIX.match(text)):
            return None
        return intent

    def _disambiguate(self, text: str, candidates: list) -> str:
        """意图消歧：当多个规则命中时，通过语义优先级判断真实意图
            text: 已归一化的用户输入
            candidates: [(intent_name, match_start_position, priority), ...]
                        v6.13 新增 match_start_position 用于实现句首位置权重
                        v6.13 新增 priority 用于实现 DB 高优先级规则直通

        规则：
        0. DB高优先级规则直通（priority<10 的训练/反馈规则优先于通用消歧）
        1. 句首意图词优先于句中出现的词（位置权重，前10字符内匹配优先）
        2. 制造业语义消歧（特定规则优先于通用动作>查询循环）
        3. 通用优先级：动作意图 > 查询意图
        4. 匹配长度权重：更长匹配短语优先（精确匹配优先于宽泛匹配）
        """
        if len(candidates) <= 1:
            return candidates[0][0] if candidates else "unknown"

        # 唯一意图名列表（去重保序）
        intent_names = list(dict.fromkeys(c[0] for c in candidates))

        # 规则0: DB高优先级规则直通（priority<10 的训练/反馈规则）
        # v6.13：避免通用消歧覆盖用户显式训练的高优先级规则。
        # 当高优先级DB规则与低优先级内置规则冲突时，缩小消歧范围到高优先级意图。
        high_pri_names = list(dict.fromkeys(
            c[0] for c in candidates if len(c) > 2 and c[2] < 10))
        if high_pri_names and len(high_pri_names) < len(intent_names):
            intent_names = high_pri_names

        # 规则0.5: 句首通用确认词 + 明确业务动作词 -> 业务意图优先（v6.51）
        # 根因："提交100件A-202的报工" 中"提交"(位置0)命中 confirm，"报工"(位置12)命中 work_report，
        #   规则1 句首位置权重（前10字符内匹配优先）会直接返回 confirm，业务意图被吞。
        #   修复：含明确业务动作词时业务意图优先于 confirm。
        if "confirm" in intent_names:
            # v6.67：confirm 与 query_order 并存时，若文本含订单状态/查询语境，订单查询优先。
            # 根因："查一下已确认的订单" 中"确认"(位置3)命中 confirm 规则，句首权重 + 动作>查询
            #   使 confirm 胜出，实际应路由订单查询流程（状态=confirmed）。
            if "query_order" in intent_names and re.search(r"(查|查看|查询|查一下|的订单|订单状态|订单进度)", text):
                return "query_order"
            if "work_report" in intent_names and re.search(r"(报工|工时|报工记录)", text):
                return "work_report"
            if "inventory_adjust" in intent_names and re.search(r"(盘盈|盘亏|盘点|调整库存)", text):
                return "inventory_adjust"
            if "stock_in" in intent_names and re.search(r"(入库|收货)", text):
                return "stock_in"
            if "stock_out" in intent_names and re.search(r"(出库|发货|领料)", text):
                return "stock_out"

        # 规则0.55: 主谓宾——句首查询动词 + create_order/query_order 并存 -> query_order（v6.67.4）
        # 根因："查看一下下订单" 中"下订单"(位置4)命中 create_order 子串（前字符"下"不在
        #   负向后顾排除集），句首"查看"(位置0)命中 query_order；动作>查询循环使 create_order 胜出。
        #   主谓宾分析：谓语=句首查询动词（查/查看/查询/看看…），"下订单"是宾语（要查看的订单）
        #   非动作 → query_order 优先。
        if "create_order" in intent_names and "query_order" in intent_names:
            if re.match(r"^(查一下|查询|查看|看看|查查|查看一下|查一查|查找|看下)", text):
                return "query_order"

        # 规则1: 句首位置权重——前10字符内匹配的意图优先
        head_threshold = 10
        head_intents = list(dict.fromkeys(
            c[0] for c in candidates if c[1] < head_threshold and c[0] in intent_names))
        if len(head_intents) == 1:
            return head_intents[0]
        # 句首有多个意图时，后续消歧在 head_intents 范围内进行
        if head_intents:
            intent_names = head_intents

        cand_set = set(intent_names)

        # ===== 制造业语义消歧（特定规则优先于通用动作>查询循环）=====

        # 规则5: 报价询价 vs 财务查询
        if "query_price" in cand_set and "financial_query" in cand_set:
            if re.search(r"(多少|多少钱|报价|询价|售价|什么价格|价格是多少)", text):
                return "query_price"
            return "financial_query"

        # 规则7: 采购管理 vs 销售下单（create_order）
        # v6.47："采购X原料/材料/物料"或采购单/供应商等明确采购管理词 -> purchase；
        #        裸"采购"（"我要采购100台B-305"）属客户下单 -> create_order
        if "purchase" in cand_set and "create_order" in cand_set:
            if re.search(r"(采购单|下采购单|供应商|采购订单|采购.{0,12}(原料|材料|物料|物资|耗材)|采购申请|请购|申购)", text):
                return "purchase"
            return "create_order"

        # 规则7.1: 流程定义训练 vs 销售下单（v6.61）
        # 根因："训练一个采购审批流程"含"采购"裸词命中 create_order，被下单意图抢占；
        # 训练动词+流程词语境（训练/创建/定义/把PDF做成流程）应路由 workflow_train
        if "workflow_train" in cand_set and "create_order" in cand_set:
            if re.search(r"(训练|创建|新建|定义|设计|定制|配置|制作|做成|生成|制定).{0,10}(流程|审批流程|工作流|审批单)|(把|用|根据|按|依据).{0,8}.*(pdf|PDF|文档|文件|附件|制度|模板).{0,12}(训练|做成|定义|创建|生成|制定).{0,10}(流程|审批|工作流)", text):
                return "workflow_train"
            return "create_order"

        # 规则8: 财务操作优先于财务查询
        # v6.41 修复："应收款还有多少" 含"收款"但属查询语义（应收/应付/对账为查询）
        if "financial_operation" in cand_set and "financial_query" in cand_set:
            if re.search(r"(应收|应付|对账|账龄|余额|账款查询|应收账款)", text):
                return "financial_query"
            if re.search(r"(付款|收款|付钱|收钱|开票|开发票)", text):
                return "financial_operation"
            return "financial_query"

        # 规则8.1: 财务操作 vs 采购（v6.41：给供应商付款应路由财务而非采购）
        if "financial_operation" in cand_set and "purchase" in cand_set:
            if re.search(r"(付款|收款|开票|开发票|付钱|收钱|打款|转账)", text):
                return "financial_operation"
            return "purchase"

        # 规则8.2: query_customer vs financial_query（v6.51：客户查询含财务词时财务查询优先）
        # 根因："查一下本月的应收对账" 中"应收"同时命中 query_customer(34) 与 financial_query(35)，
        #   前者 priority 更小先出现导致误判为客户查询。含明确财务语义词时应路由财务Agent。
        if "query_customer" in cand_set and "financial_query" in cand_set:
            if re.search(r"(对账|应收|应付|账龄|回款|发票|财务报表|资金流|利润|毛利|净利润|成本|收款|账款)", text):
                return "financial_query"
            if re.search(r"(信用|额度|客户信息|欠款)", text):
                return "query_customer"
            return "financial_query"

        # 规则8.3: work_report vs confirm（v6.51：报工含"提交"字样，避免被 confirm 句首权重抢占）
        # 根因："提交100件A-202的报工" 中"提交"(位置0)命中 confirm，"报工"(位置11)命中 work_report，
        #   句首位置权重使 confirm 胜出。含报工语义词时应优先 work_report。
        if "work_report" in cand_set and "confirm" in cand_set:
            if re.search(r"(报工|工时|报工记录)", text):
                return "work_report"
            return "confirm"

        # 规则9: 知识管理优先于知识查询
        if "knowledge_management" in cand_set and "knowledge_query" in cand_set:
            if re.search(r"(管理|知识库|经验库)", text):
                return "knowledge_management"
            return "knowledge_query"

        # 规则9.8: 管理咨询 vs 知识查询（v6.41："车间安全管理有什么建议"应路由管理咨询）
        if "management_consulting" in cand_set and "knowledge_query" in cand_set:
            if re.search(r"(安全|5S|6S|精益|PDCA|ISO|TQM|六西格玛|管理|改善|体系|制度)", text):
                return "management_consulting"
            return "knowledge_query"

        # 规则12: contract vs query_order —— 合同 vs 订单查询
        if "contract" in cand_set and "query_order" in cand_set:
            if re.search(r"合同", text):
                return "contract"
            return "query_order"

        # ===== 通用优先级排序：动作 > 查询 =====
        # v6.13 修复：financial_query 移到查询集合（原误放在动作集合，
        #   导致与 query_order 冲突时 financial_query 错误优先）
        # v6.13 补全：遗漏意图分类（complaint/attendance/
        #   return_order/org_query/report_issue/workflow_guide）
        # v6.46 C5：动作/查询分类从 DISAMBIG-CFG（DB 可训练）读取，
        # 缺省降级内置集合（_DEFAULT_ACTION_INTENTS/_DEFAULT_QUERY_INTENTS）。
        action_intents = set(_DEFAULT_ACTION_INTENTS)
        query_intents = set(_DEFAULT_QUERY_INTENTS)
        try:
            # W9：双路径导入（prog.runtime 主路径 / runtime 备用路径，历史兼容）
            try:
                from prog.runtime.param_loader import get_param_dict
            except ImportError:
                from runtime.param_loader import get_param_dict  # type: ignore
            _cfg = get_param_dict("DISAMBIG-CFG", {})
            if isinstance(_cfg, dict):
                act = _cfg.get("action_intents")
                if isinstance(act, list) and act:
                    action_intents = set(act)
                que = _cfg.get("query_intents")
                if isinstance(que, list) and que:
                    query_intents = set(que)
        except Exception:
            pass

        # 如果有动作意图，优先返回
        for intent in intent_names:
            if intent in action_intents:
                return intent

        # 如果都是查询意图，返回第一个匹配的
        return intent_names[0]

    def _llm_based_match(self, text: str,
                         session_context: Optional[Dict] = None,
                         reasoning_callback: Optional[Any] = None) -> Intent:
        """LLM辅助识别。

        参数：
            text: 用户输入
            session_context: 会话上下文
            reasoning_callback: v6.78.3 思考过程逐块回调（透传 _call_llm_with_tools，
                用于流式调用时实时输出 reasoning_content）

        返回：
            Intent（source='llm'）；LLM失败时返回unknown。
        """
        # 无LLM客户端时直接返回unknown
        # W14：source 标记为 llm 降级路径（原误标 rule——该分支属 LLM 识别
        # 分支的无客户端降级，source 应为 llm 以便追踪识别来源）
        if self._llm_client is None:
            return Intent(
                name="unknown",
                confidence=0.0,
                source="llm",
            )

        # 构建LLM提示词，要求返回结构化JSON
        prompt = self._build_llm_prompt(text, session_context)

        try:
            # v6.33：优先尝试 Function Calling（Structured Output）
            # v6.46 C5：known_intents 动态派生（KNOWN_INTENTS + DB 规则意图）
            known = self._known_intents()
            tool_result = self._call_llm_with_tools(
                prompt, reasoning_callback=reasoning_callback)
            if tool_result is not None:
                intent = self._parse_tool_call_response(tool_result, text,
                                                        known_intents=known)
                if intent.name != "unknown":
                    return intent

            # 降级：纯文本调用 + JSON 解析
            response = self._call_llm(prompt)
            if not response:
                return Intent(name="unknown", confidence=0.0, source="llm")

            # 解析LLM返回的JSON
            intent = self._parse_llm_response(response, text,
                                              known_intents=known)
            return intent
        except Exception:
            # LLM调用或解析失败，安全回退为unknown
            return Intent(name="unknown", confidence=0.0, source="llm")

    def _normalize(self, text: str) -> str:
        """输入归一化（去标点、全角转半角、统一小写）。"""
        # 全角转半角（常见全角字符）
        fullwidth_map = str.maketrans(
            "０１２３４５６７８９"
            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
            "（）＿－！＠＃＄％＾＆＊＝＋［］｛｝；：＇＂，．＜＞／？",
            "0123456789"
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "()_-!@#$%^&*=+[]{};:'\",.<>/?",
        )
        text = text.translate(fullwidth_map)
        # v6.13：中文标点归一化（原未处理，导致"查一下库存，A-202"的逗号影响正则匹配）
        # 中文标点 → 半角等价物（一一对应，21对21）
        # 使用Unicode转义避免编辑器将中文引号转为ASCII引号
        cn_punct_map = str.maketrans(
            "\uff0c\u3002\uff01\uff1f\uff1b\uff1a\u201c\u201d\u2018\u2019\uff08\uff09\u3010\u3011\u300a\u300b\u3001\u2026\u2014\u00b7\uff5e",
            ",.!?;:\"\"''()[]<>,.-.~",
        )
        text = text.translate(cn_punct_map)
        # 统一小写
        text = text.lower()
        # 去除多余空白
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # --------------------------------------------------------
    # DB规则加载 / 热更新 / 管理
    # --------------------------------------------------------

    def load_rules_from_db(self) -> int:
        """从 intent_rules 表加载启用规则（DB优先）。

        按 priority ASC 排序；加载失败或表为空时降级为内置兜底规则。

        Returns:
            int: 加载的 DB 规则数量
        """
        try:
            db = _get_db(self._database)
            rows = db.query_many(
                "intent_rules",
                filters={"enabled": True},
                order_by="priority ASC, rule_id ASC",
            ) or []
            rules = []
            for row in rows:
                pattern = row.get("regex_pattern") or ""
                if not pattern:
                    continue
                try:
                    compiled = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    continue
                rules.append(_IntentRule(
                    compiled=compiled,
                    intent_name=row.get("intent_name") or "unknown",
                    # W9：priority int 归一（DB 可能返回 str，str 与 int 比较抛 TypeError）
                    priority=int(row.get("priority") or 50),
                    target_agent=row.get("target_agent") or "",
                    target_channel=row.get("target_channel") or "business",
                    rule_id=row.get("rule_id") or "",
                    source="db",
                ))
            if rules:
                self._db_rules = rules
                self._rules = rules + self._builtin_rules
                self._db_rules_ok = True
            else:
                # DB表为空：降级为种子规则（含路由）+ 内置兜底
                self._db_rules = []
                self._rules = list(self._seed_fallback_rules) + list(self._builtin_rules)
                self._db_rules_ok = False
            return len(rules)
        except Exception:
            # DB 不可用：降级为种子规则（含路由）+ 内置兜底
            # 种子规则镜像009_intent_rules.sql，确保降级模式仍有完整路由信息
            self._db_rules = []
            self._rules = list(self._seed_fallback_rules) + list(self._builtin_rules)
            self._db_rules_ok = False
            return 0

    def _maybe_refresh_rules(self) -> None:
        """热更新检查：周期重载 intent_rules 表。

        训练变更/审批通过后写库，无需 Agent 重启，按 refresh_interval
        轮询重载即可生效。规则表规模小（数十条），轮询开销可忽略。
        """
        now = time.time()
        if now - self._db_last_check < self._refresh_interval:
            return
        self._db_last_check = now
        self.load_rules_from_db()

    def reload_rules(self) -> int:
        """强制重载 DB 规则（管理API / 审批通过后调用）。

        Returns:
            int: 加载的 DB 规则数量
        """
        self._db_last_check = 0.0
        return self.load_rules_from_db()

    def get_rules_summary(self) -> dict:
        """规则统计（供管理API）。

        Returns:
            dict: {"db_rules": N, "builtin_rules": N, "db_ok": bool}
        """
        return {
            "db_rules": len(self._db_rules),
            "builtin_rules": len(self._builtin_rules),
            "db_ok": self._db_rules_ok,
        }

    def get_db_rules(self, include_disabled: bool = False) -> list:
        """从 intent_rules 表读取规则（管理API列表用）。

        Args:
            include_disabled: 是否包含未启用规则（enabled=FALSE 待审批）

        Returns:
            list: 规则记录列表（DB不可用时为空列表）
        """
        try:
            db = _get_db(self._database)
            filters = {} if include_disabled else {"enabled": True}
            return db.query_many(
                "intent_rules",
                filters=filters,
                order_by="priority ASC, rule_id ASC",
            ) or []
        except Exception:
            return []

    # --------------------------------------------------------
    # 训练变更：L1反馈 / L2产出 / 已标注规则加载
    # --------------------------------------------------------

    def add_rule(self, pattern: str, intent_name: str, *,
                 source: str = "L2", target_agent: str = "",
                 target_channel: str = "business", enabled: bool = False) -> str:
        """运行时新增规则（L2训练产出 / LLM建议）。

        DB 可用时写入 intent_rules 表：
            - enabled=FALSE（默认）：待审批，审批通过后调用 reload_rules() 生效
            - enabled=TRUE：直接生效（调用方确认已通过审批）
        adjusted_by 标记来源（L2/LLM/MANUAL）。
        DB 不可用时降级为内存即时生效（离线兜底）。

        Args:
            pattern: 正则表达式
            intent_name: 意图名称
            source: 调整方式（L2/LLM/MANUAL）
            target_agent: 目标Agent
            target_channel: 路由通道
            enabled: 是否直接启用（默认 False 待审批）

        Returns:
            str: rule_id
        """
        rule_id = f"RULE-INT-{source}-{int(time.time())}"
        try:
            db = _get_db(self._database)
            db.insert("intent_rules", {
                "rule_id": rule_id,
                "intent_name": intent_name,
                "regex_pattern": pattern,
                "target_agent": target_agent,
                "target_channel": target_channel,
                "priority": 5,
                "enabled": enabled,
                "adjusted_by": source,
                "version": 1,
            })
            if enabled:
                self.reload_rules()
            return rule_id
        except Exception:
            # 无数据库时降级为内存即时生效
            # W13：priority 与 DB 写入一致（5）——避免离线降级模式下规则排序
            # 与线上不一致（原内存降级未设置 priority，默认 50 远低于 DB 规则）
            self._rules.append(_IntentRule(
                compiled=re.compile(pattern, re.IGNORECASE),
                intent_name=intent_name,
                target_agent=target_agent,
                target_channel=target_channel,
                source="memory",
                priority=5,
            ))
            return rule_id

    def add_feedback(self, user_input: str, recognized_intent: str, correct_intent: str,
                     session_id: str = None) -> bool:
        """记录意图识别反馈（L1 会话学习），用于训练修正（v6.45：改为审批制）

        三通道并存（L1 反馈不再直接生效，需经 rule_config_change 审批）：
            1. 写入 workflow_configs(rule_config_change) 审批记录（待审，approval_chain 可训练）
            2. 写入 intent_rules 表（enabled=FALSE 待审批，priority=1，adjusted_by='L1'，
               关联 approval_id）
            3. 写入 training_data 表（L1会话学习记录，approved=False）
            4. 不再插入内存规则表——生效须经审批端点 enabled=TRUE + reload

        Args:
            user_input: 原始用户输入
            recognized_intent: 系统识别的（错误的）意图
            correct_intent: 用户修正的正确意图
            session_id: 会话ID

        Returns:
            bool: 是否记录成功（记录≠生效，生效需审批）
        """
        import json as _json
        approval_id = ""
        rule_id = f"RULE-INT-L1-{int(time.time())}"

        # 1. 写入 workflow_configs(rule_config_change) 审批记录（L2 审批链，待审）
        #    approval_chain 优先从 DB rule_config_change 定义读取（可训练），
        #    无定义/不可用时兜底 manager 单级
        chain = self._approval_chain_for("rule_config_change") or [
            {"step": 1, "role": "manager", "action": "审批"}]
        try:
            db = _get_db(self._database)
            approval_id = str(db.insert("workflow_configs", {
                "workflow_type": "rule_config_change",
                "workflow_name": f"L1反馈规则变更审批-{datetime.now().strftime('%H%M%S')}",
                "owner_dept": "system",
                "trigger_rule": rule_id,
                "approval_chain": _json.dumps(chain, ensure_ascii=False),
                "thresholds": _json.dumps({
                    "action": "update",
                    "changed_by": "L1",
                    "session_id": session_id or "feedback",
                    "recognized": recognized_intent,
                    "proposed": {
                        "intent_name": correct_intent,
                        "regex_pattern": re.escape(user_input),
                        "priority": 1, "enabled": True,
                    },
                    "current": {},
                }, ensure_ascii=False),
                "is_active": True,
                "is_trained": False,
            }))
        except Exception:
            approval_id = ""  # 无数据库：仅记录日志，不生效

        # 2. 写入 intent_rules 表（L1 修正规则，enabled=FALSE 待审批，不直接生效）
        try:
            db = _get_db(self._database)
            db.insert("intent_rules", {
                "rule_id": rule_id,
                "intent_name": correct_intent,
                "regex_pattern": re.escape(user_input),
                "target_agent": "",
                "target_channel": "business",
                "priority": 1,
                "enabled": False,
                "adjusted_by": "L1",
                "version": 1,
                "approval_id": approval_id,
            })
        except Exception:
            pass  # 无数据库时仅记录日志

        # 3. 写入 training_data 表（L1会话学习）
        try:
            db = _get_db(self._database)
            db.insert("training_data", {
                "agent_type": "intent_recognizer",
                "intent": correct_intent,
                "user_input": user_input,
                "ai_output": recognized_intent,
                "user_correction": correct_intent,
                "metadata": {
                    "type": "intent_correction",
                    "session_id": session_id or "feedback",
                    "recognized": recognized_intent,
                    "correct": correct_intent,
                    "approval_id": approval_id,
                },
                "approved": False,
                "created_at": datetime.now().isoformat(),
            })
        except Exception:
            pass

        # 3b. v6.62：写入 conversation_corrections 表（规格 L3493 L1 学习源归档，
        #     独立于 training_data，跨会话用户纠正记录长期保存）
        try:
            db = _get_db(self._database)
            db.insert("conversation_corrections", {
                "session_id": session_id or "feedback",
                "user_input": user_input,
                "recognized": recognized_intent,
                "corrected": correct_intent,
                "agent_type": "intent_recognizer",
                "approved": False,
            })
        except Exception:
            pass

        # 4. 记录到内存反馈日志（不再插入规则表头部——生效需审批后 reload）
        if not hasattr(self, '_feedback_log'):
            self._feedback_log = []
        self._feedback_log.append({
            "input": user_input,
            "recognized": recognized_intent,
            "correct": correct_intent,
            "approved": False,
            "approval_id": approval_id,
            "timestamp": datetime.now().isoformat(),
        })

        return True

    def _approval_chain_for(self, workflow_type: str) -> list:
        """读取指定流程类型的审批链（v6.45：审批链可训练——DB workflow_configs 优先）。

        训练修改 workflow_configs.approval_chain 后，新发起的训练变更即采用新链；
        DB 不可用/无定义时返回空列表（调用方兜底单级 manager）。

        Args:
            workflow_type: 流程类型（如 rule_config_change / slot_defs_change）

        Returns:
            list: 审批链步骤列表
        """
        try:
            db = _get_db(self._database)
            row = db.query_one("workflow_configs", {"workflow_type": workflow_type})
            chain = (row or {}).get("approval_chain")
            if isinstance(chain, str):
                import json as _json
                chain = _json.loads(chain)
            if isinstance(chain, list) and chain:
                return chain
        except Exception:
            pass
        return []

    def load_trained_rules(self) -> int:
        """统计已标注（approved）训练样本（v6.46：不再注入生效）。

        v6.45 审批制：规则生效统一走 intent_rules(enabled=TRUE) + reload_rules()，
        training_data 仅作为训练样本记录；approved 样本不再直接注入规则表，
        消除"training_data.approved 绕过审批链"的潜在路径。

        Returns:
            int: 已标注样本数量（统计值，不产生任何规则注入）
        """
        count = 0
        try:
            db = _get_db(self._database)
            records = db.query_many("training_data", filters={
                "agent_type": "intent_recognizer",
                "approved": True,
            }) or []
            count = len(records)
        except Exception:
            pass
        return count

    def get_feedback_stats(self) -> dict:
        """获取反馈统计"""
        if not hasattr(self, '_feedback_log'):
            self._feedback_log = []
        return {
            "total_corrections": len(self._feedback_log),
            "recent_corrections": self._feedback_log[-10:],
        }

    # --------------------------------------------------------
    # 参数提取（从原始输入中抽取槽位）
    # --------------------------------------------------------

    @staticmethod
    def _extract_params(text: str) -> Dict[str, Any]:
        """从用户输入中提取参数槽位（v6.46：统一委托 slot_engine.extract_slots）。

        槽位定义（提取正则/值转换）全部存 SLOT-DEFS（DB 可训练，含 regexes 多语境
        回退链 / strip_patterns / value_map / unit_scale）。删除历史硬编码前 9 步
        重复提取，消除双份正则漂移——训练修改槽位定义即时生效且全链路一致
        （intent 层与 Agent 层同源）。

        返回:
            dict: 槽位名 -> 值
        """
        # W9：双路径导入（prog.runtime 主路径 / runtime 备用路径，历史兼容）
        try:
            from prog.runtime.slot_engine import extract_slots as _slot_extract
        except ImportError:
            from runtime.slot_engine import extract_slots as _slot_extract  # type: ignore
        return _slot_extract(text) or {}

    @staticmethod
    def _merge_params(base: Dict[str, Any],
                      extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """合并参数槽位（W1）：base（slot_engine 规则提取）优先，
        extra（LLM 结构化输出提取）补充 base 缺失的键——LLM 提取的
        规则正则无法识别的参数不再被覆盖丢弃。

        Args:
            base: 基础参数（规则提取结果）
            extra: 补充参数（LLM 提取结果，可为 None）

        Returns:
            dict: 合并后的参数（base 优先，extra 仅补缺失键）
        """
        merged = dict(base or {})
        for k, v in (extra or {}).items():
            if v is not None and merged.get(k) is None:
                merged[k] = v
        return merged

    # --------------------------------------------------------
    # LLM辅助方法
    # --------------------------------------------------------

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """轻量文本相似度（v6.80 few-shot 动态选样）：中文 2-gram Jaccard，零依赖零延迟。"""
        def grams(s: str) -> set:
            """字符串的 2-gram 二元组集合（相似度计算辅助）。

            参数：
                s: 待切分文本（空/None/单字安全）
            返回：
                set: 相邻两字符二元组集合；长度<2 时退化返回单字符集合
                     （短文本仍有可比的非空特征）
            """
            s = (s or "").strip().lower()
            if len(s) < 2:
                return set(s) if s else set()
            return set(s[i:i + 2] for i in range(len(s) - 1))
        ga, gb = grams(a), grams(b)
        if not ga or not gb:
            return 0.0
        inter = len(ga & gb)
        return inter / (len(ga) + len(gb) - inter) if inter else 0.0

    def _load_few_shot_examples(self, max_total: int = 30,
                                text: str = "") -> list:
        """加载 approved 训练样本作为 few-shot 示例（v6.67.5：LLM+训练数据主导）。

        从 training_data 表读取 approved=TRUE 的意图识别样本。
        v6.80（动态选样）：提供 text 时，先按与当前输入的 2-gram 相似度挑选
        最相关样本（每意图最多 2 条，保证多样），再用"每意图最新 2 条"基线
        池补足——相似样本在前（LLM 更关注当前句式），基线保底核心意图正例
        （防"查看入库记录"→stock_in 类泛化失败回归）。
        DB 不可用或无样本时返回空列表（LLM 仅依赖意图定义）。

        Args:
            max_total: few-shot 示例总上限，防止 prompt 膨胀
            text: 当前用户输入（用于相似度动态选样；空串时仅基线池）
        """
        try:
            db = _get_db(self._database)
            rows = db.query_many(
                "training_data",
                filters={"agent_type": "intent_recognizer", "approved": True},
                order_by="id DESC",
            ) or []
        except Exception:
            return []
        # 有效样本（intent 非空且非 unknown）
        cands = []
        for row in rows:
            intent = row.get("intent")
            user_input = (row.get("user_input") or "").strip()
            if not intent or not user_input or intent == "unknown":
                continue
            cands.append((user_input, intent))

        picked: list = []          # 最终选样结果
        seen = set()               # (input, intent) 去重
        # 1) 相似度动态选样（当前输入句式相关样本优先，每意图 ≤2）
        if text and cands:
            sim = sorted(
                ((self._text_similarity(text, u), u, i) for u, i in cands),
                key=lambda x: -x[0])
            sim_cnt: Dict[str, int] = {}
            for score, u, intent in sim:
                if score <= 0:
                    break
                if sim_cnt.get(intent, 0) >= 2:
                    continue
                if (u, intent) in seen:
                    continue
                picked.append((u, intent))
                seen.add((u, intent))
                sim_cnt[intent] = sim_cnt.get(intent, 0) + 1
                if len(picked) >= max_total:
                    break
        # 2) 基线保底：每意图最新 2 条（覆盖核心意图，防相似度选样偏置）
        if len(picked) < max_total:
            base_cnt: Dict[str, int] = {}
            for u, intent in cands:  # rows 已按 id DESC
                if base_cnt.get(intent, 0) >= 2:
                    continue
                if (u, intent) in seen:
                    continue
                picked.append((u, intent))
                seen.add((u, intent))
                base_cnt[intent] = base_cnt.get(intent, 0) + 1
                if len(picked) >= max_total:
                    break
        return picked

    def _db_workflow_names(self) -> list:
        """从 DB 读取已配置的流程名（v6.43：注入 LLM prompt，使其感知训练新增流程）。

        读取 workflow_configs.thresholds.trigger_keywords，返回全部关键词列表。
        DB 不可用时返回空列表（LLM 仅依赖内置 INTENT_DESCRIPTIONS）。
        """
        # W9：双路径导入（prog.runtime 主路径 / runtime 备用路径，历史兼容）
        try:
            from prog.runtime.database import get_database
        except ImportError:
            from runtime.database import get_database  # type: ignore
        db = get_database()
        if db is None:
            return []
        try:
            rows = db.query_many("workflow_configs", {"is_active": True})
            names = []
            for row in rows or []:
                thresholds = row.get("thresholds")
                if isinstance(thresholds, str):
                    import json as _json
                    try:
                        thresholds = _json.loads(thresholds)
                    except Exception:
                        thresholds = None
                tk = thresholds.get("trigger_keywords") if isinstance(thresholds, dict) else None
                if tk and isinstance(tk, list):
                    names.extend(tk)
            return names
        except Exception:
            return []

    def _db_slot_names(self) -> list:
        """从 DB 读取已配置的槽位名（v6.43：注入 LLM prompt）。

        读取 business_rules(SLOT-DEFS).config_json.slots 的 key 列表。
        DB 不可用时返回空列表。
        """
        # W9：双路径导入（prog.runtime 主路径 / runtime 备用路径，历史兼容）
        try:
            from prog.runtime.slot_engine import get_slot_defs
        except ImportError:
            from runtime.slot_engine import get_slot_defs  # type: ignore
        try:
            return list(get_slot_defs(use_cache=True).keys())
        except Exception:
            return []

    def _known_intents(self) -> set:
        """已知意图集合（v6.46 C5：KNOWN_INTENTS + DB 训练新增意图动态派生）。

        内置 KNOWN_INTENTS 为兜底白名单；DB intent_rules 表中已启用/待审批的
        规则意图名全部并入，使训练新增的意图（如新流程 start_xxx）不会被 LLM
        安全门误判为 unknown。
        """
        known = set(KNOWN_INTENTS)
        try:
            for r in self._rules:
                if getattr(r, "intent_name", ""):
                    known.add(r.intent_name)
        except Exception:
            pass
        return known

    def _build_llm_prompt(self, text: str,
                          session_context: Optional[Dict] = None) -> str:
        """构建LLM意图识别提示词（v6.33：注入意图定义+操作上下文+推理链引导）"""
        parts = ["你是AI工厂管家的意图识别模块。请分析用户输入，识别其意图。\n"]

        # 意图定义（名称+描述），帮助 LLM 理解每个意图的边界
        # v6.46 C5：KNOWN_INTENTS + DB 训练意图动态派生
        parts.append("支持的意图类型（名称 + 说明）：")
        for name in sorted(self._known_intents() - {"unknown"}):
            desc = INTENT_DESCRIPTIONS.get(name, "")
            parts.append(f"  - {name}: {desc}")
        parts.append(f"  - unknown: {INTENT_DESCRIPTIONS.get('unknown', '无法识别')}\n")

        # v6.67.5：注入 approved 训练样本 few-shot——LLM+训练数据主导的核心。
        # v6.80：动态选样——按当前输入相似度优先注入相关样本（句式发散），
        # 每意图 ≤2 条保证多样性，基线池保底核心意图（结果收敛）。
        examples = self._load_few_shot_examples(max_total=30, text=text)
        if examples:
            parts.append("已标注示例（用户输入 -> 正确意图），请参考这些示例保持分类一致：")
            for ex_input, ex_intent in examples:
                parts.append(f"  - 输入「{ex_input}」 -> {ex_intent}")
            parts.append("")

        # v6.43：注入 DB 训练获得的流程名与槽位名，使 LLM 感知训练新增的流程/参数
        # 原则：动词固定，流程名/参数名从 DB 训练获得--LLM 需知道有哪些可识别目标
        db_flow_names = self._db_workflow_names()
        if db_flow_names:
            parts.append("当前系统已配置的流程（用户提到这些流程名时考虑 workflow_start）：")
            for fname in db_flow_names:
                parts.append(f"  - {fname}")
            parts.append("")
        db_slot_names = self._db_slot_names()
        if db_slot_names:
            parts.append("当前系统已配置的槽位参数（提取用户输入中的这些参数）：")
            for sname in db_slot_names:
                parts.append(f"  - {sname}")
            parts.append("")

        # 操作上下文（最近意图、关键词、槽位）
        if session_context:
            ctx_lines = []
            last_intent = session_context.get("last_intent")
            if last_intent:
                ctx_lines.append(f"  - 最近意图：{last_intent}")
            keywords = session_context.get("keywords")
            if keywords:
                ctx_lines.append(f"  - 已提取关键词：{', '.join(keywords[:10])}")
            slots = session_context.get("slots")
            if slots:
                ctx_lines.append(f"  - 已填充槽位：{slots}")
            current_workflow = session_context.get("current_workflow")
            if current_workflow:
                ctx_lines.append(f"  - 当前流程：{current_workflow}")
            if ctx_lines:
                parts.append("用户当前操作上下文：")
                parts.extend(ctx_lines)
                parts.append("")

            # v6.80：上下文去重——history（body 回传）与 conversation_history
            # （记忆管理器 turns）同源不同格式，只注入一份：conversation_history
            # 结构化更完整（含 intent/agent）优先，history 仅作兜底，减少 token 噪声。
            conv_history = session_context.get("conversation_history")
            if conv_history and isinstance(conv_history, list):
                parts.append("最近对话轮次：")
                for t in conv_history[-3:]:
                    parts.append(
                        f"  [轮{t.get('turn', '?')}] "
                        f"用户: {t.get('input', '')} -> "
                        f"{t.get('agent', '')}({t.get('intent', '')}): "
                        f"{t.get('reply', '')}"
                    )
                parts.append("")
            else:
                recent = session_context.get("history", [])[-3:]
                if recent:
                    history_str = "\n".join(
                        f"  {m.get('role', 'user')}: {m.get('content', '')}"
                        for m in recent
                    )
                    parts.append(f"对话历史：\n{history_str}\n")

            # v6.80：对话中已提及的业务实体（product_code/order_id 等），
            # 由 coordinator 从槽位/会话注入——LLM 结合已确认实体理解当前输入
            recent_entities = session_context.get("recent_entities")
            if recent_entities:
                parts.append(
                    f"对话中已提及的实体：{json.dumps(recent_entities, ensure_ascii=False)}\n"
                )

            conv_summary = session_context.get("conversation_summary")
            if conv_summary:
                parts.append(f"历史摘要：\n{conv_summary}\n")

            relevant = session_context.get("relevant_turns")
            if relevant and isinstance(relevant, list):
                parts.append("相关历史轮次（基于关键词相似度筛选）：")
                for t in relevant:
                    parts.append(
                        f"  [轮{t.get('turn', '?')}] "
                        f"{t.get('input', '')} -> {t.get('reply', '')}"
                    )
                parts.append("")

            # v6.37：递归意图状态（意图流转轨迹）
            intent_state = session_context.get("intent_state")
            if intent_state:
                parts.append(f"意图流转：{intent_state}\n")

        parts.append(f"用户输入：{text}\n")
        parts.append(
            "请先分析用户输入与上下文的关系，再给出意图判断。\n"
            "返回JSON格式：{\"reasoning\": \"分析过程\", \"intent\": \"意图名\", "
            "\"confidence\": 0.0~1.0, \"params\": {}}\n"
            "只返回JSON，不要其他内容。"
        )
        return "\n".join(parts)

    def _call_llm(self, prompt: str) -> str:
        """调用LLM客户端，兼容多种接口形式"""
        # 尝试 call 方法
        call_method = getattr(self._llm_client, "call", None) or getattr(
            self._llm_client, "generate", None
        )
        if call_method:
            result = call_method(prompt)
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                return result.get("text", "") or result.get("content", "")
            return str(result)

        # 尝试 chat_completion 方法
        chat_method = getattr(self._llm_client, "chat_completion", None)
        if chat_method:
            messages = [{"role": "user", "content": prompt}]
            resp = chat_method(messages)
            if isinstance(resp, dict):
                return resp.get("text", "") or resp.get("content", "")
            return str(resp)

        return ""

    def _call_llm_with_tools(self, prompt: str,
                             reasoning_callback: Optional[Any] = None) -> Optional[Dict]:
        """v6.33：使用 Function Calling 调用 LLM，返回 tool_calls 响应。

        不支持 tools 参数时返回 None，调用方降级为纯文本模式。

        v6.78.3（双模型架构）：当 reasoning_callback 提供且底层 provider 支持
        stream_chat 时，改用流式调用——thinking 强模型的 reasoning_content 逐块
        经回调实时输出（前端"思考中"），同时按流式 delta 累积 tool_calls/content，
        返回结构与 chat() 完全一致，识别质量不变。
        """
        # 流式优先：识别强模型 thinking 开启时 reasoning 实时推前端
        if reasoning_callback is not None:
            stream_result = self._stream_llm_with_tools(prompt, reasoning_callback)
            if stream_result is not None:
                return stream_result

        # 优先尝试 chat 方法（LLMProvider.chat 支持 tools 参数）
        chat_method = getattr(self._llm_client, "chat", None) or getattr(
            self._llm_client, "chat_completion", None
        )
        if chat_method is None:
            return None

        try:
            messages = [{"role": "user", "content": prompt}]
            resp = chat_method(messages, tools=_CLASSIFY_INTENT_TOOL, temperature=0.1)
            if isinstance(resp, dict):
                tool_calls = resp.get("tool_calls")
                if tool_calls and isinstance(tool_calls, list):
                    return tool_calls[0]
                # 没有 tool_calls 但有 content，返回文本降级
                content = resp.get("content") or resp.get("text", "")
                if content:
                    return {"_text_fallback": content}
        except TypeError:
            # chat 方法不支持 tools 参数，降级
            return None
        except Exception:
            return None
        return None

    def _stream_llm_with_tools(self, prompt: str,
                               reasoning_callback: Any) -> Optional[Dict]:
        """v6.78.3：流式 Function Calling（识别强模型 thinking 流式输出）。

        调用底层 provider.stream_chat（支持 tools），reasoning_content 逐块经
        reasoning_callback 实时回调；流结束按 delta 累积的 tool_calls/content
        组装为与 chat() 相同的响应结构。任何环节失败返回 None，由调用方回退
        非流式 chat 路径。
        """
        provider = getattr(self._llm_client, "llm_provider", None)
        stream_method = None
        if provider is not None:
            stream_method = getattr(provider, "stream_chat", None) or getattr(
                provider, "stream_completion", None)
        if stream_method is None:
            return None
        try:
            messages = [{"role": "user", "content": prompt}]
            content_parts: list = []
            tool_calls: Optional[list] = None
            for chunk in stream_method(
                    messages, tools=_CLASSIFY_INTENT_TOOL, temperature=0.1):
                if not isinstance(chunk, dict):
                    continue
                reasoning = chunk.get("reasoning") or ""
                if reasoning:
                    try:
                        reasoning_callback(reasoning)
                    except Exception:
                        pass
                text = chunk.get("content") or chunk.get("delta") or ""
                if text:
                    content_parts.append(text)
                tcs = chunk.get("tool_calls")
                if tcs:
                    # W2：流式 tool_calls 为 delta 增量片段——按 index 累积
                    # name/arguments 字段（原直接赋值只保留最后一个 chunk，
                    # 参数会不完整导致函数调用解析失败）
                    if tool_calls is None:
                        tool_calls = []
                    for tc in tcs if isinstance(tcs, list) else [tcs]:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index", 0)
                        while len(tool_calls) <= idx:
                            tool_calls.append({
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                        target = tool_calls[idx]
                        if tc.get("id"):
                            target["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                # name 通常首个 delta 完整出现，直接赋值
                                target["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                # arguments 为增量片段，须按序拼接
                                target["function"]["arguments"] += fn["arguments"]
            if tool_calls and isinstance(tool_calls, list):
                return tool_calls[0]
            if content_parts:
                return {"_text_fallback": "".join(content_parts)}
        except Exception:
            return None
        return None

    @staticmethod
    def _sanitize_llm_output(intent_name: str, confidence: float,
                             known_intents: Optional[set] = None) -> tuple:
        """安全门校验：LLM 返回的意图名是否在已知列表中（v6.37 抽取为独立方法）。

        不在 KNOWN_INTENTS 中的意图名置为 unknown，置信度降至 0.3 以下。
        供 _parse_tool_call_response / _parse_llm_response 两条解析路径统一调用。
        v6.46 C5：known_intents 由调用方传入（KNOWN_INTENTS + DB 派生），
        缺省回退内置 KNOWN_INTENTS（保持静态调用兼容）。
        """
        known = known_intents if known_intents is not None else KNOWN_INTENTS
        if intent_name not in known:
            return "unknown", min(confidence, 0.3)
        return intent_name, confidence

    @staticmethod
    def _parse_tool_call_response(tool_call: Dict, text: str,
                                  known_intents: Optional[set] = None) -> Intent:
        """v6.33：从 Function Calling 的 tool_calls 响应中解析意图。"""
        import json

        # 纯文本降级（chat 方法返回了 content 但没有 tool_calls）
        if "_text_fallback" in tool_call:
            return IntentRecognizer._parse_llm_response(
                tool_call["_text_fallback"], text, known_intents=known_intents
            )

        # 从 tool_call 的 arguments 中解析（标准格式：{function: {arguments: "..."}}）
        func = tool_call.get("function") or {}
        arguments = func.get("arguments") or tool_call.get("arguments")
        if isinstance(arguments, str):
            try:
                data = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return Intent(name="unknown", confidence=0.0, source="llm")
        elif isinstance(arguments, dict):
            data = arguments
        else:
            return Intent(name="unknown", confidence=0.0, source="llm")

        intent_name = data.get("intent", "unknown")
        confidence = float(data.get("confidence", 0.5))
        params = data.get("params", {})

        # 安全门校验（v6.37 抽取为 _sanitize_llm_output；v6.46 C5 动态已知意图集）
        intent_name, confidence = IntentRecognizer._sanitize_llm_output(
            intent_name, confidence, known_intents=known_intents)

        return Intent(
            name=intent_name,
            params=params if isinstance(params, dict) else {},
            confidence=confidence,
            source="llm",
        )

    @staticmethod
    def _parse_llm_response(response: str, text: str,
                            known_intents: Optional[set] = None) -> Intent:
        """解析LLM返回的JSON响应"""
        import json

        # 尝试从响应中提取JSON（LLM可能返回非纯JSON文本）
        # 优先尝试直接解析
        try:
            data = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            # 尝试提取花括号内的JSON
            json_match = re.search(r'\{[^{}]+\}', response, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group())
                except (json.JSONDecodeError, TypeError):
                    return Intent(name="unknown", confidence=0.0, source="llm")
            else:
                return Intent(name="unknown", confidence=0.0, source="llm")

        intent_name = data.get("intent", "unknown")
        confidence = float(data.get("confidence", 0.5))
        params = data.get("params", {})

        # 安全门校验（v6.37 抽取为 _sanitize_llm_output；v6.46 C5 动态已知意图集）
        intent_name, confidence = IntentRecognizer._sanitize_llm_output(
            intent_name, confidence, known_intents=known_intents)

        return Intent(
            name=intent_name,
            params=params if isinstance(params, dict) else {},
            confidence=confidence,
            source="llm",
        )
