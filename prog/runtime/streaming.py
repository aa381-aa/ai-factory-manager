"""
StreamingGenerator SSE 流式生成模块
====================================

文件用途：
    实现 SSE（Server-Sent Events）流式生成器，将 LLM 流式输出转换为
    前端可消费的 SSE 数据块。

核心能力：
    1. 流式生成：将 LLM 流式输出逐块转换为 SSE 格式字符串
    2. SSE 格式化：遵循 SSE 协议格式化数据块（event/data/id 字段）
    3. 错误处理：流式过程中的异常以 SSE 错误事件下发，避免连接中断

SSE 协议说明：
    - 每个事件以两个换行符分隔
    - 字段格式：event: xxx\\ndata: xxx\\nid: xxx\\n\\n
    - 前端通过 EventSource API 消费

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - StreamingResponse + SSEHelper 提供标准 SSE 协议格式化（event/data 字段，双换行分隔），将 LLM 流式输出转换为前端可消费的 SSE 数据块（SPEC §5.6 SSE 流式输出，来源映射 §1.1.3 SSE 流式输出 / §2 流式场景门控适配）
        - 流式生成：StreamingResponse 将任意生成器封装为符合 SSE 协议的流式响应，可直接传入 WSGI/ASGI Response（SPEC §5.6）
        - 错误处理：流式过程中的异常以 SSE 错误事件下发，避免连接中断导致前端无反馈（SPEC §5.6）
    对外接口（方法/API）：
        - StreamingResponse.format_sse(data, event=None)：基础 SSE 格式化（SPEC §5.6）
        - StreamingResponse.format_json_sse(data, event=None)：JSON 数据 SSE 格式化（ensure_ascii=False 保中文）（SPEC §5.6）
        - SSEHelper.format_chunk(content, is_final=False)：内容块（message 事件）（SPEC §5.6）
        - SSEHelper.format_error(message, code=None)：错误（error 事件，消息面向用户不暴露内部栈）（SPEC §5.6）
        - SSEHelper.format_done(metadata=None)：完成（无元数据发标准 [DONE]，有元数据发 JSON）（SPEC §5.6）
        - SSEHelper.format_event(event_type, data)：自定义事件（如 meta、heartbeat）（SPEC §5.6）
        - create_streaming_response(generator, content_type="text/event-stream")：便捷工厂，可直接传入 WSGI Response（SPEC §5.6）
    错误处理要求：
        - 流式过程异常：以 SSE error 事件下发（format_error），错误消息应面向用户、不暴露内部异常栈等敏感信息（SPEC §5.6）
"""

import json
from typing import Any, Dict, Iterator, Optional, Union


class StreamingResponse:
    """
    SSE 流式响应封装类。

    设计意图：
        将任意生成器封装为符合 SSE 协议的流式响应，可直接传入
        WSGI/ASGI Response 作为响应体使用。同时提供 SSE 消息格式化工具方法。

    属性：
        generator: 数据生成器（产出 SSE 格式字符串或原始数据）
        content_type: 响应内容类型（默认 text/event-stream）
    """

    def __init__(self, generator: Iterator[str],
                 content_type: str = "text/event-stream"):
        """
        初始化 SSE 流式响应。

        参数：
            generator: 数据生成器，逐个产出 SSE 格式字符串
            content_type: 响应内容类型（默认 text/event-stream）
        """
        self.generator = generator
        self.content_type = content_type

    def __iter__(self) -> Iterator[str]:
        """
        迭代器接口，用于 WSGI/Flask Response 直接消费。

        WSGI Response 接受可迭代对象作为响应体，
        本方法将生成器委托为迭代器，逐个产出 SSE 字符串。

        返回：
            iterator: SSE 字符串迭代器
        """
        return iter(self.generator)

    # --------------------------------------------------------
    # SSE 消息格式化
    # --------------------------------------------------------
    @staticmethod
    def format_sse(data: str, event: Optional[str] = None) -> str:
        """
        格式化 SSE 消息。

        将数据和事件类型按 SSE 协议组装为字符串。

        参数：
            data: 数据内容（纯文本）
            event: 事件类型（如 message/error/done，为空则省略 event 字段）

        返回：
            str: SSE 格式字符串，以两个换行符结尾

        格式示例：
            event: message
            data: 你好

            （以两个换行符结尾）
        """
        lines = []
        if event:
            lines.append(f"event: {event}")
        # data 可能含多行，每行需以 "data: " 前缀
        for line in data.split("\n"):
            lines.append(f"data: {line}")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def format_json_sse(data: Any, event: Optional[str] = None) -> str:
        """
        格式化 JSON SSE 消息。

        将数据序列化为 JSON 字符串后按 SSE 协议组装。

        参数：
            data: 待序列化的数据（dict/list/str 等 JSON 可序列化对象）
            event: 事件类型

        返回：
            str: SSE 格式字符串

        格式示例：
            event: message
            data: {"content":"你好"}

        说明：
            JSON 字符串确保前端可通过 JSON.parse 解析 data 字段。
            ensure_ascii=False 保证中文不被转义。
        """
        json_str = json.dumps(data, ensure_ascii=False)
        return StreamingResponse.format_sse(json_str, event)


