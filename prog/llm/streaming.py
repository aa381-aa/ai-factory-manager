"""
SSE 流式输出（业务软件层 re-export）
====================================
框架能力：StreamingResponse + SSEHelper（SSE 协议格式化、流式响应封装，
可直接传入 WSGI Response）由AI工厂管家框架运行时（prog/runtime）提供。
本文件仅作 re-export。
"""
from prog.runtime.streaming import StreamingResponse, SSEHelper, create_streaming_response

__all__ = ["StreamingResponse", "SSEHelper", "create_streaming_response"]
