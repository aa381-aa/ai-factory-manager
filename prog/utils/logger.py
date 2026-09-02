"""
结构化日志（业务软件层 re-export）
==================================
框架能力：Logger 结构化日志（JSON Lines 输出 + 敏感字段自动脱敏 + audit/agent/
llm 三类专用日志，自动附加 trace_id）由AI工厂管家框架运行时（prog/runtime）提供。
业务侧日志变量与 error_codes 联动见 §A.0。
本文件仅作 re-export。
"""
from prog.runtime.logger import Logger

__all__ = ["Logger"]
