"""
槽位引擎（可训练槽位定义）
==========================
文件用途：
    统一管理意图槽位的"定义 + 提取 + 必填判定 + 引导语"，替代各 Agent 内硬编码的
    _extract_slots / _check_slots_complete / 引导语拼接。

设计说明：
    1. 槽位定义（提取正则 / value_type / 必填意图 / 引导语）存 business_rules 表
       （rule_id='SLOT-DEFS'，config_json 存完整定义），符合"业务规则参数必须存
       business_rules 表而非硬编码"的硬约束（migrations/007）。
    2. 内置 DEFAULT_SLOT_DEFS 作为降级兜底（DB 不可用 / 表为空时等价于原硬编码行为）；
       DB 配置按槽位 key 覆盖内置定义，新增 key 直接加入——训练修改槽位无需改代码。
    3. 读取优先级：DB(SLOT-DEFS) 覆盖 -> 内置默认（与 drawing_rule.get_required_fields
       的 get_param 模式一致，无额外缓存，直接查询开销可忽略）。
    4. 训练变更走 L2 审批链：submit_slot_def_change() 写 workflow_configs 审批记录
       （workflow_type='slot_defs_change'），apply_slot_def_change() 审批通过后
       UPDATE business_rules.config_json 生效（含 is_trained 标记）。
    5. 框架内嵌模块：原 agent-runtime-os 独立副本已取消，仅保留本仓库副本。

对应技术规格：
    - §A.8 意图识别治理策略（训练自动优化 -> 审批后生效）
    - §2.6 规则引擎（parameter 层参数均存 business_rules 表，可训练修改）

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 统一管理意图槽位"定义 + 提取 + 必填判定 + 引导语"：槽位定义（提取正则/value_type/必填意图/引导语）存 business_rules 表（rule_id='SLOT-DEFS'），DB 覆盖 + 内置 DEFAULT_SLOT_DEFS 兜底，替代各 Agent 硬编码 _extract_slots/_check_slots_complete（来源：SPEC 框架演进记录 v1.6.15/v1.6.18 / 业务规格书 v6.43/v6.46 / 模块拆分方案 契约3）
        - 值转换与过滤可训练：regexes/strip_patterns/value_map/unit_scale+scale_threshold/exclude_regexes（主谓宾语性过滤，防"已确认"状态定语被当客户名）（来源：业务规格书 v6.46 C3 / v6.57 / SPEC 框架演进记录 v1.6.18）
        - 文件类槽位支持：value_type="file"（attachment/doc_template），merge_uploaded_files 将随消息上传的文件合并入 attachment 槽位（来源：业务规格书 v6.44 / SPEC 框架演进记录 v1.6.16）
        - 槽位定义训练走 L2 审批链：submit_slot_def_change 写 workflow_configs 审批记录（workflow_type='slot_defs_change'），apply_slot_def_change 审批通过后 UPDATE 生效（来源：业务规格书 v6.43 / SPEC 框架演进记录 v1.6.15/v1.6.17）
    对外接口（方法/API）：
        - extract_slots(text, db=None) -> dict：从文本提取槽位（来源：模块拆分方案 契约3）
        - get_slot_defs(db=None, use_cache=True) -> dict：读取槽位定义（DB 覆盖 + 内置兜底，TTL 缓存）（来源：模块拆分方案 契约3）
        - get_required_slots(intent, db=None) -> list：必填槽位判定（required_rules 可训练，含 or 关系）（来源：业务规格书 v6.43）
        - get_prompt_hints(missing, db=None) -> dict / check_slots_complete(slots, intent, db=None)：缺槽引导语与必填完整性校验（来源：业务规格书 v6.43）
        - merge_uploaded_files(slots, attachments)：实际上传文件合并入 attachment 槽位（无文件名不产生空值避免必填误判）（来源：业务规格书 v6.44 / SPEC 框架演进记录 v1.6.16）
        - submit_slot_def_change(proposed, current, ...)：提交槽位定义变更到 L2 审批链（来源：业务规格书 v6.43）
        - apply_slot_def_change(new_defs, db=None)：审批通过后写入 business_rules.config_json 生效（含 is_trained 标记）（来源：业务规格书 v6.43 / SPEC 框架演进记录 v1.6.15）
        - invalidate_cache()：清缓存热更新（来源：模块 docstring / SPEC 框架演进记录 v1.6.15）
        - DEFAULT_SLOT_DEFS：内置默认槽位定义（DB 不可用/表为空时降级兜底，等价于原硬编码行为）（来源：模块 docstring）
    错误处理要求：
        - DB 不可用或 SLOT-DEFS 表为空：降级内置 DEFAULT_SLOT_DEFS，等价于原硬编码行为（来源：模块 docstring）
        - 训练变更未审批：提交走 L2 审批链（submit_slot_def_change），apply_slot_def_change 审批通过后才生效（来源：业务规格书 v6.43）
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from prog.runtime.approval_chain import get_approval_chain
# --------------------------------------------------------
# 内置默认槽位定义（DB 不可用时降级兜底；与历史硬编码等价）
# --------------------------------------------------------
# 每个槽位：regex(提取正则, group1 为值) / value_type / prompt_hint(引导语)
DEFAULT_SLOT_DEFS: Dict[str, Dict[str, Any]] = {
    # ---- 单据ID（先提长ID，避免与产品型号冲突）----
    "order_id": {
        # v6.46：合并生产 Agent 的"订单号:XXX"显式格式回退
        "regexes": [
            r"(SO\d{6,})",
            r"订单号\s*[：:]*\s*(SO\d{6,}|[A-Za-z]{0,4}\d{4,})",
        ],
        "value_type": "upper",
        "prompt_hint": "请提供订单号（如SO20260801001）。",
    },
    "work_order_id": {
        "regex": r"(WO\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供工单号（如WO20260801001）。",
    },
    "po_id": {
        "regex": r"(PO\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供采购单号（如PO20260801001）。",
    },
    # ---- 产品与数量 ----
    "product_code": {
        # 负向断言：字母前不能紧跟字母（排除 SO/WO/PO 中的 O），后不能紧跟数字
        "regex": r"(?<![A-Za-z])([A-Z])\s*-?\s*(\d{3})(?!\d)",
        "value_type": "upper_code",
        "prompt_hint": "请提供产品型号（如A-202、B-305）。",
    },
    "quantity": {
        "regex": r"(\d+)\s*(套|个|件|台|批|箱|只|pcs|公斤|kg)",
        "value_type": "int",
        "prompt_hint": "请提供订购数量（如100套）。",
    },
    "unit": {
        "regex": r"\d+\s*(套|个|件|台|批|箱|只|pcs|公斤|kg)",
        "value_type": "str",
        "prompt_hint": "",
    },
    # ---- 财务 ----
    "price": {
        "regex": r"(?:单价|价格|单价为|价格是)\s*(\d+(?:\.\d+)?)\s*元?",
        "value_type": "float", "prompt_hint": "",
    },
    # ---- 客户与供应商 ----
    "customer_name": {
        # v6.46：多语境回退链（原分散在各 Agent 的硬编码模式统一为可训练数据）：
        #   显式客户前缀 -> X的订单 -> 查询语境 -> 补充语境(客户是/为/叫) ->
        #   动作语境(华信电子下单) -> 为/给/帮引导语境 -> 财务动词语境(登记X收款)
        "regexes": [
            r"(?:客户是|客户为|客户叫)\s*([\u4e00-\u9fa5]{2,8})",
            r"(?:客户名[：:]\s*|客户\s{0,1})([\u4e00-\u9fa5]{2,4})",
            r"(?:查一下|查询|查看|看看|查查)\s*([\u4e00-\u9fa5]{2,8})\s*的订单",
            r"([\u4e00-\u9fa5]{2,4})的订单",
            r"(?!(?:我|你|他|她|它|我们|你们|帮|给|为|帮忙|客户|公司|忙))([\u4e00-\u9fa5]{2,8})\s*(?:追加|加单|要货|采购|订购|订货|需要|想要|下单)",
            r"(?:为|给|帮)\s*(?!我|你|他|她|它|我们|你们|客户|公司|帮忙|忙)([\u4e00-\u9fa5]{2,8}?)(?=\s*(?:生成|起草|拟|签|下单|追加|加单|要货|采购|订购|订货|做|办理|创建|开立|的)|$)",
            r"(?:登记|查询|查一下|查|录入|确认|帮|给|为)\s*([\u4e00-\u9fa5]{2,8}?)(?:的|客户|一笔|收款|付款|信用)",
        ],
        "value_type": "str", "prompt_hint": "请提供客户名（如锐科）。",
        # v6.67：主谓宾/词性过滤——"已确认的订单"中"已确认"是状态定语而非客户名，
        # "查一下我/我的订单"中前缀为查询动作/代词而非实体。命中任一正则即丢弃该次提取。
        "exclude_regexes": [
            # v6.80：补充分析类动词——"给出质量判断/改善建议/分析记录"等不得被当客户名
            r"(已确认|已审批|已通过|待审批|审批中|进行中|已取消|已入库|已出库|草稿|已提交|已发货|待处理|已完成|取消|撤回|驳回|判断|建议|分析|评估|复盘|汇总|综合|改善)",
            r"^(查一下|查询|查看|看看|查查|帮|给|为|我|你|他|她|它|我们|你们|他们|一下|这个|那个|所有|全部|各个)",
        ],
    },
    "supplier": {
        "regex": r"供应商[是为：:]*([\u4e00-\u9fa5A-Za-z0-9]{2,20})",
        "value_type": "str", "prompt_hint": "请提供供应商名称。",
    },
    # ---- 财务特有：应付/付款语境供应商名（finance_agent supplier_name）----
    "supplier_name": {
        "regex": r"(?:供应商|供货商)\s*([\u4e00-\u9fa5]{2,8}?)(?:的|应付|付款|发票)",
        "value_type": "str", "prompt_hint": "",
    },
    # ---- 时间 ----
    "date": {
        "regex": r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}日)",
        "value_type": "str", "prompt_hint": "",
    },
    "date_range": {
        "regex": r"((?:\d{4}[-/年])?\d{1,2}[-/月](?:\d{1,2}日?)?\s*(?:到|至|~|—|－)\s*(?:\d{4}[-/年])?\d{1,2}[-/月](?:\d{1,2}日?)?)",
        "value_type": "date_range", "prompt_hint": "请提供要查询的时间段（如3月到5月、2026-03-01至2026-05-31）。",
    },
    "period": {
        "regex": r"(?:(\d{4})年)?(\d{1,2})月",
        "value_type": "period", "prompt_hint": "",
    },
    "days": {
        "regex": r"(?:近|最近|过去)?\s*(\d+)\s*天", "value_type": "int",
        "prompt_hint": "",
    },
    # ---- 库存维度 ----
    "stage": {
        # v6.46：统一三套 stage 映射（intent_recognition 3态 / slot_engine 3态 /
        # warehouse 5态 INV-STAGE: raw/wip_cnc/wip_anode/wip_qc/finished）
        # value_map 可训练（DB 修改即时生效）
        "regex": r"(原材料|原料仓|原料|raw|在制品|在制加工|在制|半成品|机加工|cnc|阳极|氧化|anode|质检|品检|qc|成品|完成品|finished)",
        "value_type": "stage",
        "value_map": {
            "原材料": "raw", "原料": "raw", "原料仓": "raw", "raw": "raw",
            "在制品": "wip", "在制": "wip", "半成品": "wip",
            "在制加工": "wip_cnc", "机加工": "wip_cnc", "cnc": "wip_cnc",
            "阳极": "wip_anode", "氧化": "wip_anode", "anode": "wip_anode",
            "质检": "wip_qc", "品检": "wip_qc", "qc": "wip_qc",
            "成品": "finished", "完成品": "finished", "finished": "finished",
        },
        "prompt_hint": "",
    },
    "warehouse": {
        # v6.46：补齐仓储 Agent 特有库名（一号仓/原料仓等）
        "regex": r"(原料仓|成品仓|半成品仓|一号仓|二号仓|1号仓|2号仓|[\u4e00-\u9fa5A-Za-z]{1,6}(?:仓库|库房))",
        "value_type": "str", "prompt_hint": "",
    },
    # ---- 质量维度（v6.43 新增，支撑"查 A-202 某时间段内某种质量问题"）----
    "defect_type": {
        "regex": r"(划痕|划伤|毛刺|裂纹|裂缝|变形|色差|气泡|缩水|翘曲|砂眼|夹渣|硬度不足|尺寸超差|超差|外观不良|不良|缺陷)",
        "value_type": "str", "prompt_hint": "请提供要查询的质量问题类型（如划痕、色差、硬度不足）。",
    },
    "param_name": {
        "regex": r"(尺寸|硬度|公差|粗糙度|重量|密度|厚度|长度|宽度|圆度|平面度|垂直度|同心度|拉力|抗拉强度|屈服强度|延伸率)",
        "value_type": "str", "prompt_hint": "请提供要查询的参数名（如硬度、尺寸公差）。",
    },
    # ---- 人力维度 ----
    "employee_name": {
        "regex": r"(?:员工|查一下|查|帮|给|为)\s*([\u4e00-\u9fa5]{2,4}?)(?:的|员工|入职|离职|报工|考勤|工资|信息)",
        "value_type": "str", "prompt_hint": "请提供员工姓名。",
        # v6.67：同 customer_name——状态定语/查询前缀非实体，命中即丢弃
        "exclude_regexes": [
            r"(已确认|已审批|已通过|待审批|审批中|进行中|已取消|已入库|已出库|草稿|已提交|已发货|待处理|已完成|取消|撤回|驳回)",
            r"^(查一下|查询|查看|看看|查查|帮|给|为|我|你|他|她|它|我们|你们|他们|一下|这个|那个|所有|全部|各个)",
        ],
    },
    "employee_id": {
        # v6.46：整号提取（含 U/E 前缀），对齐 HR Agent 行为
        "regex": r"([UEe]\d{3,6})", "value_type": "upper",
        "prompt_hint": "请提供员工工号（如U001）。",
    },
    "department": {
        "regex": r"([\u4e00-\u9fa5]{2,6}(?:部|处|科|组|中心))",
        "value_type": "str", "prompt_hint": "",
    },
    # ---- 生产/设备维度 ----
    "line_name": {
        "regex": r"([一二三四五六七八九十\d]{1,2}\s*号?\s*线|产线\s*[A-Z\d]+\s*线?)",
        "value_type": "str", "prompt_hint": "请提供产线名称（如3号线）。",
    },
    "machine_id": {
        "regex": r"(CNC-[A-Z\d]+|设备\s*[A-Z\d-]{1,8}|机床\s*[A-Z\d-]{1,8})",
        "value_type": "str", "prompt_hint": "请提供设备编号（如CNC-A）。",
    },
    "due_date": {
        "regex": r"(?:交期|交期是|交付日期|预计交付)\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}日)",
        "value_type": "str", "prompt_hint": "请提供交期（如2026-09-30）。",
    },
    # ---- 销售/财务特有（v6.43 补齐，Agent 特有槽位纳入可训练）----
    "discount": {
        "regex": r"(\d+(?:\.\d+)?)\s*%|折扣\s*(\d+(?:\.\d+)?)",
        "value_type": "discount", "prompt_hint": "请提供折扣（如5%或0.05）。",
    },
    "unit_price": {
        # v6.46：合并财务 Agent 单价前缀（售价/价格/单价/给到/降到）
        "regexes": [
            r"(?:售价|价格|单价|给到|降到)\s*(\d+)",
        ],
        "value_type": "int", "prompt_hint": "请提供单价（如120）。",
    },
    "amount": {
        # v6.46：合并财务 Agent 金额表达（X万 / 金额:XXX / 收款XXX / 付款XXX）
        # v6.46.1：补充裸数字金额（500元/500块/500元整），供流程字段收集；
        # explicit_unit_regex：显式单位（元/块/圆）直接返回原值，不做万换算
        "regexes": [
            r"(\d+(?:\.\d+)?)\s*万",
            r"(?:金额|收款|付款)\s*[:：]?\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:元|块|圆)(?:整|钱)?",
        ],
        "value_type": "amount_wan", "prompt_hint": "请提供金额（如5万或50000）。",
        "explicit_unit_regex": r"(?:元|块|圆)(?:整|钱)?",
    },
    # v6.46：流程/报销字段收集（SLOT-DEFS 可训练，费用报销流程必填字段槽位）
    "expense_type": {
        "regex": r"(差旅|交通|住宿|餐饮|办公|采购|招待|会议|培训|交通费|餐费|住宿费)",
        "value_type": "str",
        "prompt_hint": "请提供费用类型（如差旅、餐饮、办公）。",
    },
    "reason": {
        "regexes": [
            r"(?:事由|原因|用途|报销事由|申请原因)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z0-9，,。.\s]{2,50})",
        ],
        "value_type": "str",
        # v6.46.1：自由文本字段标记——整句兜底优先赋给 free_text 字段
        # （reason/事由），避免被枚举类字段（expense_type 等）优先截取
        "free_text": True,
        "prompt_hint": "请提供报销事由（如出差参加客户现场验收）。",
    },
    "invoice_id": {
        "regex": r"(INV\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供发票号（如INV202608001）。",
    },
    "receipt_id": {
        "regex": r"(RC\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供入库单号（如RC202608001）。",
    },
    "contract_id": {
        "regex": r"(CT\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供合同号（如CT202608001）。",
    },
    # ---- 技术特有 ----
    "drawing_no": {
        # v6.46：兼容技术 Agent 分隔符表达（DRW-A202_001）
        "regex": r"(DRW[-_]?\w+[-_]?\d+)", "value_type": "str",
        "prompt_hint": "请提供图号（如DRW-A202-001）。",
    },
    "version": {
        "regex": r"版本\s*[:：]?\s*(V\d+(?:\.\d+)?)|(V\d+(?:\.\d+)?)",
        "value_type": "upper", "prompt_hint": "请提供版本号（如V1.0）。",
    },
    # ---- 质检特有 ----
    "batch_no": {
        # v6.46：统一为裸批次号（QC Agent 行为），支持"批次 B001"与裸 B 码
        "regex": r"(?:批次|batch)[:\s]*(B\d{6,})|(B\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供检验批次号（如批次B001）。",
    },
    "qc_id": {
        "regex": r"(QC\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供质检记录编号（如QC202608001）。",
    },
    "complaint_id": {
        "regex": r"(投诉编号\s*[:：]?\s*[A-Z0-9]{6,}|CP\d{6,})", "value_type": "upper",
        "prompt_hint": "请提供客诉编号。",
    },
    "inspection_type": {
        "regex": r"(首件检验|巡检|来料检验|成品检验|出货检验|型式检验)",
        "value_type": "str", "prompt_hint": "请提供检验类型（如来料检验、巡检）。",
    },
    "disposal_action": {
        "regex": r"(返工|返修|报废|让步接收|降级使用|退货给供应商)",
        "value_type": "str", "prompt_hint": "请提供处置方式（如返工、报废）。",
    },
    # ---- 生产特有 ----
    "line_id": {
        # v6.46：合并生产 Agent 产线编号表达（CNC1 / LINE-01 / 产线-1 / 线 1）
        "regex": r"(CNC[-]?\d+|LINE[-]?\d+|产线[-]?\d+|线\s*\d+)",
        "value_type": "upper", "prompt_hint": "请提供产线编号（如LINE-01）。",
    },
    # ---- 完成量（HR报工等）----
    "completed_qty": {
        "regex": r"(?:完成|完工|报工)\s*(\d+)\s*[件个套台只PCSpcs]",
        "value_type": "int", "prompt_hint": "请提供完成数量。",
    },
    # ---- 文件类槽位（v6.44：训练时某些槽位可能是文件，如 Word/PDF 模板、报销单）----
    # value_type="file"：值为 {"file_name","ext","type"}；仅提取带文件名的表达，
    # 无文件名时返回 None（引导语提示上传），实际上传文件由 merge_uploaded_files 合并
    "attachment": {
        "regex": r"(?:附件|我上传|上传了|有附件|提交|上传)\s*[:：]?\s*(?:了)?\s*([\u4e00-\u9fa5A-Za-z0-9._-]{1,60}?\.(?:pdf|docx?|xlsx?|pptx?|png|jpe?g|bmp|tiff|txt|md|csv))",
        "value_type": "file",
        "prompt_hint": "请上传相关文件（支持Word/PDF/Excel/图片，如报销单、图纸）。",
    },
    "doc_template": {
        "regex": r"(?:请用|使用|用|按|按照|以|根据|提供|上传|给)?\s*([\u4e00-\u9fa5A-Za-z0-9._-]{1,8}?(?:模板|样板|表单|表格|格式)(?:文件|文档)?(?:\.(?:pdf|docx?|xlsx?|pptx?|png|jpe?g))?)",
        "value_type": "file",
        "prompt_hint": "请提供模板文件（如报销单模板、合同模板）。",
    },
}

# --------------------------------------------------------
# 内置默认必填规则（意图 -> 必填槽位列表；元素含 '|' 表示 or 关系）
# --------------------------------------------------------
# 与 drawing_rule.get_required_fields 同源模式：DB(SLOT-DEFS).required_rules
# 覆盖此默认值后即时生效（无需改代码）。
DEFAULT_REQUIRED_RULES: Dict[str, List[str]] = {
    # ---- 销售事件 ----
    "create_order": ["product_code", "quantity"],
    "modify_order": ["order_id|product_code"],
    "order_cancel": ["order_id"],
    "query_order": ["order_id|product_code|customer_name"],
    "contract": ["customer_name", "quantity"],
    # ---- 仓储事件 ----
    "query_inventory": ["product_code"],
    "stock_in": ["product_code", "quantity"],
    "stock_out": ["product_code", "quantity"],
    # ---- 生产事件 ----
    "schedule_production": ["order_id|product_code"],
    "work_order_query": ["work_order_id|product_code"],
    # ---- 质量事件 ----
    "quality_action": ["defect_type"],
    # ---- 采购事件 ----
    "purchase": ["supplier", "product_code"],
    # ---- 人力事件 ----
    "work_report": ["employee_name|work_order_id"],
    # ---- 财务事件 ----
    "financial_operation": ["amount|order_id"],
    # ---- 技术事件 ----
    "drawing_management": ["product_code|drawing_no"],
    "bom_management": ["product_code"],
    # ---- 质检事件（扩展）----
    "query_qc": ["product_code|order_id|work_order_id|batch_no"],
    # ---- 生产事件（扩展）----
    "equipment_query": ["machine_id|line_id"],
    "report_issue": ["machine_id|line_id|work_order_id"],
}

# 规则配置ID（business_rules 表）
SLOT_DEFS_RULE_ID = "SLOT-DEFS"
# 审批链类型（workflow_configs）
SLOT_DEFS_CHANGE_WF = "slot_defs_change"

_DB_DEFS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_CACHE_TS = 0.0
_CACHE_TTL = 5.0
# W8：必填规则 TTL 缓存（与 get_slot_defs 共享 _CACHE_TTL 生命周期，
# 避免每次调用重复查库；训练生效后 invalidate_cache 一并清除）
_REQUIRED_RULES_CACHE: Optional[Dict[str, List[str]]] = None
_REQUIRED_RULES_TS = 0.0


def _db_config(db: Any = None) -> Dict[str, Any]:
    """读取 DB(SLOT-DEFS) 整条配置（失败/无 DB 返回空字典，由内置兜底）。

    直接使用 prog.runtime.database 鸭子接口（query_one），不依赖业务侧 param_loader。
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return {}
    try:
        row = db.query_one("business_rules", {"rule_id": SLOT_DEFS_RULE_ID},
                           ["config_json"])
        cfg = (row or {}).get("config_json")
        if isinstance(cfg, str):
            import json
            cfg = json.loads(cfg)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def get_slot_defs(db: Any = None, use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    """合并后的槽位定义：DB(SLOT-DEFS) 按 key 覆盖内置默认，新增 key 直接加入。

    Args:
        db: 可选数据库（默认 get_database()）
        use_cache: 是否使用 TTL 缓存（默认 5 秒；训练生效后可调用 invalidate_cache）

    Returns:
        dict: 槽位名 -> 槽位定义
    """
    global _DB_DEFS_CACHE, _CACHE_TS
    now = datetime.now().timestamp()
    if (use_cache and _DB_DEFS_CACHE is not None
            and now - _CACHE_TS < _CACHE_TTL):
        return _DB_DEFS_CACHE

    merged: Dict[str, Dict[str, Any]] = {}
    # 内置默认 -> DB 覆盖（同 key 整体替换，允许 DB 删除槽位=置空跳过）
    merged.update(_deep_copy(DEFAULT_SLOT_DEFS))
    for key, defn in _db_config(db).get("slots", {}).items():
        if defn is None:
            merged.pop(key, None)
            continue
        merged[key] = dict(defn)

    if use_cache:
        _DB_DEFS_CACHE = merged
        _CACHE_TS = now
    return merged


def invalidate_cache() -> None:
    """清空槽位定义与必填规则缓存（训练生效后调用，实现热更新）。"""
    global _DB_DEFS_CACHE, _CACHE_TS, _REQUIRED_RULES_CACHE, _REQUIRED_RULES_TS
    _DB_DEFS_CACHE = None
    _CACHE_TS = 0.0
    _REQUIRED_RULES_CACHE = None
    _REQUIRED_RULES_TS = 0.0


def _deep_copy(src: Dict) -> Dict:
    """浅层深拷贝：仅复制一层 dict 值（嵌套 dict 另复制），保持槽位上下文独立。

    参数：
        src: 源字典（槽位定义 dict）
    返回：
        Dict: 副本——顶层值与嵌套 dict 均为新对象，其余类型引用原值
    """
    out = {}
    for k, v in src.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    return out


# --------------------------------------------------------
# 值后处理
# --------------------------------------------------------
def _excluded(value: Any, exclude_regexes: List[str]) -> bool:
    """v6.67：提取值是否命中排除正则（主谓宾/词性过滤）。

    用于区分"确认"等词的词性用法：如"已确认的订单"中"已确认"是状态定语
    （非客户名），"查一下我"中前缀是查询动作/代词（非实体）。任一正则
    命中即视为排除。
    """
    if not exclude_regexes or value is None:
        return False
    sval = str(value)
    for pat in exclude_regexes:
        try:
            if re.search(pat, sval):
                return True
        except re.error:
            continue
    return False


def _apply_value(match: "re.Match", value_type: str, defn: Dict[str, Any]) -> Any:
    """按 value_type 对正则捕获结果做规范化。"""
    groups = match.groups()
    vtype = value_type or "str"
    # 取第一个非空捕获组（支持多分支正则如 amount 的两组）
    first_val = next((g for g in groups if g is not None), None)
    if vtype == "upper":
        return first_val.upper() if first_val else None
    if vtype == "upper_code":
        # W7：捕获组数量防御——可训练正则捕获组可能不足（<2），越界返回 None
        if len(groups) < 2 or not groups[0] or not groups[1]:
            return None
        return f"{groups[0].upper()}-{groups[1]}"
    if vtype == "int":
        try:
            return int(first_val)
        except (ValueError, TypeError):
            return None
    if vtype == "float":
        try:
            return float(first_val)
        except (ValueError, TypeError):
            return None
    if vtype == "stage":
        # v6.46：stage 映射可训练——槽位定义 value_map 优先（统一三套 stage 映射：
        # intent_recognition 3态 / slot_engine 3态 / warehouse 5态），DB 不可用时兜底内置
        stage_map = defn.get("value_map")
        if not isinstance(stage_map, dict) or not stage_map:
            stage_map = {
                "原材料": "raw", "在制品": "wip", "在制": "wip",
                "半成品": "wip", "成品": "finished",
            }
        return stage_map.get(first_val, first_val)
    if vtype == "period":
        # W7：捕获组数量防御——可训练正则可能缺少年份/月份捕获组，不足时返回 None
        if len(groups) < 2:
            return None
        year = groups[0] or str(datetime.now().year)
        try:
            return f"{year}-{int(groups[1]):02d}"
        except (ValueError, TypeError, IndexError):
            return None
    if vtype == "date_range":
        return _parse_date_range(first_val)
    if vtype == "discount":
        val = first_val
        if val is None:
            return None
        fval = float(val)
        # v6.46：百分比/小数转换可训练（unit_scale 除法 + scale_threshold 阈值）
        unit_scale = float(defn.get("unit_scale", 100))
        scale_threshold = float(defn.get("scale_threshold", 1))
        return fval / unit_scale if fval > scale_threshold else fval
    if vtype == "amount_wan":
        val = first_val
        if val is None:
            return None
        fval = float(val)
        # v6.46.1：显式单位（元/块/圆）直接返回原值，不做万换算——
        # "500元" 应为 500 而非 500*10000；"X万"（无显式单位后缀）才乘 10000
        explicit = defn.get("explicit_unit_regex")
        if explicit:
            try:
                if re.search(explicit, match.string):
                    return int(fval)
            except re.error:
                pass
        # v6.46：万/元换算可训练（unit_scale 乘法 + scale_threshold 阈值）
        unit_scale = float(defn.get("unit_scale", 10000))
        scale_threshold = float(defn.get("scale_threshold", 1000))
        return int(fval * unit_scale) if fval < scale_threshold else int(fval)
    if vtype == "file":
        # 文件类槽位：值为 {"file_name","ext","type"}；无具体文件名（仅"上传附件"）
        # 时返回 None，由 merge_uploaded_files 用实际上传文件补齐，避免空占位
        # 被必填判定误判为"已满足"
        if first_val is None:
            return None
        val = str(first_val).strip()
        ext = ""
        dot = val.rfind(".")
        if dot > 0:
            ext = val[dot + 1:].lower()
        return {"file_name": val, "ext": ext, "type": "file"}
    return first_val


def _parse_date_range(text: str) -> Dict[str, str]:
    """解析时间段字符串（"3月到5月" / "2026-03-01至2026-05-31"）为 {start,end}。"""
    parts = re.split(r"\s*(?:到|至|~|—|－)\s*", text)
    if len(parts) < 2:
        norm = _norm_date(parts[0].strip())
        return {"start": norm, "end": norm}
    start, end = parts[0].strip(), parts[1].strip()
    # 起点缺日 -> 月初；终点缺日 -> 月末
    return {"start": _norm_date(start), "end": _norm_date(end, month_end=True)}


def _norm_date(text: str, month_end: bool = False) -> str:
    """将日期文本规范为 YYYY-MM-DD（缺年份用当前年；缺日按月初/月末补齐）。"""
    m = re.match(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](?:(\d{1,2})日?)?", text)
    if not m:
        return text
    year = m.group(1) or str(datetime.now().year)
    month = int(m.group(2))
    if m.group(3):
        day = int(m.group(3))
    elif month_end:
        import calendar
        day = calendar.monthrange(int(year), month)[1]
    else:
        day = 1
    return f"{year}-{int(month):02d}-{day:02d}"


# --------------------------------------------------------
# 对外接口
# --------------------------------------------------------
def extract_slots(text: str, db: Any = None) -> Dict[str, Any]:
    """表驱动槽位提取：遍历槽位定义正则，返回全部匹配槽位。

    保持与历史 _extract_params 兼容：长ID优先（正则负向断言已避免
    SO/WO/PO 内的编号被误判为产品型号）。

    v6.46 槽位定义增强（均可训练）：
        - regex    : 单正则（兼容历史）
        - regexes  : 多正则列表，按序尝试，首个命中生效（支持客户名多语境等
                     回退链）；元素含捕获组时取第一个非空组为值
        - strip_patterns : 正则列表，匹配前先剔除（如产品码 A-202 避免被
                     裸数字数量正则误匹配）
        - value_map / unit_scale / scale_threshold : 值转换可训练参数
        - exclude_regexes : 正则列表（v6.67），提取值命中任一正则即丢弃该次
                     提取（regexes 链继续尝试下一正则）——用于主谓宾/词性
                     过滤：状态定语（"已确认的订单"）、查询前缀/代词
                     （"查一下我"）等非实体，避免被误当客户名/员工名

    Args:
        text: 用户输入
        db: 可选数据库

    Returns:
        dict: 槽位名 -> 值
    """
    slots: Dict[str, Any] = {}
    for key, defn in get_slot_defs(db=db).items():
        excludes = defn.get("exclude_regexes") or []
        patterns = defn.get("regexes")
        if isinstance(patterns, list) and patterns:
            match_text = text
            strips = defn.get("strip_patterns")
            if isinstance(strips, list):
                for _sp in strips:
                    try:
                        match_text = re.sub(_sp, "", match_text)
                    except re.error:
                        continue
            for _pat in patterns:
                try:
                    m = re.search(_pat, match_text, re.IGNORECASE)
                except re.error:
                    continue
                if m:
                    val = _apply_value(m, defn.get("value_type"), defn)
                    if val is None:
                        break  # 值转换失败：无有效值，终止本槽位
                    if _excluded(val, excludes):
                        # W10：exclude 命中不 break——regexes 链继续尝试下一正则
                        # （docstring 契约：丢弃该次提取、链继续，避免槽位丢失）
                        continue
                    slots[key] = val
                    break
            continue
        pattern = defn.get("regex")
        if not pattern:
            continue
        try:
            m = re.search(pattern, text, re.IGNORECASE)
        except re.error:
            continue
        if m:
            val = _apply_value(m, defn.get("value_type"), defn)
            if val is not None and not _excluded(val, excludes):
                slots[key] = val
    return slots


def get_required_slots(intent: str, db: Any = None) -> List[str]:
    """返回指定意图的必填槽位 key 列表（可训练，来自 DB(SLOT-DEFS).required_rules）。

    元素含 '|' 表示 or 关系（如 "order_id|product_code"：两者任一满足即视为已提供）。

    W8：必填规则复用 TTL 缓存（与 get_slot_defs 同 _CACHE_TTL），
    避免每次调用重复查库；训练生效后 invalidate_cache 清除。

    Args:
        intent: 意图名
        db: 可选数据库

    Returns:
        list: 必填槽位 key（或 or 组合表达式）
    """
    global _REQUIRED_RULES_CACHE, _REQUIRED_RULES_TS
    now = datetime.now().timestamp()
    rules = None
    if (_REQUIRED_RULES_CACHE is not None
            and now - _REQUIRED_RULES_TS < _CACHE_TTL):
        rules = _REQUIRED_RULES_CACHE
    else:
        cfg = _db_config(db)
        rules = cfg.get("required_rules") if isinstance(cfg, dict) else None
        if not isinstance(rules, dict):
            rules = None
        _REQUIRED_RULES_CACHE = rules
        _REQUIRED_RULES_TS = now
    if isinstance(rules, dict) and isinstance(rules.get(intent), list):
        return list(rules[intent])
    return list(DEFAULT_REQUIRED_RULES.get(intent, []))


def get_prompt_hints(missing: List[str], db: Any = None) -> Dict[str, str]:
    """返回缺失槽位的引导语（可训练，来自槽位定义 prompt_hint）。"""
    hints: Dict[str, str] = {}
    defs = get_slot_defs(db=db)
    for key in missing:
        base = key.split("|")[0].strip()
        defn = defs.get(base, {})
        if defn.get("prompt_hint"):
            hints[key] = defn["prompt_hint"]
    return hints


def _slot_has_value(v: Any) -> bool:
    """槽位值有效判定（W4）：None 与空串视为缺失；0/0.0/False 等业务有效值视为已填
    （原 falsy 判定会把数值 0、0.0 误判为缺失，如 quantity=0 调整、discount=0 零折扣）。"""
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def check_slots_complete(slots: Dict[str, Any], intent: str,
                         db: Any = None) -> List[str]:
    """通用必填校验：按可训练 required_for 判定缺失槽位。

    Args:
        slots: 已提取槽位字典
        intent: 意图名
        db: 可选数据库

    Returns:
        list: 缺失槽位 key 列表
    """
    missing: List[str] = []
    for req in get_required_slots(intent, db=db):
        # or 关系：任一槽位有值即满足
        if "|" in req:
            if not any(_slot_has_value(slots.get(k))
                       for k in (k.strip() for k in req.split("|"))):
                missing.append(req)
        elif not _slot_has_value(slots.get(req)):
            missing.append(req)
    return missing


def merge_uploaded_files(slots: Dict[str, Any],
                         attachments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """将用户随消息上传的文件（attachments）合并入文件类槽位。

    场景：用户训练/发起流程时，文件类槽位（attachment 等）的取值来自实际上传
    的文件（前端 file_ids -> files_api 解析），而非文本正则。本函数把上传文件
    名列表写入 slots["attachment"]，供必填判定与 Agent 消费。

    Args:
        slots: 已提取槽位字典（可为 None）
        attachments: 随消息上传的文件信息列表 [{file_id,name,text}, ...]

    Returns:
        dict: 合并后的槽位字典（attachment 为文件名列表）
    """
    slots = dict(slots or {})
    names = [a.get("name", "") for a in (attachments or []) if a.get("name")]
    if not names:
        return slots
    existing = slots.get("attachment")
    if isinstance(existing, list):
        slots["attachment"] = existing + names
    elif existing:
        slots["attachment"] = [existing] + names
    else:
        slots["attachment"] = names
    return slots


# --------------------------------------------------------
# 训练变更（L2 审批链，与意图规则同一治理机制）
# --------------------------------------------------------
def submit_slot_def_change(proposed: Dict[str, Any], current: Dict[str, Any],
                           db: Any = None, changed_by: str = "L2") -> Optional[str]:
    """提交槽位定义变更：写 workflow_configs(slot_defs_change) 审批记录。

    训练产出（LLM 建议/训练样本分析）先落审批记录待审，审批通过后调用
    apply_slot_def_change() 生效——与 intent_rules 的 L2 待审批链路一致。

    Args:
        proposed: 提议的槽位定义（{"slots": {...}} 完整 config_json）
        current: 当前槽位定义（供审批对比）
        db: 可选数据库
        changed_by: 变更来源（L2 训练 / manual）

    Returns:
        config_id（写库成功）或 None（DB 不可用）
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return None
    try:
        return db.insert("workflow_configs", {
            "workflow_type": SLOT_DEFS_CHANGE_WF,
            "workflow_name": f"槽位定义变更审批-{datetime.now().strftime('%H%M%S')}",
            "owner_dept": "system",
            "trigger_rule": SLOT_DEFS_RULE_ID,
            # v6.45：审批链从 DB slot_defs_change 定义行读取（可训练），
            # 无定义/DB 不可用时由 get_approval_chain 兜底 manager 单级
            "approval_chain": __json(get_approval_chain(SLOT_DEFS_CHANGE_WF, db=db)),
            "thresholds": __json({
                "action": "update", "proposed": proposed, "current": current,
                "changed_by": changed_by,
            }),
            "is_active": True,
            "is_trained": False,
        })
    except Exception:
        return None


def apply_slot_def_change(new_defs: Dict[str, Any], db: Any = None,
                          modified_by: str = "L2") -> bool:
    """审批通过后应用槽位定义变更：UPDATE business_rules(SLOT-DEFS).config_json。

    写入后调用 invalidate_cache() 实现热更新（无需重启）。

    Args:
        new_defs: 新槽位定义（{"version": N, "slots": {...}}）
        db: 可选数据库
        modified_by: 修改人

    Returns:
        bool: 是否更新成功
    """
    if db is None:
        from prog.runtime.database import get_database
        db = get_database()
    if db is None:
        return False
    try:
        db.update("business_rules", {
            "config_json": __json(new_defs),
            "modified_by": modified_by,
            "modified_at": datetime.now().isoformat(),
        }, {"rule_id": SLOT_DEFS_RULE_ID})
        invalidate_cache()
        return True
    except Exception:
        return False


def __json(obj: Any) -> str:
    """对象序列化为 JSON 字符串（自检/调试输出辅助）。

    参数：
        obj: 任意可 JSON 序列化对象（槽位结果/定义 dict）
    返回：
        str: ensure_ascii=False 的 JSON 串（中文保留可读）
    """
    import json
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------
# DEBUG 自检
# --------------------------------------------------------
def _self_test():
    """验证槽位引擎核心功能（不依赖 DB）。"""
    # 1. 提取基础槽位
    slots = extract_slots("帮客户张三下一笔A-202的订单，100套")
    assert slots.get("product_code") == "A-202", f"product_code 提取失败: {slots}"
    assert slots.get("quantity") == 100, f"quantity 提取失败: {slots}"
    assert slots.get("unit") == "套", f"unit 提取失败: {slots}"

    # 2. 长ID不误判为产品型号
    slots2 = extract_slots("查一下订单SO20260801001的状态")
    assert slots2.get("order_id") == "SO20260801001", f"order_id 提取失败: {slots2}"
    assert "product_code" not in slots2, f"SO 内编号被误判为产品型号: {slots2}"

    # 3. 必填判定可训练
    missing = check_slots_complete({"product_code": "A-202"}, "create_order")
    assert missing == ["quantity"], f"必填判定失败: {missing}"
    assert check_slots_complete({"quantity": 100}, "create_order") == ["product_code"]

    # 4. 引导语
    hints = get_prompt_hints(["quantity"])
    assert "quantity" in hints and "100套" in hints["quantity"]

    # 5. 时间段/质量维度（v6.43 多限定词）
    slots3 = extract_slots("查一下A-202在3月到5月的划痕问题")
    assert slots3.get("date_range", {}).get("start") == "2026-03-01", slots3
    assert slots3.get("defect_type") == "划痕", slots3

    # 6. 必填 or 关系（modify_order）
    m1 = check_slots_complete({"order_id": "SO1"}, "modify_order")
    assert m1 == [], f"modify_order 有 order_id 应无缺失: {m1}"
    m2 = check_slots_complete({}, "modify_order")
    assert m2 == ["order_id|product_code"], f"modify_order 缺 or 槽位: {m2}"

    # 7. 日期范围
    slots4 = extract_slots("2026-03-01 至 2026-05-31 的检验记录")
    assert slots4.get("date_range") == {"start": "2026-03-01", "end": "2026-05-31"}, slots4

    # 8. 文件类槽位（v6.44）：带文件名的上传表达提取 + 上传文件合并
    slots5 = extract_slots("我上传了报销单.pdf，申请报销")
    assert slots5.get("attachment") == {"file_name": "报销单.pdf", "ext": "pdf", "type": "file"}, slots5
    assert "doc_template" not in slots5 or slots5["doc_template"].get("file_name") != "报销单.pdf"
    slots6 = extract_slots("请用合同模板起草合同")
    assert slots6.get("doc_template") and slots6["doc_template"].get("file_name") == "合同模板", slots6
    # 无文件名的"上传附件"不产生空值
    slots7 = extract_slots("上传附件")
    assert "attachment" not in slots7, slots7
    # 上传文件合并
    merged = merge_uploaded_files({}, [{"name": "报销单.pdf"}, {"name": "发票.xlsx"}])
    assert merged["attachment"] == ["报销单.pdf", "发票.xlsx"], merged
    # 已有文本提取值时追加不覆盖
    merged2 = merge_uploaded_files({"attachment": {"file_name": "a.pdf", "ext": "pdf", "type": "file"}}, [])
    assert merged2["attachment"]["file_name"] == "a.pdf"

    print("slot_engine 自检通过")


from prog.runtime.debug import DEBUG
if DEBUG:
    _self_test()
