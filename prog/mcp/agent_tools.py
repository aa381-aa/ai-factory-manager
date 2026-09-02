"""
Agent 统一工具调用通道
======================

文件用途：
    实现规格书 §1.3.5/§1.3.7/§1.3.8 的 Agent 统一工具调用：
    - 各 Agent 不再直接 import 具体技能实现，统一经本通道调用文件技能/
      ToolHub 业务工具（工具发现 -> 选择 -> 执行 -> 结果处理 -> 审计记录）
    - 每次调用写入 mcp_tool_audit 表（§1.3.8：调用者/工具名/参数/结果/耗时/状态）
    - skill_registry 表安装状态同步（§1.3.2 Step 4 安装验证/§1.3.3 注册表）
    - agent_skill_registry 查询（§1.3.9 Agent Skill 按需加载）

对应技术规格章节：
    - §1.3.5 各Agent调用场景
    - §1.3.7 MCP工具调用统一流程（5步）
    - §1.3.8 MCP工具审计表
    - §1.3.9 Agent Skill按需加载与能力管理

设计说明：
    1. 进程内调用（不走 stdio/SSE），等价于 MCP tools/call 的服务端执行，
       复用同一技能实现与审计落库，避免 MCP 客户端进程启动开销
    2. 审计写入失败静默降级（不阻断业务，与操作日志降级策略一致）
    3. 未注册工具走 ToolHub 兜底（含按需安装的插件工具）
"""

import os
import time
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 工具级 RBAC 门禁（v6.82：业务数据工具不得绕过查询权限）
# ============================================================
# ToolHub 业务查询工具 -> PermissionSystem RBAC action 映射：
#   MCP tools/call 与进程内 call_agent_tool 均须先过此门禁，
#   防止"绕过查询流程 required_permission 直接查库"的权限盲区。
# 未列出的工具（文件技能等，不触业务库）不拦截。
_TOOL_PERMISSION_MAP = {
    "query_inventory": "query_inventory",
    "query_order": "query_order",
    "query_product": "query_product",
}


def check_tool_permission(tool_name: str,
                          user_context: Optional[Dict] = None) -> Tuple[bool, str]:
    """业务数据工具调用前的 RBAC 校验（v6.82）。

    规则：
        - 不在 _TOOL_PERMISSION_MAP 的工具（文件技能等）直接放行
        - admin 通配；其余角色走 PermissionSystem.check_permission
        - 无用户上下文（未登录/内部直调未传角色）对受控工具拒绝
          （fail-closed：宁可拒错不可漏查）

    返回：
        (allowed, error_message)；allowed=False 时 error_message 为
        拒绝原因（文案与多跳未授权提示同风格）。
    """
    action = _TOOL_PERMISSION_MAP.get(tool_name)
    if not action:
        return True, ""

    role = ""
    user_info: Dict[str, Any] = {}
    if isinstance(user_context, dict):
        user = user_context.get("user") or user_context
        if isinstance(user, dict):
            role = user.get("role") or user.get("user_role") or ""
            user_info = user  # 传递完整用户信息（含 user_id/department），
                              # 供临时授权 override（TG-04）按用户身份兜底
    if not role:
        return False, (f"由于「{tool_name}」（查询操作 {action}）未获得授权，"
                       "已拒绝该工具调用（缺少用户上下文，请经登录会话调用）")
    if role == "admin":
        return True, ""
    try:
        from prog.runtime.permission import PermissionSystem
        ok = bool(PermissionSystem().check_permission(user_info, action))
    except Exception:
        ok = False
    if not ok:
        return False, (f"由于「{tool_name}」（查询操作 {action}）未获得授权，"
                       "已拒绝该工具调用（可联系管理员授权或以有权限角色重试）")
    return True, ""

