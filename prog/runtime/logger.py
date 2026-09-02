"""
结构化日志模块
==============

文件用途：
    提供 Agent 运行时的结构化日志能力，覆盖审计日志、Agent 调用日志、
    LLM 调用日志三类专用日志，并支持统一的 get_logger 入口。

日志格式和级别说明：
    - 格式：JSON Lines（每行一条 JSON），便于 ELK/Loki 采集与查询。
      字段：ts, level, logger, event, user, action, result,
            duration_ms, tokens, error_code, message, extra
    - 级别：DEBUG/INFO/WARNING/ERROR/CRITICAL。
    - 输出：默认 stdout，可通过环境变量 RUNTIME_LOG_FILE 配置文件轮转输出。
    - 敏感信息过滤：password/token/secret 字段自动脱敏为 ***。
    - trace 关联：自动附加当前上下文 trace_id（v1.2 已提取，读取 runtime.trace）。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - JSON Lines 结构化日志（每行一条 JSON），便于 ELK/Loki 采集与查询（SPEC §5.2 结构化日志，来源映射 §1.6/§A.0 Logger 与 error_codes 联动）
        - 审计/Agent/LLM 三类专用日志（event=audit/agent/llm）（SPEC §5.2）
        - 敏感字段自动脱敏：SENSITIVE_KEYS 命中即替换为 ***（递归处理 dict/list）（SPEC §5.2）
        - 自动附加当前上下文 trace_id（读取 runtime.trace，无则空串），实现与 §4 trace 链路端到端关联（SPEC §5.2）
    对外接口（方法/API）：
        - Logger.get_logger(name, level=None) -> logging.Logger：统一 logger 入口（缓存复用避免重复 Handler；可选 RUNTIME_LOG_FILE 环境变量启用 10MB 轮转文件输出）（SPEC §5.2）
        - Logger.log_audit(action, user, result, details, error_code)：审计日志（event='audit'，供合规审计与审核链追溯）（SPEC §5.2）
        - Logger.log_agent(agent_name, input, output, duration_ms, intent, success)：Agent 调用日志（event='agent'，性能监控与调用追溯）（SPEC §5.2）
        - Logger.log_llm(prompt, response, tokens, duration_ms, model, ...)：LLM 调用日志（event='llm'，超长自动截断保留首尾；成本监控与重放调试）（SPEC §5.2）
    错误处理要求：
        - 敏感字段泄露风险：password/token/secret/api_key/password_hash 命中即脱敏为 ***（递归处理 dict/list）（SPEC §5.2）
        - 无 trace 上下文：trace_id 记空串，不影响未接入 trace 的调用方（SPEC §5.2）
"""

import json
import logging
import logging.handlers
import os
import threading
import time
from typing import Any, Optional


class _JsonFormatter(logging.Formatter):
    """JSON Lines 格式化器。

    将标准 LogRecord 格式化为单行 JSON，便于 ELK/Loki 等日志采集系统消费。
    标准字段：ts、level、logger、message；
    通过 extra 传入的扩展字段自动合并到 JSON 输出。
    """

    # logging.LogRecord 的标准属性名集合（这些不作为 extra 输出）
    _STANDARD_KEYS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        """将 LogRecord 格式化为 JSON 字符串"""
        log_entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # 合并通过 extra 传入的扩展字段
        for key, value in record.__dict__.items():
            if key not in self._STANDARD_KEYS:
                log_entry[key] = value
        return json.dumps(log_entry, ensure_ascii=False, default=str)


