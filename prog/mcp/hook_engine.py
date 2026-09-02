"""
Hook生命周期引擎模块

文件用途：
    提供AI工厂管家的Hook生命周期管理，支持在关键操作前后及异常时
    插入自定义处理逻辑，实现解耦的横切关注点（审计、通知、规则校验等）。

对应技术规格章节：
    - §1.6 Hook生命周期机制（pre_action, post_action, on_error）

替代demo：
    替代 demo server.py 中规则校验、通知、日志硬编码在主流程的现状。
    demo中下单流程把信用额度检查、折扣校验、通知发送直接耦合在chat函数内，
    本Hook引擎将其拆分为可独立注册、可插拔的Hook链。

三种Hook说明：
    - pre_action: 操作前Hook。例：下单前检查信用额度、校验图纸版本、
                  排产前检查产能/物料/设备。任一返回block则中断主操作。
    - post_action: 操作后Hook。例：下单后发送通知、写操作日志、
                   更新统计数据。不阻断主流程，仅副作用执行。
    - on_error: 错误Hook。例：审核失败时记录错误日志、发送告警、
                回滚事务。仅在主操作抛异常时触发。

Hook执行顺序和中断机制说明：
    1. 同一hook_type下的Hook按注册顺序priority升序执行（priority越小越先）。
    2. pre_action链中任一Hook返回 result.blocked=True，
       则立即中断后续Hook与主操作，整体返回blocked结果。
    3. post_action与on_error链不中断，全部执行（即使某Hook失败也继续后续），
       以确保审计、通知等关键副作用不丢失。
    4. on_error链在主操作抛异常时触发，异常信息通过context传入。
"""

from typing import Any, Callable, Dict, List, Optional


class HookResult:
    """单次Hook执行结果。

    用于控制Hook链的中断与结果回传。
    """

    def __init__(self, success: bool = True, blocked: bool = False,
                 message: str = "", data: Optional[Dict[str, Any]] = None) -> None:
        self.success = success
        self.blocked = blocked
        self.message = message
        self.data = data or {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "success": self.success,
            "blocked": self.blocked,
            "message": self.message,
            "data": self.data,
        }


class HookContext:
    """Hook执行上下文。

    携带操作类型、操作参数、当前用户、会话、以及错误Hook所需的异常信息。
    所有Hook共享同一上下文实例，可读取或补充上下文数据。
    """

    def __init__(self, action: str, params: Optional[Dict[str, Any]] = None,
                 user: Optional[Dict[str, Any]] = None,
                 session: Optional[Dict[str, Any]] = None,
                 error: Optional[Exception] = None) -> None:
        self.action = action
        self.params = params or {}
        self.user = user or {}
        self.session = session or {}
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        result: Dict[str, Any] = {
            "action": self.action,
            "params": self.params,
            "user": self.user,
            "session": self.session,
        }
        if self.error is not None:
            result["error"] = str(self.error)
        return result


