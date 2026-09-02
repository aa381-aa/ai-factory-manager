"""
可训练子意图识别引擎（v6.46 阶段 C4）
=======================================
将 8 个 Agent 内硬编码的子意图关键词表迁入 DB（business_rules.SUB-INTENT-DEFS），
Agent 统一委托本引擎识别子意图；DB 修改关键词后即时生效（TTL 缓存 + 热更新），
DB 不可用时降级内置默认定义。

定义结构（config_json.defs）：
    {"defs": {
        "<agent_type>": {
            "<sub_intent>": ["关键词1", "关键词2", ...],
        },
    }}

匹配语义（与各 Agent 原实现一致）：
    - 子串匹配（any(k in user_input for k in keywords)）
    - 定义顺序即优先级（首个命中的子意图生效）
    - 各 Agent 特有复合条件（如 qc 的"处置+不合格"组合、sales 的"查.*订单"
      正则、knowledge 的 save_to_kb 长度约束）仍保留在 Agent 侧，本引擎返回
      空后由 Agent 兜底逻辑处理。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 可训练子意图识别引擎：8 个 Agent 子意图关键词表迁入 DB（business_rules.SUB-INTENT-DEFS），DB 修改关键词后即时生效（TTL 缓存 + invalidate_cache 热更新），DB 不可用时降级内置默认定义（来源：SPEC §3.11.3.1 / 业务规格书 v6.46 C4 / 模块拆分方案 契约3）
        - 匹配语义与各 Agent 原实现一致：子串匹配、定义顺序即优先级（首个命中的子意图生效）；各 Agent 特有复合条件仍保留在 Agent 侧，引擎返回空后由 Agent 兜底（来源：模块拆分方案 契约3 与拆分纪律6 / SPEC §3.11.3.1）
    对外接口（方法/API）：
        - recognize_sub_intent(agent_type, user_input) -> str：返回子意图标签，无命中返回 ""（由 Agent 侧兜底）（来源：模块拆分方案 契约3）
        - get_sub_intent_keywords(agent_type, sub_intent) -> list：取指定子意图的关键词列表（来源：模块拆分方案 契约3）
        - get_sub_intent_defs(agent_type) -> dict：取 Agent 的全部子意图定义（来源：模块拆分方案 契约3）
        - invalidate_cache()：清除 TTL 缓存，DB 修改即时生效（热更新）（来源：SPEC §3.11.3.1）
        - DEFAULT_SUB_INTENT_DEFS：内置默认子意图定义（DB 不可用降级兜底，与各 Agent 原硬编码完全一致）（来源：模块 docstring）
    错误处理要求：
        - DB 不可用或定义缺失：降级内置 DEFAULT_SUB_INTENT_DEFS（来源：SPEC §3.11.3.1）
        - 无子意图命中：返回 ""，交由 Agent 侧复合条件/兜底逻辑处理（来源：模块拆分方案 契约3）
"""

import threading
import time
from typing import Any, Dict, List, Optional

