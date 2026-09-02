"""
HTML渲染辅助工具模块

文件用途：
    提供AI工厂管家前端展示所需的HTML片段渲染能力，包括表格、卡片、
    状态徽章、进度条。供各Agent输出结构化展示卡片时调用。

对应技术规格章节：
    - §1.3 Agent输出渲染为HTML卡片展示给前端

替代demo：
    替代 demo server.py 中的 render_html_table()、render_html_card()、
    render_status_badge()（demo中实际命名为 _tag/_rich/_progress/_status_tag）。
    demo中这些函数直接拼字符串且与Flask耦合，本模块解耦为纯函数工具类。
"""

from html import escape
from typing import List, Optional, Sequence


class HTMLHelper:
    """HTML渲染辅助工具。

    设计意图：
        将HTML片段生成逻辑从Agent/路由中抽离，统一转义与样式约定，
        便于前端直接innerHTML展示，也便于单元测试快照比对。
    """

    # 状态色映射（业务通用）
    STATUS_COLORS = {
        "pass": "green",
        "fail": "red",
        "pending": "yellow",
        "draft": "gray",
        "running": "blue",
        "completed": "green",
        "shipped": "blue",
        "paid": "green",
        "closed": "gray",
    }

    @staticmethod
    def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]],
                     classes: str = "") -> str:
        """渲染HTML表格。

        参数：
            headers: 表头字符串列表
            rows: 二维数据行，每行为单元格值列表
            classes: 附加到<table>的CSS类名（默认空）

        返回：
            str: 完整的<table>HTML字符串，单元格内容自动HTML转义
        """
        cls_attr = f' class="{escape(classes)}"' if classes else ""

        # 构建表头
        th_cells = "".join(
            f"<th>{HTMLHelper._escape(h)}</th>" for h in headers
        )
        thead = f"<thead><tr>{th_cells}</tr></thead>"

        # 构建数据行
        tbody_rows = []
        for row in rows:
            td_cells = "".join(
                f"<td>{HTMLHelper._escape(cell)}</td>" for cell in row
            )
            tbody_rows.append(f"<tr>{td_cells}</tr>")
        tbody = f"<tbody>{''.join(tbody_rows)}</tbody>"

        return f"<table{cls_attr} border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;'>{thead}{tbody}</table>"

    @staticmethod
    def render_card(title: str, content: str, color: str = "blue") -> str:
        """渲染HTML卡片。

        参数：
            title: 卡片标题
            content: 卡片正文HTML（可为表格或富文本）
            color: 卡片边框颜色（blue/green/red/yellow/gray，默认blue）

        返回：
            str: 完整卡片HTML字符串
        """
        # 颜色到十六进制的映射
        color_map = {
            "blue": "#3b82f6",
            "green": "#10b981",
            "red": "#ef4444",
            "yellow": "#f59e0b",
            "gray": "#6b7280",
        }
        border_color = color_map.get(color, color_map["blue"])

        return (
            f'<div style="border:1px solid {border_color};'
            f'border-left:4px solid {border_color};'
            f'border-radius:4px;padding:12px;margin:8px 0;">'
            f'<div style="font-weight:bold;color:{border_color};'
            f'margin-bottom:8px;font-size:14px;">{escape(title)}</div>'
            f'<div style="font-size:13px;line-height:1.6;">{content}</div>'
            f'</div>'
        )

    @staticmethod
    def render_status_badge(status: str, status_map: Optional[dict] = None) -> str:
        """渲染状态徽章。

        参数：
            status: 状态值（如 'pass'/'fail'/'pending'/'running'）
            status_map: 可选的自定义 {status: 文案} 映射；
                        未提供时使用status原值作为文案

        返回：
            str: <span class="badge">HTML字符串，颜色由STATUS_COLORS决定
        """
        # 获取显示文案
        if status_map and status in status_map:
            text = status_map[status]
        else:
            text = status

        # 获取颜色
        color = HTMLHelper.STATUS_COLORS.get(status, "gray")
        color_map = {
            "green": "#10b981",
            "red": "#ef4444",
            "yellow": "#f59e0b",
            "gray": "#6b7280",
            "blue": "#3b82f6",
        }
        bg_color = color_map.get(color, color_map["gray"])

        return (
            f'<span class="badge" style="display:inline-block;'
            f'padding:2px 8px;border-radius:12px;'
            f'background-color:{bg_color};color:white;'
            f'font-size:12px;font-weight:bold;">{escape(text)}</span>'
        )

    @staticmethod
    def render_progress_bar(progress: float, color: str = "green") -> str:
        """渲染进度条。

        参数：
            progress: 进度百分比0~100（float）
            color: 进度条颜色（green/blue/yellow/red，默认green）

        返回：
            str: 进度条HTML字符串（含外层容器与填充条）
        """
        # 限制进度值在0~100范围内
        pct = max(0.0, min(100.0, float(progress)))
        pct_int = int(pct)

        color_map = {
            "green": "#10b981",
            "blue": "#3b82f6",
            "yellow": "#f59e0b",
            "red": "#ef4444",
        }
        bar_color = color_map.get(color, color_map["green"])

        return (
            f'<div style="display:inline-flex;align-items:center;gap:4px;">'
            f'<span style="display:inline-block;width:80px;height:8px;'
            f'background-color:#e5e7eb;border-radius:4px;overflow:hidden;">'
            f'<span style="display:block;width:{pct_int}%;height:100%;'
            f'background-color:{bar_color};border-radius:4px;"></span>'
            f'</span>'
            f'<span style="font-size:12px;color:#374151;">{pct_int}%</span>'
            f'</div>'
        )

    @staticmethod
    def _escape(text: object) -> str:
        """HTML转义内部辅助方法。

        将对象转为字符串后进行HTML实体转义，
        防止XSS注入。None值转为空字符串。
        """
        if text is None:
            return ""
        return escape(str(text))


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert HTMLHelper is not None, "HTMLHelper 类未定义"
    # 验证HTML转义
    escaped = HTMLHelper._escape("<script>alert('xss')</script>")
    assert "<script>" not in escaped, "HTML转义失败"
    assert "&lt;script&gt;" in escaped, "HTML转义结果不正确"
    # 验证表格渲染
    table = HTMLHelper.render_table(["产品", "数量"], [["A-202", 200]])
    assert "<table" in table and "<th>产品</th>" in table, "表格渲染失败"
    assert "<td>A-202</td>" in table and "<td>200</td>" in table, "表格数据渲染失败"
    # 验证状态徽章（pass状态映射为绿色#10b981）
    badge = HTMLHelper.render_status_badge("pass")
    assert "badge" in badge and "#10b981" in badge, "状态徽章渲染失败"
    # 验证进度条
    bar = HTMLHelper.render_progress_bar(75)
    assert "75%" in bar, "进度条渲染失败"
    # 验证卡片
    card = HTMLHelper.render_card("测试标题", "<p>内容</p>")
    assert "测试标题" in card and "内容" in card, "卡片渲染失败"
    hello_world(__name__, "HTMLHelper渲染与转义验证完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
