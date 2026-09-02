"""
MCP 插件动态加载器
==================
功能：
    支持按需安装/卸载第三方 MCP 工具（插件），无需重启服务：
    1. 扫描插件目录（默认 prog/mcp/plugins/），读取每个子目录的 manifest.json
    2. 动态导入 handler 模块并注册到 ToolHub
    3. 卸载时从 ToolHub 移除并记录状态
    4. 已安装插件清单持久化到 mcp_plugins 表（无 DB 时降级为内存记录）

插件目录约定：
    prog/mcp/plugins/<plugin_name>/
        manifest.json   # 插件与工具定义
        handler.py      # 工具实现（可选，默认由 manifest.handler 指向模块）

manifest.json 格式：
    {
        "name": "example_tools",
        "description": "示例工具集",
        "version": "1.0.0",
        "tools": [
            {
                "name": "example_hello",
                "description": "示例问候工具",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "姓名"}
                    },
                    "required": ["name"]
                }
            }
        ],
        "handler": "handler"   # 默认处理模块（相对插件目录），可省略
    }

handler.py 约定：
    每个工具名对应一个同名函数：def <tool_name>(params: dict) -> dict
    返回值直接作为工具调用结果（dict）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

_DEFAULT_PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "plugins")


class PluginManager:
    """MCP 插件管理器（单例）。

    负责扫描插件目录、加载/卸载插件、持久化安装记录。
    """

    _instance: Optional["PluginManager"] = None

    def __init__(self, plugin_dir: Optional[str] = None) -> None:
        self.plugin_dir = plugin_dir or _DEFAULT_PLUGIN_DIR
        # 插件名 -> {"manifest": dict, "module": Any, "path": str, "installed_at": float}
        self._loaded: Dict[str, Dict[str, Any]] = {}
        self._load_persisted()

    @classmethod
    def get_instance(cls, plugin_dir: Optional[str] = None) -> "PluginManager":
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls(plugin_dir)
        return cls._instance

    # --------------------------------------------------------
    # 持久化
    # --------------------------------------------------------
    def _db(self) -> Any:
        """获取数据库对象（可降级为 None）。"""
        try:
            from prog.core.database import get_database
            return get_database()
        except Exception:
            return None

    def _load_persisted(self) -> None:
        """加载 DB 中已安装插件记录（仅记录，不自动重新执行 handler 加载）。"""
        db = self._db()
        if db is None:
            return
        try:
            rows = db.query_many("mcp_plugins") or []
            for row in rows:
                if row.get("plugin_name") and row["plugin_name"] not in self._loaded:
                    self._loaded[row["plugin_name"]] = {
                        "manifest": json.loads(row.get("manifest_json") or "{}"),
                        "module": None,
                        "path": row.get("path", ""),
                        "installed_at": float(row.get("installed_at") or 0),
                    }
        except Exception:
            pass

    def _persist(self, plugin_name: str, manifest: Dict[str, Any],
                 path: str) -> None:
        """持久化插件安装记录到 DB。"""
        db = self._db()
        if db is None:
            return
        try:
            from prog.core.database import get_database
            db = get_database()
            db.insert("mcp_plugins", {
                "plugin_name": plugin_name,
                "version": manifest.get("version", "1.0.0"),
                "description": manifest.get("description", ""),
                "path": path,
                "manifest_json": json.dumps(manifest, ensure_ascii=False),
                "installed_at": time.time(),
            })
        except Exception:
            pass

    def _remove_persist(self, plugin_name: str) -> None:
        """从 DB 移除插件安装记录。"""
        db = self._db()
        if db is None:
            return
        try:
            db.delete("mcp_plugins", {"plugin_name": plugin_name})
        except Exception:
            pass

    # --------------------------------------------------------
    # 目录扫描与加载
    # --------------------------------------------------------
    def scan_available(self) -> List[Dict[str, Any]]:
        """扫描插件目录，返回可安装但未安装的插件清单。"""
        available: List[Dict[str, Any]] = []
        if not os.path.isdir(self.plugin_dir):
            return available
        for entry in sorted(os.listdir(self.plugin_dir)):
            sub = os.path.join(self.plugin_dir, entry)
            if not os.path.isdir(sub):
                continue
            manifest = self._read_manifest(sub)
            if manifest is None:
                continue
            name = manifest.get("name", entry)
            available.append({
                "name": name,
                "version": manifest.get("version", "1.0.0"),
                "description": manifest.get("description", ""),
                "installed": name in self._loaded,
                "path": sub,
            })
        return available

    @staticmethod
    def _read_manifest(plugin_path: str) -> Optional[Dict[str, Any]]:
        """读取插件目录下的 manifest.json，并做基础结构校验（P1-9 加固）。

        拒绝非 dict / 缺 name / 缺 tools 的畸形 manifest，防止静默加载
        无 handler 或空工具清单的插件。
        """
        manifest_path = os.path.join(plugin_path, "manifest.json")
        if not os.path.isfile(manifest_path):
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            return None
        if not isinstance(manifest, dict):
            return None
        if not isinstance(manifest.get("name"), str) or not manifest.get("name"):
            return None
        if not isinstance(manifest.get("tools"), list):
            return None
        return manifest

    def install(self, plugin_name: str) -> Dict[str, Any]:
        """安装指定插件（扫描目录 → 加载 manifest → 注册工具到 ToolHub）。

        参数：
            plugin_name: 插件名（对应插件目录名）

        返回：
            {"success": bool, "tools": [...], "error": str|None}
        """
        if plugin_name in self._loaded:
            return {"success": False, "tools": [], "error": f"插件 '{plugin_name}' 已安装"}
        plugin_path = os.path.join(self.plugin_dir, plugin_name)
        if not os.path.isdir(plugin_path):
            return {"success": False, "tools": [], "error": f"插件目录不存在: {plugin_path}"}
        manifest = self._read_manifest(plugin_path)
        if manifest is None:
            return {"success": False, "tools": [], "error": "插件缺少 manifest.json"}

        handler = self._load_handler(plugin_path, manifest)
        tools = manifest.get("tools", [])
        registered = []
        try:
            from prog.mcp.tool_hub import ToolHub
            hub = ToolHub.get_instance()
            for tool in tools:
                name = tool.get("name")
                if not name:
                    continue
                hub.register_tool(
                    name=name,
                    handler=self._make_handler(name, handler),
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters", {"type": "object", "properties": {}}),
                )
                registered.append(name)
        except Exception as e:
            return {"success": False, "tools": registered, "error": str(e)}

        self._loaded[plugin_name] = {
            "manifest": manifest,
            "module": handler,
            "path": plugin_path,
            "installed_at": time.time(),
        }
        self._persist(plugin_name, manifest, plugin_path)
        return {"success": True, "tools": registered, "error": None}

    def uninstall(self, plugin_name: str) -> Dict[str, Any]:
        """卸载插件（从 ToolHub 移除其全部工具）。"""
        info = self._loaded.get(plugin_name)
        if info is None:
            return {"success": False, "error": f"插件 '{plugin_name}' 未安装"}
        try:
            from prog.mcp.tool_hub import ToolHub
            hub = ToolHub.get_instance()
            for tool in info["manifest"].get("tools", []):
                hub.unregister_tool(tool.get("name"))
        except Exception as e:
            return {"success": False, "error": str(e)}
        self._loaded.pop(plugin_name, None)
        self._remove_persist(plugin_name)
        return {"success": True, "error": None}

    def list_installed(self) -> List[Dict[str, Any]]:
        """列出已安装插件及工具。"""
        result = []
        for name, info in self._loaded.items():
            result.append({
                "name": name,
                "version": info["manifest"].get("version", "1.0.0"),
                "description": info["manifest"].get("description", ""),
                "tools": [t.get("name") for t in info["manifest"].get("tools", [])],
                "installed_at": info.get("installed_at", 0),
            })
        return result

    # --------------------------------------------------------
    # handler 加载与工具构造
    # --------------------------------------------------------
    def _load_handler(self, plugin_path: str, manifest: Dict[str, Any]) -> Any:
        """加载插件 handler 模块（P1-9：handler 名标识符白名单 + 路径穿越防护）。"""
        handler_mod = manifest.get("handler", "handler")
        # P1-9：handler 名必须是安全标识符，禁止绝对路径/相对路径穿越
        if not isinstance(handler_mod, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", handler_mod):
            return None
        plugin_real = os.path.realpath(plugin_path)
        module_path = os.path.realpath(os.path.join(plugin_path, f"{handler_mod}.py"))
        # P1-9：模块路径必须落在插件目录内（realpath 归一化 + os.sep 边界）
        if not (module_path == plugin_real or module_path.startswith(plugin_real + os.sep)):
            return None
        if not os.path.isfile(module_path):
            return None
        spec = importlib.util.spec_from_file_location(
            f"prog_mcp_plugin_{os.path.basename(plugin_real)}", module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def _make_handler(self, tool_name: str, module: Any) -> Any:
        """构造 ToolHub 工具处理函数。"""
        def _handler(params: Dict[str, Any]) -> Dict[str, Any]:
            if module is not None:
                fn = getattr(module, tool_name, None)
                if callable(fn):
                    result = fn(params or {})
                    if isinstance(result, dict):
                        return result
                    return {"success": True, "data": result, "error": None}
            return {"success": False, "data": None,
                    "error": f"插件工具 '{tool_name}' 无实现"}
        return _handler


def get_plugin_manager() -> PluginManager:
    """模块级便捷函数：获取插件管理器单例。"""
    return PluginManager.get_instance()


__all__ = ["PluginManager", "get_plugin_manager", "_DEFAULT_PLUGIN_DIR"]