# ============================================================
# 内置默认子意图定义（DB 不可用降级兜底；与各 Agent 原硬编码完全一致）
# ============================================================
DEFAULT_SUB_INTENT_DEFS: Dict[str, Dict[str, List[str]]] = {
    "sales": {
        "modify_price": ["改价", "改价格", "降价", "涨价", "调价", "修改价格",
                         "修改单价", "价格改", "价格降", "价格涨", "单价改", "单价降",
                         "改一下价格", "改个价格", "把价格改"],
        "create_order": ["下单", "创建订单", "新建订单", "采购", "订购", "订货",
                         "下一笔", "下个单", "想下单", "要下单"],
        "modify_order": ["修改订单", "改单", "变更", "追加", "加单", "加数量",
                         "改成", "修改数量"],
        "query_order": ["查订单", "查一下订单", "查看订单", "订单状态", "订单进度",
                        "订单情况", "订单详情", "所有订单", "订单列表"],
        "check_inventory": ["查库存", "库存", "现货", "还有多少", "剩多少",
                            "有没有货", "备货", "在制"],
        "return_order": ["退货", "退换", "退款", "退回", "申请退货"],
        "price_reference": ["报价", "价格参考", "历史价格", "成交价", "建议价",
                            "价格建议"],
        "customer_profile": ["客户画像", "客户偏好", "合作记录", "客户历史",
                             "客户档案", "客户信息", "客户查询", "客户情况",
                             "查客户"],
        "query_overview": ["数据总览", "经营数据", "经营情况", "经营状况",
                           "本月数据", "月度数据", "经营概况", "经营指标",
                           "销售数据", "订单数据", "产值数据", "产量数据",
                           "库存数据", "财务数据", "工厂概况", "数据看板",
                           "总览", "概览", "整体情况"],
        "contract": ["生成合同", "起草合同", "拟合同", "签合同", "签订合同",
                     "合同管理", "合同模板", "查合同", "查询合同", "我的合同",
                     "合同列表", "合同状态", "合同详情"],
    },
    "warehouse": {
        # v6.68：inventory_query 置于 stock_in/stock_out 之前——主谓宾查询句
        #（"查看入库记录/查看出库记录"）优先命中查询子意图，不被"入库/出库"
        # 执行词抢占（执行意图"我要入库/出库100件"无查询句式，仍命中 stock_in/out）。
        "inventory_query": ["库存", "查库存", "库存查询", "现有多少", "还有多少",
                            "剩多少", "库存状态", "库存分布", "库存价值",
                            "查看入库记录", "查看出库记录", "查一下入库", "查一下出库",
                            "入库记录", "出库记录", "发货记录", "收货记录",
                            "入库单", "出库单", "库存记录"],
        "purchase_request": ["采购申请", "请购", "申请采购", "采购单", "请购单",
                             "生成采购"],
        "shortage_check": ["缺料", "缺料检查", "物料短缺", "缺料清单", "物料不足",
                           "缺多少"],
        "stock_in": ["入库", "入仓", "收料", "收货", "上架", "采购入库",
                     "生产入库", "成品入库"],
        "stock_out": ["出库", "出仓", "领料", "发货", "销售出库", "生产领料",
                      "扣减库存"],
        "material_trace": ["物料追踪", "物料追溯", "追踪", "流向", "流转记录",
                           "物料轨迹", "全生命周期", "来源去向", "移动记录"],
        "price_compare": ["比价", "议价", "比个价", "比一下价格", "比下价格",
                          "比一比", "价格对比", "报价对比", "货比三家",
                          "供应商报价", "比价分析", "对比价格"],
    },
    "production": {
        "fault_warning": ["故障", "报警", "异常", "预警"],
        "maintenance": ["保养", "维护", "维修计划", "保养计划"],
        "equipment_efficiency": ["效率", "OEE", "oee", "设备效率", "综合效率"],
        "schedule_coordination": ["排产协同", "协同排产", "产能协同"],
        "equipment_status": ["设备状态", "设备监控", "机台状态", "机床状态",
                             "设备情况"],
        "subcontract": ["外协", "外包", "供应商"],
        "rush_order": ["插单", "加急", "紧急订单", "急单"],
        "schedule_compare": ["排产对比", "方案对比", "排产方案", "加班排产",
                             "多方案"],
        # v6.68：查询类子意图——主谓宾查询句（"查看排产计划/查看工单/生产看板"）
        # 优先于执行/分析子意图（schedule/capacity/progress），避免"查看排产计划"
        # 被"排产"执行词抢占为排产引导、"查看工单"被 schedule 的"工单"词抢占。
        # 注意：不用裸"生产计划"（"安排生产计划"是执行排产，含"生产计划"子串），
        # 仅用查询动词+生产计划完整短语。
        "schedule_query": ["排产计划", "排产情况", "排产进度", "排产安排", "排产表",
                           "排产列表", "查看排产", "查看生产计划", "查询生产计划",
                           "看看生产计划", "排产日期"],
        "work_order_query": ["查看工单", "查工单", "查一下工单", "工单列表", "工单状态",
                             "工单查询", "工单进度", "工单情况", "我的工单",
                             "工单详情", "工单记录"],
        "production_progress": ["生产看板", "产线状态", "产线进度", "看板数据",
                                "生产进度", "生产状态", "产线情况"],
        "capacity": ["产能", "负荷", "瓶颈", "利用率", "还能排", "排多少"],
        "progress": ["进度", "跟踪", "生产情况", "完成率"],
        "schedule": ["排产", "安排", "调度", "工单", "派工", "派单", "生成排产"],
    },
    "technical": {
        "version_change": ["版本变更", "版本切换", "版本升级", "切版", "换版",
                           "版本影响", "变更版本", "版本通知"],
        "similar_search": ["相似", "相似零件", "相似结构", "语义检索", "检索零件",
                           "类似零件"],
        "cost_analysis": ["成本", "成本分解", "成本结构", "成本对比", "成本分析",
                          "费用分解"],
        "bom_management": ["BOM", "bom", "物料清单", "BOM结构", "BOM展开",
                           "缺料分析", "多层展开", "子件", "组件清单"],
        # process_card 须置于 process_route 之前：其关键词（工艺卡/工艺文件等）
        # 含"工艺"子串，裸"工艺"词属于 process_route，顺序在后才能精确命中
        "process_card": ["工艺卡", "工艺文件", "工艺规程", "工艺卡片", "上传工艺",
                         "工艺参数表", "工序卡"],
        "process_route": ["工艺路线", "工序", "工时", "工艺流程", "标准工时",
                          "工序详情", "工艺", "产线工序"],
        "drawing_management": ["图纸", "版本查询", "上传版本", "新版本", "图纸版本",
                               "图号", "图纸文件", "CAD", "补全图纸", "补齐图纸",
                               "缺失字段"],
    },
    "knowledge": {
        # save_to_kb 含"录入/收录/存知识库"等关键词，但需"长度<=8"约束
        # （见 knowledge_assistant.py _recognize_sub_intent），故保留在 Agent 侧，
        # 不放入本表；此处仅收录纯关键词子意图。
        "process_guide": ["流程", "操作步骤", "怎么操作", "怎么做", "怎么填",
                          "审批流程", "填写说明", "操作指引", "办事流程",
                          "申请流程", "报销流程", "请假流程"],
        "policy_consultation": ["制度", "政策", "规定", "管理办法", "管理制度",
                                "岗位职责", "考核", "福利", "薪酬", "考勤",
                                "休假", "补贴", "规范"],
        "kb_gap_analysis": ["知识缺口", "缺口分析", "知识盲区", "高频问题",
                            "缺什么知识", "还缺哪些", "知识空白", "未收录知识",
                            "知识覆盖"],
    },
    "qc": {
        "8d_report": ["8D", "8d", "八D", "8D报告"],
        "customer_complaint": ["客诉", "客户投诉", "客户抱怨", "投诉登记",
                               "投诉处理"],
        # defect_disposal 复合条件（"处置"+"不合格/不良"）保留在 Agent 侧
        "defect_disposal": ["不合格品处置", "不合格处置", "返工", "报废",
                            "让步接收", "处置申请"],
        "quality_trace": ["质量追溯", "追溯", "批次追溯", "产品追溯", "全链路追溯"],
        "qc_inspection": ["首件检验", "首检", "FAI", "过程巡检", "巡检",
                          "工序检验", "成品检验", "入库检验", "成品入库",
                          "录入质检", "提交检验", "创建质检", "质检录入"],
        # defect_analysis 复合条件/QC-STANDARD.defect_types 缺陷类型词保留在 Agent 侧
        "defect_analysis": ["不良率", "不良分析", "缺陷分析", "不合格率", "不良原因",
                            "质量趋势", "质量分析", "Top不良", "改进建议",
                            "fail原因", "失败原因", "不合格原因", "分析原因",
                            "原因分析", "为什么不合格", "为什么fail", "怎么改进",
                            "如何改进", "改进方法", "改善", "优化质量"],
        "qc_standard": ["质检标准", "AQL", "抽样方案", "抽检标准", "检验标准",
                        "质量标准", "全检", "抽检"],
        "qc_record": ["质检记录", "质检结果", "检验记录", "质检报告", "质检情况",
                      "不合格项", "质检明细", "查质检"],
    },
    "hr": {
        "account_management": ["创建账户", "创建账号", "启用账户", "禁用账户",
                               "启用账号", "禁用账号", "账户管理", "账号管理",
                               "停用账户"],
        "onboarding": ["入职", "新员工", "建档", "新入职", "入职办理"],
        "resignation": ["离职", "辞职", "交接", "离职手续", "离职办理"],
        "piece_rate_pay": ["计件工资", "计件单价", "计件核算", "工资核算"],
        "payroll": ["工资", "薪酬", "工资单", "发工资", "工资发放", "工资台账",
                    "工资审批"],
        "work_report": ["报工", "报工记录", "提交报工", "报工查询"],
        "attendance": ["考勤", "打卡", "出勤", "迟到", "请假", "早退", "缺勤"],
        "org_management": ["组织架构", "部门", "人员列表", "员工列表", "组织结构",
                           "部门结构", "部门查询"],
        "personnel_qa": ["人员信息", "员工信息", "查人员", "查员工", "谁在",
                         "人员查询", "员工查询"],
    },
    "finance": {
        # M1：凭证/月结（置于前——"入账"仍属 payment_confirm，不与凭证词冲突）
        "post_journal": ["凭证", "记账", "做账", "分录", "编制凭证"],
        "month_end_close": ["月结", "月末结账", "期末结账", "关账", "结账"],
        # payment_confirm 复合条件（"登记/录入"+"收款"）保留在 Agent 侧
        "payment_confirm": ["收款确认", "确认收款", "登记收款", "录入收款",
                            "收到款", "收款登记", "到账确认", "入账"],
        "invoice_match": ["三单匹配", "发票匹配", "发票校验", "三单校验"],
        "payable": ["应付", "供应商付款", "付款执行", "付款审批"],
        "payment_schedule": ["付款计划", "合同付款", "到期提醒", "付款提醒"],
        "cost_variance": ["成本差异", "标准成本", "实际成本", "成本偏差"],
        "cost_line_check": ["售价低于成本", "成本线", "价格拦截", "低于成本"],
        "cost_accounting": ["成本核算", "工单成本", "利润分析", "成本分析",
                            "生产成本", "毛利", "成本计算"],
        "credit_management": ["信用", "信用额度", "信用查询", "额度调整",
                              "信用使用", "信用预警", "额度", "赊销"],
        "receivable": ["应收", "应收账款", "账龄", "催收", "欠款", "未收款",
                       "应收余额", "账款", "对账"],
    },
}

