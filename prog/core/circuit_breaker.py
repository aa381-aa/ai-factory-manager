"""
S11 熔断器（自实现 closed/open/half-open 状态机，可商用部署功能补充建议 · S11）
===============================================================================
文件用途：
    为外部依赖调用（LLM / 向量库 / ERP / 设备等）提供熔断保护：
    连续失败达到阈值后进入 open 状态，在 recovery_timeout 内快速失败
    （抛 CircuitOpenError），超时后进入 half-open 放行少量探测请求，
    探测成功转 closed，失败回 open。线程安全（threading.Lock）。

接口：
    CircuitBreaker(name, failure_threshold=5, recovery_timeout=60, half_open_max=2)
        .call(fn, *args, **kwargs)  执行受保护调用
        .state / .reset()           状态查询 / 手动复位
    get_breaker(name, **kwargs)     全局注册表（单例复用）
    CircuitOpenError                熔断快速失败异常

接线：
    - core/llm_provider.py chat 入口已接入 get_breaker("llm_chat")；
    - core/vector_store.py / core/database.py 可按相同模式接入
      （database._connect 已有 60s 连接熔断实现，保持不动）。
"""

import logging
import threading
import time
from typing import Any, Callable, Dict

_logger = logging.getLogger("prog.circuit_breaker")


class CircuitOpenError(Exception):
    """熔断器处于 open 状态，调用被快速拒绝。"""


class CircuitBreaker:
    """线程安全的熔断器状态机。

    状态流转：
        closed --连续失败达 threshold--> open --recovery_timeout 超时-->
        half-open --探测成功--> closed
        half-open --探测失败--> open（重新计时）
        half-open --达到 half_open_max 探测上限--> open
    """

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 60, half_open_max: int = 2) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_max = max(1, int(half_open_max))
        self._lock = threading.Lock()
        self._state = "closed"          # closed / open / half-open
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_requests = 0

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def stats(self) -> Dict[str, Any]:
        """返回当前状态快照（供监控/排障）。"""
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "half_open_requests": self._half_open_requests,
            }

    def reset(self) -> None:
        """手动复位为 closed（清空失败计数）。"""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._half_open_requests = 0
            self._opened_at = 0.0

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------
    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """执行受保护调用。

        - closed：正常执行；连续失败达 threshold 转 open；
        - open：recovery_timeout 内快速失败抛 CircuitOpenError；
          超时自动转 half-open 放行探测请求；
        - half-open：允许 half_open_max 个探测请求，成功转 closed，
          失败回 open；探测请求占满后快速失败。

        受保护调用抛出的异常会原样向上传播（由调用方现有降级逻辑处理）。
        """
        with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    # 熔断超时：进入 half-open 放行探测
                    self._state = "half-open"
                    self._half_open_requests = 0
                else:
                    raise CircuitOpenError(
                        f"熔断器 {self.name} 处于 open 状态（快速失败）")
            if self._state == "half-open":
                if self._half_open_requests >= self.half_open_max:
                    raise CircuitOpenError(
                        f"熔断器 {self.name} half-open 探测上限已满（快速失败）")
                self._half_open_requests += 1

        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            with self._lock:
                self._consecutive_failures += 1
                if self._state == "half-open":
                    # 探测失败：回 open 并重置计时
                    self._state = "open"
                    self._opened_at = time.monotonic()
                    self._half_open_requests = 0
                    _logger.warning("熔断器 %s half-open 探测失败，回 open", self.name)
                elif self._consecutive_failures >= self.failure_threshold:
                    self._state = "open"
                    self._opened_at = time.monotonic()
                    _logger.warning(
                        "熔断器 %s 连续失败 %d 次，转为 open（%.0fs）",
                        self.name, self._consecutive_failures, self.recovery_timeout)
                else:
                    _logger.warning(
                        "熔断器 %s 调用失败（%d/%d）：%s",
                        self.name, self._consecutive_failures,
                        self.failure_threshold, exc)
            raise
        else:
            with self._lock:
                if self._state == "half-open":
                    # 探测成功：复位为 closed
                    self._state = "closed"
                    self._half_open_requests = 0
                    self._opened_at = 0.0
                    _logger.info("熔断器 %s 探测成功，恢复 closed", self.name)
                self._consecutive_failures = 0
            return result


# ------------------------------------------------------------------
# 全局注册表
# ------------------------------------------------------------------
_BREAKERS: Dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def get_breaker(name: str, **kwargs: Any) -> CircuitBreaker:
    """获取/创建命名熔断器单例。

    参数:
        name: 熔断器名称（如 "llm_chat" / "milvus"）
        kwargs: 首次创建时的参数（failure_threshold/recovery_timeout/half_open_max）
    """
    with _BREAKERS_LOCK:
        if name not in _BREAKERS:
            _BREAKERS[name] = CircuitBreaker(name, **kwargs)
        return _BREAKERS[name]
