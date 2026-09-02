"""
统一追踪上下文（业务软件层 re-export）
======================================
框架能力：trace 链路追踪（new_trace/get_trace_id/set_trace_id/clear_trace，
contextvars 线程安全隔离，chain_id 复用 trace_id）由AI工厂管家框架运行时
（prog/runtime）提供。本文件仅作 re-export。
"""
from prog.runtime.trace import new_trace, get_trace_id, set_trace_id, clear_trace

__all__ = ["new_trace", "get_trace_id", "set_trace_id", "clear_trace"]