# ============================================================
# 内部工具（Agent 文件技能）实现映射：tool_name -> 调用函数
# ============================================================
# skill_registry 的 mcp_tool_name 与本映射对齐（§1.3.3）：
#   FS-01 read_pdf / FS-03 read_word / FS-05 read_excel / FS-07 read_image
#   FS-02 write_pdf / FS-04 write_word / FS-06 write_excel / FS-08 edit_image / FS-10 archive_files
#   + 业务扩展：parse_drawing（§2.3.2）/ parse_process_card（§2.3.5）/ read_file 等
_INTERNAL_TOOL_NAMES = frozenset({
    "parse_drawing", "parse_process_card",
    "read_file", "read_pdf", "read_word", "read_excel", "read_image",
    "write_file", "write_pdf", "write_word", "write_excel", "edit_image",
    "archive_files",
    "list_files", "upload_drawing", "download_drawing", "parse_excel",
    "generate_report",
})


def _call_internal_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """执行 Agent 文件技能（进程内 MCP 工具等价实现）。

    支持工具：
        parse_drawing      : 图纸 PDF 解析（§2.3.2 Pipeline）
        parse_process_card : 工艺卡 PDF 解析（§2.3.5）
        read_file/read_pdf/read_word/read_excel/read_image : 文件读取
        write_file/write_pdf/write_word/write_excel/edit_image/archive_files : 文件生成/处理
        list_files         : 文件列表
    """
    # 开源版：已移除 parse_drawing/parse_process_card（图纸/工艺卡解析，属商业版能力）
    if tool_name in ("read_file", "read_pdf", "read_word", "read_excel", "read_image"):
        from prog.mcp.file_skills import (PDFReader, WordReader, ExcelReader,
                                          ImageReader)
        path = params.get("file_path") or params.get("file_url")
        if not path:
            return {"success": False, "error": f"{tool_name} 缺少 file_path 参数"}
        if tool_name == "read_file":
            # v6.96 P1-6：读取路径过 uploads 白名单校验（与 parse_file/parse_drawing
            # 同机制），拒绝任意路径读取；原实现直接 open(path) 可读服务器任意文件。
            from prog.mcp.server import _resolve_allowed_file_path
            try:
                path = _resolve_allowed_file_path(path)
            except Exception as e:
                return {"success": False, "error": f"文件路径未通过白名单校验: {e}"}
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except UnicodeDecodeError:
                with open(path, "rb") as f:
                    text = f.read().decode("utf-8", errors="replace")
            return {"success": True, "text": text or "",
                    "data": {"text_len": len(text or "")}}
        # v6.97 P1-18：四类读取工具同样过 uploads 白名单校验（与 read_file 同机制），
        # 原实现直接 execute 可读服务器任意路径（M8 P1-18 白名单绕过）。
        from prog.mcp.server import _resolve_allowed_file_path
        try:
            path = _resolve_allowed_file_path(path)
        except Exception as e:
            return {"success": False, "error": f"文件路径未通过白名单校验: {e}"}
        reader_cls = {"read_pdf": PDFReader, "read_word": WordReader,
                      "read_excel": ExcelReader, "read_image": ImageReader}[tool_name]
        r = reader_cls().execute({"file_path": path})
        if not r.success:
            return {"success": False, "error": r.error or f"{tool_name} 读取失败"}
        return {"success": True, "text": r.text or "", "data": r.data or {}}
    if tool_name in ("write_file", "write_pdf", "write_word", "write_excel",
                     "edit_image", "archive_files"):
        # 生成类技能：优先专用实现，缺省走 FileWriter/FileSkills.write_file
        from prog.mcp.file_skills import FileSkills
        fs = FileSkills(file_storage=None)
        handler = getattr(fs, tool_name, None)
        if handler is not None and callable(handler):
            return handler(**params)
        # FS-02/04/06/08/10 专用生成实现（见 _WRITE_SKILL_HANDLERS）
        return _call_write_skill(tool_name, params)
    if tool_name in ("list_files", "upload_drawing", "download_drawing",
                     "parse_excel", "generate_report"):
        from prog.mcp.file_skills import FileSkills
        fs = FileSkills(file_storage=None)
        handler = getattr(fs, tool_name, None)
        if handler is None:
            return {"success": False, "error": f"技能方法 '{tool_name}' 不存在"}
        return handler(**params)
    return {"success": False, "error": f"未注册内部工具 '{tool_name}'"}