class SSEHelper:
    """
    SSE 消息格式化辅助类。

    设计意图：
        提供一组静态方法，将 LLM 流式输出的各类数据（内容块、错误、
        完成标记、自定义事件）格式化为标准 SSE 字符串。
        与 StreamingResponse 配合使用。

    使用方式：
        # 格式化内容块
        sse_str = SSEHelper.format_chunk("正在查询...")
        # 格式化错误
        sse_str = SSEHelper.format_error("生成失败", code="LLM_ERROR")
        # 格式化完成
        sse_str = SSEHelper.format_done()
    """

    # 事件类型常量
    EVENT_MESSAGE = "message"
    EVENT_ERROR = "error"
    EVENT_DONE = "done"
    EVENT_META = "meta"

    @staticmethod
    def format_chunk(content: str, is_final: bool = False) -> str:
        """
        格式化 LLM 输出内容块。

        将单个 LLM 流式输出块格式化为 SSE message 事件。

        参数：
            content: LLM 流式块文本内容
            is_final: 是否为最后一个内容块（默认 False）

        返回：
            str: SSE 格式字符串

        格式示例：
            event: message
            data: {"content":"你好","is_final":false}

        说明：
            前端收到 message 事件后，将 content 追加到回复区域。
            is_final 标记当前块是否为内容流的最后一块。
        """
        data = {"content": content, "is_final": is_final}
        return StreamingResponse.format_json_sse(data, SSEHelper.EVENT_MESSAGE)

    @staticmethod
    def format_error(message: str,
                     code: Optional[str] = None) -> str:
        """
        格式化错误消息。

        将异常信息格式化为 SSE error 事件，避免连接中断导致
        前端无反馈。前端收到 error 事件后可提示用户。

        参数：
            message: 错误提示信息（应脱敏，不暴露内部栈）
            code: 错误码（如 LLM_ERROR/TIMEOUT/AUTH_FAILED）

        返回：
            str: SSE 格式字符串

        格式示例：
            event: error
            data: {"message":"生成失败，请重试","code":"LLM_ERROR"}

        说明：
            错误消息应面向用户，不暴露内部异常栈等敏感信息。
        """
        data: Dict[str, Any] = {"message": message}
        if code:
            data["code"] = code
        return StreamingResponse.format_json_sse(data, SSEHelper.EVENT_ERROR)

    @staticmethod
    def format_done(metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        格式化完成消息。

        标记流式输出结束。前端收到 done 事件后关闭 EventSource 连接。

        参数：
            metadata: 完成时的附加元数据（如耗时、token 用量等）
                      为 None 时发送标准 [DONE] 标记

        返回：
            str: SSE 格式字符串

        格式示例（无元数据）：
            event: done
            data: [DONE]

        格式示例（有元数据）：
            event: done
            data: {"elapsed_ms":350,"tokens":128}

        说明：
            当 metadata 为 None 时，data 为纯文本 [DONE]（非 JSON），
            前端应将其视为固定结束标记直接处理，无需 JSON.parse。
        """
        if metadata:
            return StreamingResponse.format_json_sse(
                metadata, SSEHelper.EVENT_DONE
            )
        return StreamingResponse.format_sse("[DONE]", SSEHelper.EVENT_DONE)

    @staticmethod
    def format_event(event_type: str, data: Any) -> str:
        """
        格式化自定义事件。

        用于发送非标准类型的 SSE 事件（如 meta、heartbeat 等）。

        参数：
            event_type: 事件类型名称
            data: 事件数据（dict 时序列化为 JSON，其他转为字符串）

        返回：
            str: SSE 格式字符串

        格式示例：
            event: meta
            data: {"need_confirm":false,"agent_name":"销售Agent"}
        """
        if isinstance(data, (dict, list)):
            return StreamingResponse.format_json_sse(data, event_type)
        return StreamingResponse.format_sse(str(data), event_type)


# ============================================================
# 全局便捷函数
# ============================================================

def create_streaming_response(generator: Iterator[str],
                              content_type: str = "text/event-stream"
                              ) -> StreamingResponse:
    """
    创建 SSE 流式响应。

    便捷函数，封装 StreamingResponse 的实例化过程。
    传入的生成器应产出 SSE 格式字符串（可使用 SSEHelper 格式化）。

    参数：
        generator: SSE 字符串生成器
        content_type: 响应内容类型（默认 text/event-stream）

    返回：
        StreamingResponse: SSE 流式响应对象，可直接传入 WSGI Response

    使用示例：
        def gen():
            yield SSEHelper.format_chunk("正在处理...")
            yield SSEHelper.format_done({"elapsed_ms": 100})
        return Response(create_streaming_response(gen()),
                        mimetype="text/event-stream")
    """
    return StreamingResponse(generator, content_type)
