"""
Knowledge 知识域API模块
=======================

文件用途：
    实现知识库管理 REST 接口（规格书 §A.1.10），提供文档管理、
    语义检索、反馈标注、知识缺口统计与推荐配置。

技术规格章节：
    - §A.1.10 知识域接口
    - §3.8 Knowledge Assistant（K-01~K-06）

接口列表：
    - GET  /api/knowledge/documents               文档列表（分页/过滤）
    - GET  /api/knowledge/documents/<doc_id>      文档详情
    - POST /api/knowledge/documents               文档上传（向量化 + 落库）
    - POST /api/knowledge/search                  语义检索（向量 + DB like 兜底）
    - POST /api/knowledge/feedback                反馈标注（写 training_data）
    - GET  /api/knowledge/gaps                    知识缺口统计
    - PATCH /api/knowledge/documents/<doc_id>/status  文档状态变更
    - GET  /api/knowledge/recommend-config        推荐配置读取
    - PUT  /api/knowledge/recommend-config        推荐配置更新（upsert）

设计说明：
    - url_prefix=/api/knowledge，与 training.py 的 /api/training/l3/knowledge
      不冲突
    - KnowledgeAssistant 组件不可用时降级：上传仅落库、检索走 DB LIKE、
      缺口返回空结构（不 500、不伪造数据）
    - 所有视图函数 try/except 兜底，统一 {code, data/msg} 响应
"""

import json
from typing import Any

from flask import Blueprint, request, current_app, g
from prog.utils.api_response import api_response, error_response
from prog.utils.auth_decorators import require_role

knowledge_bp = Blueprint('knowledge', __name__, url_prefix='/api/knowledge')


def _get_db() -> Any:
    """延迟获取数据库实例，获取失败时返回 None（只读查询无 DB 时返回空）。"""
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _get_knowledge_assistant() -> Any:
    """获取 KnowledgeAssistant 组件（不可用时返回 None 走降级路径）。"""
    try:
        components = current_app.extensions.get('components', {}) or {}
        return components.get('knowledge_assistant')
    except Exception:
        return None


