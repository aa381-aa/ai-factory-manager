"""
查询附加词解析器（v6.65）
================================

用途：
    把用户查询输入中的"附加词"解析为结构化过滤条件（filters），
    支持产品名称、数值参数、日期范围、状态词等；规则无法解析的
    模糊片段交由 LLM 补全为结构化条件，实现"查询可以带附加词，
    模糊时调用 LLM 补全"。

filters 结构（与 DatabaseManager.query_filtered 一致）：
    [{"field": "raw", "op": "gt", "value": 100},
     {"field": "created_at", "op": "between", "value": ["2026-07-01", "2026-07-31"]},
     {"field": "product_name", "op": "like", "value": "%铝%"}]

规则（零延迟，DB 可训练降级内置）：
    1. 产品名称/型号：名称=xxx / 叫xxx的产品 / 型号是xxx
    2. 数值参数：字段中文别名 + 比较词 + 数值（如 数量大于100）
    3. 日期范围：近N天 / YYYY-MM-DD至YYYY-MM-DD / X月 / 今天/昨天/本周/本月
    4. 状态词：已完成/待审批/已通过 等（映射到 status 列）

LLM 补全：
    parse_query_filters 返回 {"filters": [...], "fuzzy": "未解析片段"}；
    调用方对 fuzzy 调用 llm_complete_filters 生成结构化 JSON（带字段/
    操作符白名单校验，防注入）。规则匹配零延迟；仅模糊片段才走 LLM。

与 slot_engine 的关系：
    槽位负责"业务流程必填字段"（quantity/product_code/stage 等），
    本解析器负责"查询附加过滤条件"（比较/范围/模糊），互不重叠。

使用方：
    - knowledge_assistant._handle_query_flow（查询流程 db 步骤）
    - warehouse_agent._handle_inventory_query（硬编码查询统一改造）
    - execute_workflow_query 接收 params["_filters"] 附加条件

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 查询附加词解析：parse_query_filters 规则零延迟解析附加词（数值参数/日期范围/状态词/产品名称），返回 {"filters", "fuzzy", "notes"}（业务规格书 v6.55）
        - LLM 补全：llm_complete_filters 把规则未解析的模糊片段补全为结构化 filters（字段名 ASCII 白名单 + 操作符 8 种白名单，防注入）（业务规格书 v6.55）
        - 查询指令整体 LLM 生成：llm_generate_query_params 注入 db 步骤表/键/字段清单，返回参数经白名单校验丢弃非法项（业务规格书 v6.55）
        - filters_to_human：filters 转人类可读描述（渲染"查询条件"行）（业务规格书 v6.55）
        - 过滤条件跨表适配：adapt_filters_to_table 产品名条件反查 product_code 集转为 IN 条件，避免多表流程 UndefinedColumn（业务规格书 v6.55 跨表过滤）
        - "我的订单"误判修复（v6.85）：停用词表补"我的/订单/我的订单"，归属代词+业务实体词不构成模糊参数（业务规格书 v6.85 / CHANGELOG v41）
    对外接口（方法/API）：
        - parse_query_filters(user_input) -> dict：解析用户查询输入中的附加过滤条件（业务规格书 v6.55）
        - llm_complete_filters(fuzzy, llm_call, table_hint="", fields=None) -> list：LLM 补全为合法 filters（白名单校验）（业务规格书 v6.55）
        - llm_generate_query_params(user_input, db_steps, llm_call) -> dict：整体 LLM 生成查询参数（业务规格书 v6.55）
        - filters_to_human(filters) -> str：filters 人类可读描述（业务规格书 v6.55）
        - adapt_filters_to_table(filters, table, db, product_table="products") -> tuple：过滤条件适配目标表实际列（返回 (adapted_filters, notes)）（业务规格书 v6.55）
    错误处理要求：
        - LLM 调用异常/返回非 JSON/JSON 结构非法：返回空列表/空参数，不中断查询流程（业务规格书 v6.55 未明确）
        - 非法字段名/操作符：白名单校验丢弃（防注入）（业务规格书 v6.55）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

# 字段中文别名 -> 列名（内置默认；DB(FIELD-ALIASES) 可覆盖/新增）
DEFAULT_FIELD_ALIASES: Dict[str, str] = {
    "产品名称": "product_name", "名称": "product_name", "品名": "product_name",
    "产品型号": "product_code", "型号": "product_code", "产品编码": "product_code",
    "编码": "product_code", "物料编码": "material_code", "物料型号": "material_code",
    "数量": "quantity", "库存": "raw", "库存数量": "raw", "剩余库存": "raw",
    "价格": "price", "单价": "unit_price", "售价": "price",
    "成本": "cost_price", "成本价": "cost_price", "金额": "total_amount",
    "总金额": "total_amount", "交期": "lead_time_days", "交期天数": "lead_time_days",
    "状态": "status", "日期": "created_at", "时间": "created_at",
    "创建时间": "created_at", "更新时间": "updated_at",
    "供应商": "supplier_id", "客户": "customer_id",
    "订单号": "order_id", "工单号": "work_order_id", "批次号": "batch_no",
}

# 比较词 -> 操作符（数值/日期/字符串通用）
_OP_WORDS: List[tuple] = [
    (re.compile(r"(?:大于等于|不小于|至少|不低于|≥|>=)\s*"), "gte"),
    (re.compile(r"(?:小于等于|不大于|至多|最多|不高于|≤|<=)\s*"), "lte"),
    (re.compile(r"(?:大于|超过|多于|高于|高于等于|>)\s*"), "gt"),
    (re.compile(r"(?:小于|低于|少于|不足|<)\s*"), "lt"),
    (re.compile(r"(?:不等于|≠)\s*"), "ne"),
    (re.compile(r"(?:等于|为|是|=)\s*"), "eq"),
]

# 状态中文 -> 英文（status 列）
_STATUS_MAP: Dict[str, str] = {
    "已审批": "approved", "已通过": "approved",
    "待审批": "pending", "审批中": "pending", "进行中": "in_progress",
    "已取消": "cancelled", "已入库": "received", "已出库": "shipped",
    "草稿": "draft", "已提交": "submitted", "已发货": "shipped",
    "待处理": "pending", "已完成": "completed", "已确认": "confirmed",
}

# 日期相对词
_RELATIVE_DAYS: Dict[str, int] = {
    "今天": 0, "昨日": -1, "昨天": -1, "前天": -2, "明天": 1, "后天": 2,
}


def _field_alias(field: str) -> str:
    """中文字段别名 -> 列名（DB FIELD-ALIASES 可训练覆盖内置默认）。"""
    try:
        from prog.runtime.param_loader import get_param_dict
        merged = dict(DEFAULT_FIELD_ALIASES)
        merged.update(get_param_dict("FIELD-ALIASES", {}))
    except Exception:
        merged = DEFAULT_FIELD_ALIASES
    return merged.get(field, "")


def _match_num_condition(text: str) -> Optional[Dict[str, Any]]:
    """匹配'字段 + 比较词 + 数值'，如'数量大于100'。返回 filter 或 None。

    字段名用已知别名精确匹配（DEFAULT_FIELD_ALIASES + DB FIELD-ALIASES），
    避免非贪婪量词把"看看库存"等前导字一并吞入导致别名失配。
    """
    try:
        from prog.runtime.param_loader import get_param_dict
        merged = dict(DEFAULT_FIELD_ALIASES)
        merged.update(get_param_dict("FIELD-ALIASES", {}))
    except Exception:
        merged = DEFAULT_FIELD_ALIASES
    if not merged:
        return None
    alias_alt = "|".join(
        sorted((k for k in merged if k), key=len, reverse=True))
    m = re.search(
        rf"({alias_alt})\s*"
        r"(?:大于等于|不小于|至少|不低于|≥|>=|小于等于|不大于|至多|最多|不高于|≤|<=|"
        r"大于|超过|多于|高于|>|小于|低于|少于|不足|<|等于|为|是|=|不等于|≠)\s*"
        r"(\d+(?:\.\d+)?)",
        text)
    if not m:
        return None
    cn_field = m.group(1)
    column = merged.get(cn_field, "")
    if not column:
        return None
    # 从原始文本判断比较词
    seg = m.group(0)
    op = "eq"
    for pat, _op in _OP_WORDS:
        if pat.search(seg):
            op = _op
            break
    value: Any = float(m.group(2)) if "." in m.group(2) else int(m.group(2))
    return {"field": column, "op": op, "value": value}


def _match_date_condition(text: str) -> Optional[Dict[str, Any]]:
    """匹配日期条件：近N天 / 绝对日期区间 / 相对日期词。返回 filter 或 None。"""
    # 近N天
    m = re.search(r"近\s*(\d+)\s*天", text)
    if m:
        days = int(m.group(1))
        if days <= 0:
            days = 1  # 防御：days 非法（<=0）时按 1 天处理，避免 start > end
        end = datetime.now().date()
        start = end - timedelta(days=days - 1)
        return {"field": "created_at", "op": "between",
                "value": [start.isoformat(), end.isoformat()]}
    # 绝对区间：YYYY-MM-DD 至/到/~ YYYY-MM-DD 或 YYYY-MM-DD~YYYY-MM-DD
    m = re.search(
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(?:至|到|~|～)\s*"
        r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
    if m:
        def _norm(s: str) -> str:
            """日期归一化：将分隔符 / 统一为 -（如 2026/05/01 -> 2026-05-01）。

            参数：
                s: 原始日期片段（可能含 / 或 -）
            返回：
                str: 统一为 - 分隔的日期串
            """
            return s.replace("/", "-")
        return {"field": "created_at", "op": "between",
                "value": [_norm(m.group(1)), _norm(m.group(2))]}
    # 单日期 YYYY-MM-DD
    m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
    if m:
        d = m.group(1).replace("/", "-")
        return {"field": "created_at", "op": "between",
                "value": [d, d]}
    # X月 / YYYY年X月
    m = re.search(r"(?:(\d{4})年)?(\d{1,2})月", text)
    if m:
        year = int(m.group(1) or datetime.now().year)
        month = int(m.group(2))
        start = datetime(year, month, 1).date()
        end = (datetime(year, month + 1, 1) - timedelta(days=1)).date()
        return {"field": "created_at", "op": "between",
                "value": [start.isoformat(), end.isoformat()]}
    # 相对日期词：今天/昨天/本周/本月/上月
    for word, delta in _RELATIVE_DAYS.items():
        if word in text:
            d = (datetime.now().date() + timedelta(days=delta))
            return {"field": "created_at", "op": "between",
                    "value": [d.isoformat(), d.isoformat()]}
    if "本周" in text:
        today = datetime.now().date()
        start = today - timedelta(days=today.weekday())
        return {"field": "created_at", "op": "between",
                "value": [start.isoformat(), today.isoformat()]}
    if "本月" in text:
        today = datetime.now().date()
        start = today.replace(day=1)
        return {"field": "created_at", "op": "between",
                "value": [start.isoformat(), today.isoformat()]}
    if "上月" in text:
        today = datetime.now().date()
        first = today.replace(day=1)
        start = (first - timedelta(days=1)).replace(day=1)
        end = first - timedelta(days=1)
        return {"field": "created_at", "op": "between",
                "value": [start.isoformat(), end.isoformat()]}
    return None


def _match_status(text: str) -> Optional[Dict[str, Any]]:
    """匹配状态词（映射到 status 列）。"""
    for cn, en in _STATUS_MAP.items():
        if cn in text:
            return {"field": "status", "op": "eq", "value": en}
    return None


def _match_product_name(text: str) -> Optional[Dict[str, Any]]:
    """匹配产品名称/型号：名称=xxx / 叫xxx的产品 / 型号是xxx。"""
    # 名称/产品名/品名 + 分隔 + 值（中文/字母数字-，最长24字）
    m = re.search(
        r"(?:产品名称|产品名|品名|名称|型号|产品型号)\s*[:：=是]?\s*"
        r"([\u4e00-\u9fa5A-Za-z0-9_\-]{1,24})", text)
    if m:
        val = m.group(1).strip()
        if val:
            return {"field": "product_name", "op": "like", "value": f"%{val}%"}
    return None


def parse_query_filters(user_input: str) -> Dict[str, Any]:
    """解析用户查询输入中的附加过滤条件（v6.65）。

    规则解析（零延迟）：数值参数 / 日期范围 / 状态 / 产品名称。
    无法解析的剩余片段存入 fuzzy（调用方可选 LLM 补全）。

    Args:
        user_input: 用户查询输入

    Returns:
        dict: {"filters": [{"field","op","value"}, ...],
               "fuzzy": str|None,   # 未解析的模糊片段（可能需 LLM）
               "notes": [str, ...]} # 人类可读的解析说明（渲染用）
    """
    filters: List[Dict[str, Any]] = []
    notes: List[str] = []
    fuzzy: Optional[str] = None

    # 1. 数值参数（最具体，先解析）
    m = _match_num_condition(user_input)
    if m:
        filters.append(m)
        cn_field = _cn_of_column(m["field"])
        notes.append(f"{cn_field}{_op_cn(m['op'])}{m['value']}")

    # 2. 日期范围
    m = _match_date_condition(user_input)
    if m:
        filters.append(m)
        notes.append(f"日期{m['value'][0]}~{m['value'][1]}")

    # 3. 状态词
    m = _match_status(user_input)
    if m:
        filters.append(m)
        notes.append(f"状态={m['value']}")

    # 4. 产品名称/型号
    m = _match_product_name(user_input)
    if m:
        filters.append(m)
        notes.append(f"名称包含{m['value'].strip('%')}")

    # 5. 模糊片段：去掉已消费的规则片段后，剩余疑似附加词
    residual = user_input
    try:
        from prog.runtime.param_loader import get_param_dict
        _merged = dict(DEFAULT_FIELD_ALIASES)
        _merged.update(get_param_dict("FIELD-ALIASES", {}))
    except Exception:
        _merged = DEFAULT_FIELD_ALIASES
    _alias_alt = "|".join(
        sorted((k for k in _merged if k), key=len, reverse=True))
    residual = re.sub(
        rf"({_alias_alt})\s*(?:大于等于|不小于|至少|不低于|≥|>=|"
        r"小于等于|不大于|至多|最多|不高于|≤|<=|大于|超过|多于|高于|>|"
        r"小于|低于|少于|不足|<|等于|为|是|=|不等于|≠)\s*\d+(?:\.\d+)?", "",
        residual)
    residual = re.sub(r"近\s*\d+\s*天", "", residual)
    residual = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*(?:至|到|~|～)\s*"
                      r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", residual)
    residual = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", residual)
    residual = re.sub(r"(?:\d{4}年)?\d{1,2}月", "", residual)
    residual = re.sub(r"今天|昨天|前天|明天|后天|本周|本月|上月", "", residual)
    residual = re.sub(
        r"(?:产品名称|产品名|品名|名称|型号|产品型号)\s*[:：=是]?\s*"
        r"[\u4e00-\u9fa5A-Za-z0-9_\-]{1,24}", "", residual)
    for cn in _STATUS_MAP:
        residual = residual.replace(cn, "")
    # 产品码（A-202 等）由 slot_engine 消费，不作为 fuzzy
    # （不用 \b：中文字符与 ASCII 之间无词边界，直接匹配字母+数字组合）
    residual = re.sub(r"(?<![A-Za-z0-9])[A-Za-z]-?\d{2,4}(?![A-Za-z0-9])", "",
                      residual)
    # 查询惯用语停用词（"查一下/看看/的库存/产品"等）不视为模糊附加词
    for w in ("查一下", "查查", "看看", "查询", "查看", "帮我", "请", "一下",
              "的库存", "库存", "产品", "物料", "记录", "情况", "吧",
              # v6.85：归属代词与业务实体词不构成模糊查询参数——
              # "查一下我的订单" 中"我的/订单"是归属+实体，非模糊片段；
              # 若保留为 fuzzy 会误判有主参数而进查询流程（缺 order_id/
              # customer_id/product_code → 无数据 + 知识库兜底），应走
              # sales Agent 直达按当前用户查订单列表。
              "我的", "订单", "我的订单"):
        residual = residual.replace(w, "")
    residual = residual.strip(" ，,。、的")
    if residual:
        fuzzy = residual

    # 去重（同字段同操作符仅保留首个）
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for f in filters:
        key = (f["field"], f["op"], str(f["value"]))
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return {"filters": uniq, "fuzzy": fuzzy, "notes": notes}


def _cn_of_column(column: str) -> str:
    """列名 -> 中文（反向查找，用于说明文案）。"""
    for cn, col in DEFAULT_FIELD_ALIASES.items():
        if col == column:
            return cn
    return column


def _op_cn(op: str) -> str:
    """操作符英文符号转中文展示符号。

    参数：
        op: 过滤操作符（gt/gte/lt/lte/eq/ne/like）
    返回：
        str: 中文符号（> / ≥ / < / ≤ / = / ≠ / 包含）；未知操作符原样返回
    """
    return {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
            "eq": "=", "ne": "≠", "like": "包含"}.get(op, op)


# 允许 LLM 补全生成的操作符白名单（防注入）
_LLM_OP_ALLOWED = {"eq", "ne", "gt", "gte", "lt", "lte", "like", "between"}
_LLM_FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _db_field_hint(fields: Optional[List[str]] = None) -> str:
    """汇总 LLM 可用的列名清单：调用方 fields + 内置字段别名列名（去重）。

    让 LLM 补全/生成时知道有哪些列可用（如"铝合金外壳"→product_name），
    避免 LLM 因未知字段而保守返回空。
    """
    out: List[str] = []
    for f in fields or []:
        if f and f not in out:
            out.append(f)
    for col in DEFAULT_FIELD_ALIASES.values():
        if col and col not in out:
            out.append(col)
    return ",".join(out)


def llm_complete_filters(fuzzy: str, llm_call: Callable[[str], str],
                         table_hint: str = "",
                         fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """调用 LLM 把模糊附加词补全为结构化 filters（v6.65）。

    Args:
        fuzzy: 规则未解析的模糊片段（如"铝外壳的"、"库存偏高的"）
        llm_call: LLM 调用函数（prompt -> str）
        table_hint: 查询表名提示（可选，帮助 LLM 选字段）
        fields: 可用列名清单（可选，默认补充内置字段别名列名；
            明确列出后 LLM 才知道"铝合金外壳"应映射到 product_name）

    Returns:
        list: 合法 filters（字段名/操作符白名单校验，非法丢弃）
    """
    if not fuzzy or not llm_call:
        return []
    table_tip = f"目标查询表：{table_hint}。" if table_hint else ""
    field_tip = (f"可用列名：{_db_field_hint(fields)}。" if fields else "")
    prompt = (
        "你是数据库查询条件解析器。请把用户描述中的附加查询条件解析为"
        "结构化 JSON 数组。\n"
        f"{table_tip}{field_tip}\n"
        "输出格式（只输出 JSON，不要多余文字）：\n"
        '[{"field": "列名", "op": "gt|gte|lt|lte|eq|ne|like|between", '
        '"value": 值}]\n'
        "规则：\n"
        "1. field 必须是可用列名中的 ASCII 列名\n"
        "2. op 只能是 gt/gte/lt/lte/eq/ne/like/between\n"
        "3. between 的 value 是 [开始, 结束]；like 的 value 是字符串\n"
        "4. 无法确定的条件不要输出，返回 []\n"
        f"用户描述：{fuzzy}\n"
    )
    try:
        raw = (llm_call(prompt) or "").strip()
    except Exception:
        return []
    # 提取 JSON 数组（容忍 ```json 包裹）
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        op = str(item.get("op", "eq"))
        value = item.get("value")
        if not _LLM_FIELD_RE.match(field):
            continue
        if op not in _LLM_OP_ALLOWED:
            continue
        if op == "between" and not (
                isinstance(value, (list, tuple)) and len(value) == 2):
            continue
        out.append({"field": field, "op": op, "value": value})
    return out


def filters_to_human(filters: List[Dict[str, Any]]) -> str:
    """filters -> 人类可读描述（渲染"查询条件"行）。"""
    parts = []
    for f in filters or []:
        field = f.get("field", "")
        op = f.get("op", "eq")
        value = f.get("value")
        cn = _cn_of_column(field)
        if op == "between" and isinstance(value, (list, tuple)):
            parts.append(f"{cn} {value[0]}~{value[1]}")
        elif op == "like":
            parts.append(f"{cn} 包含 {str(value).strip('%')}")
        elif op == "ne":
            parts.append(f"{cn} ≠ {value}")
        else:
            parts.append(f"{cn} {_op_cn(op)} {value}")
    return "；".join(parts)


# 产品名/名称字段的等价列（跨表转换用）
_NAME_FIELDS = {"product_name", "name"}


def adapt_filters_to_table(filters: List[Dict[str, Any]], table: str,
                           db: Any,
                           product_table: str = "products") -> tuple:
    """把过滤条件适配到目标表实际列（v6.65.1）。

    规则/LLM 生成的字段可能不属于目标表（如 inventory 表没有产品名列，
    "铝合金外壳的库存" → product_name），直接查会抛 UndefinedColumn。
    适配规则：
        1. 产品名/名称条件（product_name/name，eq/like）目标表无此列时，
           反查 {product_table}.product_name → product_code 集合，
           转换为 product_code IN (...)（空集 → 该条件无结果返回空列表）；
        2. 其余字段保持原样（调用方保证与目标表列一致）。

    Args:
        filters: 待适配的过滤条件列表
        table: 目标查询表
        db: 数据库对象（鸭子类型：需 query_filtered 或 query_many）
        product_table: 产品主数据表（含 product_code/product_name）

    Returns:
        tuple: (adapted_filters, notes)  notes 为适配说明（渲染用）
    """
    if not filters or not table or db is None:
        return list(filters or []), []
    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        field = f.get("field", "")
        op = f.get("op", "eq")
        value = f.get("value")
        # 产品名条件：尝试反查产品码
        if field in _NAME_FIELDS and op in ("eq", "like"):
            try:
                _like = value if op == "like" else value
                _qval = f"%{str(_like).strip('%')}%" if op == "like" else str(_like)
                if hasattr(db, "query_filtered"):
                    _rows = db.query_filtered(
                        product_table,
                        [{"field": "product_name", "op": "like", "value": _qval}],
                        columns=["product_code"], limit=500)
                else:
                    _rows = db.query_many(
                        product_table, {"product_name": str(value).strip('%')},
                        columns=["product_code"], limit=500)
            except Exception:
                _rows = None
            codes = [str(r.get("product_code")) for r in (_rows or [])
                     if isinstance(r, dict) and r.get("product_code")]
            codes = [c for c in codes if c]
            if codes:
                out.append({"field": "product_code", "op": "in", "value": codes})
                notes.append(f"名称匹配 {len(codes)} 个产品")
            else:
                # 无匹配产品 → 返回空（调用方渲染"未找到"）
                return [], notes + [f"无名称匹配「{str(value).strip('%')}」的产品"]
        else:
            out.append(f)
    return out, notes


def llm_generate_query_params(user_input: str,
                              db_steps: List[Dict[str, Any]],
                              llm_call: Callable[[str], str]) -> Dict[str, Any]:
    """整体调用 LLM 生成查询参数（v6.65，规则解析不理想时的兜底通道）。

    规则解析（parse_query_filters）无法覆盖的查询（如"查一下上周库存
    偏高的物料"），将 db 步骤的表/键/字段清单注入 prompt，由 LLM 生成：
        {"product_code": "A-202", "_filters": [{"field","op","value"}]}
    返回参数经字段名/操作符白名单校验，非法项丢弃（防注入）。

    Args:
        user_input: 用户查询输入
        db_steps: 查询流程中的 db 步骤定义列表
        llm_call: LLM 调用函数（prompt -> str）

    Returns:
        dict: {"params": {主键:值}, "filters": [...], "notes": [str]}
    """
    if not user_input or not db_steps or not llm_call:
        return {"params": {}, "filters": [], "notes": []}
    # 收集 db 步骤的表/键/字段清单（供 LLM 选字段）
    hints = []
    key_aliases = []
    for s in db_steps:
        if not isinstance(s, dict) or s.get("type", "db") != "db":
            continue
        table = s.get("table", "")
        key_field = s.get("key_field", "")
        source_key = s.get("source_key", "") or key_field
        fields = s.get("fields") or []
        hints.append(f"- 表 {table}：键 {key_field}（参数名 {source_key}），"
                     f"字段 {_db_field_hint(fields)}")
        if source_key and source_key not in key_aliases:
            key_aliases.append(source_key)
    if not hints:
        return {"params": {}, "filters": [], "notes": []}
    prompt = (
        "你是数据库查询参数解析器。根据用户查询生成结构化查询参数。\n"
        "可查询的数据源：\n" + "\n".join(hints) + "\n"
        "输出格式（只输出 JSON，不要多余文字）：\n"
        '{"<参数名>": "值", "_filters": [{"field": "列名", "op": "gt|gte|lt|lte|'
        'eq|ne|like|between", "value": 值}]}\n'
        "规则：\n"
        "1. 参数名必须是上面列出的键参数名（如 product_code）；\n"
        "2. _filters 的 field 必须是上面字段清单中的列名（ASCII）；\n"
        "3. op 只能是 gt/gte/lt/lte/eq/ne/like/between；between 的 value 是 [开始, 结束]；\n"
        "4. 无法确定的信息不要输出，宁可少给也不要编造；\n"
        "5. 完全没有可用查询条件时输出 {\"_filters\": []}。\n"
        f"用户查询：{user_input}\n"
    )
    try:
        raw = (llm_call(prompt) or "").strip()
    except Exception:
        return {"params": {}, "filters": [], "notes": []}
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {"params": {}, "filters": [], "notes": []}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {"params": {}, "filters": [], "notes": []}
    if not isinstance(data, dict):
        return {"params": {}, "filters": [], "notes": []}
    params: Dict[str, Any] = {}
    for k in key_aliases:
        v = data.get(k)
        if v not in (None, ""):
            params[k] = v
    filters: List[Dict[str, Any]] = []
    for item in data.get("_filters") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", ""))
        op = str(item.get("op", "eq"))
        value = item.get("value")
        if not _LLM_FIELD_RE.match(field):
            continue
        if op not in _LLM_OP_ALLOWED:
            continue
        if op == "between" and not (
                isinstance(value, (list, tuple)) and len(value) == 2):
            continue
        filters.append({"field": field, "op": op, "value": value})
    notes = []
    if params:
        notes.extend(f"{k}={v}" for k, v in params.items())
    if filters:
        _human = filters_to_human(filters)
        if _human:
            notes.append(f"（LLM识别）{_human}")
    return {"params": params, "filters": filters, "notes": notes}
