"""
标准 MCP Server 实现
====================
对应技术规格：§1.8 接口层 + MCP协议规范

功能：
1. JSON-RPC 2.0 消息处理
2. stdio 传输层（标准MCP传输方式）
3. SSE 传输层（HTTP Server-Sent Events）
4. initialize 握手（protocolVersion/capabilities 协商）
5. tools/list + tools/call 协议方法
6. 自动注册 ToolHub 中的工具为 MCP tools
7. 兼容 Trae、Claude Desktop 等标准 MCP 客户端

使用方式：
    # stdio 模式（标准MCP客户端连接）
    python -m prog.mcp.server --transport stdio

    # SSE 模式（HTTP服务）
    python -m prog.mcp.server --transport sse --port 8080

Trae 配置示例（.trae/mcp.json）:
    {
        "mcpServers": {
            "ai-factory": {
                "command": "python",
                "args": ["-m", "prog.mcp.server", "--transport", "stdio"],
                "cwd": "d:\\\\work\\\\INV-018"
            }
        }
    }

Claude Desktop 配置示例（claude_desktop_config.json）:
    {
        "mcpServers": {
            "ai-factory": {
                "command": "python",
                "args": ["-m", "prog.mcp.server", "--transport", "stdio"],
                "cwd": "d:\\\\work\\\\INV-018"
            }
        }
    }
"""

import json
import os
import time
import inspect
import uuid
from typing import Any, Callable, Dict, Optional

# v6.96 P1-8：tools/call 的 arguments 输入加固上限（防超大/超深嵌套 DoS）
_MAX_ARG_JSON_BYTES = 1024 * 1024  # arguments 序列化后上限 1MB
_MAX_ARG_DEPTH = 16  # arguments 嵌套深度上限


def _resolve_allowed_file_path(file_path: str) -> str:
    """解析并校验文件路径（v6.72 安全修复）：仅允许 uploads 目录内文件，拒绝路径穿越。

    与 prog/api/files_api.py 的 uploads 目录约定一致（KB_UPLOAD_DIR 可覆盖）。
    使用 realpath 归一化，防 `..` 穿越与符号链接逃逸。

    Args:
        file_path: 用户提供的文件路径

    Returns:
        规范化后的绝对路径

    Raises:
        ValueError: 路径不在 uploads 目录白名单内
    """
    if not file_path:
        raise ValueError("file_path 不能为空")
    base = os.environ.get("KB_UPLOAD_DIR", "")
    if not base:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
    base = os.path.realpath(base)
    real = os.path.realpath(file_path)
    if not (real == base or real.startswith(base + os.sep)):
        raise ValueError("文件路径不在允许的 uploads 目录内，已拒绝读取")
    return real


