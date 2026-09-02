"""
Data 基础数据API模块
====================

文件用途：
    实现基础数据查询API，提供产品、客户、产线等数据展示接口。

技术规格章节：
    - §3.2~§3.8 各领域Agent（基础数据为Agent提供查询支撑）
    - §1.1.3 Coordinator Agent（仪表盘聚合各域数据）

接口列表（实际实现状态）：
    - GET /api/data/products: 产品列表（含分页/关键词/权限裁剪）
    - GET /api/data/customers: 客户列表（含分页/关键词/联系方式脱敏）
    - GET /api/data/production-lines: 产线列表
    - GET /api/data/qc-records: 质检记录（待实现——历史 demo 路由已随 demo 移除）
    - GET /api/data/dashboard: 仪表盘聚合数据（待实现）
    - POST /api/data/export: 数据导出（待实现）

设计说明：
    - 各列表接口支持分页与筛选
    - 字段可见性按用户权限裁剪（无成本权限屏蔽 cost_price、联系方式脱敏）
"""

from typing import Any

from flask import Blueprint, request, g
from prog.utils.api_response import api_response, error_response

data_bp = Blueprint('data', __name__, url_prefix='/api/data')


def _get_db() -> Any:
    """延迟获取数据库实例，获取失败时返回 None（调用方空列表/404 降级）。"""
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _can_view_cost() -> bool:
    """从请求上下文判断是否可查看成本字段（权限来自 token 解析，见认证中间件）。"""
    perms = g.get('permissions', {})
    return bool(perms.get('can_view_cost', False))


def _mask_contact(contact: Any) -> Any:
    """联系方式脱敏（保留前3后4，中间用****替代）。"""
    if not contact or not isinstance(contact, str):
        return contact
    if len(contact) <= 7:
        return '****'
    return contact[:3] + '****' + contact[-4:]