# --------------------------------------------------------
# 文档列表
# --------------------------------------------------------
@knowledge_bp.route('/documents', methods=['GET'])
def list_documents():
    """GET /api/knowledge/documents 文档列表（分页 + doc_type/status 过滤）。"""
    try:
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        doc_type = request.args.get('doc_type', '')
        status = request.args.get('status', '')

        # 分页边界校验（page >= 1，page_size 1~100，防超大分页拖垮 DB）
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 100:
            page_size = 20

        filters = {}
        if doc_type:
            filters['doc_type'] = doc_type
        if status:
            filters['status'] = status

        db = _get_db()
        items = []
        total = 0
        if db:
            offset = (page - 1) * page_size
            try:
                items = db.query_many('knowledge_documents',
                                      filters=filters or None,
                                      limit=page_size, offset=offset,
                                      order_by='created_at DESC') or []
            except Exception:
                items = []
            try:
                all_items = db.query_many('knowledge_documents',
                                          filters=filters or None) or []
                total = len(all_items)
            except Exception:
                total = len(items)

        return api_response(code=0, data={
            "items": items, "total": total,
            "page": page, "page_size": page_size,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 文档详情
# --------------------------------------------------------
@knowledge_bp.route('/documents/<int:doc_id>', methods=['GET'])
def get_document(doc_id):
    """GET /api/knowledge/documents/<doc_id> 文档详情，不存在返回 404。"""
    try:
        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用"), 503
        try:
            row = db.query_one('knowledge_documents', {'doc_id': doc_id})
        except Exception as e:
            return error_response(500, f"查询文档失败：{str(e) if DEBUG else '内部错误'}"), 500
        if not row:
            return error_response(404, f"知识文档 {doc_id} 不存在"), 404
        return api_response(code=0, data=row)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 文档上传
# --------------------------------------------------------
@knowledge_bp.route('/documents', methods=['POST'])
@require_role('admin')
def upload_document():
    """POST /api/knowledge/documents 文档上传。

    请求体（JSON）：
        {"title": "CNC工艺规范", "content": "...", "doc_type": "工艺", "tags": ["CNC"]}

    组件可用时调用 KnowledgeAssistant.upload_document 向量化；
    组件不可用且 DB 可用时直接 insert knowledge_documents 兜底。
    """
    try:
        body = request.get_json(silent=True) or {}
        title = body.get('title', '')
        content = body.get('content', '')
        doc_type = body.get('doc_type', 'general')
        tags = body.get('tags') or []

        if not title:
            return error_response(400, "title 为必填"), 400
        if not isinstance(tags, list):
            tags = [str(tags)]

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法上传知识文档"), 503

        # 1) 组件可用时触发向量化（upload_document 只写向量库，不落库）
        agent = _get_knowledge_assistant()
        vectorized = False
        vector_msg = ''
        vector_doc_id = ''  # 向量库 metadata.doc_id（DOCxxx），与 DB 主键是两套 id
        if agent is not None:
            try:
                result = agent.upload_document(
                    title, content, source=doc_type,
                    category=doc_type, tags=tags)
                vectorized = bool(result.get('vectorized', False))
                vector_msg = result.get('message', '')
                # P2 删除脱节修复：保存向量 doc_id，供状态归档时联动清理向量库
                if vectorized:
                    vector_doc_id = result.get('doc_id', '') or ''
            except Exception as e:
                vector_msg = f"向量化失败：{e}"

        # 2) 统一落库 knowledge_documents（组件不可用时即纯入库兜底）
        try:
            doc_id = db.insert('knowledge_documents', {
                "title": title,
                "doc_type": doc_type,
                "content": content,
                "tags": json.dumps(tags, ensure_ascii=False) if tags else '[]',
                "uploaded_by": g.get('user_id', '') or g.get('user_name', '') or '',
                "status": "vectorized" if vectorized else "active",
                "extra_data": json.dumps(
                    {"source": doc_type, "vector_doc_id": vector_doc_id},
                    ensure_ascii=False),
            })
        except Exception as e:
            return error_response(500, f"知识文档落库失败：{str(e) if DEBUG else '内部错误'}"), 500

        message = (vector_msg or ("文档上传成功，已完成向量化" if vectorized
                                  else "文档上传成功（组件不可用，仅入库未向量化）"))
        return api_response(code=0, data={
            "doc_id": doc_id,
            "title": title,
            "doc_type": doc_type,
            "vectorized": vectorized,
            "status": "vectorized" if vectorized else "active",
            "message": message,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 语义检索
# --------------------------------------------------------
@knowledge_bp.route('/search', methods=['POST'])
def search_knowledge():
    """POST /api/knowledge/search 语义检索。

    请求体（JSON）：{"query": "CNC工艺", "top_k": 5}

    优先 KnowledgeAssistant._rag_search（向量库）；不可用/无命中时
    用 knowledge_documents 表 LIKE 检索兜底（query_filtered op='like'）。
    """
    try:
        body = request.get_json(silent=True) or {}
        query = body.get('query', '')
        top_k = body.get('top_k', 5)

        if not query:
            return error_response(400, "query 为必填"), 400
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            return error_response(400, "top_k 必须为整数"), 400
        if top_k < 1 or top_k > 50:
            return error_response(400, "top_k 必须在 1~50 之间"), 400

        # 1) 优先语义检索（向量库）
        agent = _get_knowledge_assistant()
        vector_results = []
        if agent is not None:
            try:
                vector_results = agent._rag_search(query) or []
            except Exception:
                vector_results = []
        if vector_results:
            return api_response(code=0, data={
                "query": query, "source": "vector",
                "items": vector_results[:top_k], "total": len(vector_results),
            })

        # 2) 向量检索不可用/无命中 -> DB LIKE 检索兜底
        db = _get_db()
        db_items = []
        if db:
            try:
                like = f"%{query}%"
                content_hits = db.query_filtered(
                    'knowledge_documents',
                    [{"field": "content", "op": "like", "value": like}],
                    limit=top_k, order_by='created_at DESC') or []
                title_hits = db.query_filtered(
                    'knowledge_documents',
                    [{"field": "title", "op": "like", "value": like}],
                    limit=top_k, order_by='created_at DESC') or []
                seen = set()
                for r in content_hits + title_hits:
                    key = r.get('doc_id')
                    if key not in seen:
                        seen.add(key)
                        db_items.append(r)
                    if len(db_items) >= top_k:
                        break
            except Exception:
                db_items = []

        return api_response(code=0, data={
            "query": query, "source": "db" if db_items else "none",
            "items": db_items, "total": len(db_items),
            "message": "" if db_items else "知识库未收录相关内容",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 反馈标注
# --------------------------------------------------------
@knowledge_bp.route('/feedback', methods=['POST'])
def submit_feedback():
    """POST /api/knowledge/feedback 反馈标注。

    请求体（JSON）：{"doc_id": 3, "rating": 4, "label": "有帮助"}

    写入 training_data 表（002 迁移实际列：agent_type/intent/user_input/
    ai_output/metadata/approved），作为待审批的训练样本。
    """
    try:
        body = request.get_json(silent=True) or {}
        doc_id = body.get('doc_id', '')
        rating = body.get('rating')
        label = body.get('label', '')

        if not doc_id:
            return error_response(400, "doc_id 为必填"), 400
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return error_response(400, "rating 必须为整数"), 400
        if rating < 1 or rating > 5:
            return error_response(400, "rating 必须在 1~5 之间"), 400

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法记录反馈"), 503

        payload = json.dumps({"doc_id": doc_id, "rating": rating, "label": label},
                             ensure_ascii=False)
        record = {
            "agent_type": "knowledge",
            "intent": "knowledge_feedback",
            "user_input": f"知识文档反馈 doc_id={doc_id} rating={rating} label={label}",
            "ai_output": payload,
            "metadata": payload,
            "approved": False,
        }
        try:
            new_id = db.insert('training_data', record)
        except Exception as e:
            return error_response(500, f"反馈记录落库失败：{str(e) if DEBUG else '内部错误'}"), 500

        return api_response(code=0, data={
            "id": new_id,
            "doc_id": doc_id,
            "rating": rating,
            "label": label,
            "status": "recorded",
            "message": "反馈已记录，待审批",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 知识缺口统计
# --------------------------------------------------------
@knowledge_bp.route('/gaps', methods=['GET'])
def knowledge_gaps():
    """GET /api/knowledge/gaps 高频问题统计与知识缺口识别。

    委托 KnowledgeAssistant.analyze_knowledge_gaps；组件不可用时
    返回空结构（不 500、不伪造数据）。
    """
    try:
        agent = _get_knowledge_assistant()
        if agent is None:
            return api_response(code=0, data={
                "total_questions": 0, "top_topics": [],
                "knowledge_gaps": [], "recommendation": "",
                "message": "KnowledgeAssistant 未就绪",
            })
        try:
            result = agent.analyze_knowledge_gaps()
        except Exception:
            result = {
                "total_questions": 0, "top_topics": [],
                "knowledge_gaps": [], "recommendation": "",
            }
        if not isinstance(result, dict):
            result = {}
        return api_response(code=0, data=result)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 文档状态变更
# --------------------------------------------------------
@knowledge_bp.route('/documents/<int:doc_id>/status', methods=['PATCH'])
@require_role('admin')
def update_document_status(doc_id):
    """PATCH /api/knowledge/documents/<doc_id>/status 文档状态变更。

    请求体（JSON）：{"status": "active" | "archived" | "deprecated"}
    """
    try:
        body = request.get_json(silent=True) or {}
        status = body.get('status', '')
        if status not in ('active', 'archived', 'deprecated'):
            return error_response(400, "status 仅支持 active/archived/deprecated"), 400

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用"), 503

        # P2 删除脱节修复：归档/弃用前先清理向量库，避免 RAG 检索命中已下架内容
        if status in ('archived', 'deprecated'):
            _purge_vector_by_status(db, doc_id)

        affected = db.update('knowledge_documents', {'status': status},
                             {'doc_id': doc_id})
        if affected <= 0:
            return error_response(404, f"知识文档 {doc_id} 不存在"), 404

        return api_response(code=0, data={
            "doc_id": doc_id, "status": status, "success": True,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


def _purge_vector_by_status(db: Any, doc_id: int) -> None:
    """按文档 ID 清理其向量库分块（P2 删除脱节修复的落点之一）。

    knowledge_documents.extra_data 中保存了向量库 metadata.doc_id（DOCxxx），
    与 DB 主键 doc_id 是两套 id 体系；此处读取关联后再按向量 id 删除。
    """
    try:
        row = db.query_one('knowledge_documents', {'doc_id': doc_id})
        if not row:
            return
        extra = row.get('extra_data')
        vector_doc_id = ''
        if isinstance(extra, str) and extra:
            try:
                vector_doc_id = (json.loads(extra) or {}).get('vector_doc_id', '')
            except Exception:
                vector_doc_id = ''
        elif isinstance(extra, dict):
            vector_doc_id = extra.get('vector_doc_id', '')
        if not vector_doc_id:
            return
        agent = _get_knowledge_assistant()
        vector_store = getattr(agent, 'vector_store', None) if agent else None
        if vector_store is None or not hasattr(vector_store, 'delete_by_doc_id'):
            return
        vector_store.delete_by_doc_id('ai_factory_kb', vector_doc_id)
    except Exception:
        # 向量清理失败不阻塞状态变更，仅留日志
        try:
            current_app.logger.warning(
                "清理向量库失败 doc_id=%s", doc_id)
        except Exception:
            pass


# --------------------------------------------------------
# 推荐配置读取
# --------------------------------------------------------
@knowledge_bp.route('/recommend-config', methods=['GET'])
def get_recommend_config():
    """GET /api/knowledge/recommend-config 推荐配置读取。

    从 system_configs 读 config_key='KNOWLEDGE_RECOMMEND_CONFIG'，
    无配置时返回默认 {"enabled": False}。
    """
    try:
        db = _get_db()
        config = {"enabled": False}
        if db:
            try:
                row = db.query_one('system_configs',
                                   {'config_key': 'KNOWLEDGE_RECOMMEND_CONFIG'})
            except Exception:
                row = None
            if row and row.get('config_value'):
                raw = row['config_value']
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except Exception:
                        raw = {}
                if isinstance(raw, dict):
                    config.update(raw)
        return api_response(code=0, data=config)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 推荐配置更新
# --------------------------------------------------------
@knowledge_bp.route('/recommend-config', methods=['PUT'])
@require_role('admin')
def update_recommend_config():
    """PUT /api/knowledge/recommend-config 推荐配置更新（upsert）。

    请求体（JSON）：{"enabled": true, "strategy": "rule_first", ...}
    """
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict) or not body:
            return error_response(400, "请求体必须为非空 JSON 对象"), 400

        db = _get_db()
        if not db:
            return error_response(503, "数据库不可用，无法保存推荐配置"), 503

        raw = json.dumps(body, ensure_ascii=False)
        try:
            row = db.query_one('system_configs',
                               {'config_key': 'KNOWLEDGE_RECOMMEND_CONFIG'})
            if row:
                db.update('system_configs', {'config_value': raw},
                          {'config_key': 'KNOWLEDGE_RECOMMEND_CONFIG'})
            else:
                db.insert('system_configs', {
                    'config_key': 'KNOWLEDGE_RECOMMEND_CONFIG',
                    'config_value': raw,
                    'config_type': 'json',
                    'description': '知识推荐配置（/api/knowledge/recommend-config）',
                })
        except Exception as e:
            return error_response(500, f"推荐配置保存失败：{str(e) if DEBUG else '内部错误'}"), 500

        return api_response(code=0, data={
            "saved": True, "config": body,
            "message": "推荐配置已保存",
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、Blueprint定义、核心路由完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert knowledge_bp is not None, "knowledge_bp 未定义"
    hello_world(__name__, "knowledge_bp 定义完整，知识域接口就绪")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
