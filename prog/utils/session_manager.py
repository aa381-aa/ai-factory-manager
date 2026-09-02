"""
会话管理器（业务软件层 re-export）
==================================
框架能力：SessionManager 会话管理（会话 CRUD + 对话历史 + token 窗口裁剪/摘要
压缩，Redis 注入/内存降级）由AI工厂管家框架运行时（prog/runtime）提供。
业务侧会话数据结构与归档表见 §1.1.3。
本文件仅作 re-export。
"""
from prog.runtime.session_manager import SessionManager

__all__ = ["SessionManager"]