# 生成类技能实现映射（FS-02/04/06/08/10，§1.3.1）
def _call_write_skill(tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """生成类技能（write_pdf/write_word/write_excel/edit_image/archive_files）。

    各实现均注入统一工具通道（agent_tools），入库 mcp_tool_audit；
    依赖库缺失时返回明确错误（不静默忽略，符合 §1.3.2 安全约束④）。
    """
    if tool_name == "write_excel":
        return _write_excel(params)
    if tool_name == "write_word":
        return _write_word(params)
    if tool_name == "edit_image":
        return _edit_image(params)
    if tool_name == "archive_files":
        return _archive_files(params)
    if tool_name == "write_pdf":
        return _write_pdf(params)
    return {"success": False, "error": f"未实现生成技能 '{tool_name}'"}


def _write_excel(params: Dict[str, Any]) -> Dict[str, Any]:
    """FS-06 Excel 生成：写入 .xlsx（openpyxl）/ .csv 降级。"""
    file_path = params.get("file_path") or params.get("path")
    if not file_path:
        return {"success": False, "error": "缺少 file_path 参数"}
    data = params.get("data") or []
    try:
        if str(file_path).lower().endswith(".csv"):
            import csv as _csv
            import io as _io
            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
            with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                if data:
                    writer = _csv.DictWriter(f, fieldnames=list(data[0].keys()))
                    writer.writeheader()
                    writer.writerows(data)
            return {"success": True, "path": file_path}
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        if data:
            headers = list(data[0].keys())
            ws.append(headers)
            for row in data:
                ws.append([row.get(h) for h in headers])
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        wb.save(file_path)
        return {"success": True, "path": file_path}
    except ImportError:
        return {"success": False, "error": "Excel 生成依赖 openpyxl 未安装，请先 pip install openpyxl"}
    except Exception as e:
        return {"success": False, "error": f"Excel 生成失败：{e}"}


def _write_word(params: Dict[str, Any]) -> Dict[str, Any]:
    """FS-04 Word 生成：写入 .docx（python-docx），支持 {key} 模板替换。"""
    file_path = params.get("file_path") or params.get("path")
    if not file_path:
        return {"success": False, "error": "缺少 file_path 参数"}
    content = params.get("content", "")
    data = params.get("data") or {}
    try:
        import docx
        doc = docx.Document()
        for para in str(content).split("\n"):
            text = para
            for k, v in data.items():
                text = text.replace("{" + str(k) + "}", str(v))
            doc.add_paragraph(text)
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        doc.save(file_path)
        return {"success": True, "path": file_path}
    except ImportError:
        return {"success": False, "error": "Word 生成依赖 python-docx 未安装，请先 pip install python-docx"}
    except Exception as e:
        return {"success": False, "error": f"Word 生成失败：{e}"}


def _write_pdf(params: Dict[str, Any]) -> Dict[str, Any]:
    """FS-02 PDF 生成：写入 .pdf（reportlab 可选，fpdf 降级）。"""
    file_path = params.get("file_path") or params.get("path")
    if not file_path:
        return {"success": False, "error": "缺少 file_path 参数"}
    content = params.get("content", "")
    data = params.get("data") or {}
    try:
        text = str(content)
        for k, v in data.items():
            text = text.replace("{" + str(k) + "}", str(v))
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        try:
            from reportlab.pdfgen import canvas as _canvas
            c = _canvas.Canvas(file_path)
            y = 780
            for line in text.split("\n"):
                c.drawString(60, y, line[:80])
                y -= 14
            c.save()
        except ImportError:
            from fpdf import FPDF
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("helvetica", size=12)
            for line in text.split("\n"):
                pdf.cell(0, 8, line[:80], ln=True)
            pdf.output(file_path)
        return {"success": True, "path": file_path}
    except ImportError:
        return {"success": False, "error": "PDF 生成依赖 reportlab/fpdf 未安装，请先 pip install reportlab"}
    except Exception as e:
        return {"success": False, "error": f"PDF 生成失败：{e}"}


def _edit_image(params: Dict[str, Any]) -> Dict[str, Any]:
    """FS-08 图片生成处理：裁剪/缩放/水印（Pillow）。"""
    file_path = params.get("file_path") or params.get("path")
    if not file_path:
        return {"success": False, "error": "缺少 file_path 参数"}
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(file_path)
        size = params.get("size")
        if size:
            img = img.resize(tuple(int(x) for x in size))
        watermark = params.get("watermark")
        if watermark:
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("simhei.ttf", 24)
            except Exception:
                font = ImageFont.load_default()
            draw.text((10, img.height - 40), str(watermark), fill=(255, 0, 0), font=font)
        img.save(file_path)
        return {"success": True, "path": file_path}
    except ImportError:
        return {"success": False, "error": "图片处理依赖 Pillow 未安装，请先 pip install Pillow"}
    except Exception as e:
        return {"success": False, "error": f"图片处理失败：{e}"}


def _archive_files(params: Dict[str, Any]) -> Dict[str, Any]:
    """FS-10 压缩文件处理：解压 zip / 打包导出。"""
    file_path = params.get("file_path") or params.get("path")
    target = params.get("target") or params.get("dest_dir")
    try:
        import zipfile
        if file_path and file_path.lower().endswith(".zip") and target:
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(file_path) as zf:
                zf.extractall(target)
            return {"success": True, "extracted_to": target, "count": len(zf.namelist())}
        source = params.get("source") or params.get("files") or []
        if not file_path:
            return {"success": False, "error": "缺少 file_path（目标 zip）"}
        with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in source:
                if os.path.isfile(p):
                    zf.write(p, os.path.basename(p))
        return {"success": True, "path": file_path}
    except Exception as e:
        return {"success": False, "error": f"压缩处理失败：{e}"}


def _get_db():
    """获取数据库访问实例（审计落库用），不可用时返回 None。"""
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _write_tool_audit(session_id: str, agent_name: str, mcp_server: str,
                      tool_name: str, input_args: Any, output_result: Any,
                      execution_ms: int, status: str,
                      error_message: Optional[str] = None) -> None:
    """写 mcp_tool_audit 审计（§1.3.8）。失败静默降级。"""
    db = _get_db()
    if db is None:
        return
    import json as _json
    try:
        db.insert("mcp_tool_audit", {
            "session_id": session_id or "",
            "agent_name": agent_name or "",
            "mcp_server": mcp_server or "file-skill-center",
            "tool_name": tool_name or "",
            "input_args": _json.dumps(input_args or {}, ensure_ascii=False),
            "output_result": _json.dumps(output_result or {}, ensure_ascii=False),
            "execution_ms": int(execution_ms or 0),
            "status": status or "success",
            "error_message": (error_message or "")[:1000],
        })
    except Exception:
        pass


def call_agent_tool(agent_name: str, tool_name: str,
                    params: Optional[Dict[str, Any]] = None,
                    session_id: str = "",
                    mcp_server: str = "file-skill-center",
                    user_context: Optional[Dict[str, Any]] = None,
                    timeout: float = 60.0) -> Dict[str, Any]:
    """Agent 统一工具调用（§1.3.7 五步 + §1.3.8 审计）。

    参数：
        agent_name: 调用 Agent 名称（如 technical_agent）
        tool_name:  工具名（parse_drawing / read_file / query_inventory ...）
        params:     调用参数字典
        session_id: 会话ID（审计用）
        mcp_server: 关联 MCP Server 名（默认 file-skill-center）
        user_context: 用户上下文（含 user.role，v6.82 业务数据工具
                      RBAC 门禁用；受控工具缺用户上下文时拒绝）
        timeout:    v6.96 P1-11 执行超时（秒），超时返回失败而非无限阻塞

    返回：
        {"success": True, "data": ..., "error": None}；
        内部工具未注册时回落 ToolHub；异常不抛出。
    """
    import threading
    start = time.time()
    params = params or {}
    state = {"status": "success", "error": None, "result": None}

    def _execute() -> None:
        """在工作线程内执行工具（P1-11：可被 join(timeout) 超时中断返回）。"""
        try:
            # ⓪ 工具级 RBAC 门禁（v6.82）：业务数据查询工具先校验权限
            #    （防绕过查询流程 required_permission 直查库；拒绝走审计）
            ok, perm_err = check_tool_permission(tool_name, user_context)
            if not ok:
                raise PermissionError(perm_err)

            # ① 工具发现/选择：先内部文件技能，未命中回落 ToolHub（含插件工具）
            if tool_name in _INTERNAL_TOOL_NAMES:
                r = _call_internal_tool(tool_name, params)
            else:
                from prog.mcp.tool_hub import ToolHub
                r = ToolHub.get_instance().call_tool(tool_name, params)
            if isinstance(r, dict) and not r.get("success", True):
                state["status"] = "failed"
                state["error"] = r.get("error") or r.get("data")
            state["result"] = r
        except Exception as e:
            state["status"] = "failed"
            state["error"] = str(e)
            state["result"] = {"success": False, "error": str(e)}

    worker = threading.Thread(target=_execute, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # P1-11：超时后不再等待（守护线程随进程退出），记录失败审计
        state["status"] = "failed"
        state["error"] = f"工具调用超时（>{timeout}s）：{tool_name}"
        state["result"] = {"success": False, "error": state["error"]}

    status = state["status"]
    error = state["error"]
    result = state["result"]
    execution_ms = int((time.time() - start) * 1000)
    _write_tool_audit(session_id, agent_name, mcp_server, tool_name,
                      params, result, execution_ms, status, error)
    return {
        "success": status == "success",
        "data": result,
        "error": error,
        "execution_ms": execution_ms,
    }


# ============================================================
# skill_registry 安装状态同步（§1.3.2 Step 4 安装验证 / §1.3.3）
# ============================================================
def _skill_install_check() -> Dict[str, bool]:
    """按依赖库探测各技能是否已安装（§1.3.2 Step 4 安装验证）。

    返回：
        {skill_id: is_installed}
    """
    def has(pkg: str) -> bool:
        try:
            __import__(pkg)
            return True
        except Exception:
            return False

    return {
        "FS-01": has("pdfplumber") or has("pypdf"),   # PDF 读取
        "FS-02": has("reportlab") or has("weasyprint"),  # PDF 生成
        "FS-03": has("docx"),                          # Word 读取
        "FS-04": has("docx"),                          # Word 生成
        "FS-05": has("openpyxl") or has("pandas"),     # Excel 读取
        "FS-06": has("openpyxl") or has("xlsxwriter"), # Excel 生成
        "FS-07": has("PIL") and has("pytesseract"),    # 图片读取
        "FS-08": has("PIL"),                           # 图片生成
        "FS-09": has("ezdxf"),                         # CAD 读取
        "FS-10": True,                                 # 压缩（标准库）
    }


def sync_skill_install_status(db: Any = None) -> Dict[str, bool]:
    """同步 skill_registry 表安装状态（§1.3.2 Step 4 安装验证后更新）。

    依赖已安装 -> is_installed=TRUE + installed_at=now；
    未安装 -> is_installed=FALSE。失败静默降级（内存返回）。
    """
    db = db or _get_db()
    status_map = _skill_install_check()
    if db is not None:
        try:
            for skill_id, installed in status_map.items():
                db.update("skill_registry", {"skill_id": skill_id}, {
                    "is_installed": bool(installed),
                    "installed_at": None,  # DB 层 NOW() 兜底
                })
        except Exception:
            pass
    return status_map


def list_agent_skills(agent_name: str, db: Any = None) -> List[Dict[str, Any]]:
    """查询 Agent 已注册技能（§1.3.9 agent_skill_registry）。

    返回：
        技能注册列表（skill_id/skill_name/skill_type/load_strategy/...）；
        DB 不可用时返回空列表。
    """
    db = db or _get_db()
    if db is None:
        return []
    try:
        rows = db.query_many("agent_skill_registry",
                             {"owner_agent": agent_name}) or []
        return list(rows)
    except Exception:
        return []
