"""
提示词模板存储读写（规格 A.4：所有提示词存储在 prompt_templates 表）
=====================================================================
    get_prompt_template(template_id)：按模板ID读取内容
        - DB 优先（prompt_templates 表，040 迁移规格结构）
        - DB 不可用/查无时降级内置默认（DEFAULT_PROMPTS，仅 debug 状态）

支持 LLM 自优化闭环（规格 L8800）：
    - set_prompt_template(version+1 / adjusted_by=LLM_SELF / approval_id)
    - 审批通过后生效，旧版本保留供 A/B 回滚
    - performance_score 由评估流程回写（待 L3 上线接入）
"""
from typing import Any, Dict, Optional

# 内置默认模板（DB 不可用时的 debug 降级；与 040 迁移种子保持一致）
DEFAULT_PROMPTS: Dict[str, str] = {
    "PROMPT_BUSINESS_CHANNEL": (
        "你是AI工厂管家的业务操作助手。你的职责是理解用户的业务指令并执行。\n\n"
        "【角色定位】你是工厂ERP系统的智能接口，用户通过自然语言与你交互完成业务操作。\n\n"
        "【可用操作】\n- 订单管理：创建/修改/取消/查询订单\n"
        "- 库存管理：查询库存/库存流转/安全库存设置\n"
        "- 生产管理：查询工单/排产查询/工序进度\n"
        "- 图纸管理：上传图纸/查询版本/图纸解析\n"
        "- 质量管理：质检记录查询/不合格品处理\n"
        "- 财务管理：信用查询/应收查询\n\n"
        "【安全规则】\n- 所有操作必须经过七层审核\n"
        "- 售价低于成本线必须拦截（不可绕过）\n"
        "- 信用额度不足必须拦截（不可绕过）\n"
        "- 仅effective版本图纸可用于生产\n"
        "- 操作需用户明确确认（\"确认执行\"为强确认词）"
    ),
    "PROMPT_MANAGEMENT_CHANNEL": (
        "你是AI工厂管家的企业管理助手。你的职责是提供企业管理咨询、制度查询与数据分析服务。\n\n"
        "【角色定位】你是工厂的管理顾问，基于企业知识库与数据分析回答管理问题。\n\n"
        "【可用服务】\n- 管理制度/流程查询（RAG检索企业知识库）\n"
        "- 管理咨询（精益生产/5S/ISO体系等）\n"
        "- 经营数据分析（产量/质量/成本趋势）\n"
        "- 流程引导（可发起的审批流程清单）\n\n"
        "【安全规则】\n- 知识库未命中时标注来源并说明\n"
        "- 涉及经营数据时注明统计口径"
    ),
    "PROMPT_RAG_SEARCH": (
        "基于以下知识片段回答用户问题。若知识片段不足以回答，明确说明"
        "\"知识库中未找到相关内容\"。\n\n"
        "【引用规则】\n- 命中知识片段时标注 [知识片段N] 与参考来源\n"
        "- 不臆造知识片段中不存在的结论\n"
        "- 结合附件内容时标注 [附件N]\n\n【知识片段】\n{context}\n\n【用户问题】\n{question}"
    ),
}


def _get_db():
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def get_prompt_template(template_id: str,
                        use_fallback: bool = True) -> Optional[str]:
    """读取提示词模板内容（规格 A.4：DB 优先）。

    use_fallback=True：DB 不可用/查无时降级内置默认（debug）；
    use_fallback=False：仅返回 DB 实存模板（供需要"DB 优先但不覆盖
    现有逻辑"的调用方区分真实命中与降级默认）。
    """
    db = _get_db()
    if db is not None:
        try:
            row = db.query_one("prompt_templates",
                               {"template_id": template_id})
            if row:
                return row.get("template_content")
        except Exception:
            pass
    return DEFAULT_PROMPTS.get(template_id) if use_fallback else None


def get_prompt_record(template_id: str) -> Optional[Dict[str, Any]]:
    """读取提示词模板完整记录（含 version/adjusted_by/performance_score）。"""
    db = _get_db()
    if db is not None:
        try:
            row = db.query_one("prompt_templates",
                               {"template_id": template_id})
            if row:
                return dict(row)
        except Exception:
            pass
    content = DEFAULT_PROMPTS.get(template_id)
    if content is None:
        return None
    return {"template_id": template_id, "template_content": content,
            "version": 1, "adjusted_by": "MANUAL", "performance_score": None}


def set_prompt_template(template_id: str, content: str, channel: str = "BOTH",
                        agent_scope: Optional[str] = None,
                        adjusted_by: str = "MANUAL",
                        approval_id: str = "") -> bool:
    """写入/更新提示词模板（version 自增，LLM_SELF 优化经审批后调用）。

    返回是否写入 DB（DB 不可用时仅更新内存降级，返回 False）。
    """
    db = _get_db()
    if db is not None:
        try:
            existed = db.query_one("prompt_templates",
                                   {"template_id": template_id})
            if existed:
                db.update("prompt_templates", {
                    "template_content": content,
                    "channel": channel,
                    "agent_scope": agent_scope,
                    "version": int(existed.get("version") or 1) + 1,
                    "adjusted_by": adjusted_by,
                    "approval_id": approval_id or existed.get("approval_id") or "",
                }, {"template_id": template_id})
            else:
                db.insert("prompt_templates", {
                    "template_id": template_id,
                    "template_content": content,
                    "channel": channel,
                    "agent_scope": agent_scope,
                    "version": 1,
                    "adjusted_by": adjusted_by,
                    "approval_id": approval_id,
                })
            return True
        except Exception:
            pass
    DEFAULT_PROMPTS[template_id] = content
    return False
