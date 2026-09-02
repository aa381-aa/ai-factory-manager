"""
共享工具模块

文件用途：
    AI工厂管家社区版v5.11的共享工具集，提供HTML渲染、意图识别、
    会话管理、结构化日志等横切能力，供各Agent与HTTP层复用。

对应技术规格章节：
    - §1.3 Agent调用共享工具
    - §1.4.2 训练数据流贯穿意图识别与会话
    - §A.0 系统配置和错误码定义（Logger使用统一错误码）

替代demo：
    替代 demo server.py 中散落的 render_html_table/render_html_card/
    render_status_badge、extract_intent、内存字典会话存储等工具函数，
    收敛为可独立测试的共享模块。

子模块：
    - html_helpers: HTML渲染辅助（表格/卡片/状态徽章/进度条）
    - intent_recognition: 双重意图识别器（规则 + LLM）
    - session_manager: Redis会话管理器
    - logger: 结构化日志（审计/Agent/LLM三类专用日志）
"""

from .html_helpers import HTMLHelper
from .intent_recognition import IntentRecognizer, Intent
from .session_manager import SessionManager
from .logger import Logger

__all__ = [
    "HTMLHelper",
    "IntentRecognizer",
    "Intent",
    "SessionManager",
    "Logger",
]
