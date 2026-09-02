from __future__ import annotations

"""
CoordinatorAgent 协调Agent模块
==============================

文件用途：
    实现AI工厂管家的中央协调Agent，负责意图识别、Agent选择、
    上下文隔离与多Agent结果聚合。

技术规格章节（原项目引用）：
    - §1.1.3 Coordinator Agent（核心协调职责）
    - §3.7 Knowledge Assistant（双通道路由判断）

核心职责：
    1. 意图识别：将用户自然语言映射为系统可处理的意图标签
    2. Agent选择：依据意图标签路由到对应领域Agent
    3. 上下文隔离：为每个Agent构造隔离的上下文，防止跨Agent数据泄露
    4. 结果聚合：当一次输入触发多Agent时，合并它们的响应

双通道路由判断：
    - 业务操作通道（写/读业务库）：意图命中具体业务意图
      -> 路由到对应领域Agent
    - 管理咨询通道（知识问答）：意图为管理制度/流程咨询
      -> 路由到 KnowledgeAssistant（可选的兜底助手）

设计原则：
    - Coordinator 自身不执行任何业务逻辑，只做路由与聚合
    - 上下文隔离为强约束，禁止Agent之间直接传递上下文
    - 意图识别失败时回退到 KnowledgeAssistant 做兜底问答

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 中央协调 Agent：完成"意图识别→Agent 选择→上下文隔离→分发→结果聚合"完整路由（双通道：业务操作 business / 管理咨询 consulting）（来源：SPEC §3.2 / 模块拆分方案 契约2）
        - 多轮延续状态管理：pending_intent 触发延续（沿用原意图+槽位合并）、metadata 回写（request_info 挂起/完成清除/只读查询不挂起）（来源：SPEC §3.2 / 业务规格书 v6.25/v6.35/v6.79/v6.84 / 模块拆分方案 契约2 多轮状态契约）
        - 流程接线：workflow_start 实例化（_try_start_workflow）、业务操作单轨制建实例（_intent_workflow_map/_start_biz_workflow）、审批推进（_try_advance_workflow/_try_advance_training）、审批生效业务回调（_apply_workflow_effect，覆盖订单/退货/排产/产品/客户/图纸 6 类）、查询流程分派（_query_workflow_map/_has_query_main_param）（来源：业务规格书 v6.14/v6.43/v6.47/v6.56/v6.58/v6.61/v6.64/v6.65）
        - 审批/流程通知走事件总线：_notify_approval_progress/_notify_rule_targets 经 publish_event 发布（EVENT_NOTIFY_CREATE/APPROVAL/EXPIRE），发布方不感知消费方（来源：模块拆分方案 契约8 / 业务规格书 v6.57/v6.78）
    对外接口（方法/API）：
        - CoordinatorAgent.__init__(agents=None, knowledge_assistant=None, llm_engine=None, intent_llm_engine=None)：注册领域 Agent 与知识助手，注入对话/意图识别 LLM（v6.78.3 双模型，intent_llm_engine 缺省回退 llm_engine）（来源：SPEC §3.2.1 / 业务规格书 v6.78.3）
        - CoordinatorAgent.route(user_input, user_context) -> AgentResponse：主路由入口（M1 /api/chat 唯一调用），五步流程（识别→选择→隔离→分发→聚合）（来源：SPEC §3.2.2 / 模块拆分方案 契约2）
        - CoordinatorAgent._recognize_intent(user_input, user_context=None, skip_llm=False, reasoning_callback=None) -> Intent：三层意图识别（规则→LLM 语义→咨询预判+兜底）（来源：SPEC §3.2 / 业务规格书 v6.78.3）
        - CoordinatorAgent._select_agent(intent) / _isolate_context(user_context) / _aggregate_results(responses)：Agent 选择（audit 回退 sales）、上下文隔离（深拷贝+裁剪敏感字段）、结果聚合（来源：SPEC §3.2.5）
        - CoordinatorAgent._extract_slots(user_input) -> dict：协调器槽位提取（product_code/quantity/order_id/customer_name）（来源：SPEC §3.2.4）
        - Intent.to_dict()：协调器意图对象（name/channel/confidence/slots/target_agent），与 intent_recognition.Intent（dataclass）结构不同（来源：SPEC §3.2.4/§3.11.1）
        - INTENT_REGEXES / CONSULTATION_PATTERNS / INTENT_AGENT_MAP / READONLY_QUERY_INTENTS：内置意图正则路由示例表、咨询预判词表、意图→(通道,Agent) 映射、只读查询意图集合（含 v6.84 workflow_query/v6.80 analysis_query）（来源：SPEC §3.2.3 / 业务规格书 v6.79/v6.80/v6.84）
    错误处理要求：
        - Agent 处理抛异常：返回 action="error" 兜底响应，不中断整体流程（来源：SPEC §3.2.2）
        - 无可用 Agent：返回 action="no_agent" 兜底响应（来源：SPEC §3.2.2）
        - 输入安全检测（SQL/prompt 注入）命中：返回 blocked_injection 阻断响应并记录 security 元数据（来源：业务规格书 v6.71）
        - 业务操作流程三道校验（starter_roles/starter_depts/initiation）失败：admin 放行继续执行业务操作，非 admin 阻断并回复拒绝原因（来源：业务规格书 v6.47 / 契约5）
        - DB 不可用/未配置：置信度阈值（_intent_params）、咨询词表（_consultation_patterns）、流程触发关键词等降级内置默认（来源：SPEC §3.2 / 业务规格书 v6.31/v6.32/v6.43）
        - 流程类型无法识别（workflow_start 未匹配流程定义）：返回 None 由知识助手引导，不挂起 pending（来源：业务规格书 v6.14/v6.46.1）
"""

import atexit
import itertools
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

# v6.80：意图漂移检测——pending 延续时区分"补充信息"与"新业务话题"
# （补充信息零延迟沿用原意图收敛；新业务话题脱离 pending 走强模型发散识别）
from prog.runtime.intent_recognition import looks_like_new_business_query


def _IS_CN(c: str) -> bool:
    """B4：汉字判定（词边界用）。"""
    return "\u4e00" <= c <= "\u9fff"

if TYPE_CHECKING:
    from prog.runtime.base_agent import BaseAgent, AgentResponse


# ============================================================
# 意图识别正则规则（框架内置的通用制造业意图路由示例）
# ============================================================

# 意图 -> 目标Agent类型 映射规则（按优先级从高到低）
# 注意：具体业务意图放在通用意图（confirm/cancel等）之前，避免被通用规则提前匹配
# 框架使用者可整体替换为自身业务的路由规则表。
INTENT_REGEXES: List[tuple] = [
    # 技术相关 -> technical_agent（图纸/BOM/工艺查询路由到technical而非production）
    (r"(图纸|图号|上传图纸|版本管理|图纸变更)", "drawing_management", "business", "technical"),
    (r"(BOM展开|物料清单|BOM结构|BOM查询|bom|BOM)", "bom_management", "business", "technical"),
    (r"(工艺路线|工序|加工工艺|工艺卡|工艺流程|加工工序|工艺参数)", "process_route", "business", "technical"),
    # HR相关 -> hr_agent
    (r"(报工|工时|报工记录|提交报工)", "work_report", "business", "hr"),
    (r"(工资|薪酬|计件工资|工资单|发工资)", "payroll", "business", "hr"),
    (r"(考勤|打卡|出勤|迟到|请假|加班)", "attendance", "business", "hr"),
    (r"(入职|新员工|建档)", "onboarding", "business", "hr"),
    (r"(离职|辞职|交接|离职手续)", "resignation", "business", "hr"),
    (r"(组织架构|部门|人员列表|员工列表|组织结构)", "org_query", "business", "hr"),
    (r"([\u4e00-\u9fa5]{1,4}部).{0,8}(几个人|多少人|人员名单|有几个人|人数)", "org_query", "business", "hr"),
    # 财务操作优先于采购
    (r"(付款|收款|开票|开发票|财务操作|付钱|收钱)", "financial_operation", "business", "finance"),
    # v6.78.3：成本专项分析（先于 financial_query，避免"成本分析"被财务查询吞并）
    # v6.84.1：补"生产成本/产品成本"（"核算一下生产成本"看门狗回退规则层后曾不命中->unknown）
    (r"(成本分析|成本核算|毛利分析|料工费|降本分析|单位成本|成本构成|成本明细|产品成本|生产成本)", "cost_analysis", "business", "finance"),
    (r"(对账|应收|应付|财务|回款|发票|利润|毛利|净利润|成本分析|账龄|财务报表|资金流)", "financial_query", "business", "finance"),
    # 采购/退货/客诉
    # v6.47："采购X原料/材料/物料"属采购申请（INT-20->warehouse），与 create_order 裸"采购"区分
    (r"(采购单|下采购单|供应商|采购订单|采购.{0,12}(原料|材料|物料|物资|耗材)|采购申请|请购|申购)", "purchase", "business", "warehouse"),
    (r"(退货|退货申请|退货单)", "return_order", "business", "sales"),
    (r"(客诉|投诉|客户投诉|质量问题投诉)", "complaint", "business", "qc"),
    # 流程启动与引导
    # v6.59：费用报销直接动词触发（与 intent_recognition DEFAULT_RULES/SEED_RULES_FALLBACK 三处同步），
    # 优先于 query_customer 泛词（"客户现场验收"含"客户"两字会误判 workflow_start）
    (r"(我要?报销|帮我报销|请帮我报销|申请报销|提交报销).{0,40}?(?:元|差旅|餐费|交通|住宿|招待|出差|会议|办公|费)", "workflow_start", "business", "knowledge"),
    (r"(报销|费用报销).{0,8}(?:元|差旅费|餐费|交通费|住宿费|招待费|办公费|会议费)", "workflow_start", "business", "knowledge"),
    (r"(发起流程|启动流程|发起审批|开始流程|提交申请|发起一个.*流程|发起.*审批流程|发起.{0,12}?(?:流程|审批)|申请.{0,12}?(?:报销|流程|审批)|提交.{0,12}?(?:报销|审批|流程申请))", "workflow_start", "business", "knowledge"),
    (r"(流程列表|可发起什么|有哪些流程|流程引导|能发起什么流程)", "workflow_guide", "consulting", "knowledge"),
    # v6.60：流程实例查询（查看既有单据/进度，非发起流程；与 intent_recognition 三处同步）
    (r"(显示|查看|查询|查一下|看看|查查|打开|调出|翻出|找一下|找找|请显示|帮我查).{0,12}(?:流程|审批|报销|实例|工作流|单据).{0,10}(?:内容|详情|信息|进度|状态|记录|历史)?", "workflow_query", "business", "knowledge"),
    (r"(?:实例|编号|单号)\s*[#]?(\d+)", "workflow_query", "business", "knowledge"),
    (r"(流程|审批|报销|工单|单据)(?:内容|详情|进度|状态|记录|历史|信息)", "workflow_query", "business", "knowledge"),
    # v6.61：流程定义训练申请（路由知识助手；与 intent_recognition 三处同步）
    (r"(训练|创建|新建|定义|设计|定制|配置|制作).{0,10}(流程|审批流程|工作流|审批单)", "workflow_train", "business", "knowledge"),
    (r"(流程|审批流程|工作流).{0,8}(训练|定义|创建|新建|定制|设计)", "workflow_train", "business", "knowledge"),
    (r"(把|用|根据|按|依据).{0,8}(这份|这个|该|一下)?(pdf|PDF|文档|文件|附件|制度|模板).{0,12}(训练|做成|定义|创建|生成|制定).{0,10}(流程|审批|工作流)", "workflow_train", "business", "knowledge"),
    # 订单相关 -> sales_agent
    (r"(下[个张笔]?单|(?<![查看找询一])下订单|下一笔订单|下个订单|下笔订单|创建订单|新建订单|订个货|帮我订|要订货|采购|订购|订货|开单|想买|要买)", "create_order", "business", "sales"),
    (r"(下一笔|下一个|下个|帮我下|我要下|给我下).{0,10}订单", "create_order", "business", "sales"),
    (r"(修改订单|改单|变更|追加|加单|加数量|改成|修改数量)", "modify_order", "business", "sales"),
    (r"(取消.{0,2}订单|取消单子|退单|撤销订单|不要这个订单)", "order_cancel", "business", "sales"),
    (r"(查订单|查看订单|查询订单|订单状态|订单进度|订单情况|订单详情|所有订单|订单列表|订单看板|我的订单|我下的单|查一下我的订单|现有订单|现在的订单|有哪些订单|订单有哪些|订单编号|全部订单|查一下订单)", "query_order", "business", "sales"),
    (r"(查一下.+的订单|查询.+的订单|查.+的订单|看看.+的订单)", "query_order", "business", "sales"),
    (r"(查一下|查询|查看|看看).{0,6}(SO|WO|PO|单号|订单号)?[A-Z]{0,2}\d{4,}.*(状态|进度|详情|情况)", "query_order", "business", "sales"),
    # v6.67.3：纯订单号输入（无查询动词，如直接输入 SO20260801001）→ query_order
    (r"(?:^|[^A-Za-z0-9])(SO|WO|PO)\d{6,}(?:$|[^A-Za-z0-9])", "query_order", "business", "sales"),
    # v6.67.4：主谓宾——句首查询动词 + "下订单"类动作短语 = 查询订单
    (r"(查一下|查询|查看|看看|查查|查看一下|查一查|查找|看下).{0,6}(下订单|订订单|已下订单|下过的订单|订过的订单|下单)", "query_order", "business", "sales"),
    # 合同相关 -> sales_agent（规格书确认归属：销售Agent生成合同，见附录A.8 INT-30）
    (r"(生成合同|起草合同|拟合同|签合同|签订合同|合同管理|合同模板|查合同|查询合同|我的合同|合同列表|合同状态|合同详情)", "contract", "business", "sales"),
    (r"(生成|起草|拟|签).{0,20}合同", "contract", "business", "sales"),
    (r"(查一下|查看|看看|查询|有).{0,10}的合同", "contract", "business", "sales"),
    (r"(多少钱|价格多少|报价|售价|单价)", "query_price", "business", "sales"),
    (r"(客户|信用|额度|账期|欠款|应收|客户信息)", "query_customer", "business", "sales"),
    # v6.80：综合分析（business+knowledge，命中 query_intent_map 后路由知识助手
    # 查询流程编排；先于"发货"出库规则——"分析发货单+质检记录"不再被 stock_out 抢走）
    (r"(分析|综合|评估|复盘|汇总).{0,16}(质检|质量|发货|反馈|投诉|订单|客户|不良|缺陷|品质|交付|退货).{0,16}(情况|状况|记录|数据|问题|状态|趋势)?", "analysis_query", "business", "knowledge"),
    (r"(质检|发货|反馈|投诉|订单|客户|品质|交付|退货).{0,8}(分析|评估|复盘|汇总|判断|建议)", "analysis_query", "business", "knowledge"),
    # 库存相关 -> warehouse_agent
    (r"(查库存|查一下库存|库存查询|查询库存|库存多少|库存情况|现货情况|还有多少|剩多少|有没有货|备货情况|看看库存|有没有库存)", "query_inventory", "business", "warehouse"),
    (r"(查.*库存|库存.*多少|库存.*情况)", "query_inventory", "business", "warehouse"),
    (r"(库存调整|调整库存|盘盈|盘亏|库存修正|库存盘点)", "inventory_adjust", "business", "warehouse"),
    (r"(入库|收货)", "stock_in", "business", "warehouse"),
    (r"(出库|发货)", "stock_out", "business", "warehouse"),
    # 生产相关 -> production_agent
    (r"(工单|创建工单|工单管理|派工|派单|工单状态)", "work_order", "business", "production"),
    (r"(工单查询|查工单|查一下工单|工单进度)", "work_order_query", "business", "production"),
    (r"(设备|设备状态|设备保养|设备故障|设备维修|设备管理|机台|机床)", "equipment", "business", "production"),
    (r"(设备查询|查设备|设备列表)", "equipment_query", "business", "production"),
    (r"(停机|设备故障报修|维修|TPM|保养报修)", "report_issue", "business", "production"),
    (r"(排产|安排生产|计划生产|生产计划)", "schedule_production", "business", "production"),  # v6.90：移除 SMED/换线/换模（归 management_consulting，见下方 ANCHOR）
    (r"(产能|排班|插单|还能排|排多少|负荷|外协|交期)", "query_schedule", "business", "production"),
    (r"(生产进度|生产看板|生产状态|产线状态|进度)", "query_production_progress", "business", "production"),
    (r"(进度查询|瓶颈|产能不足|工时|节拍|CT时间)", "query_progress", "business", "production"),
    # 质检相关 -> qc_agent
    (r"(质检|品检|QC|qc|合格率|不良品|不良率|不良分析|缺陷分析|质量趋势|质量分析|Top不良|不合格率|不合格品)", "query_qc", "business", "qc"),
    (r"(查|查一下|查询|查看|看看|查查|查一查).{0,12}(质量问题|缺陷|划痕|划伤|毛刺|裂纹|变形|色差|气泡|硬度|超差|不良|不合格)", "query_qc", "business", "qc"),  # R2.5：查询型质量检索（v6.44，与 intent_recognition 同步）
    (r"(纠正措施|预防措施|CAPA|8D|FMEA|失效模式|风险分析|PPAP|生产件批准)", "quality_action", "business", "qc"),
    # R3：质量类咨询组合（质量词+怎么办，先于知识咨询规则，路由质检Agent）
    (r"(质量|不良|缺陷|超差|不合格|品质|工艺|划痕|划伤|毛刺|裂纹|变形|色差|硬度).*(怎么办|怎么解决|怎么改善|怎么优化|怎么改进|如何改进)", "query_qc", "business", "qc"),
    # 内审查询 -> audit
    (r"(审计|内审|查账|审核记录|合规|日志|操作记录|违规|越权)", "query_audit", "business", "audit"),
    # 数据总览
    (r"(数据总览|数据看板|经营概况|工厂概况|整体情况|数据汇总|总览|概览)", "query_overview", "business", "sales"),
    (r"(经营数据|经营情况|经营状况|本月数据|月度数据|经营数据怎么样|数据总览|经营概况|经营指标|销售数据|订单数据|产值数据|产量数据|库存数据|财务数据)", "query_overview", "business", "sales"),
    # 知识管理 -> knowledge（consulting通道，与 DB 种子/INTENT_AGENT_MAP 一致）
    (r"(知识管理|知识库|文档管理|经验库)", "knowledge_management", "consulting", "knowledge"),
    # 确认/取消（通用意图，放在具体业务意图之后）
    (r"(确认|同意|批准|执行|提交|没问题|就这样)", "confirm", "business"),
    (r"(取消|放弃|不要了|算了|不了|不用了)", "cancel", "business"),
    # 管理咨询 -> knowledge（consulting通道）
    (r"(管理咨询|管理制度|流程制度|管理建议|管理优化|如何管理|怎么管理)", "management_consulting", "consulting", "knowledge"),
    # ══════════════════════════════════════════════════════════════
    # PERF-FIX-v6.40 ANCHOR::制造业管理咨询高频词（与 intent_recognition.DEFAULT_RULES 镜像）
    # 详见 intent_recognition.py ANCHOR 注释 — 根因/修复/防回归规则完全一致
    # 禁止单方面只改一侧，两侧规则集必须同步
    # ══════════════════════════════════════════════════════════════
    (r"(精益|精益生产|丰田生产方式|TPS|JIT|准时化|拉动生产|看板生产|一个流|单件流|连续流)", "management_consulting", "consulting", "knowledge"),
    (r"(5S|6S|7S|8S|整理|整顿|清扫|清洁|素养|安全|节约|学习)", "management_consulting", "consulting", "knowledge"),
    (r"(全面质量管理|TQM|QC七大手法|新QC七大手法|质量屋|QFD|质量机能展开)", "management_consulting", "consulting", "knowledge"),
    (r"(PDCA|戴明环|SDCA|持续改善|Kaizen|改善提案|小集团活动|QCC|品管圈)", "management_consulting", "consulting", "knowledge"),
    (r"(ISO|ISO9000|ISO9001|ISO14001|IATF16949|ISO.*体系|体系认证|内审|外审|管理评审)", "management_consulting", "consulting", "knowledge"),
    (r"(看板|Kanban|安灯|Andon|安东|快速换模|SMED|全员生产维护|TPM|防错|Pokayoke|5Why|五个为什么|鱼骨图|特性要因图|帕累托|柏拉图|VSM|价值流图|价值流分析|OEE|设备综合效率|稼动率|线平衡|生产节拍|Takt Time|标准作业|SOP|作业标准|标准工时|平准化|混流生产|多能工|细胞生产|Cell|工序分割|搬运改善|超市|水蜘蛛|Mizusumashi)", "management_consulting", "consulting", "knowledge"),
    (r"(六西格玛|6σ|6Sigma|DMAIC|DFSS|黑带|绿带|SBTI|精益六西格玛)", "management_consulting", "consulting", "knowledge"),
    # ══════════════════════════════════════════════════════════════
    # PERF-FIX-v6.40 ANCHOR::结束
    # ══════════════════════════════════════════════════════════════
    # 数据分析 -> knowledge（consulting通道）
    (r"(数据分析|数据统计|数据报表|经营分析|报表分析|趋势分析)", "data_analysis", "consulting", "knowledge"),
    # 问候/寒暄
    (r"(你好|您好|hi|hello|早上好|下午好|晚上好|在吗|哈喽)", "greeting", "consulting", "knowledge"),
    (r"(谢谢|感谢|多谢|thanks|辛苦了)", "thanks", "consulting", "knowledge"),
    (r"(再见|拜拜|bye|走了|回见)", "farewell", "consulting", "knowledge"),
    (r"(你是谁|你能做什么|功能|帮助|怎么用|有什么用)", "help", "consulting", "knowledge"),
    # 系统操作
    (r"(登录|切换|谁在用|退出|我是谁|当前用户|登出|注销)", "system", "business", "knowledge"),
    # 知识查询
    (r"(问一下|搜一下|搜索|上网查|网上查|查询资料|查资料|查一下资料|查查资料|说明书|文档|怎么办|怎么解决|有什么建议|怎么改善)", "knowledge_query", "consulting", "knowledge"),
    (r"(如何降低|怎么降低|降低成本|如何降本|怎么降本|降本增效|如何提高|怎么提升|如何提升|怎么提高|如何改善|怎么优化|如何优化|降本|提效)", "knowledge_query", "consulting", "knowledge"),
    (r"(操作规程|作业指导书|操作手册|作业规范|工艺规范|安全规程|操作规范)", "knowledge_query", "consulting", "knowledge"),
]