# 缓存：DB 定义 TTL 缓存（5 秒，训练修改后延迟生效；invalidate_cache 即时生效）
_cache: Dict[str, Any] = {"ts": 0.0, "defs": None}
_CACHE_TTL = 5.0
# W23：缓存加载锁（double-checked）——并发且缓存过期时避免多线程重复查库
_CACHE_LOCK = threading.Lock()


def invalidate_cache() -> None:
    """热更新：清除 DB 定义缓存，下次识别立即加载最新训练结果。"""
    _cache["ts"] = 0.0
    _cache["defs"] = None


def _load_db_defs() -> Dict[str, Dict[str, List[str]]]:
    """从 business_rules(SUB-INTENT-DEFS).defs 读取（TTL 缓存 + 热更新）。

    返回 DB 中的完整 defs 字典（可能为空）；DB 不可用/未配置返回 {}。
    """
    now = time.time()
    if _cache["defs"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["defs"]
    with _CACHE_LOCK:
        # double-checked：锁内再判一次（其他线程可能已刷新缓存）
        if _cache["defs"] is not None and time.time() - _cache["ts"] < _CACHE_TTL:
            return _cache["defs"]
        defs: Dict[str, Dict[str, List[str]]] = {}
        try:
            from prog.runtime.param_loader import get_param
            raw = get_param("SUB-INTENT-DEFS", "defs", None)
            if isinstance(raw, dict):
                for agent_type, sub_map in raw.items():
                    if not isinstance(sub_map, dict):
                        continue
                    defs[agent_type] = {
                        name: (list(kws) if isinstance(kws, list) else [])
                        for name, kws in sub_map.items()
                    }
        except Exception:
            defs = {}
        _cache["defs"] = defs
        _cache["ts"] = time.time()
    return defs


def get_sub_intent_defs(agent_type: str) -> Dict[str, List[str]]:
    """获取指定 Agent 的子意图定义（DB 覆盖同名子意图 + 新增子意图，缺省用内置）。

    参数：
        agent_type: Agent 类型（sales/warehouse/production/technical/
                    knowledge/qc/hr/finance）

    返回：
        dict: {子意图名: [关键词, ...]}，定义顺序即匹配优先级
    """
    db_defs = _load_db_defs()
    merged: Dict[str, List[str]] = {}
    for name, kws in (DEFAULT_SUB_INTENT_DEFS.get(agent_type) or {}).items():
        merged[name] = list(kws)
    # DB 覆盖/新增
    for name, kws in (db_defs.get(agent_type) or {}).items():
        merged[name] = list(kws)
    return merged


def get_sub_intent_keywords(agent_type: str, sub_intent: str) -> List[str]:
    """获取指定 Agent 某子意图的关键词列表（DB 覆盖，缺省用内置）。

    供含复合条件/交错顺序的 Agent（sales/qc/finance/knowledge）逐子意图
    取关键词，保持原 if 链顺序不变（DB 定义无法表达的复合条件可原位夹在中间）。
    """
    return get_sub_intent_defs(agent_type).get(sub_intent, [])


def recognize_sub_intent(agent_type: str, user_input: str) -> str:
    """按定义顺序识别子意图（首个命中生效）。

    参数：
        agent_type: Agent 类型
        user_input: 用户输入文本

    返回：
        str: 子意图标签；无命中返回空串（由 Agent 侧复合条件/默认值兜底）
    """
    if not user_input:
        return ""
    for sub_intent, keywords in get_sub_intent_defs(agent_type).items():
        if any(k in user_input for k in keywords):
            return sub_intent
    return ""


# ============================================================
# DEBUG 自检（发行版自动跳过）
# ============================================================
def _self_test():
    """DEBUG模式自检：验证子意图识别基座正确。"""
    from prog.runtime.debug import hello_world
    assert recognize_sub_intent("sales", "帮我下一笔订单") == "create_order"
    assert recognize_sub_intent("sales", "改一下价格") == "modify_price"
    assert recognize_sub_intent("warehouse", "查一下库存") == "inventory_query"
    assert recognize_sub_intent("production", "设备报警了") == "fault_warning"
    assert recognize_sub_intent("technical", "查看BOM结构") == "bom_management"
    assert recognize_sub_intent("qc", "做一个8D报告") == "8d_report"
    assert recognize_sub_intent("hr", "这个月的考勤") == "attendance"
    assert recognize_sub_intent("finance", "应收账款还有多少") == "receivable"
    # 复合条件不命中（由 Agent 兜底），引擎返回空
    assert recognize_sub_intent("sales", "查一下A-202的订单") == ""
    assert recognize_sub_intent("qc", "不合格品怎么处置") == ""
    hello_world(__name__, "子意图引擎定义完整")


from prog.runtime.debug import DEBUG
if DEBUG:
    _self_test()