# --------------------------------------------------------
# 产品列表
# --------------------------------------------------------
@data_bp.route('/products', methods=['GET'])
def list_products():
    """GET /api/data/products 产品列表。"""
    try:
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        category = request.args.get('category', '')
        keyword = request.args.get('keyword', '')
        can_view_cost = _can_view_cost()
        # D-5：分页参数边界校验（page>=1、1<=page_size<=100），防负 offset/全表拉取
        if page < 1 or not (1 <= page_size <= 100):
            return error_response(400, "page>=1 且 1<=page_size<=100"), 400

        filters = {}
        if category:
            filters['category'] = category

        db = _get_db()
        items = []
        total = 0
        if db:
            try:
                all_items = db.query_many('products', filters=filters or None) or []
            except Exception:
                all_items = []
            # D-3：关键词过滤改为在"全量结果"上执行（原在分页后过滤，第 2 页起
            # 匹配记录永远搜不到、total 也不含过滤）；字段用真实列 product_name
            if keyword:
                kw = keyword.lower()
                all_items = [p for p in all_items
                             if kw in (p.get('product_name', '')
                                       + p.get('product_code', '')).lower()]
            total = len(all_items)
            offset = (page - 1) * page_size
            items = all_items[offset:offset + page_size]

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空列表
        # 权限裁剪：无成本查看权限时屏蔽真实成本列 cost_price（D-1：原写不存在的
        # 'cost' 键，真实列 cost_price 原样返回致成本泄露）
        if not can_view_cost:
            for p in items:
                p.pop('cost_price', None)

        return api_response(code=0, data={
            "items": items, "total": total,
            "page": page, "page_size": page_size,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 产品详情
# --------------------------------------------------------
@data_bp.route('/products/<product_code>', methods=['GET'])
def get_product(product_code):
    """GET /api/data/products/<product_code> 产品详情。"""
    try:
        can_view_cost = _can_view_cost()
        db = _get_db()
        product = None
        if db:
            try:
                product = db.query_one('products', {'product_code': product_code})
            except Exception:
                product = None

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回 404
        if not product:
            return error_response(404, f"产品 {product_code} 不存在"), 404

        # 权限裁剪（D-1：屏蔽真实成本列 cost_price，原写不存在的 'cost' 键致成本泄露）
        if not can_view_cost:
            product.pop('cost_price', None)

        return api_response(code=0, data=product)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 客户列表
# --------------------------------------------------------
@data_bp.route('/customers', methods=['GET'])
def list_customers():
    """GET /api/data/customers 客户列表。"""
    try:
        page = request.args.get('page', 1, type=int)
        page_size = request.args.get('page_size', 20, type=int)
        keyword = request.args.get('keyword', '')
        # D-5：分页参数边界校验
        if page < 1 or not (1 <= page_size <= 100):
            return error_response(400, "page>=1 且 1<=page_size<=100"), 400

        db = _get_db()
        items = []
        total = 0
        if db:
            try:
                all_items = db.query_many('customers') or []
            except Exception:
                all_items = []
            # D-3：关键词过滤在"全量结果"上执行（原在分页后过滤且字段名错误——
            # 真实列 customer_name，c.get('name')/c.get('company') 恒空致搜索失效）
            if keyword:
                kw = keyword.lower()
                all_items = [c for c in all_items
                             if kw in (c.get('customer_name', '')
                                       + c.get('customer_id', '')).lower()]
            total = len(all_items)
            offset = (page - 1) * page_size
            items = all_items[offset:offset + page_size]

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空列表
        # 联系方式脱敏（D-2：对真实列 contact_phone 脱敏——原 c.get('contact') 恒空
        # 脱敏空操作，真实 contact_phone/contact_person 明文泄露）+ 计算可用额度
        for c in items:
            c['contact_phone'] = _mask_contact(c.get('contact_phone', ''))
            limit = c.get('credit_limit', 0) or 0
            used = c.get('credit_used', 0) or 0
            c['credit_available'] = max(0, limit - used)

        return api_response(code=0, data={
            "items": items, "total": total,
            "page": page, "page_size": page_size,
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 客户详情
# --------------------------------------------------------
@data_bp.route('/customers/<customer_id>', methods=['GET'])
def get_customer(customer_id):
    """GET /api/data/customers/<customer_id> 客户详情。"""
    try:
        db = _get_db()
        customer = None
        if db:
            try:
                customer = db.query_one('customers', {'customer_id': customer_id})
            except Exception:
                customer = None

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回 404
        if not customer:
            return error_response(404, f"客户 {customer_id} 不存在"), 404

        # 联系方式脱敏（D-2：对真实列 contact_phone 脱敏）+ 计算可用额度
        customer['contact_phone'] = _mask_contact(customer.get('contact_phone', ''))
        limit = customer.get('credit_limit', 0) or 0
        used = customer.get('credit_used', 0) or 0
        customer['credit_available'] = max(0, limit - used)

        return api_response(code=0, data=customer)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 产线列表
# --------------------------------------------------------
@data_bp.route('/production-lines', methods=['GET'])
def list_production_lines():
    """GET /api/data/production-lines 产线列表。"""
    try:
        db = _get_db()
        items = []
        if db:
            try:
                items = db.query_many('production_lines',
                                      order_by='line_id ASC') or []
            except Exception:
                items = []

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空列表
        return api_response(code=0, data={"items": items, "total": len(items)})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 仪表盘聚合（S4：订单/库存/质检/生产/客户 KPI）
# --------------------------------------------------------
@data_bp.route('/dashboard', methods=['GET'])
def dashboard():
    """GET /api/data/dashboard 仪表盘 KPI 聚合。

    - 订单：本月订单数/销售额（orders.total_amount，created_at 当月）
    - 库存：库存总量/库存金额（inventory 各阶段数量与价值列求和）
    - 质检：本月质检通过率（qc_records.result，pass / fail+rework+scrap）
    - 生产：本月工单数（work_orders.created_at 当月）
    - 客户：客户总数、本月新增（customers.created_at 当月）

    单项表缺失/查询失败时该项优雅降级为 0；DB 整体不可达返回 code=500 固定文案。
    """
    db = _get_db()
    data = {
        "order": {"month_count": 0, "month_amount": 0.0},
        "inventory": {"total_qty": 0, "total_value": 0.0},
        "qc": {"month_pass_rate": 0.0},
        "production": {"month_work_order_count": 0},
        "customer": {"total": 0, "month_new": 0},
    }
    if db is None:
        return error_response(500, "数据库不可用，请稍后再试"), 500

    # 订单：本月订单数/销售额
    try:
        row = db.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total_amount), 0) AS amount "
            "FROM orders WHERE created_at >= date_trunc('month', NOW())"
        ).fetchone()
        if row:
            m = row._mapping
            data["order"]["month_count"] = int(m.get("cnt") or 0)
            data["order"]["month_amount"] = float(m.get("amount") or 0)
    except Exception:
        pass

    # 库存：总量（五阶段求和）/ 金额（价值列求和）
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(raw + wip_cnc + wip_anode + wip_qc + finished), 0) AS qty, "
            "COALESCE(SUM(COALESCE(raw_value,0) + COALESCE(wip_value,0) "
            "+ COALESCE(finished_value,0)), 0) AS value "
            "FROM inventory"
        ).fetchone()
        if row:
            m = row._mapping
            data["inventory"]["total_qty"] = int(m.get("qty") or 0)
            data["inventory"]["total_value"] = float(m.get("value") or 0)
    except Exception:
        pass

    # 质检：本月通过率 = pass / (pass + fail + rework + scrap)
    try:
        row = db.execute(
            "SELECT COALESCE(COUNT(*) FILTER (WHERE result = 'pass'), 0) AS passed, "
            "COALESCE(COUNT(*) FILTER (WHERE result IN ('fail','rework','scrap')), 0) AS failed "
            "FROM qc_records WHERE created_at >= date_trunc('month', NOW())"
        ).fetchone()
        if row:
            m = row._mapping
            passed = int(m.get("passed") or 0)
            failed = int(m.get("failed") or 0)
            judged = passed + failed
            data["qc"]["month_pass_rate"] = round(passed / judged * 100, 2) if judged else 0.0
    except Exception:
        pass

    # 生产：本月工单数
    try:
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM work_orders "
            "WHERE created_at >= date_trunc('month', NOW())"
        ).fetchone()
        if row:
            data["production"]["month_work_order_count"] = int(row._mapping.get("cnt") or 0)
    except Exception:
        pass

    # 客户：总数/本月新增
    try:
        row = db.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS month_new "
            "FROM customers"
        ).fetchone()
        if row:
            m = row._mapping
            data["customer"]["total"] = int(m.get("total") or 0)
            data["customer"]["month_new"] = int(m.get("month_new") or 0)
    except Exception:
        pass

    return api_response(code=0, data=data)


# --------------------------------------------------------
# BOM查询
# --------------------------------------------------------
@data_bp.route('/bom/<product_code>', methods=['GET'])
def get_bom(product_code):
    """GET /api/data/bom/<product_code> BOM查询。"""
    try:
        db = _get_db()
        items = []
        if db:
            try:
                items = db.query_many('bom',
                                      filters={'product_code': product_code},
                                      order_by='seq ASC') or []
            except Exception:
                items = []

        # v6.58：移除 PostgreSQL 降级模拟数据——DB 不可用/查无均返回空组件列表
        return api_response(code=0, data={
            "product_code": product_code,
            "components": items,
            "total": len(items),
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
    assert data_bp is not None, "data_bp 未定义"
    hello_world(__name__, "data_bp 定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