# 咨询类问题预判关键词
CONSULTATION_PATTERNS = [
    '怎么办', '怎么解决', '如何解决', '有什么办法', '有没有什么办法',
    '有什么建议', '怎么改善', '怎么优化', '如何提升', '怎么降低',
    '如何降本', '怎么降本', '降本增效', '怎么提高', '如何提高', '怎么提升',
    '如何降低', '降低成本', '有没有好', '有什么好', '案例', '实战案例', '真实案例',
    '为什么', '什么原因', '怎么做到', '如何实现', '怎么实现',
    '怎么管理', '如何管理', '怎么控制', '如何控制',
    '矛盾', '冲突', '难点', '痛点', '挑战',
]


def _consultation_patterns() -> list:
    """咨询类预判关键词（v6.31 从 system_configs 读取，可配置扩展无需改代码）。

    system_configs.config_key='CONSULTATION_PATTERNS' 存 JSON 数组；
    DB 不可用/未配置时降级返回内置默认词表。
    W3：TTL 缓存（_CONFIG_CACHE_TTL 秒）——避免每轮对话查库；
    配置变更在 TTL 后自动重新加载。
    """
    cached = _INTENT_PARAM_CACHE.get("consultation")
    if cached and (time.time() - cached[0]) < _CONFIG_CACHE_TTL:
        return cached[1]
    with _CONFIG_CACHE_LOCK:
        # 双重检查：锁内再查一次（其他线程可能已刷新缓存）
        cached = _INTENT_PARAM_CACHE.get("consultation")
        if cached and (time.time() - cached[0]) < _CONFIG_CACHE_TTL:
            return cached[1]
        try:
            from prog.runtime.database import get_database
            import json
            db = get_database()
            row = db.query_one("system_configs", {"config_key": "CONSULTATION_PATTERNS"})
            if row and row.get("config_value"):
                val = json.loads(row["config_value"])
                if isinstance(val, list) and val:
                    _INTENT_PARAM_CACHE["consultation"] = (time.time(), list(val))
                    return list(val)
        except Exception:
            pass
        _INTENT_PARAM_CACHE["consultation"] = (time.time(), CONSULTATION_PATTERNS)
    return CONSULTATION_PATTERNS


# 意图识别置信度阈值（v6.32 配置化，DB 不可用降级内置）
# W3：配置缓存统一 TTL 策略（原：_intent_params 永久缓存 vs _consultation_patterns
# 每次查库——矛盾）。统一为 TTL 缓存容器：键 "params" / "consultation" 存
# (加载时间戳, 值)，TTL 后自动重新查库（配置热更新），期间免查库。
# 保留 _INTENT_PARAM_CACHE 变量名：测试 fixture 依赖 clear() 失效缓存。
_CONFIG_CACHE_TTL = 60.0
_INTENT_PARAM_CACHE: Dict = {}
# I15：配置缓存读写锁（双重检查锁定）——避免并发下多个线程同时查库并写回缓存
_CONFIG_CACHE_LOCK = threading.Lock()

# W2：对话持久化写库线程池（有界 4 线程）——替代每轮裸 threading.Thread，
# 避免高并发下线程无界创建
_LOG_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="log-writer")

# S6：工单 ID 生成进程级自增序号（itertools.count 在 CPython GIL 下原子），
# 消除同一秒内多订单生成相同 WO 的碰撞风险（原 ts%10000+工序序号可碰撞）
_WO_ID_SEQ = itertools.count(1)


def _shutdown_log_executor() -> None:
    """W31：程序退出时优雅关闭日志写库线程池，确保待写入日志落盘不丢失。"""
    try:
        _LOG_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass


atexit.register(_shutdown_log_executor)

_INTENT_PARAM_DEFAULTS = {
    "confidence_llm_min": 0.7,      # LLM 返回意图需达到的最低置信度
    "confidence_rule_hit": 0.9,     # 正则规则命中置信度
    "confidence_consult_pre": 0.6,  # 咨询类预判置信度
    "confidence_unknown": 0.3,      # 未识别兜底置信度
    "confidence_pending_continuation": 0.8,  # 多轮延续置信度
}


def _intent_params() -> dict:
    """意图识别置信度阈值（system_configs.INTENT-PARAMS JSON），DB 不可用降级内置。

    v6.32：三层识别置信度阈值配置化，调整无需改代码；
    配置键 INTENT-PARAMS 存 {"confidence_llm_min": 0.7, ...}。
    W3：TTL 缓存（_CONFIG_CACHE_TTL 秒）——配置变更在 TTL 后自动生效。
    """
    cached = _INTENT_PARAM_CACHE.get("params")
    if cached and (time.time() - cached[0]) < _CONFIG_CACHE_TTL:
        return cached[1]
    with _CONFIG_CACHE_LOCK:
        # 双重检查：锁内再查一次（其他线程可能已刷新缓存）
        cached = _INTENT_PARAM_CACHE.get("params")
        if cached and (time.time() - cached[0]) < _CONFIG_CACHE_TTL:
            return cached[1]
        val = dict(_INTENT_PARAM_DEFAULTS)
        try:
            from prog.runtime.database import get_database
            import json
            db = get_database()
            row = db.query_one("system_configs", {"config_key": "INTENT-PARAMS"})
            if row and row.get("config_value"):
                parsed = json.loads(row["config_value"])
                if isinstance(parsed, dict):
                    for k in _INTENT_PARAM_DEFAULTS:
                        if isinstance(parsed.get(k), (int, float)):
                            val[k] = parsed[k]
        except Exception:
            pass
        _INTENT_PARAM_CACHE["params"] = (time.time(), val)
    return val



# ============================================================
# 意图对象
# ============================================================

class Intent:
    """
    意图对象。

    用于封装意图识别结果，传递给 _select_agent() 做路由。

    属性说明：
        - name: 意图标签（如"create_order"/"query_inventory"）
        - channel: 所属通道（"business" 业务操作 / "consulting" 管理咨询）
        - confidence: 识别置信度（0~1）
        - slots: 从输入中抽取的槽位（如客户名、产品名、数量）
        - target_agent: 目标Agent类型（如"sales"/"knowledge"）
    """

    def __init__(self, name: str = "unknown", channel: str = "business",
                 confidence: float = 0.0, slots: Optional[Dict[str, Any]] = None,
                 target_agent: str = "", source: str = ""):
        """初始化意图对象。"""
        self.name = name
        self.channel = channel
        self.confidence = confidence
        self.slots = slots or {}
        self.target_agent = target_agent
        # W27：识别来源字段（rule/llm/fallback/pending 等；供对话持久化
        # metadata.source 追踪，消除 getattr(intent, "source", "") 恒空的死代码）
        self.source = source

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "channel": self.channel,
            "confidence": self.confidence,
            "slots": self.slots,
            "target_agent": self.target_agent,
            "source": self.source,
        }


