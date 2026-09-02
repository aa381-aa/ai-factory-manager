"""
WarehouseAgent 仓储采购Agent模块
================================

文件用途：
    实现仓储与采购领域的业务Agent，处理库存查询、入库出库、库存预警、
    采购建议、五阶段库存流转等仓储相关意图。

技术规格章节：
    - §3.4 Warehouse-Procurement Agent（P1优先级）
    - §2 规则引擎（库存规则接入）

核心能力（§3.4 W-01~W-09）：
    W-01 实时库存多维度可视化（五阶段分布、库存价值）
    W-02 订单确认后事务级库存扣减（乐观锁version+事务）
    W-03 安全库存阈值监控与缺料预警
    W-04 多层BOM递归展开与缺料检查
    W-05 物料全生命周期移动追踪（五阶段流转+流水记录）
    W-06 可替代物料检索与规格匹配
    W-07 采购订单创建与全生命周期跟踪
    W-08 供应商管理与评价
    W-09 比价议价辅助与外协采购执行

数据通道：
    业务操作通道 -> PostgreSQL（inventory, inventory_movements, purchase_orders, suppliers, bom表）

功能清单（规格/变更对照）：
    应实现功能（能力编号 → 规格书章节）：
        - W-01 实时库存多维度可视化：五阶段分布/安全库存预警/库存价值（规格书 §3.4.1）
        - W-02 订单确认后事务级库存扣减：乐观锁+五阶段流转（规格书 §3.4.1）
        - W-03 安全库存阈值监控与缺料预警（规格书 §3.4.1）
        - W-04 多层BOM递归展开与缺料检查（规格书 §3.4.1）
        - W-05 物料全生命周期移动追踪：inventory_movements 流水查询（规格书 §3.4.1；v6.63 补挂载）
        - W-06 可替代物料检索与规格匹配（规格书 §3.4.1）
        - W-07 采购订单创建与全生命周期跟踪（规格书 §3.4.1）
        - W-08 供应商管理与评价（规格书 §3.4.1）
        - W-09 比价议价辅助与外协采购执行（规格书 §3.4.1；v6.63 补挂载）
    子意图分发：
        - inventory_query：_handle_inventory_query —— 库存查询（INT-03 查询库存，规格书 §A.8.1；W-01）
        - stock_in：_handle_stock_in —— 入库（规格书 §3.4.2 Step 1~3）
        - stock_out：_handle_stock_out —— 出库（规格书 §3.4.2 Step 4）
        - shortage_check：_handle_shortage_check —— 缺料检查（W-03/W-04，规格书 §3.4.1）
        - purchase_request：_handle_purchase_request —— 采购申请/采购订单（INT-20 采购操作，规格书 §A.8.1；W-07）
        - material_trace：_handle_material_trace —— 物料追溯（W-05，规格书 §3.4.1；v6.63）
        - price_compare：_handle_price_compare —— 供应商比价（W-09，规格书 §3.4.1；v6.63）
    对外接口（方法/API）：
        - WarehouseAgent.process(user_input, context)：主处理入口 —— 按子意图分发（契约 1，模块拆分方案）
        - WarehouseAgent.deduct_for_order(product_code, quantity, ...)：订单确认事务级扣减（W-02，规格书 §3.4.1）
        - WarehouseAgent.check_safety_stock()：安全库存巡检（W-03，规格书 §3.4.1）
        - WarehouseAgent.transfer_stage(product_code, quantity, ...)：五阶段流转（R.2.6，规格书 §3.4.2）
        - WarehouseAgent.track_material_movements(product_code)：物料移动追踪（W-05，规格书 §3.4.1）
        - WarehouseAgent.search_alternative_materials(material_code)：替代料检索（W-06，规格书 §3.4.1）
        - WarehouseAgent.create_purchase_order(supplier_id, items, ...)：PO 创建（W-07，规格书 §3.4.1）
        - WarehouseAgent.track_purchase_order(po_id)：PO 跟踪（W-07，规格书 §3.4.1）
        - WarehouseAgent.evaluate_supplier(supplier_id)：供应商评分（W-08，规格书 §3.4.6）
        - WarehouseAgent.manage_supplier(action, supplier_data)：供应商管理（W-08，规格书 §3.4.1）
        - WarehouseAgent.compare_prices(material_code, quantity)：比价（W-09，规格书 §3.4.6）
    错误处理要求：
        - 库存不足/超卖：乐观锁 version 校验拒绝并提示（规格书 §3.4.1 W-02 事务级扣减）
        - 五阶段流转非法（阶段不可跳过）：规则阻断（规格书 §3.4.2，R.2.6）
        - 单一供应商采购比例超 70% / 新供应商首单超 ¥50,000：安全规则拦截（规格书 §3.4.6 算法安全边界）
"""

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from prog.agents.base_agent import BaseAgent, AgentResponse
# S5 修复：五阶段列名白名单（防 SQL 注入）——来自 models.inventory，
# 与 inventory 表列设计保持一致（raw/wip_cnc/wip_anode/wip_qc/finished）
from prog.models.inventory import INVENTORY_STAGE_COLUMNS


# 库存五阶段定义（按流转顺序排列，与DB inventory表列名一致）
INVENTORY_STAGES = [
    "raw",          # 原材料仓
    "wip_cnc",      # 在制-机加工
    "wip_anode",    # 在制-阳极氧化
    "wip_qc",       # 在制-质检
    "finished",     # 成品仓
]

# 阶段中文名称映射
STAGE_NAMES = {
    "raw": "原材料仓",
    "wip_cnc": "在制-机加工",
    "wip_anode": "在制-阳极氧化",
    "wip_qc": "在制-质检",
    "finished": "成品仓",
}


def _inv_stages() -> list:
    """返回五阶段定义（v6.30 从 INV-STAGE.stages 读取，可训练增删/调整顺序）。

    INV-STAGE 种子 stages 与 INVENTORY_STAGES 一致（列名列表）；
    训练修改 stages 后此处即时生效；DB 不可用/读取失败降级默认。
    """
    from prog.runtime.param_loader import get_param
    stages = get_param("INV-STAGE", "stages", INVENTORY_STAGES)
    if isinstance(stages, list) and stages:
        return list(stages)
    return list(INVENTORY_STAGES)


def _stage_names() -> dict:
    """返回阶段中文名映射（基于可训练 stages，未知阶段以原文兜底）。"""
    names = dict(STAGE_NAMES)
    for stage in _inv_stages():
        names.setdefault(stage, stage)
    return names



def _procure_weights() -> dict:
    """采购比价与供应商评价权重（PROCURE-PARAMS，可训练），DB 不可用降级默认。"""
    from prog.runtime.param_loader import get_param_dict
    return get_param_dict("PROCURE-PARAMS", {
        "compare_weight_price": 0.7, "compare_weight_delivery": 0.3,
        "supplier_weight_delivery": 0.30, "supplier_weight_quality": 0.30,
        "supplier_weight_price": 0.25, "supplier_weight_service": 0.15,
    })


