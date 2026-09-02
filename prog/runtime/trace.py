"""
统一追踪上下文模块（框架版，§4.7.2.1 补正项① trace 链路追踪）
==============================================================
文件用途：
    提供请求级 trace_id，贯穿 Agent 运行时全链路：
    请求入口 -> Coordinator 路由 -> Agent 处理 -> LLM 调用
    -> 七层审核链 -> 日志记录，实现端到端可观测性。

对应技术规格章节：
    - §4.7.2.1 补正项①：可观测性（trace链路追踪，框架 v1.1 已提取）

实现说明：
    - 使用 contextvars 实现线程安全隔离，并发请求各自持有独立 trace_id
    - new_trace() 在请求入口调用（Coordinator/API层），
      下游模块通过 get_trace_id() 读取并附加到日志/审核记录
    - 无 trace_id 时返回空串，不影响未接入模块的原有行为

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 统一 trace_id 贯穿「审核链 → Agent → LLM → 数据库」全链路，替代跨日志手工关联（SPEC §4 可观测性，§4.7.2.1 补正项①，v1.1 已提取）
        - contextvars 线程安全隔离：并发请求各自持有独立 trace_id，请求结束清理防串号（SPEC §4）
        - trace_id 请求入口接线（v6.84）：HTTP 请求级（create_app before_request new_trace / teardown_request clear_trace）、MCP JSON-RPC 请求级（handle_request）、调度任务级（scheduler._execute）（CHANGELOG v40）
        - 审核链复用：AuditEngine.audit() 复用当前 trace_id 作为 chain_id（无 trace 回退 uuid4），实现审核链 → Agent → 日志端到端关联（SPEC §4）
    对外接口（方法/API）：
        - new_trace() -> str：请求入口生成新的 trace_id 并绑定当前上下文（32 位十六进制，完整 128 位熵）（SPEC §4）
        - get_trace_id() -> str：读取当前上下文 trace_id，未启用追踪时返回空串（SPEC §4）
        - set_trace_id(trace_id) -> str：显式设置当前上下文 trace_id（外部已生成时使用）（SPEC §4）
        - clear_trace()：清除当前上下文 trace_id（请求结束清理，防串号）（SPEC §4 / CHANGELOG v40）
    错误处理要求：
        - 无 trace_id：get_trace_id() 返回空串，不影响未接入模块的原有行为（SPEC §4）
"""

import contextvars
import uuid

# 当前上下文的 trace_id（空串=未启用追踪）
_trace_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "trace_id", default="")


def new_trace() -> str:
    """生成新的 trace_id 并绑定到当前上下文。

    Returns:
        str: 新生成的 trace_id（32位十六进制，完整 128 位熵）
    """
    trace_id = uuid.uuid4().hex
    _trace_id_var.set(trace_id)
    return trace_id


def get_trace_id() -> str:
    """获取当前上下文的 trace_id。

    Returns:
        str: trace_id；未启用追踪时返回空串
    """
    return _trace_id_var.get()


def set_trace_id(trace_id: str) -> str:
    """显式设置当前上下文的 trace_id（外部已生成时使用）。

    Args:
        trace_id: 外部传入的 trace_id

    Returns:
        str: 设置后的 trace_id
    """
    _trace_id_var.set(trace_id or "")
    return trace_id or ""


def clear_trace() -> None:
    """清除当前上下文的 trace_id。"""
    _trace_id_var.set("")
# ============================================================
# O2：轻量 span 追踪（不引入 opentelemetry）
# ============================================================
# 追加实现说明：
#   - start_span(name) 创建 Span 并压入当前上下文 span 栈；
#   - span.end(attrs=None) 记录耗时并输出 event="span" 结构化日志
#     （runtime.logger.Logger），随后从栈中弹出自身；
#   - get_active_spans() 返回当前 span 栈（副本），供嵌套调用查看；
#   - 既有 new_trace / get_trace_id / set_trace_id / clear_trace 接口不变。
# ============================================================

import time

# 当前上下文的 span 栈（contextvars 线程安全隔离）
_spans_var: contextvars.ContextVar = contextvars.ContextVar(
    "active_spans", default=[])


class Span:
    """轻量 span：start_span 返回，end(attrs) 记录耗时到结构化日志。

    属性：
        name: span 名称
        start_time: 开始时间（epoch 秒）
        start_ns: 开始性能计数（用于耗时计算）
        attrs: 附加属性（end 时合并）
        duration_ms: 结束后的耗时（毫秒，end 前为 None）
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.start_time = time.time()
        self.start_ns = time.perf_counter()
        self.attrs: dict = {}
        self.duration_ms: float = None
        self._ended = False

    def end(self, attrs: dict = None) -> None:
        """结束 span：记录耗时并输出 event="span" 结构化日志。

        参数:
            attrs: 附加属性字典（合并进日志 extra），可选
        """
        if self._ended:
            return
        self._ended = True
        self.duration_ms = (time.perf_counter() - self.start_ns) * 1000.0
        if attrs:
            try:
                self.attrs.update(attrs)
            except Exception:
                pass
        try:
            from prog.runtime.logger import Logger
            logger = Logger.get_logger("runtime.trace")
            logger.info(
                "span | %s | %.2fms", self.name, self.duration_ms,
                extra={
                    "event": "span",
                    "span_name": self.name,
                    "duration_ms": round(self.duration_ms, 3),
                    "trace_id": get_trace_id(),
                    "attrs": self.attrs,
                })
        except Exception:
            pass
        # 从当前 span 栈弹出自身（仅当处于栈顶，避免非栈序 end 破坏栈）
        try:
            stack = _spans_var.get()
            if stack and stack[-1] is self:
                _spans_var.set(stack[:-1])
        except Exception:
            pass


def start_span(name: str) -> Span:
    """启动 span 并压入当前上下文 span 栈。

    参数:
        name: span 名称（如 "llm_call" / "db_query"）

    返回:
        Span 实例；完成时调用 span.end(attrs=None)。
    """
    span = Span(name)
    try:
        stack = list(_spans_var.get())
        stack.append(span)
        _spans_var.set(stack)
    except Exception:
        pass
    return span


def get_active_spans() -> list:
    """返回当前上下文 span 栈（list 副本，未启用时为空列表）。

    嵌套调用可用于输出调用链信息（如请求超时告警附加 span 名称列表）。
    """
    try:
        return list(_spans_var.get())
    except Exception:
        return []