class HookEngine:
    """Hook生命周期引擎。

    设计意图：
        提供统一的Hook注册与执行入口，支持pre/post/error三类Hook链，
        实现操作前后横切逻辑的解耦与可插拔。

    属性：
        _hooks: {event: [(priority, handler), ...]} 有序Hook列表
    """

    # 三种Hook类型常量
    PRE_ACTION = "pre_action"
    POST_ACTION = "post_action"
    ON_ERROR = "on_error"

    # 支持的业务事件
    SUPPORTED_EVENTS = [
        "before_order_create",
        "after_order_create",
        "before_order_audit",
        "after_order_audit",
        "before_production_start",
        "after_production_complete",
        "after_qc_pass",
        "after_qc_fail",
        "before_inventory_update",
        "after_inventory_update",
        "before_shipment",
        "after_shipment",
        "before_payment",
        "after_payment",
        "on_error",
    ]

    def __init__(self) -> None:
        """初始化钩子注册表。"""
        # {event: [(priority, handler), ...]}，按 priority 升序排列
        self._hooks: Dict[str, List[tuple]] = {}

    def register_hook(self, event: str, handler: Callable, priority: int = 0) -> None:
        """注册钩子。

        参数：
            event: 事件名称（如 before_order_create / after_order_create）
            handler: 处理函数，签名 (context) -> Any
            priority: 优先级，数值越小越先执行（默认0）

        说明：
            同一事件可注册多个钩子，按priority升序执行。
        """
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append((priority, handler))
        # 按 priority 升序排序
        self._hooks[event].sort(key=lambda x: x[0])

    def unregister_hook(self, event: str, handler: Callable) -> bool:
        """注销钩子。

        参数：
            event: 事件名称
            handler: 要注销的处理函数

        返回：
            True 表示注销成功，False 表示未找到
        """
        if event not in self._hooks:
            return False
        original_len = len(self._hooks[event])
        self._hooks[event] = [
            (p, h) for p, h in self._hooks[event] if h is not handler
        ]
        if not self._hooks[event]:
            del self._hooks[event]
        return len(self._hooks.get(event, [])) < original_len or event not in self._hooks

    def trigger(self, event: str, context: Any) -> List[Any]:
        """触发钩子，返回所有处理器结果。

        参数：
            event: 事件名称
            context: 执行上下文（HookContext 实例或字典）

        返回：
            所有处理器返回值组成的列表，按priority升序排列；
            某个处理器抛异常时记录错误信息并继续执行后续钩子。
        """
        results: List[Any] = []
        hooks = self._hooks.get(event, [])
        for _priority, handler in hooks:
            try:
                result = handler(context)
                results.append(result)
            except Exception as e:
                # 记录错误但继续执行后续钩子，确保审计/通知等副作用不丢失
                results.append({"error": str(e), "handler": getattr(handler, "__name__", str(handler))})
        return results

    def get_hooks(self, event: str) -> List[tuple]:
        """获取事件的钩子列表。

        参数：
            event: 事件名称

        返回：
            [(priority, handler), ...] 列表的副本
        """
        return list(self._hooks.get(event, []))

    def execute_hooks(self, hook_type: str, context: HookContext) -> HookResult:
        """执行指定类型的Hook链（兼容接口）。

        参数：
            hook_type: Hook类型（pre_action/post_action/on_error）
            context: 执行上下文

        返回：
            HookResult；pre_action链中任一Hook blocked=True 时立即中断并返回；
            post_action/on_error链汇总结果，不中断。
        """
        results = self.trigger(hook_type, context)
        # 检查是否有 blocked 结果
        for r in results:
            if isinstance(r, HookResult) and r.blocked:
                return r
            if isinstance(r, dict) and r.get("blocked"):
                return HookResult(
                    success=False,
                    blocked=True,
                    message=r.get("message", ""),
                    data=r.get("data"),
                )
        return HookResult(success=True, blocked=False, data={"results": results})

    def clear(self, hook_type: Optional[str] = None) -> None:
        """清空Hook（用于测试或重载）。

        参数：
            hook_type: 指定类型则只清该类型；None清空全部。
        """
        if hook_type is None:
            self._hooks.clear()
        else:
            self._hooks.pop(hook_type, None)


# 默认全局Hook引擎实例
default_engine = HookEngine()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world

    assert HookEngine is not None, "HookEngine 类未定义"
    assert HookResult is not None, "HookResult 类未定义"
    assert HookContext is not None, "HookContext 类未定义"
    # 验证基本功能：注册 -> 触发 -> 注销
    engine = HookEngine()
    called = []

    def handler(ctx):
        called.append(True)
        return "ok"

    engine.register_hook("test_event", handler, priority=0)
    results = engine.trigger("test_event", {"key": "value"})
    assert results == ["ok"], f"trigger 结果不符预期: {results}"
    assert called == [True], "handler 未被调用"
    engine.unregister_hook("test_event", handler)
    assert engine.get_hooks("test_event") == [], "注销后钩子列表应为空"
    hello_world(__name__, "Hook注册/触发/注销验证通过")


from prog.core.debug import DEBUG

if DEBUG:
    _self_test()