class MCPServer:
    """标准 MCP Server

    实现 MCP 协议规范：
    - JSON-RPC 2.0 消息格式
    - initialize/initialized 握手
    - tools/list 列出工具
    - tools/call 调用工具
    - 支持 stdio 和 SSE 两种传输层
    """

    PROTOCOL_VERSION = "2024-11-05"  # MCP协议版本
    SERVER_NAME = "ai-factory-mcp"
    SERVER_VERSION = "6.16"

    def __init__(self):
        self._tools = {}  # tool_name -> {description, inputSchema, handler}
        self._initialized = False
        self._register_builtin_tools()
        self._sync_skill_status()

    def _sync_skill_status(self):
        """启动时同步 skill_registry 安装状态（§1.3.2 Step 4 安装验证）。

        v6.77：进程内按依赖库探测 FS-01~10 是否安装并回写 skill_registry，
        保证 Agent 技能清单（list_agent_skills）展示真实可用性。
        """
        try:
            from prog.mcp.agent_tools import sync_skill_install_status
            sync_skill_install_status()
        except Exception:
            pass

    def _register_builtin_tools(self):
        """注册内置工具

        从 ToolHub 和 SkillRegistry 自动导入工具定义，
        转换为 MCP 标准格式（description + inputSchema + handler）
        """
        # 1. 从 ToolHub 导入（v6.96：统一走 get_instance() 单例，避免与
        # tools/list / tools/call 的 ToolHub.get_instance() 持有不同实例）
        try:
            from prog.mcp.tool_hub import ToolHub
            hub = ToolHub.get_instance()
            for tool in hub.list_tools():
                self.register_tool(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    input_schema=tool.get("parameters", {"type": "object", "properties": {}}),
                    handler=lambda params, t=tool: hub.call_tool(t["name"], params),
                )
        except Exception:
            pass

        # 2. 注册ISO导入工具
        self.register_tool(
            name="iso_import",
            description="导入ISO 9001/IATF 16949标准条款，支持从PDF/Word/文本文件导入，初始化业务流程规则",
            input_schema={
                "type": "object",
                "properties": {
                    "standard": {"type": "string", "enum": ["9001", "16949", "both"], "default": "both"},
                    "require_approval": {"type": "boolean", "default": True,
                                         "description": "v6.73 起固定为 True 忽略传入值（安全强制：ISO 导入必须经审批链）"},
                    "file_path": {"type": "string", "description": "ISO标准文件路径（支持.pdf/.docx/.doc/.txt/.md/.json），不传则使用预定义条款"},
                },
            },
            handler=self._handle_iso_import,
        )

        # 3. 注册规则查询工具
        self.register_tool(
            name="query_rules",
            description="查询当前业务规则配置",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string", "description": "规则ID（可选，不传则返回全部）"},
                },
            },
            handler=self._handle_query_rules,
        )

        # 4. 注册意图识别工具
        self.register_tool(
            name="recognize_intent",
            description="识别用户输入的意图",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "用户输入文本"},
                },
                "required": ["text"],
            },
            handler=self._handle_recognize_intent,
        )

        # 5. 注册文件解析工具
        self.register_tool(
            name="parse_file",
            description="解析文件内容（支持PDF/Word/Excel/图片OCR）",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件路径"},
                },
                "required": ["file_path"],
            },
            handler=self._handle_parse_file,
        )

        # 开源版：已移除 parse_drawing 工具注册（图纸解析，属商业版能力）

    def register_tool(self, name: str, description: str, input_schema: dict, handler: Callable):
        """注册MCP工具"""
        self._tools[name] = {
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
        }

    def handle_request(self, request: dict, user_context: dict = None) -> Optional[dict]:
        """处理JSON-RPC 2.0请求

        支持的方法：
        - initialize: MCP握手
        - notifications/initialized: 握手完成通知
        - tools/list: 列出所有工具
        - tools/call: 调用工具
        - ping: 心跳

        参数：
            request: JSON-RPC 2.0 请求字典
            user_context: 用户身份上下文（SSE 传输层认证后传入，
                          含 user_id/role，可选）
        """
        # v6.84：MCP 请求级 trace_id 接线（规格书 §4.7.2）——工具调用审计与
        # 写库钩子带上 trace_id；请求结束清理，避免线程复用串号
        try:
            from prog.runtime.trace import new_trace, clear_trace
        except Exception:
            new_trace = None
            clear_trace = None
        if new_trace is not None:
            new_trace()
        try:
            return self._handle_request_inner(request, user_context)
        finally:
            if clear_trace is not None:
                clear_trace()

    def _handle_request_inner(self, request: dict, user_context: dict = None) -> Optional[dict]:
        """JSON-RPC 2.0 请求分发（v6.84 从 handle_request 抽出，供 trace 包裹）。"""
        # v6.96 P1-10：非 dict 请求（批量数组等）直接兜底 -32603，
        # 不再对 request.get() 触发 AttributeError；方法体异常统一转 -32603。
        if not isinstance(request, dict):
            return self._error(None, -32603, "Invalid request: expected a JSON-RPC object")
        req_id = request.get("id")
        try:
            method = request.get("method")
            params = request.get("params", {})
            # v6.73 健壮性：params 非对象（字符串/数组/空）时返回错误而非触发 AttributeError
            # 无响应（外部 AI 可发畸形请求，协议层需容错）
            if not isinstance(params, dict):
                return self._error(req_id, -32602, "Invalid params: expected object")

            if method == "initialize":
                return self._handle_initialize(req_id, params)
            elif method == "notifications/initialized":
                self._initialized = True
                return None  # 通知不需要响应
            elif method == "tools/list":
                return self._handle_tools_list(req_id)
            elif method == "tools/call":
                return self._handle_tools_call(req_id, params, user_context)
            elif method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            else:
                return self._error(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            # P1-10：任何未处理异常兜底为内部错误，避免异常泄漏到传输层
            return self._error(req_id, -32603, f"Internal error: {e}")

    def _handle_initialize(self, req_id, params):
        """处理initialize握手"""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": True},
                },
                "serverInfo": {
                    "name": self.SERVER_NAME,
                    "version": self.SERVER_VERSION,
                },
            },
        }

    def _handle_tools_list(self, req_id):
        """列出所有工具（MCP标准格式）

        除 MCPServer 内置工具外，动态合并 ToolHub 已注册工具
        （含通过 /api/mcp/plugins/<name>/install 安装的插件工具），
        实现按需安装后无需重启即可被 MCP 客户端发现。
        """
        tools = []
        for name, tool in self._tools.items():
            tools.append({
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["inputSchema"],
            })
        # 动态合并 ToolHub 工具（内置 + 插件）
        try:
            from prog.mcp.tool_hub import ToolHub
            hub = ToolHub.get_instance()
            known = {t["name"] for t in tools}
            for tool in hub.list_tools():
                if tool["name"] in known:
                    continue
                tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("parameters", {"type": "object", "properties": {}}),
                })
        except Exception:
            pass
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    def _handle_tools_call(self, req_id, params, user_context: dict = None):
        """调用工具（§1.3.7 五步 + §1.3.8 审计记录）"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        _call_start = time.time()

        # v6.96 P1-8：arguments 输入加固（大小/嵌套深度上限），拒绝畸形载荷
        args_err = self._check_arguments_limits(arguments)
        if args_err:
            self._audit_tool_call(
                tool_name, arguments,
                {"success": False, "error": args_err},
                user_context, _call_start, error=args_err)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": args_err}],
                },
            }

        # v6.96 P0-1：工具级 RBAC 门禁前置——预注册内置工具与 ToolHub 动态工具
        # 统一过 check_tool_permission（原实现只在 tool is None 分支校验，导致
        # 预注册工具命中 _tools 直接执行 handler 绕过查询权限门禁）。
        try:
            from prog.mcp.agent_tools import check_tool_permission
        except Exception:
            check_tool_permission = None
        if check_tool_permission is not None:
            ok, perm_err = check_tool_permission(tool_name, user_context)
            if not ok:
                self._audit_tool_call(
                    tool_name, arguments,
                    {"success": False, "error": perm_err},
                    user_context, _call_start, error=perm_err)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": perm_err}],
                    },
                }

        # 先查 MCPServer 内置工具
        tool = self._tools.get(tool_name)
        # 未命中则动态查找 ToolHub（含按需安装的插件工具）
        if tool is None:
            try:
                from prog.mcp.tool_hub import ToolHub
                hub = ToolHub.get_instance()
                result = hub.call_tool(tool_name, arguments)
                self._audit_tool_call(tool_name, arguments, result,
                                      user_context, _call_start)
                if not result.get("success") and result.get("error") == f"工具 '{tool_name}' 不存在":
                    return self._error(req_id, -32602, f"Unknown tool: {tool_name}")
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                        ],
                    },
                }
            except Exception as e:
                return self._error(req_id, -32602, f"Unknown tool: {tool_name}")

        try:
            handler = tool["handler"]
            # 若 handler 接受 user_context 参数则传入，保持向后兼容
            try:
                _sig = inspect.signature(handler)
                if "user_context" in _sig.parameters:
                    result = handler(arguments, user_context=user_context)
                else:
                    result = handler(arguments)
            except (ValueError, TypeError):
                result = handler(arguments)
            self._audit_tool_call(tool_name, arguments, result,
                                  user_context, _call_start)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": str(result) if not isinstance(result, str) else result}
                    ],
                },
            }
        except Exception as e:
            self._audit_tool_call(tool_name, arguments,
                                  {"success": False, "error": str(e)},
                                  user_context, _call_start, error=str(e))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                },
            }

    @staticmethod
    def _check_arguments_limits(arguments) -> Optional[str]:
        """P1-8：校验 tools/call arguments 的类型/大小/嵌套深度上限。

        返回错误信息字符串，合法时返回 None。
        """
        if arguments is None:
            return None
        if not isinstance(arguments, dict):
            return "arguments 必须为 JSON 对象"
        try:
            size = len(json.dumps(arguments, ensure_ascii=False))
        except (TypeError, ValueError):
            return "arguments 含不可 JSON 序列化内容"
        if size > _MAX_ARG_JSON_BYTES:
            return f"arguments 过大（{size} 字节），超过上限 {_MAX_ARG_JSON_BYTES} 字节"
        if not MCPServer._check_args_depth(arguments, 0):
            return f"arguments 嵌套深度超过上限 {_MAX_ARG_DEPTH}"
        return None

    @staticmethod
    def _check_args_depth(node, depth: int) -> bool:
        """递归检查嵌套深度；深度超限即返回 False（递归深度被 depth 上限约束）。"""
        if depth > _MAX_ARG_DEPTH:
            return False
        if isinstance(node, dict):
            return all(MCPServer._check_args_depth(v, depth + 1) for v in node.values())
        if isinstance(node, (list, tuple)):
            return all(MCPServer._check_args_depth(v, depth + 1) for v in node)
        return True

    def _audit_tool_call(self, tool_name, arguments, result,
                         user_context: dict = None, call_start=None,
                         error: str = None):
        """MCP 工具调用审计（§1.3.8 mcp_tool_audit 表，失败静默降级）。"""
        try:
            from prog.mcp.agent_tools import _write_tool_audit
            agent_name = ""
            session_id = ""
            if isinstance(user_context, dict):
                agent_name = user_context.get("agent_name") or user_context.get("agent") or ""
                session_id = user_context.get("session_id") or ""
            exec_ms = int((time.time() - (call_start or time.time())) * 1000)
            status = "failed" if (error or (isinstance(result, dict)
                                            and result.get("success") is False)) else "success"
            _write_tool_audit(
                session_id=session_id,
                agent_name=agent_name or "mcp_client",
                mcp_server="file-skill-center",
                tool_name=tool_name,
                input_args=arguments,
                output_result=result if not isinstance(result, str) else {"text": result},
                execution_ms=exec_ms,
                status=status,
                error_message=error or (result.get("error") if isinstance(result, dict) else None),
            )
        except Exception:
            pass

    def _error(self, req_id, code, message):
        """返回JSON-RPC错误"""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    # ====== 工具处理函数 ======

    def _handle_iso_import(self, params):
        """处理ISO导入工具调用（商业版能力，开源版不可用）"""
        return json.dumps({"success": False,
                           "error": "ISO 导入为商业版能力，社区版不可用"},
                          ensure_ascii=False)

    def _handle_query_rules(self, params):
        """查询规则配置"""
        # 开源版：规则配置管理器（prog.rules，商业 know-how）不在开源范围
        try:
            from prog.rules.config_manager import get_config_manager
            mgr = get_config_manager()
        except Exception:
            return json.dumps({"error": "规则配置管理为商业版能力，开源版不可用"},
                              ensure_ascii=False)
        rule_id = params.get("rule_id")
        if rule_id:
            config = mgr.get_config(rule_id)
            return json.dumps({"rule_id": rule_id, "config": config}, ensure_ascii=False)
        else:
            versions = mgr.get_all_config_versions()
            return json.dumps(versions, ensure_ascii=False)

    def _handle_recognize_intent(self, params):
        """意图识别"""
        from prog.utils.intent_recognition import IntentRecognizer
        recognizer = IntentRecognizer()
        text = params.get("text", "")
        intent = recognizer.recognize(text)
        return json.dumps({
            "intent": intent.name,
            "confidence": intent.confidence,
            "params": intent.params,
            "source": intent.source,
        }, ensure_ascii=False)

    def _handle_parse_file(self, params):
        """文件解析"""
        file_path = params.get("file_path", "")
        ext = os.path.splitext(file_path)[1].lower()

        try:
            # v6.72 安全修复：路径白名单校验，拒绝读取 uploads 目录外文件（防任意文件读取）
            file_path = _resolve_allowed_file_path(file_path)
            if ext == ".pdf":
                from prog.mcp.file_skills import PDFReader
                reader = PDFReader()
                result = reader.execute({"file_path": file_path})
                return result.text if hasattr(result, 'text') else str(result)
            elif ext in (".docx", ".doc"):
                from prog.mcp.file_skills import WordReader
                reader = WordReader()
                result = reader.execute({"file_path": file_path})
                return result.text if hasattr(result, 'text') else str(result)
            elif ext in (".xlsx", ".xls", ".csv"):
                from prog.mcp.file_skills import ExcelReader
                reader = ExcelReader()
                result = reader.execute({"file_path": file_path})
                return result.text if hasattr(result, 'text') else str(result)
            elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
                from prog.mcp.file_skills import ImageReader
                reader = ImageReader()
                result = reader.execute({"file_path": file_path})
                return result.text if hasattr(result, 'text') else str(result)
            else:
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read()
        except ImportError as e:
            return f"依赖未安装: {e}"
        except Exception as e:
            return f"解析失败: {e}"


# ====== 传输层实现 ======

class StdioTransport:
    """stdio 传输层

    标准MCP传输方式，通过标准输入输出读写JSON-RPC消息。
    每条消息以换行符分隔。
    """

    def __init__(self, server: MCPServer):
        self.server = server

    def run(self):
        """启动stdio传输层"""
        import sys
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = self.server.handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                continue
            except Exception as e:
                sys.stderr.write(f"MCP Server Error: {e}\n")
                sys.stderr.flush()


class SSETransport:
    """SSE 传输层

    HTTP Server-Sent Events 传输方式。
    客户端通过 GET /sse 建立连接，通过 POST /messages 发送请求。
    """

    def __init__(self, server: MCPServer, port: int = 8080):
        self.server = server
        self.port = port

    def run(self):
        """启动SSE传输层"""
        from flask import Flask, Response, jsonify, request as flask_request
        from prog.api.auth import _decode_token
        import queue
        import threading

        app = Flask(__name__)
        clients = {}  # client_id -> queue
        clients_lock = threading.Lock()  # P1-7：clients 并发访问保护

        @app.route("/sse")
        def sse():
            # SSE 认证：EventSource 不支持自定义 header，从 query 参数 ?token=xxx 读取
            token = flask_request.args.get("token", "")
            try:
                payload = _decode_token(token) if token else None
            except Exception:
                payload = None
            if payload is None:
                return jsonify({"code": 401, "msg": "MCP 认证失败"}), 401

            def stream():
                # v6.96 P1-7：client_id 改用 UUID（原 str(id(thread)) 在线程复用
                # 时会漂移/冲突），并在 GeneratorExit/异常时用 finally 清理会话，
                # 避免 clients 字典持续泄漏。
                client_id = uuid.uuid4().hex
                q = queue.Queue()
                with clients_lock:
                    clients[client_id] = q
                try:
                    yield f"event: ready\ndata: {client_id}\n\n"
                    while True:
                        try:
                            data = q.get(timeout=30)
                            if data is None:
                                break
                            yield f"data: {data}\n\n"
                        except queue.Empty:
                            yield f": keepalive\n\n"
                finally:
                    with clients_lock:
                        clients.pop(client_id, None)
            return Response(stream(), mimetype="text/event-stream")

        @app.route("/messages", methods=["POST"])
        def messages():
            # Bearer token 认证
            auth_header = flask_request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
            try:
                payload = _decode_token(token) if token else None
            except Exception:
                payload = None
            if payload is None:
                return jsonify({"code": 401, "msg": "MCP 认证失败"}), 401
            user_context = {
                "user_id": payload.get("user_id", ""),
                "role": payload.get("role", ""),
            }

            request_data = flask_request.get_json()
            # v6.96 P1-7：SSE 会话关联——客户端从 /sse 的 ready 事件取 session_id，
            # POST 时随请求体/query 带回；响应写入该会话的队列，经 SSE 流回推，
            # 不再直接把 JSON 当 HTTP 响应返回（原实现客户端永远收不到响应）。
            session_id = ""
            if isinstance(request_data, dict):
                session_id = request_data.get("session_id") or ""
            if not session_id:
                session_id = flask_request.args.get("session_id") or ""
            with clients_lock:
                q = clients.get(session_id)
            if q is None:
                return jsonify({"code": 400, "msg": "未知的 MCP SSE 会话（缺少/错误 session_id）"}), 400
            response = self.server.handle_request(request_data, user_context)
            if response is not None:
                q.put(json.dumps(response, ensure_ascii=False))
            return "", 202

        app.run(host="127.0.0.1", port=self.port, threaded=True)


# ====== 命令行入口 ======

def main():
    """命令行入口"""
    import argparse
    parser = argparse.ArgumentParser(description="AI工厂管家 MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                        help="传输方式: stdio（默认）或 sse")
    parser.add_argument("--port", type=int, default=8080,
                        help="SSE模式端口号（默认8080）")
    args = parser.parse_args()

    server = MCPServer()

    if args.transport == "stdio":
        transport = StdioTransport(server)
    else:
        transport = SSETransport(server, args.port)

    transport.run()


if __name__ == "__main__":
    main()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world

    assert MCPServer is not None, "MCPServer 类未定义"
    assert StdioTransport is not None, "StdioTransport 类未定义"
    assert SSETransport is not None, "SSETransport 类未定义"
    # 验证服务器初始化与协议常量
    server = MCPServer()
    assert server.PROTOCOL_VERSION == "2024-11-05", "协议版本应为 2024-11-05"
    assert server.SERVER_NAME == "ai-factory-mcp", "服务器名称不正确"
    assert server._initialized is False, "初始化前 _initialized 应为 False"
    # 验证内置工具已注册
    assert "iso_import" in server._tools, "iso_import 工具未注册"
    assert "query_rules" in server._tools, "query_rules 工具未注册"
    assert "recognize_intent" in server._tools, "recognize_intent 工具未注册"
    assert "parse_file" in server._tools, "parse_file 工具未注册"
    # 验证 initialize 握手
    resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["jsonrpc"] == "2.0", "响应应为 JSON-RPC 2.0 格式"
    assert resp["result"]["protocolVersion"] == "2024-11-05", "协议版本不正确"
    assert "capabilities" in resp["result"], "响应应包含 capabilities"
    assert "serverInfo" in resp["result"], "响应应包含 serverInfo"
    # 验证 tools/list
    resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = [t["name"] for t in resp["result"]["tools"]]
    assert "recognize_intent" in tool_names, "tools/list 应包含 recognize_intent"
    hello_world(__name__, "MCPServer 协议握手与工具注册验证完整")


from prog.core.debug import DEBUG

if DEBUG:
    _self_test()
