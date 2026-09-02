"""
API 路由模块
============

模块用途：
    AI工厂管家的Flask API路由层，按业务域拆分为多个Blueprint。

技术规格章节：
    - §1.1.3 Coordinator Agent（API层调用Coordinator分发）
    - §3.2~§3.8 各领域Agent（对应API调用对应Agent）
    - §2 LLM安全门控（API层透传门控结果）

Blueprint 划分：
    - chat.py: 对话接口（同步 + SSE 流式 + 会话历史）
    - auth.py: 认证接口（登录/登出/用户信息）
    - orders.py: 订单接口（CRUD + 状态流转 + 时间线）
    - inventory_api.py: 库存接口（五阶段查询/流转/缺料/流水）
    - audit_api.py: 审核链接口（七层审核链）
    - notifications_api.py: 通知接口（列表/已读/删除 + 事件订阅）
    - llm_api.py: LLM配置接口（配置/测试/模型/用量）
    - data_api.py: 基础数据接口（产品/客户/产线）
    - system_api.py: 系统监控接口（健康/状态/配置/版本）
    - intent_rules_api.py: 意图规则管理接口（CRUD + 审批流转）
    - training.py: 训练系统接口（L1-L4 + ISO 导入）
    - mcp_api.py: MCP 插件管理接口
    - files_api.py: 文件上传/解析/读取接口

架构说明：
    - 每个Blueprint聚焦单一业务域，便于维护
    - 业务操作类接口（orders/inventory等）调用对应Agent处理
    - 对话类接口（chat）调用CoordinatorAgent统一分发
    - 所有接口经JWT鉴权（auth模块签发）
    - Flask Blueprint统一在register_blueprints()中注册
"""

from typing import Any

# 各Blueprint的导入与注册（实现时取消注释）
# from .chat import chat_bp
# from .auth import auth_bp
# from .orders import orders_bp
# from .inventory_api import inventory_bp
# from .audit_api import audit_bp
# from .notifications_api import notifications_bp
# from .llm_api import llm_bp
# from .data_api import data_bp
# from .system_api import system_bp


def register_blueprints(app: Any) -> None:
    """
    在Flask app上注册所有API Blueprint。

    设计意图：
        统一注册入口，app工厂函数调用此方法完成全部路由挂载，
        避免在各子模块分散注册导致的循环依赖。

    参数：
        app: Flask应用实例

    注册的Blueprint列表：
        - chat_bp        -> /api/chat
        - auth_bp        -> /api/auth
        - orders_bp      -> /api/orders
        - inventory_bp   -> /api/inventory
        - audit_bp       -> /api/audit
        - notifications_bp -> /api/notifications
        - llm_bp         -> /api/llm
        - data_bp        -> /api/data
        - system_bp      -> /api/system
    """
    from flask import Blueprint

    # 对话接口
    try:
        from prog.api.chat import chat_bp
        app.register_blueprint(chat_bp)
    except Exception as e:
        print(f"[WARN] 注册chat_bp失败：{e}")

    # 订单接口
    try:
        from prog.api.orders import orders_bp
        app.register_blueprint(orders_bp)
    except Exception as e:
        print(f"[WARN] 注册orders_bp失败：{e}")

    # 其他接口（如已实现）
    for module_path, bp_var in [
        ("prog.api.auth", "auth_bp"),
        ("prog.api.data_api", "data_bp"),
        ("prog.api.system_api", "system_bp"),
        ("prog.api.llm_api", "llm_bp"),
        ("prog.api.inventory_api", "inventory_bp"),
        ("prog.api.audit_api", "audit_bp"),
        ("prog.api.notifications_api", "notifications_bp"),
        ("prog.api.training", "training_bp"),
        ("prog.api.mcp_api", "mcp_bp"),
        ("prog.api.files_api", "files_bp"),
        ("prog.api.documents_api", "documents_bp"),
    ]:
        try:
            import importlib
            module = importlib.import_module(module_path)
            bp = getattr(module, bp_var, None)
            if bp is not None:
                app.register_blueprint(bp)
        except ImportError:
            pass
        except Exception as e:
            print(f"[WARN] 注册{bp_var}失败：{e}")


__all__ = [
    # "chat_bp",
    # "auth_bp",
    # "orders_bp",
    # "inventory_bp",
    # "audit_bp",
    # "notifications_bp",
    # "llm_bp",
    # "data_bp",
    # "system_bp",
    "register_blueprints",
]
