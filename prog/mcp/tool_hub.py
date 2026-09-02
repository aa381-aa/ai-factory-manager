"""
MCP工具中心模块

文件用途：
    AI工厂管家的MCP（Model Context Protocol）工具中心，
    提供统一的工具注册、调用、管理能力。
    内置库存查询、订单查询、产品查询、通知创建等业务工具，
    支持运行时动态注册第三方工具。

对应技术规格章节：
    - §1.3 MCP工具中心
    - §1.3.3 MCP技能注册机制

替代demo：
    替代 demo server.py 中工具调用散落在各处的现状。
    demo中意图识别后直接调用data_manager方法，无统一工具抽象，
    本工具中心将业务能力收敛为可注册、可发现的MCP工具。

设计说明：
    1. ToolHub 为单例类，全系统共享同一工具注册表
    2. 每个工具包含 name / handler / description / parameters 四要素
    3. 内置工具在实例化时自动注册
    4. call_tool 统一返回 dict，包含 success / data / error 字段
"""

from typing import Any, Callable, Dict, List, Optional


class ToolHub:
    """MCP工具中心（单例）。

    设计意图：
        集中管理所有MCP工具的注册、发现与调用，解耦工具调用方与实现方。

    属性：
        _tools: 工具名 -> 工具信息字典的映射
        _instance: 单例实例
    """

    _instance: Optional["ToolHub"] = None

    def __init__(self) -> None:
        """初始化工具注册表并注册内置工具。"""
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._register_builtin_tools()

    @classmethod
    def get_instance(cls) -> "ToolHub":
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_tool(
        self,
        name: str,
        handler: Callable,
        description: str,
        parameters: Dict[str, Any],
    ) -> None:
        """注册工具。

        参数：
            name: 工具唯一名称
            handler: 处理函数，签名 (params: dict) -> dict
            description: 工具描述
            parameters: 工具参数Schema（JSON Schema格式）
        """
        self._tools[name] = {
            "name": name,
            "handler": handler,
            "description": description,
            "parameters": parameters,
        }

    def unregister_tool(self, name: str) -> bool:
        """注销工具。

        参数：
            name: 工具名称

        返回：
            True 表示注销成功，False 表示工具不存在
        """
        return self._tools.pop(name, None) is not None

    def call_tool(self, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用工具。

        参数：
            name: 工具名称
            params: 调用参数字典

        返回：
            包含 success / data / error 字段的结果字典；
            工具不存在或执行异常时返回 success=False，不抛异常。
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"success": False, "data": None, "error": f"工具 '{name}' 不存在"}
        try:
            result = tool["handler"](params or {})
            # 处理函数返回 dict 时直接使用，否则包装为 data
            if isinstance(result, dict):
                return result
            return {"success": True, "data": result, "error": None}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有已注册工具。

        返回：
            工具信息列表，每项包含 name / description / parameters
            （不含 handler，避免序列化问题）。
        """
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in self._tools.values()
        ]

    def get_tool_schema(self, name: str) -> Dict[str, Any]:
        """获取工具Schema。

        参数：
            name: 工具名称

        返回：
            工具的完整Schema字典（name / description / parameters）；
            工具不存在时返回空字典。
        """
        tool = self._tools.get(name)
        if tool is None:
            return {}
        return {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        }

    # ============================================================
    # 内置工具注册
    # ============================================================
    def _register_builtin_tools(self) -> None:
        """注册内置业务工具。"""
        self.register_tool(
            "query_inventory",
            self._tool_query_inventory,
            "查询库存信息，支持按产品编码过滤",
            {
                "type": "object",
                "properties": {
                    "product_code": {
                        "type": "string",
                        "description": "产品编码（可选，不传则查全部）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回记录上限",
                    },
                },
            },
        )
        self.register_tool(
            "query_order",
            self._tool_query_order,
            "查询订单信息，支持按订单号或状态过滤",
            {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "订单号（可选）"},
                    "status": {"type": "string", "description": "订单状态（可选）"},
                    "limit": {"type": "integer", "description": "返回记录上限"},
                },
            },
        )
        self.register_tool(
            "query_product",
            self._tool_query_product,
            "查询产品信息，支持按产品编码、类别或关键字搜索",
            {
                "type": "object",
                "properties": {
                    "product_code": {"type": "string", "description": "产品编码（可选）"},
                    "category": {"type": "string", "description": "产品类别（可选）"},
                    "keyword": {"type": "string", "description": "搜索关键字（可选）"},
                    "limit": {"type": "integer", "description": "返回记录上限"},
                },
            },
        )
        # v6.72 安全修复：create_notification 写工具已从 MCP 暴露面剔除——外部 AI
        # 经 tools/call 可向任意 target_user 写入 notifications 伪造通知（社会工程）；
        # 通知创建改由内部 prog.api.notifications_api.create_notification 完成。

    # ============================================================
    # 内置工具处理函数
    # ============================================================
    @staticmethod
    def _tool_query_inventory(params: Dict[str, Any]) -> Dict[str, Any]:
        """查询库存工具处理函数。"""
        from prog.core.database import get_database

        db = get_database()
        product_code = params.get("product_code")
        limit = params.get("limit")
        if product_code:
            row = db.query_one("inventory", {"product_code": product_code})
            data = [row] if row else []
        else:
            data = db.query_many("inventory", limit=limit, order_by="product_code")
        return {"success": True, "data": data, "error": None}

    @staticmethod
    def _tool_query_order(params: Dict[str, Any]) -> Dict[str, Any]:
        """查询订单工具处理函数。"""
        from prog.core.database import get_database

        db = get_database()
        order_id = params.get("order_id")
        status = params.get("status")
        limit = params.get("limit")
        if order_id:
            row = db.query_one("orders", {"order_id": order_id})
            data = [row] if row else []
        else:
            filters: Dict[str, Any] = {}
            if status:
                filters["status"] = status
            data = db.query_many(
                "orders",
                filters=filters or None,
                limit=limit,
                order_by="order_date DESC",
            )
        return {"success": True, "data": data, "error": None}

    @staticmethod
    def _tool_query_product(params: Dict[str, Any]) -> Dict[str, Any]:
        """查询产品工具处理函数。"""
        from prog.core.database import get_database

        db = get_database()
        product_code = params.get("product_code")
        category = params.get("category")
        keyword = params.get("keyword")
        limit = params.get("limit")
        if product_code:
            row = db.query_one("products", {"product_code": product_code})
            data = [row] if row else []
        elif keyword or category:
            # 使用 ILIKE 模糊搜索
            sql = "SELECT * FROM products WHERE 1=1"
            sql_params: Dict[str, Any] = {}
            if keyword:
                sql += " AND (product_code ILIKE :kw OR product_name ILIKE :kw OR spec ILIKE :kw)"
                sql_params["kw"] = f"%{keyword}%"
            if category:
                sql += " AND category = :category"
                sql_params["category"] = category
            sql += " ORDER BY product_code"
            if limit:
                sql += f" LIMIT {int(limit)}"
            result = db.execute(sql, sql_params)
            data = [dict(r._mapping) for r in result.fetchall()]
        else:
            data = db.query_many("products", limit=limit, order_by="product_code")
        return {"success": True, "data": data, "error": None}


def get_tool_hub() -> ToolHub:
    """模块级便捷函数：获取工具中心单例。"""
    return ToolHub.get_instance()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world

    assert ToolHub is not None, "ToolHub 类未定义"
    assert get_tool_hub is not None, "get_tool_hub 函数未定义"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG

if DEBUG:
    _self_test()