class CoordinatorAgent:
    """
    协调Agent。

    设计意图：
        作为系统的中央调度器，承接所有用户输入，完成意图识别与Agent分发。
        自身不持有业务状态，仅持有各领域Agent的引用。

    属性：
        agents: 已注册的领域Agent字典 {agent_type: BaseAgent实例}
        knowledge_assistant: 知识助手实例（管理咨询通道）
        intent_recognizer: 意图识别器（可选，未注入时使用内置规则路由）

    生命周期：
        route() -> _recognize_intent -> _select_agent -> _isolate_context
              -> agent.process() -> _aggregate_results -> AgentResponse

    三层意图识别：
        第一层：正则规则快速匹配（零延迟、零 token，覆盖 ~80% 高频意图）
        第二层：LLM 语义理解（规则未命中时调用 LLM 做意图分类+槽位提取，覆盖 ~15%）
        第三层：上下文延续 + 咨询预判 + 兜底（~5%）
    """

    # 意图名 -> (通道, 目标Agent) 映射表
    # 用于将 IntentRecognizer 返回的意图名映射到 coordinator 的路由信息
    # v6.13：扩充 DB 种子规则（migrations/009）已有但映射表缺失的意图路由，
    # 避免 LLM/规则返回这些意图时 target_agent 为空导致兜底到 knowledge
    INTENT_AGENT_MAP: Dict[str, tuple] = {
        # 销售
        "create_order": ("business", "sales"),
        "query_order": ("business", "sales"),
        "modify_order": ("business", "sales"),
        "order_cancel": ("business", "sales"),
        "query_price": ("business", "sales"),
        "contract": ("business", "sales"),  # 合同生成/查询（DB RULE-INT-004/005）
        "query_customer": ("business", "sales"),  # 客户查询（DB RULE-INT-042）
        "return_order": ("business", "sales"),
        # v6.30 路由修正（与规格 §A.8 INT-20/INT-24/INT-25 及 INTENT_REGEXES 降级路径一致）：
        #   采购操作(INT-20)->仓储Agent；退货(INT-24)->销售Agent；客诉(INT-25)->QC Agent
        "purchase": ("business", "warehouse"),
        "complaint": ("business", "qc"),
        # 仓储
        "query_inventory": ("business", "warehouse"),
        "inventory_adjust": ("business", "warehouse"),
        "stock_in": ("business", "warehouse"),
        "stock_out": ("business", "warehouse"),
        # 生产
        "schedule_production": ("business", "production"),
        "query_progress": ("business", "production"),
        "query_schedule": ("business", "production"),  # 排产查询（DB RULE-INT-031）
        "query_production_progress": ("business", "production"),  # 生产进度（DB RULE-INT-038）
        "work_order_query": ("business", "production"),
        "work_order": ("business", "production"),  # 工单管理（DB RULE-INT-012）
        "equipment_query": ("business", "production"),
        "equipment": ("business", "production"),  # 设备管理（DB RULE-INT-014）
        "report_issue": ("business", "production"),
        # 技术
        "drawing_op": ("business", "technical"),
        "drawing_management": ("business", "technical"),
        "bom_management": ("business", "technical"),
        "process_route": ("business", "technical"),
        # 质检
        "query_qc": ("business", "qc"),
        "quality_action": ("business", "qc"),
        # HR
        "work_report": ("business", "hr"),
        "payroll": ("business", "hr"),
        "attendance": ("business", "hr"),
        "onboarding": ("business", "hr"),
        "resignation": ("business", "hr"),
        "org_query": ("business", "hr"),
        # 财务
        "financial_query": ("business", "finance"),
        "financial_operation": ("business", "finance"),
        "cost_analysis": ("business", "finance"),  # 成本分析（v6.78.3，财务口径核算）
        # 审计/总览
        "query_audit": ("business", "audit"),  # 内审查询（DB RULE-INT-044）
        "query_overview": ("business", "sales"),  # 数据总览（DB RULE-INT-045）
        # 知识/咨询
        "knowledge_query": ("consulting", "knowledge"),
        "knowledge_management": ("consulting", "knowledge"),
        "management_consulting": ("consulting", "knowledge"),
        "data_analysis": ("consulting", "knowledge"),
        "analysis_query": ("business", "knowledge"),  # 综合分析（v6.80，查询流程承接）
        # 寒暄类（DB RULE-INT-052~055，统一路由知识助手兜底）
        "chitchat": ("consulting", "knowledge"),
        "greeting": ("consulting", "knowledge"),
        "thanks": ("consulting", "knowledge"),
        "farewell": ("consulting", "knowledge"),
        "help": ("consulting", "knowledge"),
        # 流程
        # workflow_start 路由说明：业务通道(business) + 知识助手(knowledge)，
        # 语义为"发起流程时由知识助手引导用户选择流程模板并填写表单"，
        # 与 DB 种子 RULE-INT-027 一致。knowledge_query 也路由 knowledge 但通道为
        # consulting（咨询问答），两者通过 channel 区分语义
        "workflow_start": ("business", "knowledge"),
        "workflow_guide": ("consulting", "knowledge"),
        # W7：补齐 INTENT_REGEXES 中已有但映射表缺失的流程意图路由
        # （workflow_query=查看既有流程单据/进度，workflow_train=流程定义训练申请，
        # 与 INTENT_REGEXES 119-125 行及 intent_recognition 三处同步）
        "workflow_query": ("business", "knowledge"),
        "workflow_train": ("business", "knowledge"),
        # 系统（与 DB 种子 RULE-INT-056 一致，路由知识助手兜底）
        "system_op": ("business", "knowledge"),
        "system": ("business", "knowledge"),  # system 别名（DB RULE-INT-056）
        # 通用确认/取消（无具体目标Agent，由调用方上下文决定）
        "confirm": ("business", ""),
        "cancel": ("business", ""),
    }

    # v6.79：只读查询类意图（业务通道直达路径，不含 query_flow 已排除项）。
    # 此类意图执行完毕（出结果/查无数据）不应挂起 pending_intent——否则遗留
    # pending 会把下一轮新查询吞并为原意图延续（例："OEE给我拉一下"查无数据
    # 遗留 query_progress pending；随后"咱库存是不是快见底了"规则未命中→unknown
    # →被延续为 query_progress 误路由）。仅当 Agent 明确向用户索要查询参数
    # （action=request_info/ask_more_info，如"请提供订单号"→"SOxxx"）才挂起，
    # 此时下方 route() pending 挂起条件不拦截，多轮补充照常延续。
    READONLY_QUERY_INTENTS: frozenset = frozenset({
        "query_inventory", "query_order", "query_price", "query_customer",
        "query_progress", "query_schedule", "query_production_progress",
        "equipment_query", "query_qc", "query_audit", "query_overview",
        "cost_analysis", "financial_query", "work_order_query",
        # v6.84：workflow_query（查看既有流程单据/进度）为只读查询，执行完毕
        # 不挂 pending——否则遗留 pending 吞掉下一轮无业务词输入（"那合同呢"），
        # 且不属 query_flow 分派路径（knowledge 直达）。仅当知识助手索要查询
        # 参数（request_info/ask_more_info）时才挂起，多轮补充照常延续。
        "workflow_query",
        # v6.80：综合分析（查询流程承接，只读分析不挂 pending）
        "analysis_query",
    })

    # S7：待审批阶段留言判定的"业务意图旁路"集合——明确命中写操作类业务
    # 意图时（如"帮我下个单"/"查工资"）走正常路由，不被吞为审批留言。
    # 查询/咨询/补充说明类输入（financial_query/unknown/knowledge_query 等）
    # 仍按 v6.85 语义视为留言（"请补充住宿发票和审批理由"含"发票"触发
    # financial_query 仍需落留言），本集合只拦截"新的写操作请求"。
    _APPROVAL_COMMENT_BYPASS_INTENTS: frozenset = frozenset({
        "create_order", "modify_order", "order_cancel", "return_order",
        "complaint", "contract", "purchase", "stock_in", "stock_out",
        "inventory_adjust", "schedule_production", "work_order",
        "report_issue", "equipment", "drawing_management", "bom_management",
        "process_route", "quality_action", "work_report", "payroll",
        "attendance", "onboarding", "resignation", "financial_operation",
        "workflow_start", "workflow_train",
    })

    def _db_route_map(self) -> Dict[str, tuple]:
        """从 IntentRecognizer 已加载的 intent_rules 表规则派生 意图名->(channel, agent) 路由表。

        v6.31：路由随训练变化——训练修改 intent_rules.target_agent/target_channel 后
        reload 即生效，内置兜底 / INTENT_REGEXES 降级路径的路由均以 DB 为准。
        DB 不可用或表为空时返回空表（调用方回退 INTENT_AGENT_MAP / INTENT_REGEXES 自带路由）。
        """
        recognizer = self._intent_recognizer
        if recognizer is None or not getattr(recognizer, "_db_rules_ok", False):
            return {}
        route_map: Dict[str, tuple] = {}
        for rule in getattr(recognizer, "_db_rules", []):
            if rule.intent_name not in route_map and rule.target_agent:
                route_map[rule.intent_name] = (
                    rule.target_channel or "business", rule.target_agent)
        return route_map

    def __init__(self, agents: Optional[Dict[str, Any]] = None,
                 knowledge_assistant: Any = None,
                 llm_engine: Any = None,
                 intent_llm_engine: Any = None):
        """
        初始化协调Agent。

        参数：
            agents: 已注册的业务领域Agent字典，key为agent_type
            knowledge_assistant: 知识助手实例（管理咨询通道兜底）
            llm_engine: LLM引擎实例（对话/Agent回复通道，快模型）
            intent_llm_engine: LLM引擎实例（v6.78.3 双模型架构：语义
                理解专用强模型，注入 IntentRecognizer 做第二层语义识别。
                为 None 时回退使用 llm_engine，保持单模型兼容）
        """
        self.agents = agents or {}
        self.knowledge_assistant = knowledge_assistant

        # R1修复：保存 llm_engine（供 chat.py 延迟注入 LLM 语义摘要，
        # 并注入 IntentRecognizer 启用第二层 LLM 语义识别）
        self._llm_engine = llm_engine

        # 三层意图识别器：规则匹配优先（零延迟），规则未命中时启用 LLM 语义兜底
        # v6.78.3：识别用强模型（intent_llm_engine 优先，缺省回退 llm_engine）
        try:
            from prog.runtime.intent_recognition import IntentRecognizer
            self._intent_recognizer = IntentRecognizer(
                llm_client=intent_llm_engine or llm_engine)
        except Exception:
            self._intent_recognizer = None

    # --------------------------------------------------------
    # 主路由入口
    # --------------------------------------------------------
    def route(self, user_input: str, user_context: Dict[str, Any]) -> "AgentResponse":
        """
        主路由入口。

        设计意图：
            接收所有用户输入，完成"识别->选择->隔离->分发->聚合"的完整流程。
            这是外部调用 Coordinator 的唯一入口。

        参数：
            user_input: 用户原始输入文本
            user_context: 用户会话上下文（含身份、权限、对话历史）

        返回：
            AgentResponse: 最终聚合后的响应

        流程：
            1. _recognize_intent 识别意图
            2. _select_agent 选择目标Agent
            3. _isolate_context 隔离上下文
            4. 调用目标Agent.process()
            5. _aggregate_results 聚合（单Agent时直接透传）
        """
        from prog.runtime.base_agent import AgentResponse

        # 1. 意图识别
        # v6.39：有 pending_intent 时跳过 LLM 回退（多轮补充信息如"A-202"不触发LLM延迟）
        pending = (user_context or {}).get("pending_intent")
        has_pending = bool(pending and isinstance(pending, dict) and pending.get("target_agent"))
        # v6.80 意图漂移检测（发散-收敛平衡）：pending 下"新业务话题"（含明确
        # 业务名词，如 pending 下单收集中问"咱库存…"）不跳过 LLM——交由强模型
        # 发散重新识别；纯补充信息（产品码/数量/订单号）才跳过 LLM 收敛沿用原意图。
        skip_llm = has_pending and not looks_like_new_business_query(user_input)
        # v6.78.3 双模型架构：chat.py 预识别已用强模型计算 intent（并流式推送
        # reasoning 到前端），此处复用避免第二次强模型调用（双延迟/双 token）。
        # 仅当预识别的 skip_llm 语义与本路由一致时复用，否则重新识别（如预识别
        # 因 pending 跳过 LLM、而本路由判定无 pending 需完整识别）。
        _pre_intent = (user_context or {}).get("_pre_intent")
        _pre_skip = (user_context or {}).get("_pre_intent_skip_llm")
        if _pre_intent is not None and _pre_skip == skip_llm:
            intent = _pre_intent
        else:
            intent = self._recognize_intent(user_input, user_context, skip_llm=skip_llm)

        # 1.1 审批推进接线（v6.56）：待审批阶段 confirm/同意 -> advance_step。
        # 缺陷4根因修复：此前 confirm 意图无业务调用，"同意这笔报销"落入知识问答。
        # 字段收集完成后 pending.phase='awaiting_approval' 且携带 workflow_instance，
        # 此时 confirm 按 DB 审批链（approval_chain）逐级推进流程实例审批。
        # v6.61：pending.phase='workflow_train' 时 confirm 推进流程定义训练审批
        # （workflow_configs 审批记录行），全部通过后流程定义生效。
        _p_train = None
        if isinstance(pending, dict) and pending.get("phase") == "workflow_train":
            _p_train = pending
        if intent.name == "confirm" and _p_train:
            adv_result = self._try_advance_training(
                _p_train.get("config_id"), user_context)
            wf_name = adv_result.get("workflow_name") or "流程定义训练"
            if adv_result.get("success"):
                if adv_result.get("completed"):
                    doc = ""
                    try:
                        ka = self.knowledge_assistant
                        if ka is not None and hasattr(ka, "render_training_doc"):
                            doc = ka.render_training_doc(
                                _p_train.get("config_id"),
                                adv_result.get("proposed") or {},
                                (user_context or {}).get("user", {}),
                                steps_done=adv_result.get("steps_done"),
                                status="completed",
                                current_step=adv_result.get("current_step", 1))
                    except Exception:
                        doc = ""
                    content = (doc + f"\n\n✅ 「{wf_name}」流程训练已审批通过并生效（审批单 "
                               f"{_p_train.get('config_id')}）。" if doc else
                               f"✅ 「{wf_name}」流程训练已审批通过并生效。")
                    response = AgentResponse(
                        content=content,
                        action="training_applied",
                        agent_name="流程训练Agent",
                    )
                else:
                    content = (f"「{wf_name}」流程训练审批已通过"
                               f"（第 {adv_result.get('current_step')}/"
                               f"{len(adv_result.get('chain') or [])} 步），"
                               f"待下一审批人处理。")
                    response = AgentResponse(
                        content=content,
                        action="training_advanced",
                        agent_name="流程训练Agent",
                    )
            else:
                content = (f"流程训练审批推进失败：{adv_result.get('error')}。"
                           f"请确认您是否为当前步骤审批人。")
                response = AgentResponse(
                    content=content,
                    action="training_failed",
                    agent_name="流程训练Agent",
                )
            aggregated = self._aggregate_results([response])
            meta = dict(aggregated.metadata or {})
            meta.update({
                "intent": intent.name,
                "intent_agent": intent.target_agent,
                "intent_channel": intent.channel,
            })
            if adv_result.get("success") and adv_result.get("completed"):
                # 训练全部通过：流程已生效，清除待延续状态
                meta["pending_intent"] = None
            else:
                # 多级审批：保留训练审批单供下一审批人继续推进
                meta["pending_intent"] = _p_train
            aggregated.metadata = meta
            self._log_interaction(user_input, intent, aggregated, user_context)
            return aggregated

        _p_approve = None
        if isinstance(pending, dict) and pending.get("phase") == "awaiting_approval":
            _pwf = pending.get("workflow_instance")
            if isinstance(_pwf, dict) and _pwf.get("instance_id"):
                _p_approve = _pwf
        if intent.name == "confirm" and _p_approve:
            adv_result = self._try_advance_workflow(
                _p_approve.get("instance_id"),
                _p_approve.get("workflow_type"),
                user_context)
            if adv_result.get("success"):
                if adv_result.get("completed"):
                    content = (f"「{adv_result.get('workflow_name')}」流程审批已全部通过，"
                               f"流程已完成并生效。")
                else:
                    content = (f"「{adv_result.get('workflow_name')}」审批已通过"
                               f"（实例 {adv_result.get('instance_id')}），"
                               f"流程推进至第 {adv_result.get('current_step')} 步，"
                               f"待下一审批人处理。")
            else:
                content = (f"审批推进失败：{adv_result.get('error')}。"
                           f"请确认您是否为当前步骤审批人。")
            response = AgentResponse(
                content=content,
                action=("approval_advanced" if adv_result.get("success")
                        else "approval_failed"),
                agent_name="流程审批Agent",
            )
            aggregated = self._aggregate_results([response])
            meta = dict(aggregated.metadata or {})
            meta.update({
                "intent": intent.name,
                "intent_agent": intent.target_agent,
                "intent_channel": intent.channel,
            })
            if adv_result.get("success"):
                if adv_result.get("completed"):
                    # 全部审批通过：清除待审批延续状态
                    meta["pending_intent"] = None
                else:
                    # 多级审批：保留实例供下一审批人继续推进
                    meta["pending_intent"] = {
                        "name": pending.get("name", "workflow_start"),
                        "target_agent": pending.get("target_agent", "knowledge"),
                        "slots": dict(pending.get("slots") or {}),
                        "action": "awaiting_approval",
                        "phase": "awaiting_approval",
                        "workflow_instance": {
                            "instance_id": _p_approve.get("instance_id"),
                            "workflow_type": _p_approve.get("workflow_type"),
                        },
                    }
            else:
                # 推进失败：保留待审批状态，允许重试
                meta["pending_intent"] = pending
            aggregated.metadata = meta
            self._log_interaction(user_input, intent, aggregated, user_context)
            return aggregated

        # S6（v6.88）：待审批阶段取消/驳回分支——"取消/不要了"不再静默清 pending
        # （此前 cancel 意图 target 为空 → 兜底清 pending → workflow_instances
        # 永久 running、无取消记录）。现置实例 cancelled、留痕 operation_logs、
        # 通知发起人，并清除 pending 延续。
        if intent.name == "cancel" and _p_approve:
            _cinst_id = _p_approve.get("instance_id")
            _cinst_type = _p_approve.get("workflow_type")
            cancel_result = self._cancel_workflow_instance(
                _cinst_id, _cinst_type, user_context)
            if cancel_result.get("success"):
                content = (f"「{cancel_result.get('workflow_name')}」审批已取消"
                           f"（实例 {_cinst_id}），流程终止，发起人已收到通知。")
            else:
                content = (f"流程取消失败：{cancel_result.get('error')}。")
            response = AgentResponse(
                content=content,
                action=("workflow_cancelled" if cancel_result.get("success")
                        else "workflow_cancel_failed"),
                agent_name="流程审批Agent",
            )
            aggregated = self._aggregate_results([response])
            meta = dict(aggregated.metadata or {})
            meta.update({
                "intent": intent.name,
                "intent_agent": intent.target_agent,
                "intent_channel": intent.channel,
                # 取消后不再延续待审批状态
                "pending_intent": None,
            })
            aggregated.metadata = meta
            self._log_interaction(user_input, intent, aggregated, user_context)
            return aggregated

        # v1.6.57 审批留言（多人协作场景①）：待审批阶段，非 confirm/cancel、
        # 非查询动词开头的输入识别为审批意见/留言，写入 workflow_comments 供
        # workflow_query 单据展示；保持 pending（awaiting_approval）不变，
        # 申请人/后续审批人可同上下文往返。留言不进入知识库（过程性数据）。
        # v6.85 修正：留言输入可能命中其他业务意图（"请补充住宿发票和审批理由"
        # 含"发票"触发 financial_query），不可按意图白名单排除——待审批阶段
        # 除 confirm/cancel 与查询动词开头外均视为留言（查询走正常路由）。
        if _p_approve and intent.name not in ("confirm", "cancel"):
            _cmt_content = (user_input or "").strip()
            # S7 修复：待审批阶段明确命中写操作类业务意图时（如"帮我下个单"），
            # 视为新业务请求走正常路由，不被吞为审批留言；留言判定仅保留
            # 咨询/查询/补充说明类输入（v6.85"补充住宿发票"场景仍走留言）。
            _cmt_bypass = (
                intent.name in CoordinatorAgent._APPROVAL_COMMENT_BYPASS_INTENTS)
            # 查询动词开头（显示/查看/查询/查/看下等）视为查询而非留言
            if (_cmt_content and not _cmt_bypass and not re.match(
                    r"^(显示|查看|查询|查一下|查查|看看|打开|调出|翻出|找一下|"
                    r"找找|请显示|帮我查|请查看|看下|查下|请查|帮查)", _cmt_content)):
                _cmt_inst_id = _p_approve.get("instance_id")
                _cmt_step = 1
                _cmt_ok = False
                _cmt_err = ""
                try:
                    from prog.runtime.workflow_enforcer import WorkflowEnforcer
                    _db = None
                    try:
                        from prog.runtime.database import get_database
                        _db = get_database()
                    except Exception:
                        _db = None
                    _wf = WorkflowEnforcer(database=_db)
                    _inst = _wf._get_instance(_cmt_inst_id)
                    _cmt_step = int((_inst or {}).get("current_step") or 1)
                    _cmt_res = _wf.add_comment(
                        _cmt_inst_id, _cmt_step,
                        (user_context or {}).get("user", {}) or {},
                        _cmt_content)
                    _cmt_ok = bool(_cmt_res.get("success"))
                    _cmt_err = str(_cmt_res.get("error") or "")
                except Exception as e:
                    _cmt_err = str(e)
                if _cmt_ok:
                    content = (f"📌 已记录审批意见（实例 {_cmt_inst_id}，第 "
                               f"{_cmt_step} 步）：「{_cmt_content[:120]}」\n"
                               f"可继续补充意见，或输入「同意」推进审批。")
                else:
                    content = f"留言记录失败：{_cmt_err}。"
                response = AgentResponse(
                    content=content,
                    action=("workflow_comment" if _cmt_ok
                            else "workflow_comment_failed"),
                    agent_name="流程审批Agent",
                )
                aggregated = self._aggregate_results([response])
                meta = dict(aggregated.metadata or {})
                meta.update({
                    "intent": intent.name,
                    "intent_agent": intent.target_agent,
                    "intent_channel": intent.channel,
                    # 保持待审批延续，供下一轮 confirm 推进或继续留言
                    "pending_intent": pending,
                })
                aggregated.metadata = meta
                self._log_interaction(
                    user_input, intent, aggregated, user_context)
                return aggregated

        # 多轮延续：若会话上下文中存在"待延续的业务意图"（pending_intent），
        # 且当前输入未明确命中新的业务意图，则沿用原意图路由到原Agent。
        # 触发条件：存在 pending 且（当前意图为 unknown / knowledge_query /
        # greeting / help / chitchat（R5：谢谢/感谢等确认信号，避免中断多轮业务））
        # v6.36 修复：移除 v6.13 的 has_business_slot/is_confirm_like 限制，
        #   该限制与 v6.35 多轮上下文延续需求冲突（"fail原因是什么"、"你好"等
        #   无业务槽位的追问/闲聊应沿用 pending 意图，流程未完成前不改变方向）。
        # v6.65.3：跟踪 pending 是否实际延续生效（区别于"pending 存在但
        # 当前输入重新命中了明确业务意图"）。查询流程分派条件改用此标志，
        # 使上一轮"查一下库存"引导产生的 pending 不会拦截本轮带参查询。
        _pending_continued = False
        if has_pending:
            # v6.46：流程字段收集进行中（pending 携带 workflow_instance）时，
            # 任意输入均沿用 workflow_start，交由字段收集器消化（补充字段/引导），
            # 避免"出差参加客户现场验收"等补充信息被误识别为其他业务意图而中断流程
            # v6.56：awaiting_approval（字段已收齐、待审批）不再视为字段收集模式——
            # confirm/cancel 由审批推进分支处理；其余输入走正常路由，
            # 避免 pending 吞并审批完成后的后续业务操作。
            wf_collecting = bool(
                isinstance(pending, dict) and pending.get("workflow_instance")
                and pending.get("phase") != "awaiting_approval")
            # v6.46：confirm/thanks 等确认信号沿用 pending 业务意图（"好的/确认"不中断流程）
            # v6.80 意图漂移检测（发散-收敛平衡）：新业务话题（含明确业务名词，
            # 如 pending 下单收集中问"咱库存…"）脱离 pending 延续——
            #   * 追问类意图（unknown/knowledge/greeting/help/chitchat/confirm/thanks）：
            #     含业务名词即脱离，交由强模型重新识别（发散）；
            #   * wf_collecting（字段收集）：非明确业务意图仍视为补充字段/追问
            #     继续收集（"生产日期是…"等含业务词的字段值不误断为新话题）。
            _explicit_business = intent.name not in (
                "unknown", "knowledge_query", "greeting", "help",
                "chitchat", "confirm", "thanks")
            _new_topic = looks_like_new_business_query(user_input)
            # v6.86.1：字段收集进行中（wf_collecting）时，若输入能被槽位引擎
            # 提取到当前流程必填字段，视为字段补充（如"事由：出差参加客户现场
            # 验收"补 reason）——即使被意图识别为显式业务意图（含"客户"被误识别
            # 为 query_customer），也应沿用 pending 延续交由字段收集器消化，
            # 修正 v6.80 漂移检测对字段补充输入的错误拦截（v6.46 注释约定
            # "任意输入均沿用 workflow_start" 的回归）。
            _wf_supply = wf_collecting and self._wf_field_supply(user_input, pending)
            if _wf_supply or (not _explicit_business and (wf_collecting or not _new_topic)):
                _pending_continued = True
                # 沿用 pending 意图，合并新抽取的槽位
                merged_slots = dict(pending.get("slots", {}))
                if intent.slots:
                    merged_slots.update(intent.slots)
                pending_name = "workflow_start" if wf_collecting else pending.get("name", "unknown")
                intent = Intent(
                    name=pending_name,
                    channel="business",
                    confidence=_intent_params()["confidence_pending_continuation"],
                    slots=merged_slots,
                    target_agent=pending["target_agent"],
                )

        # 2. 选择目标Agent
        agent = self._select_agent(intent)

        # 2.5 v6.43：DB 流程名直接匹配--用户输入含任一 DB trigger_keyword
        # 且意图未命中明确业务意图（unknown/knowledge_query/management_consulting 等）
        # 时，自动归为 workflow_start，使纯流程名（"费用报销"不带动词）也能触发。
        # 原则：动词固定（发起/申请/提交 硬编码在意图正则），流程名从 DB 训练获得；
        #       此处补"纯流程名"路径，让 DB 定义的流程名不依赖动词前置即可识别。
        if intent.name in ("unknown", "knowledge_query", "management_consulting",
                           "data_analysis", "help", "chitchat"):
            wf_match = self._match_workflow_by_name(user_input)
            if wf_match:
                intent = Intent(
                    name="workflow_start",
                    channel="business",
                    target_agent="knowledge",
                    confidence=0.85,
                )

        # 3. 隔离上下文
        isolated_context = self._isolate_context(user_context)
        # 将意图信息注入上下文
        isolated_context["intent"] = intent.name
        isolated_context["channel"] = intent.channel
        isolated_context["agent_type"] = intent.target_agent
        # 不传 sub_intent：各 Agent 有自己的 _recognize_sub_intent()，
        # coordinator 的意图名与 Agent 子意图名不一致会导致误走 LLM 兜底
        # 合并槽位
        if intent.slots:
            existing_slots = isolated_context.get("slots", {})
            if isinstance(existing_slots, dict):
                existing_slots.update(intent.slots)
            else:
                existing_slots = intent.slots
            isolated_context["slots"] = existing_slots

        # 3.5 workflow_start 意图：尝试调用 WorkflowEnforcer 实例化流程
        # v6.14：修复 workflow_start 仅做文档引导、未真正创建流程实例的问题
        if intent.name == "workflow_start":
            wf_result = self._try_start_workflow(user_input, user_context, intent)
            if not wf_result:
                # v6.46：多轮延续——本输入无流程关键词（如补充"500元/差旅"）时，
                # 从 pending_intent 恢复已启动的流程实例继续字段收集
                _pend = user_context.get("pending_intent")
                if isinstance(_pend, dict) and _pend.get("workflow_instance"):
                    wf_result = _pend.get("workflow_instance")
            if wf_result:
                isolated_context["workflow_instance"] = wf_result
            else:
                # v6.46.1：触发流程意图但未匹配到流程定义（含"发起一个流程"
                # 等无具体流程名）-> 交由知识助手提示"流程不存在可新建"
                isolated_context["workflow_start_failed"] = True

        # v6.47 单轨制：业务操作意图（INT-01~27 操作类）经 intent_map 映射
        # 流程定义后创建流程实例。映射全部由训练/DB 定义（代码不硬编码）：
        #   DB workflow_configs.thresholds.intent_map 声明流程承接的业务意图，
        #   训练新建/修改流程后意图命中即自动建实例。
        # 规则：
        #   - 查询类意图不配置映射（无审批链需求），不建实例
        #   - 仅意图首轮（无 pending 延续）建实例，多轮补字段不重复建
        #   - 三道校验失败：admin 放行（继续执行业务操作），非 admin 阻断
        wf_start_error = None
        # v6.99 优化：单轨制建实例（_intent_workflow_map）与查询流程分派
        # （_query_workflow_map）两处 workflow 映射共用一次全表查询——原各自
        # SELECT * 跨公网 19.4MB 大字段传两遍（≈42s 瓶颈）；合并后只传一遍
        # （≈21s）。仅首个映射触发时取行，第二处复用；首处未触发则第二处
        # 内部自取（保持惰性，pending 延续仍零查询）。
        _workflow_rows = None
        if (intent.channel == "business" and intent.name != "workflow_start"
                and not has_pending
                and not isolated_context.get("workflow_instance")):
            _workflow_rows = self._load_workflow_rows()
            im = self._intent_workflow_map(rows=_workflow_rows)
            mapped = im.get(intent.name)
            if mapped:
                _wf_type, _biz_type, _biz_id = mapped
                wf_result, wf_err = self._start_biz_workflow(
                    _wf_type, _biz_type, _biz_id, user_context)
                if wf_result:
                    isolated_context["workflow_instance"] = wf_result
                else:
                    u_role = ((user_context or {}).get("user") or {}).get("role", "")
                    if u_role != "admin":
                        wf_start_error = (_wf_type, wf_err)

        # v6.64 查询流程分派（独立于单轨制：不建实例、不进审批链，不触碰
        # 上方 _intent_workflow_map 逻辑）：查询类意图经
        # workflow_configs.thresholds.query_intent_map 映射到查询流程定义
        # （gate_checks.query_steps 多步骤：查库/知识库/网络/LLM 生成）。
        # 命中 → 路由知识助手编排执行并渲染（注入 query_flow 上下文）；
        # 未映射 → 保持原 Agent 直达路径（零延迟不变）。
        query_flow_wf = None
        if (intent.channel == "business" and not wf_start_error
                and not _pending_continued
                and not isolated_context.get("workflow_instance")):
            _qmap = self._query_workflow_map(rows=_workflow_rows)
            if intent.name in _qmap:
                # v6.65.2：查询须有主要参数（产品型号/名称、数值条件如
                # "库存大于1000"、或模糊词交由 LLM 补全）；完全没有时
                # （如"查一下库存"）不进查询流程，保持 Agent 直达引导
                # （warehouse 提示提供产品型号/附加条件），交互更友好。
                if self._has_query_main_param(user_input, intent.slots or {}):
                    query_flow_wf = _qmap[intent.name]
        if query_flow_wf:
            agent = self._select_agent(Intent(
                name=intent.name, channel="business",
                target_agent="knowledge", confidence=intent.confidence,
                slots=dict(intent.slots or {})))
            isolated_context["query_flow"] = {
                "workflow_type": query_flow_wf,
                "slots": dict(intent.slots or {}),
            }

        # 4. 调用目标Agent
        if wf_start_error:
            # 业务操作发起流程被三道校验阻断（规格书：无权发起时回复拒绝原因）
            _wf_type, wf_err = wf_start_error
            response = AgentResponse(
                content=(f"无法启动「{_wf_type}」流程：{wf_err}。\n"
                         "流程定义（starter_roles/starter_depts/initiation）"
                         "未通过发起者校验；可联系管理员调整流程定义，"
                         "或经训练修改流程后重试。"),
                action="no_agent",
                agent_name="协调Agent",
            )
        elif agent is not None:
            try:
                response = agent.process(user_input, isolated_context)
            except Exception as e:
                response = AgentResponse(
                    content=f"处理请求时发生错误：{e}",
                    action="error",
                    agent_name=getattr(agent, "agent_name", "未知Agent"),
                )
        else:
            # 无可用Agent时兜底
            response = AgentResponse(
                content="抱歉，我暂时无法处理您的请求。请尝试重新描述您的需求。",
                action="no_agent",
                agent_name="协调Agent",
            )

        # 5. 聚合结果（单Agent时直接透传）
        aggregated = self._aggregate_results([response])
        # 将意图信息写入响应元数据（供会话层维护多轮延续状态）
        meta = dict(aggregated.metadata or {})
        meta.update({
            "intent": intent.name,
            "intent_agent": intent.target_agent,
            "intent_channel": intent.channel,
        })
        # v6.65.3：查询流程分派时清除上一轮 pending_intent（如"查一下库存"
        # 引导产生的），使会话层不再保留旧 pending，下一轮查询不会被
        # has_pending 拦截而退回 Agent 直达。
        if isolated_context.get("query_flow"):
            meta["pending_intent"] = None
        # 多轮状态管理：业务意图命中时记录 last_business_intent，供追问延续
        # 排除 no_agent/error：无可用Agent或处理出错时不设 pending_intent，
        # 避免会话层延续一个无法处理的意图。
        # v6.45：流程字段收集模式（workflow_start）——knowledge assistant 通过
        # metadata.wf_slots 上报已收集字段；未集齐时合并进 pending_intent.slots
        # 实现跨轮收集，集齐（__done__）后清除延续状态。
        wf_slots = aggregated.metadata.get("wf_slots")
        if wf_slots is not None:
            if wf_slots.get("__done__"):
                # v6.56：字段收集完成 -> 流程进入待审批阶段。
                # 不再清除 pending，而是保留流程实例（phase=awaiting_approval），
                # 使后续"同意/批准"（confirm 审批推进分支）可定位实例并逐级推进审批。
                pending_slots = dict(intent.slots or {})
                chat_slots = isolated_context.get("slots") or {}
                if isinstance(chat_slots, dict):
                    pending_slots.update(chat_slots)
                meta["pending_intent"] = {
                    "name": intent.name,
                    "target_agent": intent.target_agent,
                    "slots": pending_slots,
                    "action": "awaiting_approval",
                    "phase": "awaiting_approval",
                    "workflow_instance": {
                        "instance_id": wf_slots.get("instance_id"),
                        "workflow_type": wf_slots.get("workflow_type"),
                    },
                }
            else:
                pending_slots = dict(intent.slots or {})
                chat_slots = isolated_context.get("slots") or {}
                if isinstance(chat_slots, dict):
                    pending_slots.update(chat_slots)
                pending_slots.update(wf_slots.get("fields") or {})
                meta["pending_intent"] = {
                    "name": intent.name,
                    "target_agent": intent.target_agent,
                    "slots": pending_slots,
                    "action": "request_info",
                    # v6.46：携带流程实例，多轮延续时无需重新匹配关键词即可恢复
                    "workflow_instance": {
                        "instance_id": wf_slots.get("instance_id"),
                        "workflow_type": wf_slots.get("workflow_type"),
                    },
                }
        elif (intent.channel == "business" and intent.target_agent
              and aggregated.action not in ("no_agent", "error")
              # v6.65.1：查询流程（query_flow 上下文命中）为一次性只读操作，
              # 不挂起 pending——否则下一轮同意图查询被 has_pending 拦截，
              # 查询流程分派被跳过退回 Agent 直达（如"查一下库存"第二轮）
              and not isolated_context.get("query_flow")
              # v6.46.1：workflow_start 未成功启动（_try_start_workflow 返回 None 且
              # 无 pending workflow_instance 可恢复）时不挂起 pending——避免"发起一个
              # 流程"等无法匹配流程名的输入被挂起为 workflow_start，后续轮次反复
              # 重试启动而永远进不了字段收集（与"多轮延续输入无关键词返回 None"同类）
              and not (intent.name == "workflow_start"
                       and not isolated_context.get("workflow_instance"))
              # v6.79：只读查询类意图执行完毕不挂起 pending——否则遗留 pending
              # 吞掉下一轮新查询（"OEE给我拉一下"查无数据 → 下一句"咱库存是不是
              # 快见底了"规则未命中→unknown→被延续为 query_progress 误路由）。
              # Agent 向用户索要查询参数（request_info/ask_more_info）时不拦截，
              # 由下方请求态分支/本分支正常挂起，多轮补充照常延续。
              and not (intent.name in self.READONLY_QUERY_INTENTS
                       and aggregated.action not in ("request_info", "ask_more_info"))):
            # v6.46：pending slots 合并 chat 层注入的槽位（attachment 文件等），
            # 避免多轮延续（request_info 追问后）时已上传文件丢失
            pending_slots = dict(intent.slots or {})
            chat_slots = isolated_context.get("slots") or {}
            if isinstance(chat_slots, dict):
                pending_slots.update(chat_slots)
            meta["pending_intent"] = {
                "name": intent.name,
                "target_agent": intent.target_agent,
                "slots": pending_slots,
                "action": aggregated.action,
            }
            # v6.61：流程定义训练申请提交后挂起待延续——confirm/同意 时推进
            # workflow_configs 训练审批记录行（config_id 定位审批单）
            if intent.name == "workflow_train":
                meta["pending_intent"] = {
                    "name": "workflow_train",
                    "target_agent": intent.target_agent,
                    "slots": pending_slots,
                    "action": "workflow_train_pending",
                    "phase": "workflow_train",
                    "config_id": aggregated.metadata.get("training_id"),
                }
        elif aggregated.action == "request_info":
            meta["pending_intent"] = {
                "name": intent.name,
                "target_agent": intent.target_agent,
                "slots": dict(intent.slots or {}),
            }
        else:
            meta["pending_intent"] = None  # 通知会话层清除
        aggregated.metadata = meta
        # v6.34：异步记录对话用于持续学习闭环
        self._log_interaction(user_input, intent, aggregated, user_context)
        return aggregated

    # --------------------------------------------------------
    # 对话持久化（v6.34：支持持续学习闭环）
    # --------------------------------------------------------
    def _log_interaction(self, user_input: str, intent: Intent,
                         response: "AgentResponse",
                         user_context: Optional[Dict] = None) -> None:
        """异步写入对话记录到 training_data 表。

        每次意图识别+Agent回复后自动记录 {input, intent, confidence, output}，
        为持续学习（隐式反馈/主动学习采样）提供数据源。
        DB 不可用时静默降级，不影响主流程。
        """
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return

            session_id = (user_context or {}).get("session_id", "auto")
            content = getattr(response, "content", "")
            if len(content) > 2000:
                content = content[:2000] + "..."

            record = {
                "agent_type": intent.target_agent or "coordinator",
                "intent": intent.name,
                "user_input": user_input,
                "ai_output": content,
                "metadata": {
                    "type": "auto_log",
                    "session_id": session_id,
                    "confidence": intent.confidence,
                    "source": getattr(intent, "source", ""),
                    "channel": intent.channel,
                },
                "approved": False,
                "created_at": datetime.now().isoformat(),
            }

            def _write():
                """后台线程写入自动日志训练样本。

                闭包捕获 db / record：
                    db: 数据库访问层（get_database() 返回，可能为 None 前置拦截）
                    record: 自动日志样本字典（agent_type/intent/user_input/
                            ai_output/metadata/approved/created_at）
                失败静默吞掉：自动日志为旁路采集，写库异常不影响主业务链路。
                """
                try:
                    db.insert("training_data", record)
                except Exception:
                    pass

            # W2：有界线程池提交（原为裸 threading.Thread 每轮创建，高并发线程无界）
            _LOG_EXECUTOR.submit(_write)
        except Exception:
            pass

    # --------------------------------------------------------
    # 意图识别
    # --------------------------------------------------------
    def _recognize_intent(self, user_input: str,
                          user_context: Optional[Dict] = None,
                          skip_llm: bool = False,
                          reasoning_callback: Optional[Any] = None) -> Intent:
        """
        三层意图识别。

        第一层：IntentRecognizer 规则匹配（零延迟、零 token）
        第二层：IntentRecognizer LLM 语义理解（规则未命中时）
        第三层：咨询预判 + 兜底知识助手

        参数：
            user_input: 用户输入文本
            user_context: 用户会话上下文（v6.33：注入 LLM prompt 增强意图判断）
            skip_llm: 跳过LLM回退（多轮延续时仅做规则匹配，避免补充信息触发LLM延迟）
            reasoning_callback: v6.78.3 可选回调 callable(str)——识别强模型
                思考过程逐块回调（流式输出 reasoning_content，前端"思考中"展示）

        返回：
            Intent: 意图对象，包含标签、通道、槽位等
        """
        # 槽位提取（保留 coordinator 原有的精细槽位提取）
        slots = self._extract_slots(user_input)

        # v6.33：构建 session_context，注入 LLM prompt（操作上下文+对话历史）
        # v6.35：增加对话摘要（last_input/last_reply/last_action），减少多轮误判
        # v6.36：注入3轮历史+摘要+相关轮次（滑动窗口+摘要压缩+相关性筛选）
        session_context: Dict[str, Any] = {}
        if slots:
            session_context["slots"] = slots
        if user_context:
            history = user_context.get("history", [])
            if history:
                session_context["history"] = history
            pending = user_context.get("pending_intent")
            if pending and isinstance(pending, dict):
                session_context["last_intent"] = pending.get("name")
                session_context["last_action"] = pending.get("action")
            current_workflow = user_context.get("current_workflow")
            if current_workflow:
                session_context["current_workflow"] = current_workflow
            # 对话摘要：上一轮的用户输入和回复摘要
            last_input = user_context.get("last_input")
            if last_input:
                session_context["last_input"] = last_input
            last_reply = user_context.get("last_reply")
            if last_reply:
                session_context["last_reply"] = last_reply
            # v6.36：3轮对话历史 + 摘要 + 相关轮次
            conv_history = user_context.get("conversation_history")
            if conv_history:
                session_context["conversation_history"] = conv_history
            conv_summary = user_context.get("conversation_summary")
            if conv_summary:
                session_context["conversation_summary"] = conv_summary
            relevant_turns = user_context.get("relevant_turns")
            if relevant_turns:
                session_context["relevant_turns"] = relevant_turns
            # v6.37：递归意图状态
            intent_state = user_context.get("intent_state")
            if intent_state:
                session_context["intent_state"] = intent_state

        # === 第一层 + 第二层：IntentRecognizer 混合识别 ===
        # IntentRecognizer 规则存储于 intent_rules 表（DB优先 + 内置兜底），
        # 规则内容可经训练变更（L1反馈/L2产出/LLM建议，审批后生效，v6.29）。
        if self._intent_recognizer is not None:
            try:
                recognized = self._intent_recognizer.recognize(
                    user_input, session_context=session_context or None,
                    skip_llm=skip_llm,
                    reasoning_callback=reasoning_callback,
                )
                if recognized and recognized.name != "unknown":
                    merged_slots = {**recognized.params, **slots}
                    # DB 规则命中时自带路由信息（target_agent/target_channel，
                    # 训练可调整路由），优先使用规则自带路由
                    if recognized.target_agent:
                        return Intent(
                            name=recognized.name,
                            channel=recognized.channel or "business",
                            confidence=recognized.confidence,
                            slots=merged_slots,
                            target_agent=recognized.target_agent,
                        )
                    # 内置规则兜底命中：路由优先取 intent_rules 表（v6.31，
                    # 训练可调整 target_agent/target_channel），DB 不可用/表空回退映射表
                    db_route = self._db_route_map().get(recognized.name)
                    if db_route:
                        channel, target_agent = db_route
                    else:
                        agent_info = self.INTENT_AGENT_MAP.get(recognized.name)
                        channel, target_agent = agent_info if agent_info else ("business", "")
                    if target_agent:
                        return Intent(
                            name=recognized.name,
                            channel=channel,
                            confidence=recognized.confidence,
                            slots=merged_slots,
                            target_agent=target_agent,
                        )
                    # 意图名不在映射表中，但 LLM 返回了有效意图
                    # 尝试用 coordinator 的正则规则补充匹配
                    if recognized.confidence >= _intent_params()["confidence_llm_min"]:
                        for rule in INTENT_REGEXES:
                            pattern, intent_name = rule[0], rule[1]
                            channel = rule[2] if len(rule) > 2 else "business"
                            target_agent = rule[3] if len(rule) > 3 else ""
                            if intent_name == recognized.name:
                                return Intent(
                                    name=intent_name,
                                    channel=channel,
                                    confidence=recognized.confidence,
                                    slots={**recognized.params, **slots},
                                    target_agent=target_agent,
                                )
            except Exception:
                pass  # IntentRecognizer 失败，降级到纯规则

        # === 降级路径：coordinator 内置正则规则 ===
        # v6.67.5：skip_llm（多轮延续，has_pending）时跳过 INTENT_REGEXES 降级正则——
        # 补充信息（如纯订单号 SO20260801001）应由 pending_intent 延续机制消化，
        # 不应被降级正则误判为新业务意图（如 query_order）而打断多轮流程。
        db_route_map = self._db_route_map()
        if not skip_llm:
            for rule in INTENT_REGEXES:
                pattern = rule[0]
                intent_name = rule[1]
                channel = rule[2] if len(rule) > 2 else "business"
                target_agent = rule[3] if len(rule) > 3 else ""
                if re.search(pattern, user_input, re.IGNORECASE):
                    # v6.31：路由优先取 intent_rules 表（训练可调整），否则用规则自带路由
                    db_route = db_route_map.get(intent_name)
                    if db_route:
                        channel, target_agent = db_route
                    return Intent(
                        name=intent_name,
                        channel=channel,
                        confidence=0.9,
                        slots=slots,
                        target_agent=target_agent,
                    )

        # === 第三层：咨询类预判 + 兜底 ===
        if any(p in user_input for p in _consultation_patterns()):
            return Intent(
                name="knowledge_query",
                channel="consulting",
                confidence=_intent_params()["confidence_consult_pre"],
                slots=slots,
                target_agent="knowledge",
            )

        return Intent(
            name="unknown",
            channel="consulting",
            confidence=_intent_params()["confidence_unknown"],
            slots=slots,
            target_agent="knowledge",
        )

    def _extract_slots(self, user_input: str) -> Dict[str, Any]:
        """从用户输入中提取槽位。"""
        slots: Dict[str, Any] = {}

        # 提取产品型号（如 A-202）
        product_match = re.search(r'([A-Z]-\d{3})', user_input, re.IGNORECASE)
        if product_match:
            slots["product_code"] = product_match.group(1).upper()
        else:
            product_match2 = re.search(r'([A-Z])\s*-?\s*(\d{3})', user_input, re.IGNORECASE)
            if product_match2:
                slots["product_code"] = f"{product_match2.group(1).upper()}-{product_match2.group(2)}"

        # 提取数量
        qty_match = re.search(r'(\d+)\s*[套件个台只PCSpcs]', user_input)
        if qty_match:
            slots["quantity"] = int(qty_match.group(1))

        # 提取订单号
        order_match = re.search(r'(SO\d{6,})', user_input, re.IGNORECASE)
        if order_match:
            slots["order_id"] = order_match.group(1).upper()

        # 提取客户名（仅在下单/追加等动作语境下抽取；排除人称代词/助词/帮忙等前缀）
        customer_match = re.search(r'([\u4e00-\u9fa5]{2,8})\s*(?:追加|加单|要货|采购|订购|订货|需要|想要|下单)', user_input)
        if customer_match and not customer_match.group(1).startswith(
                ("我", "你", "他", "她", "它", "我们", "你们", "帮", "给", "为", "帮忙", "客户")):
            slots["customer_name"] = customer_match.group(1)
        else:
            # 查询语境客户名：如"查一下张三的订单"（"我的"不匹配，避免把当前用户误当客户）
            query_customer = re.search(r'(?:查一下|查询|查看|看看|查查)\s*([\u4e00-\u9fa5]{2,8})\s*的订单', user_input)
            if query_customer:
                slots["customer_name"] = query_customer.group(1)
            else:
                # 补充语境客户名：如"客户是张明"（多轮补客户名）
                customer_after = re.search(r'(?:客户是|客户为|客户叫)\s*([\u4e00-\u9fa5]{2,8})', user_input)
                if customer_after:
                    slots["customer_name"] = customer_after.group(1)
                else:
                    # 为/给/帮 引导语境：如"为张明生成合同"（排除人称代词与"客户/帮忙"，
                    # 前瞻动作词避免把"生成一份"并入客户名）
                    for_ctx = re.search(
                        r'(?:为|给|帮)\s*(?!我|你|他|她|它|我们|你们|客户|公司|帮忙|忙)'
                        r'([\u4e00-\u9fa5]{2,8}?)(?=\s*(?:生成|起草|拟|签|下单|追加|加单|要货|采购|订购|订货|做|办理|创建|开立|的)|$)', user_input)
                    if for_ctx:
                        slots["customer_name"] = for_ctx.group(1)

        return slots

    # --------------------------------------------------------
    # Agent选择
    # --------------------------------------------------------
    def _select_agent(self, intent: Intent) -> "BaseAgent":
        """
        根据意图选择目标Agent。

        设计意图：
            依据意图的 target_agent 字段路由到对应领域Agent。
            管理咨询通道统一路由到 knowledge_assistant。
            未命中任何Agent时回退到 knowledge_assistant 兜底。

        参数：
            intent: 意图对象

        返回：
            BaseAgent: 目标Agent实例
        """
        target = intent.target_agent

        # 业务通道：按target_agent查找已注册的Agent
        if intent.channel == "business" and target:
            agent = self.agents.get(target)
            if agent is not None:
                return agent
            # audit 类型无独立Agent时回退到 sales
            if target == "audit":
                return self.agents.get("sales")

        # 咨询通道：路由到 knowledge_assistant
        if self.knowledge_assistant is not None:
            return self.knowledge_assistant

        # 兜底：使用 sales_agent（如已注册）
        if "sales" in self.agents:
            return self.agents["sales"]

        # 最终兜底：返回None，由route处理
        return None

    # --------------------------------------------------------
    # 上下文隔离
    # --------------------------------------------------------
    def _isolate_context(self, user_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        上下文隔离。

        设计意图：
            为目标Agent构造隔离的上下文副本，仅包含该Agent可见的字段，
            防止跨Agent数据泄露（如销售Agent不应看到成本线、内审数据）。

        参数：
            user_context: 原始用户上下文

        返回：
            dict: 隔离后的上下文（深拷贝并裁剪敏感字段）

        隔离策略：
            - 用户身份与权限：所有Agent可见
            - 业务状态：仅对应Agent可见
            - 对话历史：按Agent分段，仅注入当前Agent的历史
        """
        import copy
        # 深拷贝避免污染原始上下文
        isolated = copy.deepcopy(user_context) if user_context else {}

        # 确保基本字段存在
        isolated.setdefault("user", {})
        isolated.setdefault("history", [])
        isolated.setdefault("slots", {})
        isolated.setdefault("data", {})

        # 裁剪敏感字段（根据Agent类型决定可见性）
        agent_type = isolated.get("agent_type", "")
        # 非内审Agent不应看到内审数据
        if agent_type != "audit":
            isolated.pop("audit_data", None)
        # 非财务Agent不应看到财务明细
        if agent_type != "finance":
            isolated.pop("financial_data", None)

        return isolated

    # 内置流程触发关键词（DB workflow_configs.thresholds.trigger_keywords 覆盖后优先；
    # 与 007_business_rules_workflow_configs.sql 的流程定义对应，v6.43 起可训练新增流程）
    _BUILTIN_WF_KEYWORDS: Dict[str, tuple] = {
        # v6.59：参数变更类流程（cost_markup_change/version_sm_change/
        # sched_constraint_change/inv_stage_change/bom_check_change/
        # drawing_field_change/rule_config_change）对话触发已收敛——
        # 真实修改走 training API 审批（config_manager），对话触发仅建空转实例
        # （无字段收集/无业务生效）；migration 034 已移除 DB trigger_keywords。
        # 内置仅保留纯对话型流程（费用报销）；customer_change/product_change/
        # drawing_change/production_schedule/order_approve/return_process 由
        # DB trigger_keywords + intent_map 触发（027/018 迁移），无内置兜底。
        # v6.45：费用报销流程（对齐 OPERATIONS_PROMPT_GUIDE L311 训练示例）
        "expense_reimbursement": (["费用报销", "报销", "报销流程", "报销单", "报销申请"], "expense", "auto"),
    }

    def _wf_field_supply(self, user_input: str, pending: Any) -> bool:
        """字段收集补充判定（v6.86.1）：输入是否补充了当前流程必填字段。

        用于 pending 延续分支——字段收集进行中（wf_collecting），若输入能被
        slot_engine 提取到当前流程必填字段（SLOT-DEFS.required_rules →
        workflow_configs.gate_checks.required_fields → 内置兜底三字段），
        视为字段补充，即使意图被误识别为显式业务意图也应沿用 workflow_start
        交由字段收集器消化（v6.46"任意输入均沿用"约定回归；v6.80 漂移检测
        只对真新业务话题生效，不误伤字段值补充）。

        Args:
            user_input: 用户输入
            pending: pending_intent（含 workflow_instance.workflow_type）

        Returns:
            bool: True=输入补充了当前流程必填字段，应沿用 pending 延续
        """
        try:
            wf_type = ""
            wf_inst = (pending or {}).get("workflow_instance") if isinstance(pending, dict) else None
            if isinstance(wf_inst, dict):
                wf_type = wf_inst.get("workflow_type") or ""
            if not wf_type:
                return False
            required = self._wf_required_fields(wf_type)
            if not required:
                return False
            from prog.runtime.slot_engine import extract_slots
            slots = extract_slots(user_input) or {}
            # 命中任一必填字段（含 or 表达式拆分）即视为字段补充
            return any(
                str(f).split("|")[0].strip() in slots
                or any(s in slots for s in str(f).split("|") if s.strip())
                for f in required)
        except Exception:
            return False

    def _wf_required_fields(self, wf_type: str) -> list:
        """流程必填字段（v6.86.1，与 knowledge_assistant 三层配置一致）：
        1. SLOT-DEFS.required_rules（slot_engine 表驱动）
        2. workflow_configs.gate_checks.required_fields（DB 定义行）
        3. 内置兜底报销三字段（DB 不可用降级）
        """
        try:
            from prog.runtime.slot_engine import get_required_slots
            req = get_required_slots(wf_type)
            if req:
                return list(req)
        except Exception:
            pass
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
        return ["amount", "expense_type", "reason"]

    def _match_workflow_by_name(self, user_input: str) -> Optional[str]:
        """纯流程名匹配（v6.43）：用户输入含任一 DB trigger_keyword 时返回 workflow_type。

        用于"费用报销"等纯流程名（不带动词前缀）也能触发 workflow_start 的场景。
        仅匹配 trigger_keywords 中长度 >= 3 的关键词，避免"成本""版本"等短词误触发。

        v6.60：查询类语句（显示/查看/查询/实例N等）一律不在此触发——
        查看既有流程单据/进度由 workflow_query 意图承接，避免
        "显示刚才报销流程内容"被误判为发起新流程（实例错建）。

        Returns:
            workflow_type 或 None
        """
        txt = (user_input or "").strip()
        if not txt:
            return None
        # 查询动词前缀 -> 属流程查询，不触发流程启动
        if re.search(r"^(显示|查看|查询|查一下|看看|查查|打开|调出|翻出|找一下|找找|请显示|帮我查|请查看|看下|查下)", txt):
            return None
        # 训练/定义动词前缀（v6.61）-> 属流程定义训练申请，不触发流程启动
        # （"训练一个报销流程"含"报销流程"不应误建费用报销实例）
        if re.search(r"^(训练|创建|新建|定义|设计|定制|配置|制作|帮我训练|帮我创建|我要训练)", txt):
            return None
        # 含"实例N/编号N" -> 定位既有实例，不触发流程启动
        if re.search(r"(?:实例|编号|单号)\s*[#]?\d+", txt):
            return None
        # 流程/审批 + 内容/详情/进度/状态 -> 查询既有单据
        if re.search(r"(流程|审批|报销|工单|单据)(?:内容|详情|进度|状态|记录|历史|信息)", txt):
            return None
        kw_map = self._workflow_keywords_map()
        for wf_type, (keywords, _, _) in kw_map.items():
            for kw in keywords:
                # B4：词边界匹配——关键词不得嵌入普通汉字词内
                #（如"采购"不匹配"采购经理"）；允许前接触发动词、
                # 后接流程后缀词（"请假"匹配"请假流程/请假单"）
                if len(kw) >= 3 and self._kw_in_boundary(kw, user_input):
                    return wf_type
        return None

    # B4：流程关键词词边界——前接触发动词集 / 后接流程后缀词集
    _WF_KW_PREFIX = set("发起启动申请提交新建创建走进行要查询显示看看待")
    _WF_KW_SUFFIX = set("流程审批单的申请报销工单")

    @classmethod
    def _kw_in_boundary(cls, kw: str, text: str) -> bool:
        """流程关键词词边界匹配（B4）。

        关键词命中处的前后邻接字符：非汉字/空白/标点直接放行；
        前邻汉字仅允许触发类动词，后邻汉字仅允许流程后缀词，
        其余情况视为嵌入普通词（如"采购经理"），不触发。
        """
        for m in re.finditer(re.escape(kw), text):
            s, e = m.start(), m.end()
            pre = text[s - 1] if s > 0 else ""
            post = text[e] if e < len(text) else ""
            pre_ok = (not pre) or (not _IS_CN(pre)) or pre in cls._WF_KW_PREFIX
            post_ok = (not post) or (not _IS_CN(post)) or post in cls._WF_KW_SUFFIX
            if pre_ok and post_ok:
                return True
        return False

    def _workflow_keywords_map(self, db: Any = None) -> Dict[str, tuple]:
        """流程触发关键词映射（v6.43 可训练）：DB(workflow_configs) 优先 + 内置兜底。

        新流程通过训练写入 workflow_configs（thresholds 含 trigger_keywords/biz_type/
        biz_id）后自动可被识别触发，无需改代码；DB 不可用时降级内置默认。

        Args:
            db: 可选数据库

        Returns:
            dict: workflow_type -> (关键词列表, biz_type, biz_id)
        """
        keywords = dict(self._BUILTIN_WF_KEYWORDS)
        if db is None:
            from prog.runtime.database import get_database
            db = get_database()
        if db is None:
            return keywords
        try:
            rows = db.query_many("workflow_configs", {"is_active": True})
            for row in rows or []:
                wf_type = row.get("workflow_type")
                if not wf_type:
                    continue
                thresholds = row.get("thresholds")
                if isinstance(thresholds, str):
                    import json as _json
                    try:
                        thresholds = _json.loads(thresholds)
                    except Exception:
                        thresholds = None
                tk = thresholds.get("trigger_keywords") if isinstance(thresholds, dict) else None
                if tk and isinstance(tk, list) and tk:
                    keywords[wf_type] = (
                        list(tk),
                        thresholds.get("biz_type", "auto"),
                        thresholds.get("biz_id", "auto"),
                    )
        except Exception:
            pass
        return keywords

    def _load_workflow_rows(self, db: Any = None) -> list:
        """加载启用的 workflow_configs 全表行（v6.99 优化）。

        供 _intent_workflow_map/_query_workflow_map 共用一次查询结果——
        原两处映射各自全表 SELECT *，跨公网同 19.4MB 大字段传两遍
        （route 42s 瓶颈）；合并为一次查询后只传一遍（~21s）。
        查询失败返回空列表（调用方按无映射处理，与 DB 无数据一致）。
        """
        if db is None:
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                db = None
        if db is None:
            return []
        try:
            return db.query_many("workflow_configs", {"is_active": True}) or []
        except Exception:
            return []

    def _intent_workflow_map(self, db: Any = None, rows: Any = None) -> Dict[str, tuple]:
        """意图→流程类型映射（v6.47 单轨制核心）。

        业务操作意图（INT-01~27 中的操作类）经本映射找到对应 workflow_type，
        由 coordinator 创建流程实例。映射来源：
            DB workflow_configs.thresholds.intent_map（可训练）——新建/修改流程
            时声明该流程承接的业务意图，训练生效后意图命中即自动建实例。
        intent_map 值为字符串（流程名）或 dict（{workflow_type,biz_type,biz_id}）。
        查询类意图不配置映射（无审批链需求），不建实例。
        设计约束：**全部由训练/种子定义，代码不硬编码任何流程与映射**——
        DB 无映射时返回空 dict（业务操作不建实例，保持既有行为）。

        Args:
            db: 数据库实例；None 时取全局单例。
            rows: 可选——调用方已加载的 workflow_configs 行（route 中与
                _query_workflow_map 共用一次查询，避免跨公网重复全表拉取）；
                None 时内部自取。

        Returns:
            dict: intent_name -> (workflow_type, biz_type, biz_id)
        """
        out: Dict[str, tuple] = {}
        if rows is None:
            rows = self._load_workflow_rows(db)
        if not rows:
            return out
        try:
            for row in rows:
                wf_type = row.get("workflow_type")
                thresholds = row.get("thresholds")
                if isinstance(thresholds, str):
                    import json as _json
                    try:
                        thresholds = _json.loads(thresholds)
                    except Exception:
                        thresholds = None
                im = thresholds.get("intent_map") if isinstance(thresholds, dict) else None
                if not isinstance(im, dict):
                    continue
                biz_type = thresholds.get("biz_type", "auto")
                biz_id = thresholds.get("biz_id", "auto")
                for intent_name, mapped in im.items():
                    if not intent_name:
                        continue
                    if isinstance(mapped, dict):
                        out[str(intent_name)] = (
                            mapped.get("workflow_type") or wf_type,
                            mapped.get("biz_type") or biz_type,
                            mapped.get("biz_id") or biz_id,
                        )
                    else:
                        out[str(intent_name)] = (
                            str(mapped) or wf_type, biz_type, biz_id)
        except Exception:
            pass
        return out

    def _query_workflow_map(self, db: Any = None, rows: Any = None) -> Dict[str, str]:
        """意图→查询流程类型映射（v6.64）。

        查询类意图经 DB workflow_configs.thresholds.query_intent_map
        （{intent: workflow_type}）关联到查询流程定义；仅当定义行
        gate_checks.query_steps（查库/知识库/网络/生成步骤）非空时生效。

        与 _intent_workflow_map（单轨制）的区别：
            - 查询流程不建 workflow_instances、不进审批链、不触碰单轨制逻辑
            - 命中后由知识助手编排执行多步骤查库/知识库/网络/LLM 生成
            - 未映射的查询意图保持原 Agent 直达路径（零延迟不变）

        Args:
            db: 数据库实例；None 时取全局单例。
            rows: 可选——调用方已加载的 workflow_configs 行（与
                _intent_workflow_map 共用一次查询，见 _load_workflow_rows）；
                None 时内部自取。

        Returns:
            dict: intent_name -> workflow_type（查询流程）
        """
        out: Dict[str, str] = {}
        if rows is None:
            rows = self._load_workflow_rows(db)
        if not rows:
            return out
        try:
            import json as _json
            for row in rows:
                thresholds = row.get("thresholds")
                if isinstance(thresholds, str):
                    try:
                        thresholds = _json.loads(thresholds)
                    except Exception:
                        thresholds = None
                gc = row.get("gate_checks")
                if isinstance(gc, str):
                    try:
                        gc = _json.loads(gc)
                    except Exception:
                        gc = None
                qim = (thresholds.get("query_intent_map")
                       if isinstance(thresholds, dict) else None)
                has_qs = bool((gc or {}).get("query_steps"))
                if not isinstance(qim, dict) or not has_qs:
                    continue
                for intent_name, wf in qim.items():
                    if intent_name:
                        out[str(intent_name)] = (
                            str(wf) or row.get("workflow_type", ""))
        except Exception:
            pass
        return out

    def _has_query_main_param(self, user_input: str,
                              slots: Optional[Dict[str, Any]] = None) -> bool:
        """判断查询输入是否具备主要参数（v6.65.2）。

        查询须有主要参数才走查询流程（多步骤查库/知识库/LLM 编排）：
            1. 主键/业务编码槽位（product_code/material_code/order_id/work_order_id 等）
            2. 规则解析出的附加条件（数值/日期/状态/产品名）
            3. 模糊片段（交由 LLM 补全/生成，如"库存偏高的"、"铝合金外壳的"）
        完全没有主要参数（如"查一下库存"）→ 返回 False，保持 Agent 直达引导。

        Args:
            user_input: 用户查询输入
            slots: 已提取的槽位（意图槽位）

        Returns:
            bool: 是否具备主要参数
        """
        slots = slots or {}
        for k, v in slots.items():
            if v and (k.endswith("_code") or k in ("product_code", "material_code",
                                                   "order_id", "work_order_id",
                                                   "product_name", "name")):
                return True
        try:
            from prog.runtime.query_param_parser import parse_query_filters
            _parsed = parse_query_filters(user_input or "")
            if _parsed.get("filters") or _parsed.get("fuzzy"):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _fallback_user_info(user_context: Optional[Dict]) -> Dict:
        """无嵌套 user 键时的兜底用户信息（v6.94 FK 瑕疵方案 B）。

        优先回退 ctx 顶层 user_id/role（MCP 路径 user_context 为顶层扁平
        结构 {"user_id":..., "role":...}，无嵌套 "user" 键，原实现一律跳
        system 占位导致 ctx 身份丢失）；顶层亦无身份才用 system 占位
        （070 迁移已在 users 表补 system 账号；workflow_enforcer.
        _validate_created_by 再做 FK 校验，未迁移环境回退 NULL 双保险）。
        """
        ctx = user_context or {}
        ctx_uid = ctx.get("user_id") or ctx.get("id") or ""
        if ctx_uid:
            return {"user_id": str(ctx_uid), "role": ctx.get("role") or "",
                    "department": ctx.get("department") or ""}
        return {"user_id": "system", "role": "manager", "department": "system"}

    def _start_biz_workflow(self, wf_type: str, biz_type: str, biz_id: str,
                            user_context: Optional[Dict]) -> tuple:
        """业务操作意图启动流程实例（v6.47 单轨制）。

        直接调用 WorkflowEnforcer.start_workflow（三道校验：starter_roles/
        starter_depts/initiation），成功返回实例 dict，失败返回 (None, error)。

        Returns:
            (instance_dict_or_None, error_msg_or_None)
        """
        try:
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            db = None
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                pass
            enforcer = WorkflowEnforcer(database=db)
            user_info = (user_context or {}).get("user", {}) or {}
            if not user_info:
                user_info = self._fallback_user_info(user_context)
            # biz_id='auto' 表示自动生成：生成唯一值，避免 workflow_instances
            # (biz_type, biz_id) 唯一约束冲突
            # W8：毫秒时间戳追加随机后缀，消除同一毫秒内多个请求碰撞风险
            if not biz_id or biz_id == "auto":
                biz_id = f"auto_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
            result = enforcer.start_workflow(
                workflow_type=wf_type,
                biz_type=biz_type or "auto",
                biz_id=biz_id,
                user=user_info,
            )
            if result.get("success"):
                # v6.61.1：业务意图路径流程发起审计——与 workflow_start 路径对称
                try:
                    from prog.runtime.database import get_database as _gd2
                    _db2 = _gd2()
                    if _db2 is not None:
                        _db2.insert("operation_logs", {
                            # v6.94 FK 双保险：user_id 空值回退 None（空串
                            # 违反 fk_operation_logs_user，NULL 可过外键）
                            "user_id": (user_info.get("user_id")
                                        or user_info.get("id") or None),
                            "action": "workflow_start",
                            "details": {
                                "instance_id": result.get("instance_id"),
                                "workflow_type": wf_type,
                                "biz_type": biz_type or "auto",
                                "biz_id": biz_id,
                                "initiator": (user_info.get("name")
                                              or user_info.get("user_id")
                                              or user_info.get("id") or ""),
                            },
                        })
                except Exception:
                    pass
                return {"instance_id": result.get("instance_id"),
                        "workflow_type": wf_type}, None
            return None, result.get("error") or "流程实例创建失败"
        except Exception:
            return None, "流程引擎不可用"

    def _match_keyword_boundary(self, user_input: str,
                                keywords: List[str]) -> bool:
        """词边界/否定语境关键词匹配（B.3 P0）。

        问题：原 `kw in user_input` 子串匹配，"我不报销"含"报销"即误触发
        费用报销流程（否定语境误建实例）。
        修复：任一关键词至少存在一处"未被否定词紧邻"的命中才算匹配；
        关键词所有命中均被否定词紧邻时跳过（"不/没/不要/取消/拒绝…"）。
        中文无空格分词，无法用 \\b，退化为"否定词紧邻关键词前缀"检测。
        """
        for kw in keywords:
            start = 0
            while True:
                pos = user_input.find(kw, start)
                if pos < 0:
                    break
                if not self._is_negated_context(user_input, pos):
                    return True
                start = pos + len(kw)
        return False

    @staticmethod
    def _is_negated_context(user_input: str, kw_pos: int) -> bool:
        """判定关键词命中处是否处于否定语境（关键词前紧邻否定词）。

        例："我不报销"中"报销"前邻"不"→ True（不触发流程）；
            "我报销500元"中"报销"前邻"我"→ False（正常触发）。
        """
        prefix = user_input[:kw_pos]
        return bool(re.search(
            r"(不|没|没有|无|别|非|否|不要|不用|不想|不愿|不需要|"
            r"取消|拒绝|撤销|请勿|勿|算了|罢了|不用了)$", prefix))

    def _try_start_workflow(self, user_input: str,
                            user_context: Optional[Dict],
                            intent: Intent) -> Optional[Dict]:
        """尝试调用 WorkflowEnforcer 实例化流程（v6.14 新增）。

        当意图为 workflow_start 时，尝试从用户输入中识别流程类型并启动流程实例。
        无法识别流程类型时返回 None，由 KnowledgeAssistant 引导用户选择。
        v6.43：流程触发关键词可训练——DB workflow_configs.thresholds.trigger_keywords
        优先，内置关键词兜底；新流程训练入库后自动可触发。
        W1：匹配到流程类型后复用 _start_biz_workflow（含 v6.61.1 流程发起
        审计）——原实现直接 start_workflow，且 operation_logs 审计写段与
        _start_biz_workflow 重复。

        Returns:
            Optional[Dict]: 启动成功返回 {"instance_id":..., "workflow_type":...}，
                            无法启动返回 None
        """
        try:
            db = None
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                pass

            # I13：无需在此创建 WorkflowEnforcer（下方 _start_biz_workflow 内部
            # 统一创建），此处仅用 db 加载流程触发关键词映射
            # 流程触发关键词映射（DB 训练定义优先 + 内置兜底）
            wf_keywords = self._workflow_keywords_map(db)

            # 从输入中匹配流程类型
            matched_wf = None
            for wf_type, (keywords, biz_type, biz_id) in wf_keywords.items():
                if self._match_keyword_boundary(user_input, keywords):
                    matched_wf = (wf_type, biz_type, biz_id)
                    break

            if not matched_wf:
                return None  # 无法识别流程类型，由知识助手引导

            wf_type, biz_type, biz_id = matched_wf
            # W1：复用 _start_biz_workflow（含流程发起审计），单一路径
            instance, _err = self._start_biz_workflow(
                wf_type, biz_type, biz_id, user_context)
            if instance is None:
                return None
            return {
                "instance_id": instance.get("instance_id"),
                "workflow_type": wf_type,
            }
        except Exception:
            pass
        return None

    def _try_advance_training(self, config_id: Optional[int],
                              user_context: Optional[Dict]) -> dict:
        """流程定义训练审批推进（v6.61）：调 WorkflowEnforcer.advance_training_approval
        推进 workflow_configs 训练审批记录行（approval_chain 逐级 + steps_done 签字），
        全部通过后 apply_workflow_def_change 写入定义行生效。

        Args:
            config_id: 训练审批单（workflow_configs 记录行）config_id
            user_context: 会话上下文（取当前审批人角色/姓名）

        Returns:
            dict: {"success": bool, "completed": bool, "current_step": int,
                   "chain": list, "proposed": dict, "steps_done": list,
                   "workflow_name": str, "error": str}
        """
        try:
            from prog.runtime.workflow_enforcer import advance_training_approval
            db = None
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                pass
            user_info = (user_context or {}).get("user", {}) or {}
            if not user_info:
                user_info = self._fallback_user_info(user_context)
            return advance_training_approval(int(config_id), user_info, db=db)
        except Exception as e:
            return {"success": False, "completed": False, "error": str(e)}

    def _try_advance_workflow(self, instance_id: int, workflow_type: str,
                              user_context: Optional[Dict]) -> dict:
        """审批推进（v6.56）：调用 WorkflowEnforcer.advance_step 推进流程实例。

        与 _try_start_workflow 对称：发起走 start_workflow，审批推进走
        advance_step。流程定义（approval_chain/gate_checks）来自 DB
        workflow_configs，多级审批逐级推进，全部通过后实例标记 completed。

        Args:
            instance_id: 流程实例ID
            workflow_type: 流程类型（用于取流程名）
            user_context: 会话上下文（取当前用户角色）

        Returns:
            dict: {"success": bool, "instance_id":..., "workflow_name":...,
                   "current_step":..., "completed": bool, "error": str}
        """
        try:
            # B.3 P0：coerce_instance_id 兼容内存模式字符串 ID（"M{seq}"）
            # 与 DB 模式整数 ID，避免 int("M1") 抛 ValueError
            from prog.runtime.workflow_enforcer import (
                WorkflowEnforcer, coerce_instance_id)
            db = None
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                pass
            enforcer = WorkflowEnforcer(database=db)
            instance_id = coerce_instance_id(instance_id)
            user_info = (user_context or {}).get("user", {}) or {}
            if not user_info:
                user_info = self._fallback_user_info(user_context)
            result = enforcer.advance_step(instance_id, user_info)
            if result.get("success"):
                config = enforcer._get_workflow_config(workflow_type) or {}
                wf_name = config.get("workflow_name") or workflow_type
                # v6.58 + B.3 P0：审批确认审计已由 advance_step 原子写入——
                # _update_instance(audit_row=...) 在**同一事务**内完成
                # workflow_instances.steps_done 更新 + operation_logs 插入，
                # 任一失败整体回滚（推进失败），此处不再单独写日志，杜绝
                # "steps_done 已生效但审计缺失"的不一致。
                # v6.58：审批全部通过后的业务生效回调（drawing.approved 固定流程）
                if bool(result.get("completed")):
                    self._apply_workflow_effect(workflow_type, instance_id,
                                                user_info)
                # v6.57：审批结果同步所有流程相关人员（下一步审批人/发起人/
                # notify_rules 目标），通用流程流转
                self._notify_approval_progress(
                    enforcer, config, instance_id, wf_name,
                    result.get("current_step"),
                    bool(result.get("completed")), user_info)
                return {
                    "success": True,
                    "instance_id": instance_id,
                    "workflow_type": workflow_type,
                    "workflow_name": wf_name,
                    "current_step": result.get("current_step"),
                    "completed": bool(result.get("completed")),
                    "error": None,
                }
            return {
                "success": False,
                "instance_id": instance_id,
                "error": result.get("error") or "审批推进失败",
            }
        except Exception as e:
            return {"success": False, "error": f"审批引擎异常: {e}"}

    def _cancel_workflow_instance(self, instance_id: int, workflow_type: str,
                                  user_context: Optional[Dict]) -> dict:
        """待审批流程取消（S6，v6.88）：置实例 cancelled、留痕、通知发起人。

        此前 awaiting_approval 阶段"取消/不要了"→ cancel 意图 target 为空 →
        兜底清 pending → workflow_instances 永久 running、无取消记录。
        本方法补齐取消分支：更新实例状态 cancelled + operation_logs 留痕 +
        event_bus 通知发起人（复用审批通知事件）。

        Args:
            instance_id: 流程实例ID
            workflow_type: 流程类型（用于取流程名）
            user_context: 会话上下文（取当前用户）

        Returns:
            dict: {"success": bool, "workflow_name": str, "error": str}
        """
        try:
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            from prog.runtime.event_bus import (
                EVENT_NOTIFY_CREATE, publish_event)
            db = None
            try:
                from prog.runtime.database import get_database
                db = get_database()
            except Exception:
                pass
            enforcer = WorkflowEnforcer(database=db)
            instance = enforcer._get_instance(instance_id)
            if not instance:
                return {"success": False, "workflow_name": workflow_type,
                        "error": f"流程实例 {instance_id} 不存在"}
            config = enforcer._get_workflow_config(workflow_type) or {}
            wf_name = config.get("workflow_name") or workflow_type
            # 置 cancelled（_update_instance 返回 False 表示 DB 写失败）
            if not enforcer._update_instance(
                    instance_id, {"status": "cancelled"}):
                return {"success": False, "workflow_name": wf_name,
                        "error": "流程实例状态更新失败（DB 写入异常）"}
            # 留痕 operation_logs
            operator = (user_context or {}).get("user", {}) or {}
            try:
                if db is not None:
                    db.insert("operation_logs", {
                        # W30：与 _start_biz_workflow 统一取值顺序（user_id 优先，id 兜底）
                        # v6.94 FK 双保险：user_id 空值回退 None（空串违反
                        # fk_operation_logs_user，NULL 可过外键）
                        "user_id": (operator.get("user_id")
                                    or operator.get("id") or None),
                        "action": "workflow_cancelled",
                        "details": {
                            "instance_id": instance_id,
                            "workflow_type": workflow_type,
                            "cancelled_by": (operator.get("name")
                                             or operator.get("id")
                                             or operator.get("user_id") or ""),
                        },
                    })
            except Exception:
                pass
            # 通知发起人（复用审批通知事件通道）
            creator = instance.get("created_by") or ""
            try:
                publish_event(
                    EVENT_NOTIFY_CREATE,
                    {"ntype": "warning",
                     "title": f"审批已取消：{wf_name}",
                     "content": (f"「{wf_name}」流程（实例 {instance_id}）"
                                 f"已被取消，流程终止。"),
                     "target_user": creator},
                    source="coordinator")
            except Exception:
                pass
            return {"success": True, "workflow_name": wf_name, "error": None}
        except Exception as e:
            return {"success": False, "workflow_name": workflow_type,
                    "error": f"取消失败：{e}"}

    def _apply_workflow_effect(self, workflow_type: str, instance_id: int,
                               operator: dict) -> None:
        """审批全部通过后的业务生效回调（v6.58/v6.59，固定流程，不可训练）。

        规格书 §1.6.1 事件表：
            drawing.approved   -> 通知生产部、更新有效版本、归档旧版
            order.approved     -> 订单 draft->confirmed（进入生产队列）
            return.approved    -> 退货单 approved（启动收货/退款）
            schedule.approved  -> 按审批通过的排产方案生成工单
            product.approved   -> 新产品建档生效
            customer.approved  -> 客户信用额度更新生效
        流程实例全部步骤通过（status=completed）时按 workflow_type 分派
        业务生效动作；业务数据变更失败不影响审批实例状态（由操作日志留痕）。
        """
        try:
            if workflow_type == "drawing_change":
                self._apply_drawing_change_effect(instance_id, operator)
            elif workflow_type == "order_approve":
                self._apply_order_approve_effect(instance_id, operator)
            elif workflow_type == "return_process":
                self._apply_return_process_effect(instance_id, operator)
            elif workflow_type == "production_schedule":
                self._apply_production_schedule_effect(instance_id, operator)
            elif workflow_type == "product_change":
                self._apply_product_change_effect(instance_id, operator)
            elif workflow_type == "customer_change":
                self._apply_customer_change_effect(instance_id, operator)
        except Exception:
            pass

    @staticmethod
    def _get_instance_biz_data(instance_id: int, db: Any):
        """从流程实例读取暂存业务数据（extra_data.biz_data，P1 门禁写入）。

        Returns:
            (instance: dict, biz_data: dict)
        """
        from prog.runtime.workflow_enforcer import WorkflowEnforcer
        enforcer = WorkflowEnforcer(database=db)
        instance = enforcer._get_instance(instance_id) or {}
        extra = instance.get("extra_data") or {}
        if isinstance(extra, str):
            try:
                import json as _json
                extra = _json.loads(extra)
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            extra = {}
        return instance, (extra.get("biz_data") or {} if isinstance(extra.get("biz_data"), dict) else {})

    def _apply_order_approve_effect(self, instance_id: int, operator: dict) -> None:
        """订单确认审批生效：订单 draft->confirmed（规格书 §2.5.5 下单-审批-生产）。"""
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            instance, biz_data = self._get_instance_biz_data(instance_id, db)
            order_id = biz_data.get("order_id") or instance.get("biz_id") or ""
            if not order_id or not str(order_id).startswith("SO"):
                return
            affected = db.update("orders", {"status": "confirmed"},
                                 {"order_id": order_id})
            if affected > 0:
                db.insert("operation_logs", {
                    "user_id": (operator or {}).get("user_id") or "",
                    "action": "order_confirmed",
                    "details": {"order_id": order_id,
                                "workflow_instance": instance_id},
                })
        except Exception:
            pass

    def _apply_return_process_effect(self, instance_id: int, operator: dict) -> None:
        """退货审批生效：退货单 approved + 原订单对应库存回退（规格书 return_process）。"""
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            instance, biz_data = self._get_instance_biz_data(instance_id, db)
            return_id = biz_data.get("return_id") or instance.get("biz_id") or ""
            if not return_id or not str(return_id).startswith("RT"):
                return
            ret = db.query_one("return_orders", {"return_id": return_id})
            if not ret:
                return
            db.update("return_orders", {"status": "approved"},
                      {"return_id": return_id})
            # 库存回退：按原订单产品数量回退至 raw 阶段
            original_order_id = ret.get("original_order_id") or ""
            if original_order_id:
                order = db.query_one("orders", {"order_id": original_order_id})
                if order and order.get("product_code"):
                    qty = (ret.get("extra_data") or {})
                    if isinstance(qty, str):
                        try:
                            import json as _json
                            qty = _json.loads(qty)
                        except Exception:
                            qty = {}
                    back_qty = (qty.get("return_qty") or 0) if isinstance(qty, dict) else 0
                    if back_qty > 0:
                        # R3 修复：inventory 为五阶段列式结构（raw/wip_cnc/wip_anode/
                        # wip_qc/finished，无 stage/quantity/type/remark 列）——原实现
                        # insert 不存在的列恒报 UndefinedColumn 被吞，退货单 approved
                        # 但库存永不回退。改为按列增量回退 raw 阶段 + 写流水留痕。
                        inv = db.query_one("inventory",
                                           {"product_code": order.get("product_code")})
                        if inv:
                            cur_raw = int(inv.get("raw", 0) or 0)
                            db.update("inventory", {"raw": cur_raw + int(back_qty)},
                                      {"product_code": order.get("product_code")})
                        else:
                            db.insert("inventory", {
                                "product_code": order.get("product_code"),
                                "raw": int(back_qty),
                            })
                        try:
                            db.insert("inventory_movements", {
                                "product_code": order.get("product_code"),
                                "movement_type": "return_in",
                                "from_stage": "",
                                "to_stage": "raw",
                                "quantity": int(back_qty),
                                "operator": (operator or {}).get("user_id") or "",
                                "reference_no": return_id,
                                "extra_data": {},
                            })
                        except Exception:
                            # 流水留痕失败不阻断库存回退（已回退成功）
                            pass
            db.insert("operation_logs", {
                "user_id": (operator or {}).get("user_id") or "",
                "action": "return_approved",
                "details": {"return_id": return_id,
                            "order_id": original_order_id,
                            "workflow_instance": instance_id},
            })
        except Exception:
            pass

    def _apply_production_schedule_effect(self, instance_id: int, operator: dict) -> None:
        """排产方案审批生效：按暂存排产数据生成工单（planned，规格书 production_schedule）。"""
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            instance, biz_data = self._get_instance_biz_data(instance_id, db)
            order_id = biz_data.get("order_id") or ""
            product_code = biz_data.get("product_code") or ""
            quantity = biz_data.get("quantity") or 0
            if not order_id or not product_code or not quantity:
                return
            # 按暂存工序明细生成工单；无明细时生成一条汇总工单
            steps = biz_data.get("process_steps") or []
            now = datetime.now()
            # S6：工单ID唯一性——时间戳后追加进程级自增序号（4 位），
            # 消除同一秒内多订单/多工序生成相同 WO 的碰撞风险
            _wo_seq = next(_WO_ID_SEQ)
            if steps and isinstance(steps, list):
                for i, st in enumerate(steps, start=1):
                    wo_id = (f"WO{now.strftime('%Y%m%d')}"
                             f"{int(now.timestamp()) % 10000:04d}{_wo_seq + i:04d}")
                    db.insert("work_orders", {
                        "work_order_id": wo_id, "order_id": order_id,
                        "product_code": product_code, "bom_level": 1,
                        "quantity": quantity,
                        "status": "planned",
                        "extra_data": {"step": st if isinstance(st, dict) else {"name": st}},
                    })
            else:
                wo_id = (f"WO{now.strftime('%Y%m%d')}"
                         f"{int(now.timestamp()) % 10000:04d}{_wo_seq:04d}")
                db.insert("work_orders", {
                    "work_order_id": wo_id, "order_id": order_id,
                    "product_code": product_code, "bom_level": 1,
                    "quantity": quantity, "status": "planned",
                })
            db.insert("operation_logs", {
                "user_id": (operator or {}).get("user_id") or "",
                "action": "schedule_effective",
                "details": {"order_id": order_id, "product_code": product_code,
                            "workflow_instance": instance_id},
            })
        except Exception:
            pass

    def _apply_product_change_effect(self, instance_id: int, operator: dict) -> None:
        """新产品建档审批生效：审批通过后写入 products 表（规格书 product_change）。"""
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            instance, biz_data = self._get_instance_biz_data(instance_id, db)
            product_code = biz_data.get("product_code") or ""
            product_name = biz_data.get("product_name") or ""
            if not product_code or not product_name:
                return
            existing = db.query_one("products", {"product_code": product_code})
            if existing:
                return
            db.insert("products", {
                "product_code": product_code,
                "product_name": product_name,
                "category": biz_data.get("category", ""),
                "spec": biz_data.get("spec", ""),
                "unit": biz_data.get("unit", "套"),
                "price": biz_data.get("price", 0),
                "cost_price": biz_data.get("cost_price", 0),
                "drawing_version": biz_data.get("drawing_version", "1.0"),
                "description": biz_data.get("description", ""),
            })
            db.insert("operation_logs", {
                "user_id": (operator or {}).get("user_id") or "",
                "action": "product_created",
                "details": {"product_code": product_code,
                            "product_name": product_name,
                            "workflow_instance": instance_id},
            })
        except Exception:
            pass

    def _apply_customer_change_effect(self, instance_id: int, operator: dict) -> None:
        """客户信用额度审批生效：审批通过后更新 customers.credit_limit（规格书 customer_change）。"""
        try:
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            instance, biz_data = self._get_instance_biz_data(instance_id, db)
            customer_id = biz_data.get("customer_id") or ""
            new_limit = biz_data.get("credit_limit")
            if not customer_id or new_limit is None:
                return
            affected = db.update("customers", {"credit_limit": new_limit},
                                 {"customer_id": customer_id})
            if affected > 0:
                db.insert("operation_logs", {
                    "user_id": (operator or {}).get("user_id") or "",
                    "action": "credit_limit_updated",
                    "details": {"customer_id": customer_id,
                                "credit_limit": new_limit,
                                "workflow_instance": instance_id},
                })
        except Exception:
            pass

    def _apply_drawing_change_effect(self, instance_id: int, operator: dict) -> None:
        """图纸版本变更生效：新版本->effective、旧版本->superseded、同步 products.drawing_version。

        事务性执行版本切换（规格书 §2.3.4 版本切换事务）：
            Step1 旧版本失效（superseded）
            Step2 新版本生效（effective）
            Step3 同步 products.drawing_version（R.2.4 下单版本一致性校验使用）
            Step4 版本切换日志（审计追溯）
        """
        try:
            from prog.runtime.workflow_enforcer import WorkflowEnforcer
            from prog.runtime.database import get_database
            db = get_database()
            if db is None:
                return
            enforcer = WorkflowEnforcer(database=db)
            instance = enforcer._get_instance(instance_id) or {}
            if instance.get("workflow_type") != "drawing_change":
                return
            biz_id = instance.get("biz_id") or ""
            if not str(biz_id).isdigit():
                return
            drawing = db.query_one("drawings", {"drawing_id": int(biz_id)})
            if not drawing:
                return
            product_code = drawing.get("product_code") or ""
            new_version = drawing.get("version") or ""
            if not product_code or not new_version:
                return
            # W4：旧版本在 Step1 置 superseded 之前查询（原实现取 drawing.extra_data
            # 无意义——extra_data 不是版本号）。旧版本 = 同产品当前 effective 的图纸版本。
            old_version = ""
            try:
                old_row = db.query_one(
                    "drawings",
                    {"product_code": product_code, "status": "effective"},
                    ["version"])
                if old_row:
                    old_version = str(old_row.get("version") or "")
            except Exception:
                old_version = ""
            try:
                # S5 补偿回滚：数据库层 insert/update 为独立连接 autocommit（无跨
                # 方法事务），任一步骤失败会留下"旧版已失效、新版未生效"中间态。
                # 用 try/except 包裹四步，失败时补偿回滚已执行步骤，保证原子性。
                # Step1 旧版本失效（兼容 active/effective 状态标记）
                db.execute(
                    "UPDATE drawings SET status = 'superseded' "
                    "WHERE product_code = :pc AND status IN ('effective', 'active')",
                    {"pc": product_code})
                # Step2 新版本生效
                db.update("drawings", {"status": "effective"},
                          {"drawing_id": drawing["drawing_id"]})
                # Step3 同步 products.drawing_version
                db.update("products", {"drawing_version": new_version},
                          {"product_code": product_code})
                # Step4 版本切换日志（审计追溯）
                db.insert("operation_logs", {
                    "user_id": (operator or {}).get("user_id") or "",
                    "action": "drawing_version_effective",
                    "details": {
                        "product_code": product_code,
                        "new_version": new_version,
                        "old_version": old_version,
                        "drawing_id": drawing["drawing_id"],
                        "workflow_instance": instance_id,
                    },
                })
            except Exception as _tx_err:
                # 补偿回滚：恢复旧版本 effective 状态与产品版本，避免"无有效图纸"
                try:
                    db.execute(
                        "UPDATE drawings SET status = 'effective' "
                        "WHERE product_code = :pc AND status = 'superseded'",
                        {"pc": product_code})
                    db.update("products", {"drawing_version": old_version},
                              {"product_code": product_code})
                except Exception:
                    pass
                # 版本切换失败留痕（便于人工介入）
                try:
                    db.insert("operation_logs", {
                        "user_id": (operator or {}).get("user_id") or "",
                        "action": "drawing_version_effective_failed",
                        "details": {
                            "product_code": product_code,
                            "new_version": new_version,
                            "error": str(_tx_err),
                            "workflow_instance": instance_id,
                        },
                    })
                except Exception:
                    pass
        except Exception:
            pass

    def _notify_approval_progress(self, enforcer, config, instance_id,
                                  wf_name, new_step, completed,
                                  approver_user) -> None:
        """审批结果同步所有流程相关人员（v6.57，通用流程流转）。

        - 单步通过：通知下一步审批角色（新待办）+ 发起人（进度更新）
        - 全部通过：通知发起人 + workflow_configs.notify_rules 目标
        - 通知持久化 DB，点击后经 chat resume_workflow 恢复审批上下文
        """
        try:
            from prog.runtime.event_bus import (
                EVENT_NOTIFY_CREATE, EVENT_NOTIFY_APPROVAL,
                EVENT_NOTIFY_EXPIRE, publish_event)
            import json as _json
            wf_type = config.get("workflow_type", "")
            instance = enforcer._get_instance(instance_id) or {}
            creator = instance.get("created_by") or ""
            if completed:
                # 全部通过：通知发起人 + notify_rules 目标
                # v6.70：完成即退出——全部通过后该实例不再有待办，失效全部审批待办通知
                # v6.78.2：失效与完成通知合并为单一事件（expire_before 标志），
                # handler 单线程内先失效旧待办再创建完成通知，消除 Redis 跨主题乱序。
                publish_event(EVENT_NOTIFY_CREATE,
                              {"ntype": "info",
                               "title": f"审批完成：{wf_name}",
                               "content": f"「{wf_name}」流程（实例 {instance_id}）审批已全部通过并生效。",
                               "target_user": creator,
                               "expire_before": {"workflow_type": wf_type,
                                                 "instance_id": instance_id}},
                              source="coordinator")
                self._notify_rule_targets(config, wf_name, instance_id,
                                          "approved", creator)
            else:
                # 单步通过：通知下一步审批人（新待办）+ 发起人（进度）
                # v6.70：完成即退出——推进前失效该实例旧"审批待办"通知
                # （上一审批人待办即时退出，避免已办结通知长期滞留列表）
                # v6.78.2：失效与新待办合并为单一事件（expire_before 标志），
                # handler 单线程内先失效旧待办再创建新待办，消除 Redis 跨主题乱序。
                chain = config.get("approval_chain") or []
                if isinstance(chain, str):
                    try:
                        chain = _json.loads(chain)
                    except Exception:
                        chain = []
                next_role = ""
                if isinstance(chain, list) and new_step and 0 < new_step <= len(chain):
                    next_role = (chain[new_step - 1] or {}).get("role", "")
                # v6.69：待办通知携带实例 biz_data（金额/事由/申请人等业务明细），
                # 审批人在铃铛列表/点击后即可看到具体内容方便判断，不再为空。
                # v6.69.1：修正 WorkflowEnforcer 属性名（database -> _db），
                # 原 AttributeError 被 except 吞掉导致单步推进通知（待办+进度）全部中断。
                _, biz_data = self._get_instance_biz_data(instance_id, enforcer.db)
                publish_event(EVENT_NOTIFY_APPROVAL,
                              {"workflow_type": wf_type,
                               "instance_id": instance_id,
                               "workflow_name": wf_name,
                               "step_role": next_role,
                               "target_user": "",
                               "biz_detail": biz_data or None,
                               "expire_before": {"workflow_type": wf_type,
                                                 "instance_id": instance_id}},
                              source="coordinator")
                publish_event(EVENT_NOTIFY_CREATE,
                              {"ntype": "info",
                               "title": f"审批进度：{wf_name}",
                               "content": f"「{wf_name}」流程（实例 {instance_id}）第 {new_step - 1} 步"
                                          f"已通过，流程推进至第 {new_step} 步。",
                               "target_user": creator},
                              source="coordinator")
        except Exception:
            pass

    def _notify_rule_targets(self, config, wf_name, instance_id,
                             event, creator) -> None:
        """按 workflow_configs.notify_rules 通知目标（通用流程流转）。

        event 匹配（approved/effective 等）；target 为角色时按 users.role_id
        匹配，requester/creator 等标识发给发起人。
        v6.57：结果同步类通知使用"审批结果"标题如实反映流程结果，
        不复用"审批待办"（避免流程已完成仍提示待审批）。
        """
        try:
            from prog.runtime.event_bus import EVENT_NOTIFY_CREATE, publish_event
            import json as _json
            rules = config.get("notify_rules") or []
            if isinstance(rules, str):
                try:
                    rules = _json.loads(rules)
                except Exception:
                    rules = []
            if not isinstance(rules, list):
                return
            wf_type = config.get("workflow_type", "")
            extra = {"workflow_type": wf_type, "instance_id": instance_id or 0}
            title = f"审批结果：{wf_name}"
            content = (f"「{wf_name}」流程（实例 {instance_id}）审批结果已同步"
                       f"（事件：{event}）。")
            for rule in rules:
                if not isinstance(rule, dict) or rule.get("event") != event:
                    continue
                target = rule.get("target") or ""
                if not target:
                    continue
                target_user = ""
                if target in ("requester", "creator", "发起人"):
                    target_user = creator
                if target_user:
                    publish_event(EVENT_NOTIFY_CREATE,
                                  {"ntype": "info", "title": title,
                                   "content": content,
                                   "target_user": target_user,
                                   "extra": dict(extra)},
                                  source="coordinator")
                else:
                    # 角色目标：按 users.role_id 匹配所有在职用户
                    try:
                        from prog.runtime.database import get_database
                        db = get_database()
                    except Exception:
                        db = None
                    if db is not None:
                        try:
                            rows = db.query_many("users",
                                                 {"role_id": target,
                                                  "status": "active"}) or []
                        except Exception:
                            rows = []
                        for r in rows:
                            uid = r.get("user_id")
                            if uid:
                                publish_event(EVENT_NOTIFY_CREATE,
                                              {"ntype": "info", "title": title,
                                               "content": content,
                                               "target_user": uid,
                                               "extra": dict(extra)},
                                              source="coordinator")
        except Exception:
            pass

    # --------------------------------------------------------
    # 结果聚合
    # --------------------------------------------------------
    def _aggregate_results(self, responses: List["AgentResponse"]) -> "AgentResponse":
        """
        多Agent结果聚合。

        设计意图：
            当一次输入触发多个Agent时，合并它们的响应为单一 AgentResponse。

        参数：
            responses: 各Agent的响应列表

        返回：
            AgentResponse: 聚合后的响应

        聚合策略：
            - 文本部分按Agent顺序拼接
            - 结构化数据合并为字典
            - 任一Agent标记 need_confirm 则整体需确认
            - 任一Agent规则阻断则整体阻断
        """
        from prog.runtime.base_agent import AgentResponse

        if not responses:
            return AgentResponse(
                content="暂无处理结果。",
                agent_name="协调Agent",
            )

        # 单Agent时直接透传
        if len(responses) == 1:
            return responses[0]

        # 多Agent聚合
        contents = []
        data: Dict[str, Any] = {}
        rules_violated: List[str] = []
        need_confirm = False
        is_blocked = False
        agent_names = []

        for resp in responses:
            if resp.content:
                contents.append(f"【{resp.agent_name}】\n{resp.content}")
            if resp.data:
                data[resp.agent_name] = resp.data
            rules_violated.extend(resp.rules_violated)
            if resp.need_confirm:
                need_confirm = True
            if resp.action == "blocked":
                is_blocked = True
            agent_names.append(resp.agent_name)

        return AgentResponse(
            content="\n\n".join(contents),
            data=data,
            action="blocked" if is_blocked else "aggregated",
            need_confirm=need_confirm,
            rules_violated=rules_violated,
            agent_name="协调Agent",
            metadata={"agent_names": agent_names},
        )

    def route_compound(self, user_input: str,
                       user_context: Optional[Dict] = None) -> "AgentResponse":
        """开源版：复合句直接透传基础路由（多跳拆解/并行为商业版能力）"""
        return self.route(user_input, user_context)

    def _select_agent_for_error(self, user_input: str) -> Any:
        """异常兜底：尽力识别输入对应的Agent（仅供错误响应命名）。

        W5：传 skip_llm=True——错误路径仅做规则匹配，不触发 LLM 语义识别
        （避免错误兜底带来额外 LLM 延迟/双 token）。
        """
        try:
            intent = self._recognize_intent(user_input, skip_llm=True)
            return self._select_agent(intent)
        except Exception:
            return None