class Logger:
    """结构化日志器。

    设计意图：
        封装 Python 标准 logging，提供面向 Agent 运行时场景的便捷方法，
        统一 JSON Lines 格式输出与敏感字段脱敏。

    属性：
        _loggers: 已创建的 logger 缓存（避免重复配置 Handler）
        _default_level: 默认日志级别
    """

    _loggers: dict = {}
    _default_level = logging.INFO
    # W31（并发）：logger 缓存读写锁——避免并发 get_logger 重复创建 logger/Handler
    _loggers_lock = threading.Lock()

    # 敏感字段关键字（命中即脱敏）
    SENSITIVE_KEYS = ("password", "token", "secret", "api_key", "password_hash")

    @classmethod
    def get_logger(cls, name: str, level: Optional[int] = None) -> logging.Logger:
        """获取/创建 logger。

        参数：
            name: logger 名称（通常为模块名或 Agent 名）
            level: 日志级别，未指定则使用 _default_level

        返回：
            logging.Logger 实例；同名 logger 复用缓存，避免重复 Handler。
        """
        with cls._loggers_lock:
            # 命中缓存：若传入 level 与已有 logger 不同，则更新其级别
            if name in cls._loggers:
                logger = cls._loggers[name]
                if level is not None and getattr(logger, "level", None) != level:
                    set_level = getattr(logger, "setLevel", None)
                    if set_level is not None:
                        set_level(level)
                return logger

            logger = logging.getLogger(name)
            logger.setLevel(level if level is not None else cls._default_level)

            # 避免重复添加 Handler
            if not logger.handlers:
                formatter = _JsonFormatter()

                # 控制台输出（stdout）
                console_handler = logging.StreamHandler()
                console_handler.setFormatter(formatter)
                logger.addHandler(console_handler)

                # 可选文件输出：通过环境变量 RUNTIME_LOG_FILE 配置文件路径
                log_file = os.environ.get("RUNTIME_LOG_FILE")
                if log_file:
                    file_handler = logging.handlers.RotatingFileHandler(
                        log_file,
                        maxBytes=10 * 1024 * 1024,  # 10MB
                        backupCount=5,
                        encoding="utf-8",
                    )
                    file_handler.setFormatter(formatter)
                    logger.addHandler(file_handler)

            # 不向父 logger 传播，避免重复输出
            logger.propagate = False
            cls._loggers[name] = logger
            return logger

    @classmethod
    def log_audit(cls, action: str, user: str, result: str,
                  details: Optional[dict] = None, error_code: Optional[str] = None) -> None:
        """审计日志。

        参数：
            action: 审计的动作（如 'order.create', 'order.audit'）
            user: 操作用户
            result: 结果（'pass'/'blocked'/'fail'）
            details: 附加详情字典
            error_code: 关联错误码（如 'E101'）

        说明：
            输出 event='audit' 的结构化日志，供合规审计与审核链追溯。
            自动附加当前上下文的 trace_id（无则空串）。
        """
        logger = cls.get_logger("runtime.audit")
        extra = {
            "event": "audit",
            "trace_id": cls._get_trace(),
            "action": action,
            "user": user,
            "result": result,
            "error_code": error_code or "",
            "details": cls._mask_sensitive(details) if details else {},
        }
        # 根据结果映射日志级别
        if result == "pass":
            level = logging.INFO
        elif result == "blocked":
            level = logging.WARNING
        else:
            level = logging.ERROR
        logger.log(level, f"audit | {action} | user={user} | result={result}",
                   extra=extra)

    @classmethod
    def log_agent(cls, agent_name: str, input_data: Any, output_data: Any,
                  duration_ms: float, intent: Optional[str] = None,
                  success: bool = True) -> None:
        """Agent 调用日志。

        参数：
            agent_name: Agent 名称
            input_data: Agent 输入（用户输入或上游 Intent）
            output_data: Agent 输出
            duration_ms: 调用耗时毫秒
            intent: 识别出的意图名（可选）
            success: 是否成功

        说明：
            输出 event='agent' 的结构化日志，用于 Agent 性能监控与调用追溯。
        """
        logger = cls.get_logger("runtime.agent")
        extra = {
            "event": "agent",
            "trace_id": cls._get_trace(),
            "agent": agent_name,
            "intent": intent or "",
            "success": success,
            "duration_ms": duration_ms,
            "input": cls._mask_sensitive(input_data),
            "output": cls._mask_sensitive(output_data),
        }
        level = logging.INFO if success else logging.ERROR
        status = "success" if success else "fail"
        logger.log(level, f"agent | {agent_name} | {status} | {duration_ms}ms",
                   extra=extra)

    @classmethod
    def log_llm(cls, prompt: str, response: str, tokens: int,
                duration_ms: float, model: Optional[str] = None,
                success: bool = True, error: Optional[str] = None) -> None:
        """LLM 调用日志。

        参数：
            prompt: 提示词（超长自动截断保留首尾）
            response: LLM 响应（超长自动截断）
            tokens: 消耗 token 数（prompt+response 合计）
            duration_ms: 调用耗时毫秒
            model: 模型名称（如 'doubao-pro'）
            success: 是否成功
            error: 失败时的错误信息

        说明：
            输出 event='llm' 的结构化日志，用于 LLM 成本监控与重放调试。
        """
        # L2：高频成功调用 10% 采样入库（失败 100% 记录）——降低日志量，
        # 成本/指标统计由 metrics 侧全量采集（见 /metrics 端点）
        if success:
            import random
            if random.random() >= 0.1:
                return
        logger = cls.get_logger("runtime.llm")
        extra = {
            "event": "llm",
            "trace_id": cls._get_trace(),
            "model": model or "",
            "success": success,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "error": error or "",
            # 超长文本截断，保留首尾各 1000 字符
            "prompt": cls._truncate(prompt, 2000),
            "response": cls._truncate(response, 2000),
        }
        level = logging.INFO if success else logging.ERROR
        status = "success" if success else "fail"
        logger.log(level, f"llm | {model or 'unknown'} | {status} | tokens={tokens}",
                   extra=extra)

    @staticmethod
    def _get_trace() -> str:
        """获取当前上下文的 trace_id（v1.2 已提取）。

        通过 runtime.trace 的 contextvars 读取，无追踪上下文时返回空串，
        不影响未接入 trace 的调用方。
        """
        try:
            from prog.runtime.trace import get_trace_id
            return get_trace_id()
        except Exception:
            return ""

    @classmethod
    def _mask_sensitive(cls, data: Any) -> Any:
        """敏感字段脱敏（递归处理 dict/list）。

        遍历 dict 的 key，命中 SENSITIVE_KEYS 的值替换为 '***'；
        递归处理嵌套的 dict 和 list。
        """
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                # key 名命中敏感关键字则脱敏
                if any(sensitive in str(key).lower() for sensitive in cls.SENSITIVE_KEYS):
                    result[key] = "***"
                else:
                    result[key] = cls._mask_sensitive(value)
            return result
        elif isinstance(data, list):
            return [cls._mask_sensitive(item) for item in data]
        else:
            return data

    @staticmethod
    def _truncate(text: Any, max_len: int = 2000) -> str:
        """截断超长文本，保留首尾各一半长度。"""
        if not isinstance(text, str):
            text = str(text)
        if len(text) <= max_len:
            return text
        half = max_len // 2
        return text[:half] + "...[truncated]..." + text[-half:]
