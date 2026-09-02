"""
MCP工具中心模块

文件用途：
    AI工厂管家社区版v5.11的MCP（Model Context Protocol）工具中心，
    提供文件技能、技能注册、Hook生命周期管理等核心能力。

对应技术规格章节：
    - §1.3 MCP工具中心 - 文件技能（PDF/Word/Excel/图片读写）
    - §1.3.3 MCP技能注册机制
    - §1.6 Hook生命周期机制（pre_action, post_action, on_error）

替代demo：
    替代 demo server.py 中无文件处理能力的缺陷，
    补齐AI工厂管家对结构化文档（PDF/Word/Excel/图片）的读写支持。

子模块：
    - file_skills: 文件读写技能（PDFReader/WordReader/ExcelReader/ImageReader/FileWriter）
    - skill_registry: MCP技能注册中心
    - hook_engine: Hook生命周期引擎
"""

from .file_skills import FileSkill, SkillResult
from .skill_registry import SkillRegistry
from .hook_engine import HookEngine, HookResult
from prog.mcp.server import MCPServer, StdioTransport, SSETransport

__all__ = [
    "FileSkill",
    "SkillResult",
    "SkillRegistry",
    "HookEngine",
    "HookResult",
    "MCPServer",
    "StdioTransport",
    "SSETransport",
]

__version__ = "5.11.0"