class WarehouseAgent(BaseAgent):
    """
    仓储采购Agent（§3.4，P1优先级）。

    承载仓储与采购域全部交互，重点在于库存准确性保障与五阶段流转控制。
    使用乐观锁（version 字段）防止超卖，所有操作记录 inventory_movements 流水。
    """

    def __init__(self, llm_provider: Any = None, database: Any = None):
        """初始化仓储Agent。

        参数：
            llm_provider: LLM提供方接口（库存分析/出入库引导）
            database: PostgreSQL数据库访问层（库存/出入库记录）

        装配：applicable_rules=["inventory_rule", "bom_rule"]
              （库存校验 + BOM校验）
        """
        super().__init__(
            agent_name="仓储Agent",
            agent_type="warehouse",
            llm_provider=llm_provider,
            database=database,
        )
        self.applicable_rules = ["inventory_rule", "bom_rule"]

    # --------------------------------------------------------
    # 主处理入口
    # --------------------------------------------------------
    def process(self, user_input: str, context: Dict[str, Any]) -> AgentResponse:
        """仓储Agent主处理入口，根据子意图分发到对应处理器。"""
        start_time = time.time()
        sub_intent = context.get("sub_intent", "")
        if not sub_intent:
            sub_intent = self._recognize_sub_intent(user_input)
        if not sub_intent:
            # 多轮延续兜底：用户补充纯信息（如产品型号 A-202）未含业务关键词时，
            # 依据 coordinator 恢复的业务意图（pending 延续）沿用原业务子意图，
            # 避免误走 LLM 兜底（数秒延迟 + 无结构化 HTML 渲染）
            sub_intent = self._intent_to_sub_intent(context)

        if sub_intent == "inventory_query":
            response = self._handle_inventory_query(user_input, context)
        elif sub_intent == "stock_in":
            response = self._handle_stock_in(user_input, context)
        elif sub_intent == "stock_out":
            response = self._handle_stock_out(user_input, context)
        elif sub_intent == "shortage_check":
            response = self._handle_shortage_check(user_input, context)
        elif sub_intent == "purchase_request":
            response = self._handle_purchase_request(user_input, context)
        elif sub_intent == "material_trace":
            # W-05 物料全生命周期移动追踪（handler 已就绪，v6.63 补挂载）
            response = self._handle_material_trace(user_input, context)
        elif sub_intent == "price_compare":
            # W-09 比价议价辅助（handler 已就绪，v6.63 补挂载）
            response = self._handle_price_compare(user_input, context)
        else:
            response = super().process(user_input, context)

        elapsed = round((time.time() - start_time) * 1000, 2)
        response.metadata["elapsed_ms"] = elapsed
        response.metadata["sub_intent"] = sub_intent
        return response

    def _recognize_sub_intent(self, user_input: str) -> str:
        """从用户输入中识别仓储子意图。

        v6.46 C4：关键词表迁入 DB(SUB-INTENT-DEFS) 可训练，统一委托引擎。
        """
        from prog.runtime.sub_intent_engine import recognize_sub_intent as _rsi
        if _rsi is not None:
            try:
                return _rsi(self.agent_type, user_input) or ""
            except Exception:
                pass
        return ""

    def _intent_to_sub_intent(self, context: Dict[str, Any]) -> str:
        """coordinator 业务意图名 -> 仓储子意图 映射（多轮延续兜底）。

        多轮延续时用户补充纯信息（如"A-202"）不含业务关键词，子意图识别为空，
        依据 coordinator 恢复的业务意图（pending 延续后的 intent）沿用原子意图。
        """
        intent = context.get("intent", "")
        return {
            "query_inventory": "inventory_query",
            "inventory_adjust": "inventory_query",
            "stock_in": "stock_in",
            "stock_out": "stock_out",
            "purchase": "purchase_request",
        }.get(intent, "")

    def _extract_slots(self, user_input: str) -> Dict[str, Any]:
        """从用户输入中提取仓储槽位（v6.46：统一委托 slot_engine，删除硬编码重复正则）。

        槽位定义（产品/数量/五阶段 stage/库名/供应商/工单号等）全部存 SLOT-DEFS
        （DB 可训练，stage value_map 统一 INV-STAGE 5 态）；此处仅保留仓储特有的
        兜底——无单位裸数字视为数量（剔除产品码后取首个数字，历史行为保留）。
        """
        from prog.runtime.slot_engine import extract_slots as _se
        slots: Dict[str, Any] = _se(user_input) if _se is not None else {}

        # 兜底：无单位裸数字数量（剔除产品码避免 A-202 的 202 被误当数量）
        if "quantity" not in slots:
            stripped = re.sub(r'[A-Za-z]-?\d{3}', '', user_input)
            num_match = re.search(r'(\d+)', stripped)
            if num_match:
                slots["quantity"] = int(num_match.group(1))

        return slots

    def _merge_slots(self, context: Dict[str, Any], new_slots: Dict[str, Any]) -> Dict[str, Any]:
        """合并 context 权威槽位与 NL 提取槽位（S7 修复，对齐 production_agent 双轨制）。

        背景：原实现用 new_slots 直接覆盖 context 已收集槽位——REST 链路
        （context["slots_authoritative"]=True）以 context 为权威，覆盖式更新会
        丢弃协调器注入的权威槽位；对话多轮记忆也会被覆盖。合并规则：

        - REST 链路（slots_authoritative=True）：context["slots"] 为权威值，
          覆盖 NL 提取结果（与 M3 production_agent B5 修复同构）；
        - 对话链路：NL 本轮提取值优先（支持用户改口），context 已收集槽位
          仅补 NL 未提取到的键（多轮记忆兜底）。
        """
        ctx_slots = context.get("slots") if isinstance(
            context.get("slots"), dict) else {}
        ctx_slots = {k: v for k, v in ctx_slots.items()
                     if v not in (None, "", 0)} or {}
        if not ctx_slots:
            return dict(new_slots)
        if context.get("slots_authoritative"):
            merged = dict(new_slots)
            merged.update(ctx_slots)
            return merged
        merged = dict(ctx_slots)
        merged.update({k: v for k, v in new_slots.items()
                       if v not in (None, "", 0)})
        return merged

    def _check_slots_complete(self, slots: Dict[str, Any], intent: str) -> list:
        """检查槽位完整性（v6.43：必填槽位列表可训练，来自 DB(SLOT-DEFS).required_rules）。

        参数：
            slots: 槽位字典
            intent: 子意图（stock_in / stock_out 等）

        返回：
            list: 缺失的字段名列表
        """
        from prog.runtime.slot_engine import check_slots_complete as _check
        if _check is not None:
            return _check(slots, intent)
        # 降级：优先从 DB(SLOT-DEFS).required_rules 读取（可训练）；
        # 仅 DB 亦不可用时才兜底内置默认（P6 修复：消除硬编码必填字段与
        # 可训练 required_rules 并存的口径分歧）
        try:
            from prog.runtime.slot_engine import get_required_slots
            return get_required_slots(intent)
        except Exception:
            pass
        if intent in ("stock_in", "stock_out"):
            return [f for f in ("product_code", "quantity") if not slots.get(f)]
        return []

    # --------------------------------------------------------
    # 提示词构建
    # --------------------------------------------------------
    def _build_prompt(self, user_input: str, context: Dict[str, Any]) -> str:
        """构建仓储Agent专用提示词，注入库存数据、BOM信息、供应商信息。"""
        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        slots = context.get("slots", {}) if isinstance(context.get("slots"), dict) else {}

        inventory_text = self._load_inventory_for_prompt()
        suppliers_text = self._load_suppliers_for_prompt()

        history = context.get("history", [])
        history_text = ""
        if history:
            ctx_items = []
            for h in history[-2:]:
                if isinstance(h, dict):
                    if h.get("user"):
                        ctx_items.append(f"用户：{h['user']}")
                    if h.get("ai"):
                        ctx_items.append(f"AI：{h['ai'][:100]}")
            history_text = "\n".join(ctx_items) if ctx_items else "（无历史对话）"
        else:
            history_text = "（无历史对话）"

        slots_text = "、".join(f"{k}={v}" for k, v in slots.items()) if slots else "（暂无）"

        prompt = f"""你是「仓储Agent」，AI工厂管家的仓储与采购助手，负责库存管理、出入库操作、缺料检查与采购申请。

## 用户身份
- 姓名：{user_info.get('title', '') if isinstance(user_info, dict) else ''}（{user_info.get('name', '') if isinstance(user_info, dict) else ''}）
- 工号：{user_info.get('id', '') if isinstance(user_info, dict) else ''} | 部门：{user_info.get('department', '') if isinstance(user_info, dict) else ''}

## 用户权限（严格遵守）
- 可操作入库：{perms.get('can_stock_in', False)}
- 可操作出库：{perms.get('can_stock_out', False)}
- 可发起采购：{perms.get('can_purchase', False)}
- 可查看成本：{perms.get('can_view_cost', False)}

## 库存五阶段
原材料仓 -> 在制-机加工 -> 在制-阳极氧化 -> 在制-质检 -> 成品仓

## 库存概览
{inventory_text}

## 供应商信息
{suppliers_text}

## 已收集信息（槽位）
{slots_text}

## 最近对话上下文
{history_text}

## 回复规范
1. 用自然、专业的中文回复
2. 严格遵守权限：无权限操作时明确告知
3. 出入库操作需确认物料编码、数量、阶段
4. 缺料检查需关联BOM需求
5. 回复控制在300字以内，重点突出

## 用户输入
{user_input}
"""
        return prompt

    def _load_inventory_for_prompt(self) -> str:
        """加载库存概览用于注入提示词。"""
        if self.database is not None:
            try:
                items = self.database.query_many("inventory", limit=10, order_by="product_code") or []
                if items:
                    lines = []
                    for it in items:
                        lines.append(
                            f"  - {it.get('product_code', '?')}："
                            f"原材料{it.get('raw', 0)} | "
                            f"在制{it.get('wip_cnc', 0)} | "
                            f"成品{it.get('finished', 0)}"
                        )
                    return "\n".join(lines)
            except Exception:
                pass
        # v6.47：移除静态 mock 库存兜底——库存数据须来自 inventory 表（训练/录入）
        return "暂无库存数据，请先录入或训练。"

    def _load_suppliers_for_prompt(self) -> str:
        """加载供应商信息用于注入提示词。"""
        if self.database is not None:
            try:
                suppliers = self.database.query_many("suppliers", limit=10) or []
                if suppliers:
                    lines = []
                    for s in suppliers:
                        # suppliers 表列名为 supplier_name（非 name）
                        lines.append(
                            f"  - {s.get('supplier_name', s.get('name', '?'))}"
                            f"（{s.get('supplier_id', '?')}）："
                            f"供货周期{s.get('lead_time_days', '?')}天"
                        )
                    return "\n".join(lines)
            except Exception:
                pass
        # v6.89：移除静态 mock 供应商兜底——供应商须来自 suppliers 表（训练/录入）
        return "暂无供应商数据，请先录入或训练。"

    # --------------------------------------------------------
    # W-01 库存查询（实时库存多维度可视化）
    # --------------------------------------------------------
    def _handle_inventory_query(self, user_input: str,
                                context: Dict[str, Any]) -> AgentResponse:
        """处理库存查询意图（W-01）。展示五阶段分布、库存价值、安全库存预警。

        v6.65 增强：查询支持附加词（产品名称/数值参数/日期/状态）——
        无精确产品码时按附加词过滤查询多条记录；渲染风格与查询流程
        卡片一致（info-card/card-row 体系）。
        """
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)
        product_code = slots.get("product_code")

        is_overview = any(k in user_input for k in ["总览", "全部", "所有", "整体", "概览", "库存分布", "库存价值"])

        if is_overview:
            inventory_list = self._query_all_inventory_from_db()
            content = self._format_inventory_overview(inventory_list)
            total_value = self._calculate_total_inventory_value(inventory_list)
            content += f"<div class='card-row'><span class='label'>库存总价值</span><span class='value gold'>¥{total_value:,.2f}</span></div>"
            return AgentResponse(
                content=content, action="inventory_overview",
                agent_name=self.agent_name,
                data={"inventory_list": inventory_list, "total_value": total_value},
            )

        if not product_code:
            # v6.65：无精确产品码时，尝试解析附加词（如"库存大于100的"、
            # "近7天入库的"）按条件过滤查询，否则引导提供产品型号
            filtered = self._query_inventory_by_extra(user_input)
            if filtered is not None:
                return filtered
            return AgentResponse(
                content="<div class='muted'>好的，请告诉我具体想查哪个产品的库存（如 A-202、B-305、M-101），或者描述一下条件（如「库存大于100的产品」），我这就帮您查。</div>",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots},
            )

        inventory = self._query_inventory_from_db(product_code)
        if inventory:
            content = self._format_inventory_detail(inventory)
            safety_stock = inventory.get("safety_stock", 0)
            total_available = inventory.get("raw", 0) + inventory.get("finished", 0)
            if safety_stock and total_available < safety_stock:
                content += f"<div class='warn'>⚠️ 预警：可用库存（{total_available}）低于安全库存（{safety_stock}），建议及时采购。</div>"
            unit_cost = inventory.get("unit_cost", 0) or inventory.get("raw_value", 0)
            total_qty = sum(inventory.get(s, 0) for s in _inv_stages())
            if unit_cost:
                content += f"<div class='card-row'><span class='label'>库存价值</span><span class='value gold'>¥{total_qty * unit_cost:,.2f}</span></div>"
            return AgentResponse(
                content=content, action="inventory_detail",
                agent_name=self.agent_name,
                data={"product_code": product_code, "inventory": inventory},
            )
        return AgentResponse(
            content=f"<div class='muted'>未找到产品 {product_code} 的库存记录。</div>",
            action="not_found", agent_name=self.agent_name,
            data={"product_code": product_code},
        )

    def _query_inventory_from_db(self, product_code: str) -> Dict[str, Any]:
        """从数据库查询单个产品的五阶段库存。键名与DB列名一致。

        v6.89：移除静态 mock 兜底——库存数据须来自 inventory 表（训练/录入），
        空库返回空 dict（调用方按 not_found / 数量为 0 处理）。
        """
        if self.database is not None:
            try:
                inv = self.database.query_one("inventory", {"product_code": product_code})
                if inv:
                    return inv
            except Exception:
                pass
        return {}

    def _query_all_inventory_from_db(self) -> List[Dict[str, Any]]:
        """从数据库查询全部库存概览。"""
        if self.database is not None:
            try:
                items = self.database.query_many("inventory", limit=50, order_by="product_code") or []
                if items:
                    return items
            except Exception:
                pass
        # v6.47：移除静态 mock 库存兜底——库存数据须来自 inventory 表（训练/录入）
        return []

    def _query_inventory_by_extra(self,
                                  user_input: str) -> Optional[AgentResponse]:
        """v6.65：按附加词过滤查询库存（无精确产品码时）。

        规则解析（零延迟）：数值参数（库存大于100）/ 日期（近7天）/
        状态词 / 产品名称模糊；规则未覆盖的模糊片段调用 LLM 补全。
        有附加条件 → 返回过滤结果卡片（渲染风格与查询流程一致）；
        无附加条件 → 返回 None（调用方引导提供产品型号）。

        附加词与库存表列的映射：
            raw/finished（五阶段数量）、name（产品名）、created_at（日期）。
        """
        try:
            from prog.runtime.query_param_parser import (
                parse_query_filters, llm_complete_filters, filters_to_human)
            parsed = parse_query_filters(user_input)
            filters = list(parsed.get("filters") or [])
            notes = list(parsed.get("notes") or [])
            fuzzy = parsed.get("fuzzy")
            # LLM 补全模糊片段（如"铝外壳的"→ product_name like 等）；
            # 传库存表字段清单，LLM 才知道"铝合金外壳"应映射到 name/product_name
            if fuzzy:
                filled = llm_complete_filters(fuzzy, self._call_llm,
                                              table_hint="inventory",
                                              fields=["product_code", "name",
                                                      "raw", *(_inv_stages() or []),
                                                      "safety_stock",
                                                      "created_at",
                                                      "updated_at"])
                if filled:
                    filters.extend(filled)
                    _human = filters_to_human(filled)
                    if _human:
                        notes.append(f"（LLM识别）{_human}")
            if not filters:
                return None
        except Exception:
            return None

        db = self._get_db()
        if db is None or not hasattr(db, "query_filtered"):
            return None
        # v6.65.1：字段适配——产品名条件（inventory 无产品名列）反查
        # products 转 product_code IN(...)，避免 SQL UndefinedColumn
        try:
            from prog.runtime.query_param_parser import adapt_filters_to_table
            filters, adapt_notes = adapt_filters_to_table(filters, "inventory", db)
            notes.extend(adapt_notes)
        except Exception:
            pass
        if not filters:
            cond = "；".join(notes) if notes else "所述条件"
            return AgentResponse(
                content=f"<div class='muted'>未找到满足条件（{cond}）的库存记录。</div>",
                action="not_found", agent_name=self.agent_name,
                data={"filters": filters, "notes": notes, "items": []},
            )
        try:
            items = db.query_filtered("inventory", filters, limit=50,
                                      order_by="product_code") or []
        except Exception:
            return None
        if not items:
            cond = "；".join(notes) if notes else "所述条件"
            return AgentResponse(
                content=f"<div class='muted'>未找到满足条件（{cond}）的库存记录。</div>",
                action="not_found", agent_name=self.agent_name,
                data={"filters": filters, "notes": notes, "items": []},
            )

        # 渲染：与查询流程卡片一致的 info-card/card-row 体系
        rows = []
        for inv in items:
            code = inv.get("product_code", "?")
            name = inv.get("name", "")
            raw = inv.get("raw", 0)
            wip = sum(inv.get(s, 0) for s in _inv_stages()
                      if s not in ("raw", "finished"))
            finished = inv.get("finished", 0)
            total = raw + wip + finished
            rows.append(
                f"<div class='card-row'><span class='label'>{code}"
                f"{(' ' + name) if name else ''}</span>"
                f"<span class='value'>原材料{raw} · 在制{wip} · "
                f"成品{finished} · <strong class='highlight'>共{total}</strong></span></div>"
            )
        cond_line = "；".join(notes) if notes else "附加条件"
        content = (
            f"<div class='section-title'>📦 库存筛选查询</div>"
            f"<div class='tip-inline'>🔎 附加条件：{cond_line}</div>"
            f"<div class='info-card'>" + "".join(rows)
            + f"<div class='card-row'><span class='label'>匹配记录</span>"
            f"<span class='value'>{len(items)} 条</span></div></div>"
        )
        return AgentResponse(
            content=content, action="inventory_filtered",
            agent_name=self.agent_name,
            data={"filters": filters, "notes": notes, "items": items},
        )

    def _format_inventory_detail(self, inventory: Dict[str, Any]) -> str:
        """格式化单个产品库存详情为HTML卡片。"""
        code = inventory.get("product_code", "?")
        name = inventory.get("name", "")
        title = f"📦 {name}（{code}）库存详情" if name else f"📦 产品 {code} 库存详情"

        stages = []
        for stage in _inv_stages():
            qty = inventory.get(stage, 0)
            stages.append(
                f"<div class='card-row'><span class='label'>{_stage_names().get(stage, stage)}</span>"
                f"<span class='value'>{qty}</span></div>"
            )
        total = sum(inventory.get(s, 0) for s in _inv_stages())
        safety = inventory.get("safety_stock", 0)

        html = f"<div class='section-title'>{title}</div>"
        html += "<div class='kpi-grid'>"
        html += f"<div class='kpi-item'><div class='kpi-val'>{inventory.get('raw', 0)}</div><div class='kpi-label'>原材料</div></div>"
        wip_total = sum(inventory.get(s, 0) for s in _inv_stages() if s != "raw" and s != "finished")
        html += f"<div class='kpi-item'><div class='kpi-val'>{wip_total}</div><div class='kpi-label'>在制品</div></div>"
        html += f"<div class='kpi-item'><div class='kpi-val'>{inventory.get('finished', 0)}</div><div class='kpi-label'>成品</div></div>"
        html += "</div>"
        html += "<div class='info-card'>"
        html += "".join(stages)
        html += f"<div class='card-row'><span class='label'><strong class='highlight'>库存总量</strong></span><span class='value'><strong class='highlight'>{total}</strong></span></div>"
        if safety:
            html += f"<div class='card-row'><span class='label'>安全库存</span><span class='value'>{safety}</span></div>"
        html += "</div>"
        html += "<span class='muted'>数据来源：WMS系统实时同步</span>"
        return html

    def _format_inventory_overview(self, inventory_list: List[Dict[str, Any]]) -> str:
        """格式化库存概览为HTML表格。"""
        if not inventory_list:
            # v6.68：缺少对应训练结果时提示"暂无X流程"，不任选执行路径
            return "<div class='muted'>暂无库存查询流程。如需要，请先通过训练建立库存查询流程（录入库存数据）。</div>"
        html = "<div class='section-title'>📦 库存总览</div>"
        html += "<div class='info-card'>"
        html += "<table><thead><tr><th>产品</th><th>原材料</th><th>在制</th><th>成品</th><th>总量</th><th>安全库存</th><th>状态</th></tr></thead><tbody>"
        for inv in inventory_list:
            raw = inv.get("raw", 0)
            wip = inv.get("wip_cnc", 0) + inv.get("wip_anode", 0) + inv.get("wip_qc", 0)
            finished = inv.get("finished", 0)
            total = raw + wip + finished
            name = inv.get("name", "")
            safety = inv.get("safety_stock", 0)
            code = inv.get("product_code", "?")
            status = "<span class='warn'>⚠️ 偏低</span>" if safety and (raw + finished) < safety else "<span class='highlight'>✓ 正常</span>"
            html += f"<tr><td><strong>{code}</strong><br><span class='muted'>{name}</span></td><td>{raw}</td><td>{wip}</td><td>{finished}</td><td><strong>{total}</strong></td><td>{safety}</td><td>{status}</td></tr>"
        html += "</tbody></table></div>"
        return html

    def _calculate_total_inventory_value(self, inventory_list: List[Dict[str, Any]]) -> float:
        """计算库存总价值。"""
        total_value = 0.0
        for inv in inventory_list:
            unit_cost = inv.get("unit_cost", 0) or 0
            total_qty = sum(inv.get(s, 0) for s in _inv_stages())
            total_value += total_qty * unit_cost
        return total_value

    # --------------------------------------------------------
    # W-02 订单确认后事务级库存扣减
    # --------------------------------------------------------
    def deduct_for_order(self, product_code: str, quantity: int,
                         operator: str = "", order_id: str = "") -> Dict[str, Any]:
        """订单确认后事务级库存扣减（W-02）。

        订单确认时自动扣减原材料、增加在制品（raw -> wip_cnc），
        使用乐观锁+事务保证原子性。
        """
        try:
            movement_id = self.transfer_stage(
                product_code, quantity, "raw", "wip_cnc", operator, order_id
            )
            updated_inv = self._query_inventory_from_db(product_code)
            return {"success": True, "movement_id": movement_id,
                    "product_code": product_code, "quantity": quantity,
                    "inventory": updated_inv}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --------------------------------------------------------
    # W-03 安全库存阈值监控与缺料预警
    # --------------------------------------------------------
    def check_safety_stock(self) -> List[Dict[str, Any]]:
        """安全库存阈值监控（W-03）。扫描全部库存，返回低于安全库存的物料清单。"""
        inventory_list = self._query_all_inventory_from_db()
        alerts = []
        for inv in inventory_list:
            safety = inv.get("safety_stock", 0) or 0
            if not safety:
                continue
            available = inv.get("raw", 0) + inv.get("finished", 0)
            if available < safety:
                alerts.append({
                    "product_code": inv.get("product_code", ""),
                    "name": inv.get("name", ""),
                    "available": available, "safety_stock": safety,
                    "shortage": safety - available,
                })
        return alerts

    # --------------------------------------------------------
    # 入库
    # --------------------------------------------------------
    def _handle_stock_in(self, user_input: str,
                         context: Dict[str, Any]) -> AgentResponse:
        """处理入库意图。校验阶段合法性，记录流水，使用乐观锁。"""
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)

        # v6.43：必填列表可训练（DB(SLOT-DEFS).required_rules["stock_in"]）
        missing = self._check_slots_complete(slots, "stock_in")
        if missing:
            display = {"product_code": "产品/物料编码", "quantity": "数量"}
            parts = [display.get(k, k) for k in missing]
            return AgentResponse(
                content=f"入库操作还需要以下信息：{'、'.join(parts)}。请补充完整。",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots, "missing": missing},
            )

        product_code = slots["product_code"]
        quantity = slots["quantity"]
        stage = slots.get("stage", "raw")
        if any(k in user_input for k in ["成品入库", "生产入库", "完工入库"]):
            stage = "finished"

        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        # W4 修复：权限默认拒绝（原 True 兜底放行导致无权限用户可入库）
        if not perms.get("can_stock_in", False):
            return AgentResponse(
                content="您没有入库操作权限，请联系仓储部。",
                action="permission_denied", agent_name=self.agent_name,
            )

        rule_data = {"product_code": product_code, "quantity": quantity,
                     "stage": stage, "operation": "stock_in", "user_permissions": perms}
        rule_result = self._apply_rules(rule_data)
        blocked = getattr(rule_result, "blocked", False)
        if blocked:
            message = getattr(rule_result, "message", "")
            rule_name = getattr(rule_result, "rule_name", "")
            return AgentResponse(
                content=f"入库被规则阻断：{message}", action="blocked",
                rules_violated=[rule_name] if rule_name else [],
                agent_name=self.agent_name, data={"slots": slots},
            )

        try:
            movement_id = self._execute_stock_in(product_code, quantity, stage, context)
            updated_inv = self._query_inventory_from_db(product_code)
            content = (
                f"入库成功！流水号：{movement_id}\n"
                f"产品：{product_code}，数量：{quantity}，阶段：{_stage_names().get(stage, stage)}\n"
                f"更新后库存：\n{self._format_inventory_detail(updated_inv)}"
            )
            return AgentResponse(
                content=content, action="stock_in_success",
                agent_name=self.agent_name,
                data={"movement_id": movement_id, "product_code": product_code,
                      "quantity": quantity, "stage": stage, "inventory": updated_inv},
            )
        except Exception as e:
            return AgentResponse(
                content=f"入库操作失败：{str(e)}", action="error",
                agent_name=self.agent_name, data={"slots": slots},
            )

    def _execute_stock_in(self, product_code: str, quantity: int, stage: str,
                          context: Dict[str, Any]) -> str:
        """执行入库操作：更新库存（乐观锁）+ 记录流水。"""
        # S5 修复：stage 拼入 SQL 前做列名白名单校验（防注入）
        if stage not in INVENTORY_STAGE_COLUMNS:
            raise ValueError(f"非法库存阶段：{stage}")
        db = self._get_db()
        if db is None:
            return f"IM{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        now = datetime.now()
        # W1 修复：movement_id 加 %f 毫秒，避免同一秒多笔流水 ID 冲突
        movement_id = f"IM{now.strftime('%Y%m%d%H%M%S%f')}"

        with db.transaction() as session:
            from sqlalchemy import text

            row = session.execute(
                text("SELECT * FROM inventory WHERE product_code = :pc FOR UPDATE"),
                {"pc": product_code}
            ).fetchone()

            if row:
                inv = dict(row._mapping)
                current_version = inv.get("version", 1)
                current_qty = inv.get(stage, 0)
                new_qty = current_qty + quantity

                result = session.execute(
                    text(f"UPDATE inventory SET {stage} = :new_qty, version = version + 1 "
                         f"WHERE product_code = :pc AND version = :ver"),
                    {"new_qty": new_qty, "pc": product_code, "ver": current_version}
                )
                if result.rowcount == 0:
                    raise Exception("库存并发冲突，请重试")
            else:
                session.execute(
                    text(f"INSERT INTO inventory (product_code, {stage}, version, created_at) "
                         f"VALUES (:pc, :qty, 1, :now)"),
                    {"pc": product_code, "qty": quantity, "now": now}
                )

            user_info = context.get("user", {}) if isinstance(context, dict) else {}
            operator = user_info.get("name", "") if isinstance(user_info, dict) else ""
            session.execute(
                text("INSERT INTO inventory_movements "
                     "(movement_id, product_code, movement_type, from_stage, to_stage, "
                     "quantity, operator, reference_no, created_at) VALUES "
                     "(:mid, :pc, :mtype, :from_s, :to_s, :qty, :op, :ref, :now)"),
                {"mid": movement_id, "pc": product_code, "mtype": "stock_in",
                 "from_s": "", "to_s": stage, "qty": quantity, "op": operator,
                 "ref": "", "now": now}
            )

        # v6.72 审计补记：原生 SQL 写操作不触发 DatabaseManager 审计钩子，
        # 此处手动记录，保证 operation_logs 覆盖库存变动全量写路径
        try:
            db._audit_write("update", "inventory", {
                "product_code": product_code, "stage": stage, "quantity": quantity,
                "version": "version + 1"})
            db._audit_write("insert", "inventory_movements", {
                "movement_id": movement_id, "product_code": product_code,
                "movement_type": "stock_in", "quantity": quantity})
        except Exception:
            pass  # 审计降级：不阻断业务

        return movement_id

    # --------------------------------------------------------
    # 出库
    # --------------------------------------------------------
    def _handle_stock_out(self, user_input: str,
                          context: Dict[str, Any]) -> AgentResponse:
        """处理出库意图。严格校验出库量不超过可用量，使用乐观锁防止超卖。"""
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)

        # v6.43：必填列表可训练（DB(SLOT-DEFS).required_rules["stock_out"]）
        missing = self._check_slots_complete(slots, "stock_out")
        if missing:
            display = {"product_code": "产品/物料编码", "quantity": "数量"}
            parts = [display.get(k, k) for k in missing]
            return AgentResponse(
                content=f"出库操作还需要以下信息：{'、'.join(parts)}。请补充完整。",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots, "missing": missing},
            )

        product_code = slots["product_code"]
        quantity = slots["quantity"]
        stage = slots.get("stage", "finished")
        if any(k in user_input for k in ["生产领料", "领料", "领用于生产"]):
            stage = "raw"
        elif any(k in user_input for k in ["销售出库", "发货", "出货"]):
            stage = "finished"

        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        # W4 修复：权限默认拒绝（原 True 兜底放行导致无权限用户可出库）
        if not perms.get("can_stock_out", False):
            return AgentResponse(
                content="您没有出库操作权限，请联系仓储部。",
                action="permission_denied", agent_name=self.agent_name,
            )

        inventory = self._query_inventory_from_db(product_code)
        current_qty = inventory.get(stage, 0) if inventory else 0
        if current_qty < quantity:
            return AgentResponse(
                content=(f"出库失败：{_stage_names().get(stage, stage)}库存不足。\n"
                         f"产品：{product_code}，当前库存：{current_qty}，申请出库：{quantity}"),
                action="insufficient_stock", agent_name=self.agent_name,
                data={"product_code": product_code, "quantity": quantity,
                      "stage": stage, "current_qty": current_qty},
            )

        rule_data = {"product_code": product_code, "quantity": quantity,
                     "stage": stage, "operation": "stock_out",
                     "current_qty": current_qty, "user_permissions": perms}
        rule_result = self._apply_rules(rule_data)
        blocked = getattr(rule_result, "blocked", False)
        if blocked:
            message = getattr(rule_result, "message", "")
            rule_name = getattr(rule_result, "rule_name", "")
            return AgentResponse(
                content=f"出库被规则阻断：{message}", action="blocked",
                rules_violated=[rule_name] if rule_name else [],
                agent_name=self.agent_name, data={"slots": slots},
            )

        try:
            movement_id = self._execute_stock_out(product_code, quantity, stage, context)
            updated_inv = self._query_inventory_from_db(product_code)
            content = (
                f"出库成功！流水号：{movement_id}\n"
                f"产品：{product_code}，数量：{quantity}，阶段：{_stage_names().get(stage, stage)}\n"
                f"更新后库存：\n{self._format_inventory_detail(updated_inv)}"
            )
            return AgentResponse(
                content=content, action="stock_out_success",
                agent_name=self.agent_name,
                data={"movement_id": movement_id, "product_code": product_code,
                      "quantity": quantity, "stage": stage, "inventory": updated_inv},
            )
        except Exception as e:
            return AgentResponse(
                content=f"出库操作失败：{str(e)}", action="error",
                agent_name=self.agent_name, data={"slots": slots},
            )

    def _execute_stock_out(self, product_code: str, quantity: int, stage: str,
                           context: Dict[str, Any]) -> str:
        """执行出库操作：更新库存（乐观锁）+ 记录流水。"""
        # S5 修复：stage 拼入 SQL 前做列名白名单校验（防注入）
        if stage not in INVENTORY_STAGE_COLUMNS:
            raise ValueError(f"非法库存阶段：{stage}")
        db = self._get_db()
        if db is None:
            return f"IM{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        now = datetime.now()
        # W1 修复：movement_id 加 %f 毫秒，避免同一秒多笔流水 ID 冲突
        movement_id = f"IM{now.strftime('%Y%m%d%H%M%S%f')}"

        with db.transaction() as session:
            from sqlalchemy import text

            row = session.execute(
                text("SELECT * FROM inventory WHERE product_code = :pc FOR UPDATE"),
                {"pc": product_code}
            ).fetchone()

            if not row:
                raise Exception(f"产品 {product_code} 无库存记录")

            inv = dict(row._mapping)
            current_version = inv.get("version", 1)
            current_qty = inv.get(stage, 0)

            if current_qty < quantity:
                raise Exception(f"{_stage_names().get(stage, stage)}库存不足，当前{current_qty}，需出库{quantity}")

            new_qty = current_qty - quantity

            result = session.execute(
                text(f"UPDATE inventory SET {stage} = :new_qty, version = version + 1 "
                     f"WHERE product_code = :pc AND version = :ver"),
                {"new_qty": new_qty, "pc": product_code, "ver": current_version}
            )
            if result.rowcount == 0:
                raise Exception("库存并发冲突，请重试")

            user_info = context.get("user", {}) if isinstance(context, dict) else {}
            operator = user_info.get("name", "") if isinstance(user_info, dict) else ""
            session.execute(
                text("INSERT INTO inventory_movements "
                     "(movement_id, product_code, movement_type, from_stage, to_stage, "
                     "quantity, operator, reference_no, created_at) VALUES "
                     "(:mid, :pc, :mtype, :from_s, :to_s, :qty, :op, :ref, :now)"),
                {"mid": movement_id, "pc": product_code, "mtype": "stock_out",
                 "from_s": stage, "to_s": "", "qty": quantity, "op": operator,
                 "ref": "", "now": now}
            )

        # v6.72 审计补记：原生 SQL 写操作不触发 DatabaseManager 审计钩子
        try:
            db._audit_write("update", "inventory", {
                "product_code": product_code, "stage": stage, "quantity": quantity,
                "version": "version + 1"})
            db._audit_write("insert", "inventory_movements", {
                "movement_id": movement_id, "product_code": product_code,
                "movement_type": "stock_out", "quantity": quantity})
        except Exception:
            pass  # 审计降级：不阻断业务

        return movement_id

    # --------------------------------------------------------
    # W-05 库存阶段流转 + 物料追踪
    # --------------------------------------------------------
    def transfer_stage(self, product_code: str, quantity: int,
                       from_stage: str, to_stage: str,
                       operator: str = "", reference_no: str = "") -> str:
        """库存阶段流转（W-05，供其他Agent调用）。

        实现五阶段间的库存流转（raw->wip_cnc->wip_anode->wip_qc->finished），
        使用乐观锁保证并发安全，同时记录流水。
        """
        # S5 修复：from_stage/to_stage 拼入 SQL 前做列名白名单校验（防注入）
        if from_stage not in INVENTORY_STAGE_COLUMNS or to_stage not in INVENTORY_STAGE_COLUMNS:
            raise ValueError(f"无效的库存阶段：{from_stage} -> {to_stage}")

        from_idx = _inv_stages().index(from_stage)
        to_idx = _inv_stages().index(to_stage)
        # S4 修复：仅允许相邻正向流转（与 API INVENTORY_STAGE_FLOW / 测试
        # test_transfer_stage_skip_stage_400 对齐）——原 to_idx <= from_idx
        # 会放行跳级（raw->finished 索引差 4），此处收紧为严格相邻
        if to_idx != from_idx + 1:
            raise ValueError(f"阶段流转顺序不合法：{from_stage} -> {to_stage}，仅允许相邻正向流转")

        db = self._get_db()
        if db is None:
            return f"IM{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

        now = datetime.now()
        # W1 修复：movement_id 加 %f 毫秒，避免同一秒多笔流水 ID 冲突
        movement_id = f"IM{now.strftime('%Y%m%d%H%M%S%f')}"

        with db.transaction() as session:
            from sqlalchemy import text

            row = session.execute(
                text("SELECT * FROM inventory WHERE product_code = :pc FOR UPDATE"),
                {"pc": product_code}
            ).fetchone()

            if not row:
                raise Exception(f"产品 {product_code} 无库存记录")

            inv = dict(row._mapping)
            current_version = inv.get("version", 1)
            from_qty = inv.get(from_stage, 0)

            if from_qty < quantity:
                raise Exception(
                    f"{_stage_names().get(from_stage, from_stage)}库存不足，"
                    f"当前{from_qty}，需流转{quantity}"
                )

            new_from_qty = from_qty - quantity
            to_qty = inv.get(to_stage, 0)
            new_to_qty = to_qty + quantity

            result = session.execute(
                text(f"UPDATE inventory SET {from_stage} = :from_qty, {to_stage} = :to_qty, "
                     f"version = version + 1 WHERE product_code = :pc AND version = :ver"),
                {"from_qty": new_from_qty, "to_qty": new_to_qty,
                 "pc": product_code, "ver": current_version}
            )
            if result.rowcount == 0:
                raise Exception("库存并发冲突，请重试")

            session.execute(
                text("INSERT INTO inventory_movements "
                     "(movement_id, product_code, movement_type, from_stage, to_stage, "
                     "quantity, operator, reference_no, created_at) VALUES "
                     "(:mid, :pc, :mtype, :from_s, :to_s, :qty, :op, :ref, :now)"),
                {"mid": movement_id, "pc": product_code, "mtype": "transfer",
                 "from_s": from_stage, "to_s": to_stage, "qty": quantity,
                 "op": operator, "ref": reference_no, "now": now}
            )

        # v6.72 审计补记：原生 SQL 写操作不触发 DatabaseManager 审计钩子
        try:
            db._audit_write("update", "inventory", {
                "product_code": product_code, "from_stage": from_stage,
                "to_stage": to_stage, "quantity": quantity, "version": "version + 1"})
            db._audit_write("insert", "inventory_movements", {
                "movement_id": movement_id, "product_code": product_code,
                "movement_type": "transfer", "quantity": quantity})
        except Exception:
            pass  # 审计降级：不阻断业务

        return movement_id

    def track_material_movements(self, product_code: str,
                                 limit: int = 20) -> List[Dict[str, Any]]:
        """物料全生命周期移动追踪（W-05）。

        查询指定物料的全部出入库流水记录，追踪五阶段流转历史。
        """
        db = self._get_db()
        if db is not None:
            try:
                movements = db.query_many(
                    "inventory_movements", {"product_code": product_code},
                    limit=limit, order_by="created_at DESC",
                ) or []
                return movements
            except Exception:
                pass
        # v6.63：移除静态 mock 流水兜底（对齐 v6.46 D2 原则）——物料移动记录
        # 须来自 inventory_movements 表（录入/训练），空库返回空列表
        return []

    def _handle_material_trace(self, user_input: str,
                               context: Dict[str, Any]) -> AgentResponse:
        """处理物料移动追踪意图（W-05，v6.63 挂入 process 分发）。

        查询指定物料的出入库流水，呈现五阶段流转记录。
        """
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)
        product_code = slots.get("product_code")
        if not product_code:
            return AgentResponse(
                content="<div class='muted'>请提供要追踪的物料/产品型号（如A-202、M-101）。</div>",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots},
            )
        movements = self.track_material_movements(product_code, limit=20)
        # M4：追加批次维度——inventory.batch_id / 流水批次信息 → 批次全链路
        # （batches/batch_genealogy；迁移未执行或 DB 不可达降级返回空结构，不破坏现有行为）
        batch_dimension = self._query_batch_dimension(product_code, movements)
        if not movements:
            return AgentResponse(
                content=(f"<div class='muted'>暂无 {product_code} 的出入库流水记录"
                         f"（物料移动数据须来自 inventory_movements 表的录入/训练）。</div>"),
                action="not_found", agent_name=self.agent_name,
                data={"product_code": product_code, "movements": [],
                      "batch_dimension": batch_dimension},
            )
        _mtype_names = {"stock_in": "入库", "stock_out": "出库",
                        "transfer": "阶段流转", "adjust": "库存调整",
                        "return": "退货"}
        lines = [
            f"<div class='card-row'><span class='label'>物料型号</span>"
            f"<span class='value'>{product_code}</span></div>",
            f"<div class='card-row'><span class='label'>流水记录</span>"
            f"<span class='value'>{len(movements)} 条</span></div>",
        ]
        for m in movements:
            mtype = _mtype_names.get(m.get("movement_type", ""),
                                     m.get("movement_type", ""))
            lines.append(
                f"<div class='card-row'><span class='label'>{mtype} · {m.get('created_at', '')}</span>"
                f"<span class='value'>数量 {m.get('quantity', 0)} · {m.get('operator', '')}"
                f" · {m.get('reference_no', '')}</span></div>"
            )
        content = "\n".join(lines)
        return AgentResponse(
            content=content, action="material_trace",
            agent_name=self.agent_name,
            data={"product_code": product_code, "movements": movements,
                  "batch_dimension": batch_dimension},
        )

    def _query_batch_dimension(self, product_code: str,
                               movements: List[Dict[str, Any]]) -> Dict[str, Any]:
        """M4：物料追溯追加批次维度。

        优先取 inventory.batch_id（082 迁移列），movements 的 extra_data 中
        携带批次号时一并记录；存在 batch_id 时展开批次全链路。任何异常
        （迁移未执行/DB 不可达）降级返回空结构，不破坏现有物料追溯行为。
        """
        dim: Dict[str, Any] = {"batch_id": None, "batch_no": None, "chain": None}
        try:
            db = self._get_db()
            if db is None:
                return dim
            inv = db.query_one("inventory", {"product_code": product_code})
            batch_id = (inv or {}).get("batch_id")
            if not batch_id:
                return dim
            dim["batch_id"] = batch_id
            # 流水 extra_data 中可能携带批次号（尽力而为，无则 None）
            for m in movements or []:
                ed = m.get("extra_data") or {}
                if isinstance(ed, str):
                    try:
                        import json as _json
                        ed = _json.loads(ed)
                    except Exception:
                        ed = {}
                if isinstance(ed, dict) and ed.get("batch_no"):
                    dim["batch_no"] = ed["batch_no"]
                    break
            dim["chain"] = self._expand_batch_chain(db, batch_id)
        except Exception:
            # 迁移未执行（表不存在）/ DB 不可达：降级，批次维度为空结构
            pass
        return dim

    def _expand_batch_chain(self, db: Any, batch_id: Any) -> Dict[str, Any]:
        """沿 batch_genealogy 展开批次全链路（正向子批次/反向父批次），防环。"""
        batch = db.query_one("batches", {"batch_id": batch_id})
        if not batch:
            return None
        parents = self._collect_chain_batches(db, "up", batch_id)
        children = self._collect_chain_batches(db, "down", batch_id)
        return {"batch": batch, "parents": parents, "children": children}

    def _collect_chain_batches(self, db: Any, direction: str,
                               root_id: Any) -> List[Dict[str, Any]]:
        """广度优先收集批次链上的批次（up=父系 / down=子系），seen 防环。"""
        found: List[Dict[str, Any]] = []
        seen = {root_id}
        queue = [root_id]
        while queue:
            bid = queue.pop(0)
            if direction == "down":
                edges = db.query_many(
                    "batch_genealogy", {"parent_batch_id": bid}) or []
                next_ids = [g.get("child_batch_id") for g in edges]
            else:
                edges = db.query_many(
                    "batch_genealogy", {"child_batch_id": bid}) or []
                next_ids = [g.get("parent_batch_id") for g in edges]
            for nid in next_ids:
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                b = db.query_one("batches", {"batch_id": nid})
                if b:
                    found.append(b)
                    queue.append(nid)
        return found

    # --------------------------------------------------------
    # W-04 多层BOM递归展开与缺料检查
    # --------------------------------------------------------
    def _handle_shortage_check(self, user_input: str,
                               context: Dict[str, Any]) -> AgentResponse:
        """处理缺料检查意图（W-04）。递归展开BOM，对比各层级物料需求与库存。"""
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)

        order_id = slots.get("order_id")
        work_order_id = slots.get("work_order_id")
        product_code = slots.get("product_code")
        quantity = slots.get("quantity", 0)

        if not order_id and not work_order_id and not (product_code and quantity):
            return AgentResponse(
                content="请提供要检查缺料的订单号（如SO2024001）、工单号（如WO2024001），或产品型号+数量。",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots},
            )

        bom_requirements = self._get_bom_requirements(product_code, quantity, order_id, work_order_id)

        if not bom_requirements:
            return AgentResponse(
                content="未找到对应的BOM信息，无法进行缺料检查。",
                action="not_found", agent_name=self.agent_name,
                data={"slots": slots},
            )

        rule_data = {"product_code": product_code, "bom_items": bom_requirements,
                     "operation": "shortage_check",
                     # v6.49 引擎数据契约：BOM-CHECK 引擎需要 data.order_items/material_id + data.product_code
                     "order_items": [
                         {"material_id": item.get("material_code", ""),
                          "quantity": item.get("quantity", 0)}
                         for item in bom_requirements
                     ]}
        rule_result = self._apply_rules(rule_data)
        blocked = getattr(rule_result, "blocked", False)
        if blocked:
            message = getattr(rule_result, "message", "")
            rule_name = getattr(rule_result, "rule_name", "")
            return AgentResponse(
                content=f"缺料检查被规则阻断：{message}", action="blocked",
                rules_violated=[rule_name] if rule_name else [],
                agent_name=self.agent_name, data={"slots": slots},
            )

        shortage_list = []
        for item in bom_requirements:
            material_code = item.get("material_code", "")
            required_qty = item.get("required_qty", 0)
            inventory = self._query_inventory_from_db(material_code)
            available_qty = 0
            if inventory:
                available_qty = inventory.get("raw", 0) + inventory.get("finished", 0)
            shortage = max(0, required_qty - available_qty)
            shortage_list.append({
                "material_code": material_code,
                "material_name": item.get("material_name", ""),
                "bom_level": item.get("bom_level", 1),
                "required_qty": required_qty,
                "available_qty": available_qty,
                "shortage_qty": shortage,
                "unit": item.get("unit", "个"),
            })

        actual_shortage = [s for s in shortage_list if s["shortage_qty"] > 0]
        content = self._format_shortage_result(shortage_list, actual_shortage, product_code, quantity)

        return AgentResponse(
            content=content, action="shortage_result",
            agent_name=self.agent_name,
            data={"shortage_list": shortage_list, "actual_shortage": actual_shortage,
                  "product_code": product_code, "quantity": quantity,
                  "order_id": order_id, "work_order_id": work_order_id},
        )

    def _query_bom_children(self, parent_code: str) -> List[Dict[str, Any]]:
        """查询产品的直接子件列表（bom表parent_product_code查询）。"""
        db = self._get_db()
        if db is not None:
            try:
                items = db.query_many("bom", {"parent_product_code": parent_code}) or []
                if items:
                    return items
            except Exception:
                pass
        # v6.46 D2：移除静态 mock BOM 兜底——BOM 结构须由训练/录入获得，
        # 空库返回空列表（调用方 _expand_bom_recursive 递归自然终止，并提示暂无 BOM 数据）
        return []

    def _expand_bom_recursive(self, product_code: str, order_qty: float = 1,
                              level: int = 1,
                              visited: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        """递归展开BOM，返回展平的物料需求列表（W-04核心算法）。

        算法（§1.7.2.7）：
        1. 从成品的BOM获取一级子件列表
        2. 对每个子件检查是否有自己的BOM
        3. 若有，递归展开下一层，bom_level递增
        4. 递归终止：子件在bom表中无记录（叶子节点）
        5. 累乘计算：总需求量 = 订单数量 × 各级用量 × (1 + 损耗率)
        """
        if visited is None:
            visited = set()
        if product_code in visited:
            return []  # 防止循环引用
        visited.add(product_code)

        children = self._query_bom_children(product_code)
        result: List[Dict[str, Any]] = []
        for child in children:
            child_code = child.get("child_product_code", "")
            quantity_per = child.get("quantity", 0) or 0
            scrap_rate = child.get("scrap_rate", 0) or 0
            total_qty = order_qty * quantity_per * (1 + scrap_rate)

            sub_children = self._expand_bom_recursive(child_code, total_qty, level + 1, visited)
            is_leaf = len(sub_children) == 0

            result.append({
                "material_code": child_code,
                "material_name": child.get("material_name", child_code),
                "bom_level": level,
                "quantity_per_unit": quantity_per,
                "required_qty": total_qty,
                "scrap_rate": scrap_rate,
                "is_critical": child.get("is_critical", False),
                "is_leaf": is_leaf,
                "unit": child.get("unit", "个"),
            })
            result.extend(sub_children)

        visited.discard(product_code)
        return result

    def _get_bom_requirements(self, product_code: Optional[str], quantity: int,
                              order_id: Optional[str] = None,
                              work_order_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取BOM物料需求（递归展开多层BOM）。"""
        if not product_code or not quantity:
            return []
        return self._expand_bom_recursive(product_code, quantity)

    def _format_shortage_result(self, shortage_list: List[Dict[str, Any]],
                                actual_shortage: List[Dict[str, Any]],
                                product_code: Optional[str], quantity: int) -> str:
        """格式化缺料检查结果。"""
        lines = []
        if product_code and quantity:
            lines.append(f"产品 {product_code} 生产 {quantity} 套的缺料检查结果：")
        else:
            lines.append("缺料检查结果：")

        if not actual_shortage:
            lines.append("✅ 物料充足，无缺料。")
        else:
            lines.append(f"⚠️ 共 {len(actual_shortage)} 种物料缺料：")
            for s in actual_shortage:
                lines.append(
                    f"  - [{s.get('bom_level', 1)}级] {s['material_code']}（{s['material_name']}）："
                    f"需求{s['required_qty']}{s['unit']}，"
                    f"可用{s['available_qty']}{s['unit']}，"
                    f"缺口{s['shortage_qty']}{s['unit']}"
                )

        lines.append("\n全部物料对比：")
        for s in shortage_list:
            status = "✅充足" if s["shortage_qty"] == 0 else "⚠️缺料"
            lines.append(
                f"  - [{s.get('bom_level', 1)}级] {s['material_code']}（{s['material_name']}）："
                f"需求{s['required_qty']}{s['unit']} / "
                f"可用{s['available_qty']}{s['unit']} {status}"
            )
        return "\n".join(lines)

    # --------------------------------------------------------
    # W-06 可替代物料检索与规格匹配
    # --------------------------------------------------------
    def search_alternative_materials(self, material_code: str) -> List[Dict[str, Any]]:
        """可替代物料检索与规格匹配（W-06）。"""
        db = self._get_db()
        if db is not None:
            try:
                alternatives = db.query_many(
                    "material_alternatives", {"material_code": material_code}) or []
                if alternatives:
                    return alternatives
            except Exception:
                pass
        # v6.46 D2：移除静态 mock 可替代物料兜底——须由训练/录入获得，
        # 空库返回空列表（调用方提示暂无替代物料数据）
        return []

    # --------------------------------------------------------
    # 采购申请 / W-07 采购订单创建与全生命周期跟踪
    # --------------------------------------------------------
    def _handle_purchase_request(self, user_input: str,
                                 context: Dict[str, Any]) -> AgentResponse:
        """处理采购申请意图。基于缺料清单推荐采购量，关联供应商，生成采购申请单。"""
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)

        product_code = slots.get("product_code")
        quantity = slots.get("quantity", 0)

        if not product_code:
            return AgentResponse(
                content="请提供要生成采购申请的产品/物料型号（如A-202、M-101）。",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots},
            )

        user_info = context.get("user", {})
        perms = user_info.get("permissions", {}) if isinstance(user_info, dict) else {}
        # W4 修复：权限默认拒绝（原 True 兜底放行导致无权限用户可发起采购）
        if not perms.get("can_purchase", False):
            return AgentResponse(
                content="您没有发起采购申请的权限，请联系采购部。",
                action="permission_denied", agent_name=self.agent_name,
            )

        bom_requirements = self._get_bom_requirements(product_code, quantity)

        purchase_items = []
        if bom_requirements:
            for item in bom_requirements:
                material_code = item["material_code"]
                inventory = self._query_inventory_from_db(material_code)
                available_qty = 0
                safety_stock = 0
                if inventory:
                    available_qty = inventory.get("raw", 0) + inventory.get("finished", 0)
                    safety_stock = inventory.get("safety_stock", 0)
                required_qty = item["required_qty"]
                shortage = max(0, required_qty - available_qty)
                suggested_qty = shortage + max(0, safety_stock - available_qty)
                if suggested_qty > 0:
                    supplier = self._get_supplier_for_material(material_code)
                    purchase_items.append({
                        "material_code": material_code,
                        "material_name": item["material_name"],
                        "required_qty": required_qty,
                        "available_qty": available_qty,
                        "shortage_qty": shortage,
                        "suggested_qty": suggested_qty,
                        "unit": item["unit"],
                        "supplier": supplier,
                        "urgency": "高" if shortage > 0 else "中",
                    })
        else:
            inventory = self._query_inventory_from_db(product_code)
            available_qty = 0
            safety_stock = 0
            if inventory:
                available_qty = inventory.get("raw", 0) + inventory.get("finished", 0)
                safety_stock = inventory.get("safety_stock", 0)
            shortage = max(0, quantity - available_qty)
            suggested_qty = shortage + max(0, safety_stock - available_qty)
            if suggested_qty > 0:
                supplier = self._get_supplier_for_material(product_code)
                purchase_items.append({
                    "material_code": product_code,
                    "material_name": inventory.get("name", "") if inventory else "",
                    "required_qty": quantity,
                    "available_qty": available_qty,
                    "shortage_qty": shortage,
                    "suggested_qty": suggested_qty,
                    "unit": "个",
                    "supplier": supplier,
                    "urgency": "高" if shortage > 0 else "中",
                })

        if not purchase_items:
            return AgentResponse(
                content="当前库存充足，无需采购。",
                action="no_purchase_needed", agent_name=self.agent_name,
                data={"product_code": product_code, "quantity": quantity},
            )

        pr_id = f"PR{datetime.now().strftime('%Y%m%d%H%M%S')}"
        self._create_purchase_request_in_db(pr_id, purchase_items, context)
        content = self._format_purchase_request(pr_id, purchase_items, product_code)

        return AgentResponse(
            content=content, action="purchase_request_created",
            need_confirm=True, agent_name=self.agent_name,
            data={"pr_id": pr_id, "purchase_items": purchase_items,
                  "product_code": product_code, "quantity": quantity, "status": "draft"},
        )

    def _get_supplier_for_material(self, material_code: str) -> Dict[str, Any]:
        """获取物料的推荐供应商。

        v6.47：移除静态 mock 兜底——供应商-物料关联须来自 supplier_materials 表
        （训练/录入），空库返回空字典（调用方提示暂无供应商数据）。
        """
        db = self._get_db()
        if db is not None:
            try:
                suppliers = db.query_many("supplier_materials", {"material_code": material_code}, limit=1) or []
                if suppliers:
                    return suppliers[0]
            except Exception:
                pass
        return {"supplier_id": "", "name": "", "lead_time_days": 0, "unit_price": 0}

    def _create_purchase_request_in_db(self, pr_id: str,
                                       purchase_items: List[Dict[str, Any]],
                                       context: Dict[str, Any]) -> bool:
        """在数据库中创建采购申请记录。"""
        db = self._get_db()
        if db is None:
            return False
        try:
            user_info = context.get("user", {}) if isinstance(context, dict) else {}
            applicant = user_info.get("name", "") if isinstance(user_info, dict) else ""
            now = datetime.now()
            db.insert("purchase_requests", {
                "pr_id": pr_id, "applicant": applicant, "status": "draft",
                "total_items": len(purchase_items), "created_at": now,
            })
            for item in purchase_items:
                supplier = item.get("supplier", {})
                db.insert("purchase_request_items", {
                    "pr_id": pr_id, "material_code": item["material_code"],
                    "material_name": item["material_name"],
                    "quantity": item["suggested_qty"], "unit": item["unit"],
                    "supplier_id": supplier.get("supplier_id", ""),
                    "supplier_name": supplier.get("name", ""),
                    "unit_price": supplier.get("unit_price", 0),
                    "urgency": item["urgency"],
                })
            return True
        except Exception:
            return False

    def _format_purchase_request(self, pr_id: str,
                                 purchase_items: List[Dict[str, Any]],
                                 product_code: str) -> str:
        """格式化采购申请单为文本。"""
        lines = [
            f"采购申请单已生成：{pr_id}",
            f"关联产品：{product_code}",
            f"共 {len(purchase_items)} 项物料：",
        ]
        for item in purchase_items:
            supplier = item.get("supplier", {})
            lines.append(
                f"  - {item['material_code']}（{item['material_name']}）："
                f"建议采购{item['suggested_qty']}{item['unit']} | "
                f"缺口{item['shortage_qty']}{item['unit']} | "
                f"供应商：{supplier.get('name', '?')}（{supplier.get('lead_time_days', '?')}天） | "
                f"紧急度：{item['urgency']}"
            )
        lines.append("\n请确认是否提交此采购申请。")
        return "\n".join(lines)

    def create_purchase_order(self, supplier_id: str, items: List[Dict[str, Any]],
                              operator: str = "", po_type: str = "standard") -> Dict[str, Any]:
        """创建采购订单（W-07）。po_type支持standard/subcontract（外协）。

        v6.89（S6）：原实现吞掉写库异常仍返回 po_data（假成功），且仅写
        purchase_orders 主表不写明细。现改为：DB 写失败如实抛 RuntimeError；
        同时写 purchase_order_items 明细（025 迁移四业务列 po_id/material_code/
        quantity/unit_price），并补写 operator 列（025 迁移已加）。
        """
        if not items:
            raise ValueError("采购订单明细为空，无法创建")
        po_id = f"PO{datetime.now().strftime('%Y%m%d%H%M%S')}"
        total_amount = sum((i.get("quantity", 0) * i.get("unit_price", 0)) for i in items)
        po_data = {
            "po_id": po_id, "supplier_id": supplier_id,
            "total_amount": total_amount, "status": "draft",
            "po_type": po_type, "items": items, "operator": operator,
        }
        db = self._get_db()
        if db is None:
            raise RuntimeError("数据库不可用，无法创建采购订单")
        # D1 修复：主表 + 明细多语句写入用事务包裹（原多个 db.insert 各自独立
        # commit，明细写一半失败会留下无明细的孤儿采购单）；中间失败整体回滚。
        try:
            with db.transaction() as session:
                from sqlalchemy import text
                session.execute(text(
                    "INSERT INTO purchase_orders (po_id, supplier_id, total_amount, "
                    "status, po_type, operator, created_at) VALUES "
                    "(:po_id, :sid, :amount, 'draft', :ptype, :op, :now)"),
                    {"po_id": po_id, "sid": supplier_id, "amount": total_amount,
                     "ptype": po_type, "op": operator, "now": datetime.now()})
                for item in items:
                    session.execute(text(
                        "INSERT INTO purchase_order_items "
                        "(po_id, material_code, quantity, unit_price) VALUES "
                        "(:po_id, :mc, :qty, :price)"),
                        {"po_id": po_id,
                         "mc": item.get("material_code", ""),
                         "qty": item.get("quantity", 0),
                         "price": item.get("unit_price", 0)})
        except Exception as e:
            raise RuntimeError(f"采购订单写入失败：{e}") from e
        return po_data

    def track_purchase_order(self, po_id: str) -> Dict[str, Any]:
        """采购订单全生命周期跟踪（W-07）。"""
        db = self._get_db()
        if db is not None:
            try:
                po = db.query_one("purchase_orders", {"po_id": po_id})
                if po:
                    items = db.query_many("purchase_order_items", {"po_id": po_id}) or []
                    po["items"] = items
                    return po
            except Exception:
                pass
        # v6.89：移除静态 mock 采购单兜底——采购单须来自 purchase_orders 表（训练/录入），
        # 空库返回空记录（调用方提示暂无采购单）
        return {"po_id": po_id, "items": [], "msg": "暂无采购单记录"}

    # --------------------------------------------------------
    # W-08 供应商管理与评价
    # --------------------------------------------------------
    def evaluate_supplier(self, supplier_id: str) -> Dict[str, Any]:
        """供应商评价（W-08）。基于交期/质量/价格/服务四维加权评分。"""
        db = self._get_db()
        if db is not None:
            try:
                supplier = db.query_one("suppliers", {"supplier_id": supplier_id})
                if supplier and supplier.get("rating"):
                    return supplier
            except Exception:
                pass
        # v6.47：移除静态 mock 供应商评价兜底——评价须来自 suppliers 表（训练/录入）
        return {
            "supplier_id": supplier_id,
            "name": "",
            "scores": {},
            "weights": _procure_weights(),
            "overall_score": 0, "rating": "",
            "evaluation_note": "暂无供应商评价数据，请先录入或训练。",
        }

    def manage_supplier(self, action: str, supplier_data: Dict[str, Any]) -> Dict[str, Any]:
        """供应商管理CRUD（W-08）。action: create/update/query/delete。"""
        db = self._get_db()
        if db is None:
            return {"success": False, "error": "数据库不可用"}
        try:
            if action == "query":
                supplier_id = supplier_data.get("supplier_id", "")
                if supplier_id:
                    result = db.query_one("suppliers", {"supplier_id": supplier_id})
                else:
                    result = db.query_many("suppliers", limit=50) or []
                return {"success": True, "data": result}
            elif action == "create":
                supplier_id = db.insert("suppliers", supplier_data)
                return {"success": True, "supplier_id": supplier_id}
            elif action == "update":
                supplier_id = supplier_data.pop("supplier_id", "")
                db.update("suppliers", supplier_data, {"supplier_id": supplier_id})
                return {"success": True, "supplier_id": supplier_id}
            else:
                return {"success": False, "error": f"不支持的操作: {action}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # --------------------------------------------------------
    # W-09 比价议价辅助与外协采购执行
    # --------------------------------------------------------
    def compare_prices(self, material_code: str, quantity: float) -> Dict[str, Any]:
        """比价议价辅助（W-09）。多供应商比价，推荐最优供应商。"""
        db = self._get_db()
        if db is not None:
            try:
                quotes = db.query_many("supplier_quotes", {"material_code": material_code}) or []
                if quotes:
                    return self._analyze_price_comparison(quotes, material_code, quantity)
            except Exception:
                pass
        # v6.46 D2：移除静态 mock 报价兜底——供应商报价须由训练/录入获得，
        # 空库返回空比价结果（recommended_supplier=None，调用方提示暂无报价数据）
        return self._analyze_price_comparison([], material_code, quantity)

    def _analyze_price_comparison(self, quotes: List[Dict[str, Any]],
                                  material_code: str, quantity: float) -> Dict[str, Any]:
        """分析比价数据，推荐最优供应商。"""
        analyzed = []
        prices = [q.get("unit_price", 0) or 0 for q in quotes]
        leads = [q.get("lead_time_days", 0) or 0 for q in quotes]
        min_price = min(prices) if prices else 0
        max_price = max(prices) if prices else 1
        max_lead = max(leads) if leads else 1
        for q in quotes:
            unit_price = q.get("unit_price", 0) or 0
            total_cost = unit_price * quantity
            lead_time = q.get("lead_time_days", 0) or 0
            price_score = 1 - (unit_price - min_price) / (max_price - min_price + 0.01)
            lead_score = (max_lead - lead_time) / max_lead if max_lead > 0 else 0
            _w = _procure_weights()
            composite_score = _w["compare_weight_price"] * price_score + _w["compare_weight_delivery"] * lead_score
            analyzed.append({
                "supplier_id": q.get("supplier_id", ""),
                "supplier_name": q.get("supplier_name", ""),
                "unit_price": unit_price, "total_cost": total_cost,
                "lead_time_days": lead_time,
                "composite_score": round(composite_score, 3),
            })
        analyzed.sort(key=lambda x: x["composite_score"], reverse=True)
        best = analyzed[0] if analyzed else None
        savings = 0
        if best and len(analyzed) > 1:
            max_cost = max(a["total_cost"] for a in analyzed)
            savings = max_cost - best["total_cost"]
        return {
            "material_code": material_code, "quantity": quantity,
            "quotes": analyzed, "recommended_supplier": best,
            "potential_savings": round(savings, 2),
        }

    def _handle_price_compare(self, user_input: str,
                              context: Dict[str, Any]) -> AgentResponse:
        """处理比价议价意图（W-09，v6.63 挂入 process 分发）。

        多供应商报价对比，推荐综合评分最优供应商。
        """
        new_slots = self._extract_slots(user_input)
        slots = self._merge_slots(context, new_slots)
        material_code = slots.get("product_code") or slots.get("material_code")
        quantity = slots.get("quantity", 1) or 1
        if not material_code:
            return AgentResponse(
                content="<div class='muted'>请提供要比价的物料型号（如M-101、A-202）。</div>",
                action="request_info", agent_name=self.agent_name,
                data={"slots": slots},
            )
        result = self.compare_prices(material_code, float(quantity))
        quotes = result.get("quotes") or []
        if not quotes:
            return AgentResponse(
                content=(f"<div class='muted'>暂无物料 {material_code} 的供应商报价"
                         f"（报价数据须来自 supplier_quotes 表的录入/训练）。</div>"),
                action="not_found", agent_name=self.agent_name,
                data={"material_code": material_code, "quotes": []},
            )
        lines = [
            f"<div class='card-row'><span class='label'>比价物料</span>"
            f"<span class='value'>{material_code}</span></div>",
            f"<div class='card-row'><span class='label'>采购数量</span>"
            f"<span class='value'>{quantity}</span></div>",
        ]
        for i, q in enumerate(quotes, 1):
            lines.append(
                f"<div class='card-row'><span class='label'>[{i}] "
                f"{q.get('supplier_name', q.get('supplier_id', ''))}</span>"
                f"<span class='value'>¥{q.get('unit_price', 0):,.2f} · "
                f"交期{q.get('lead_time_days', 0)}天 · 评分{q.get('composite_score', 0)}</span></div>"
            )
        best = result.get("recommended_supplier")
        if best:
            lines.append(
                f"<div class='warn'>✅ 推荐供应商："
                f"{best.get('supplier_name', best.get('supplier_id', ''))}"
                f"（综合评分 {best.get('composite_score', 0)}）</div>"
            )
        savings = result.get("potential_savings", 0) or 0
        if savings > 0:
            lines.append(
                f"<div class='card-row'><span class='label'>较最高价可节省</span>"
                f"<span class='value gold'>¥{savings:,.2f}</span></div>"
            )
        return AgentResponse(
            content="\n".join(lines), action="price_compare",
            agent_name=self.agent_name,
            data=result,
        )

    # --------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------
    def _get_db(self) -> Any:
        """获取数据库访问实例。"""
        if self.database is not None:
            return self.database
        try:
            from prog.core.database import get_database
            return get_database()
        except Exception:
            return None


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.runtime.debug import hello_world
    assert WarehouseAgent is not None, "WarehouseAgent 类未定义"
    from prog.agents.base_agent import BaseAgent
    assert issubclass(WarehouseAgent, BaseAgent), "WarehouseAgent 未继承 BaseAgent"
    agent = WarehouseAgent()
    assert agent.agent_name == "仓储Agent"
    assert agent.agent_type == "warehouse"
    assert "inventory_rule" in agent.applicable_rules
    assert "bom_rule" in agent.applicable_rules
    assert agent._recognize_sub_intent("查一下A-202的库存") == "inventory_query"
    assert agent._recognize_sub_intent("100个A-202采购入库") == "stock_in"
    assert agent._recognize_sub_intent("生产领料50个A-202") == "stock_out"
    assert agent._recognize_sub_intent("检查缺料情况") == "shortage_check"
    assert agent._recognize_sub_intent("生成采购申请") == "purchase_request"
    slots = agent._extract_slots("100个A-202入原料仓")
    assert slots.get("product_code") == "A-202"
    assert slots.get("quantity") == 100
    assert slots.get("stage") == "raw"
    assert len(INVENTORY_STAGES) == 5
    assert "raw" in INVENTORY_STAGES
    assert "wip_anode" in INVENTORY_STAGES
    assert "finished" in INVENTORY_STAGES
    inv = agent._query_inventory_from_db("A-202")
    assert inv is not None
    # v6.89：空库返回空 dict（无 mock 兜底），有数据时才校验五阶段键
    if inv:
        assert "raw" in inv
    assert hasattr(agent, "transfer_stage")
    assert hasattr(agent, "deduct_for_order")
    assert hasattr(agent, "check_safety_stock")
    assert hasattr(agent, "track_material_movements")
    assert hasattr(agent, "search_alternative_materials")
    assert hasattr(agent, "create_purchase_order")
    assert hasattr(agent, "track_purchase_order")
    assert hasattr(agent, "evaluate_supplier")
    assert hasattr(agent, "compare_prices")
    # 验证BOM递归展开（v6.46 D2 起无 mock 兜底：DB 有 BOM 数据时验证递归算法；
    # 空库/无 DB 时降级为仅验证返回类型，保证 DEBUG 自检可空库通过）
    bom_req = agent._get_bom_requirements("A-202", 10)
    assert isinstance(bom_req, list), "BOM展开应返回列表"
    db = agent._get_db()
    if db is not None:
        try:
            has_bom = bool(db.query_many("bom", {"parent_product_code": "A-202"}))
        except Exception:
            has_bom = False
    else:
        has_bom = False
    if has_bom:
        assert len(bom_req) >= 2, "BOM递归展开应返回多级子件"
        codes = [item["material_code"] for item in bom_req]
        assert "M-301" in codes, "BOM递归展开应包含三级子件M-301"
    hello_world(__name__, "核心类定义完整")

from prog.runtime.debug import DEBUG
if DEBUG:
    _self_test()
