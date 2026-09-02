"""
EventBus 事件总线模块
=====================

文件用途：
    定义事件总线的统一接口层，基于 Redis Streams 实现发布订阅与异步事件驱动。
    用于 Agent 间通信、业务状态变更通知、审计日志采集等场景。

设计说明：
    1. 抽象基类 EventBus 定义统一契约（publish / subscribe / unsubscribe / close）
    2. RedisStreamBus 为默认实现，基于 Redis Streams（XADD / XREAD）
    3. Stream 名称前缀默认 "runtime_events"，可通过 config.stream_prefix 配置
    4. 未安装 redis 或连接失败时降级为内存 pub/sub（同步触发已注册处理器）
    5. MAXLEN 裁剪旧消息，防止 Stream 无限增长

事件消息结构:
    {
        "event_type": "xxx",
        "event_id": "uuid",
        "timestamp": "2026-08-02T10:00:00Z",
        "source": "...",
        "payload": { ... },
        "audit_hash": "..."  // 事件哈希，用于审计链
    }

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - EventBus 抽象基类 + RedisStreamBus 默认实现（Redis Streams：XADD/XREAD，MAXLEN 裁剪 10000 条，后台消费线程），用于 Agent 间通信、业务状态变更通知、审计日志采集（SPEC §5.4 事件总线，来源映射 §1.8.5 EventBus 统一接口层）
        - 未装 redis 或连接失败时降级内存 pub/sub（同步触发已注册处理器）（SPEC §5.4）
        - Stream 名前缀默认 runtime_events（config.stream_prefix 可配置）（SPEC §5.4）
        - 通知事件主题（契约 8，v6.78）：EVENT_NOTIFY_CREATE/APPROVAL/EXPIRE + publish_event，发布方（coordinator/agents）只发布不感知消费方，M1 组装期 register_notification_handlers 订阅（模块拆分方案 契约8 / CHANGELOG v39）
        - 通知事件顺序性收敛（v6.78.2）：审批推进分支不再独立发布 EXPIRE 事件，失效信息合并进创建事件 payload.expire_before，_on_notify_expire 订阅保留向后兼容（模块拆分方案 契约8 / CHANGELOG v39）
    对外接口（方法/API）：
        - EventBus.publish(topic, message) -> str：发布事件，返回事件 ID（SPEC §5.4）
        - EventBus.subscribe(topic, handler)：订阅事件（SPEC §5.4）
        - EventBus.unsubscribe(topic, handler)：取消订阅（SPEC §5.4）
        - EventBus.close()：关闭连接，释放资源（SPEC §5.4）
        - RedisStreamBus(config)：Redis Streams 默认实现（XADD/XREAD，MAXLEN 10000，后台消费线程）（SPEC §5.4）
        - create_event_bus(config=None) -> EventBus：工厂函数，按配置创建实例（SPEC §5.4）
        - get_event_bus() -> EventBus：模块级事件总线单例（SPEC §5.4）
        - publish_event(topic, payload, source="") -> str：发布标准业务事件（EVENT_NOTIFY_* 常量）（模块拆分方案 契约8）
    错误处理要求：
        - redis 未安装或连接失败：降级内存 pub/sub（同步触发已注册处理器）（SPEC §5.4）
        - 事件处理器执行异常：不中断发布/消费循环（规格书未明确）
"""

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 尝试导入 redis，未安装时降级为内存 pub/sub
try:
    import redis as _redis_module
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    logger.warning("redis 模块未安装，EventBus 将降级为内存 pub/sub")


class EventBus:
    """
    事件总线抽象基类

    定义所有事件总线实现必须遵循的统一契约。
    子类需实现 publish / subscribe / unsubscribe / close 方法。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化事件总线

        参数:
            config: 事件总线配置字典，包含 host / port / db / tls / password 等字段
        """
        self._config = config or {}

    def publish(self, topic: str, message: Dict[str, Any]) -> str:
        """
        发布事件

        参数:
            topic: 事件主题（如 order_created）
            message: 事件消息字典

        返回:
            事件 ID
        """
        raise NotImplementedError

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Dict[str, Any]], None],
    ) -> None:
        """
        订阅事件

        注册一个处理器，当指定主题的事件发布时被调用。

        参数:
            topic: 事件主题
            handler: 事件处理回调函数，接收事件消息字典
        """
        raise NotImplementedError

    def unsubscribe(
        self,
        topic: str,
        handler: Callable[[Dict[str, Any]], None],
    ) -> None:
        """
        取消订阅

        参数:
            topic: 事件主题
            handler: 已注册的处理器
        """
        raise NotImplementedError

    def close(self) -> None:
        """关闭连接，释放资源"""
        raise NotImplementedError


# 保持向后兼容的别名
EventBusBase = EventBus


class RedisStreamBus(EventBus):
    """
    Redis Streams 事件总线实现

    基于 Redis 5.0+ 的 Streams 数据结构实现：
        - publish 使用 XADD 写入 Stream
        - subscribe 使用 XREAD 阻塞式消费
        - 支持 MAXLEN 裁剪旧消息，防止 Stream 无限增长

    降级模式:
        当 redis 模块未安装或连接失败时，降级为内存 pub/sub：
        - publish 同步触发所有已注册的处理器
        - subscribe 注册处理器到内存字典
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化 Redis Stream 事件总线

        参数:
            config: Redis 配置字典，包含 host / port / db / tls / password
                    / stream_prefix 字段
        """
        super().__init__(config)
        self._redis = None
        # 主题 -> 处理器列表（Redis 模式与内存模式共用）
        self._handlers: Dict[str, List[Callable]] = {}
        # 主题 -> (消费线程, 停止事件) 的映射
        self._consumer_threads: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._init_redis()

    def _init_redis(self) -> None:
        """初始化 Redis 连接，未安装或连接失败时降级为内存模式"""
        if not _REDIS_AVAILABLE:
            logger.warning("redis 模块不可用，使用内存 pub/sub 模拟事件总线")
            return

        cfg = self._config
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 6379)
        db = cfg.get("db", 0)
        password = cfg.get("password")
        tls = cfg.get("tls", False)

        try:
            self._redis = _redis_module.Redis(
                host=host,
                port=port,
                db=db,
                password=password,
                ssl=tls,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            # 测试连接是否可用
            self._redis.ping()
        except Exception as e:
            logger.warning("Redis 连接失败 (%s)，降级为内存 pub/sub", e)
            self._redis = None

    def _stream_name(self, topic: str) -> str:
        """构建 Redis Stream 名称"""
        prefix = self._config.get("stream_prefix", "runtime_events")
        return f"{prefix}:{topic}"

    def publish(self, topic: str, message: Dict[str, Any]) -> str:
        """
        发布事件

        Redis 模式使用 XADD 写入 Stream；内存模式同步触发处理器。

        参数:
            topic: 事件主题
            message: 事件消息字典

        返回:
            事件 ID（Redis 模式为 Stream 消息 ID，内存模式为 UUID）
        """
        event_id = message.get("event_id", str(uuid.uuid4()))
        timestamp = message.get(
            "timestamp",
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        if self._redis is not None:
            # Redis 模式：使用 XADD 发布到 Stream
            stream_name = self._stream_name(topic)
            fields = {
                "event_id": event_id,
                "topic": topic,
                "timestamp": timestamp,
                "data": json.dumps(message, ensure_ascii=False, default=str),
            }
            # MAXLEN 裁剪旧消息，防止 Stream 无限增长
            message_id = self._redis.xadd(stream_name, fields, maxlen=10000)
            return message_id

        # 内存降级模式：同步触发已注册的处理器
        event = {
            "event_id": event_id,
            "topic": topic,
            "timestamp": timestamp,
            "data": message,
        }
        with self._lock:
            handlers = list(self._handlers.get(topic, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error("事件处理器执行失败 (topic=%s): %s", topic, e)
        return event_id

    def subscribe(
        self,
        topic: str,
        handler: Callable[[Dict[str, Any]], None],
    ) -> None:
        """
        订阅事件

        Redis 模式注册处理器并启动后台消费线程（使用 XREAD 阻塞读取）；
        内存模式仅注册处理器到字典。

        参数:
            topic: 事件主题
            handler: 事件处理回调函数
        """
        with self._lock:
            if topic not in self._handlers:
                self._handlers[topic] = []
            self._handlers[topic].append(handler)

            # 消费线程检查与启动放入锁内（double-checked locking），
            # 避免并发 subscribe 同一 topic 时重复启动消费线程
            if self._redis is not None and topic not in self._consumer_threads:
                stop_event = threading.Event()
                thread = threading.Thread(
                    target=self._consume_loop,
                    args=(topic, stop_event),
                    daemon=True,
                )
                self._consumer_threads[topic] = (thread, stop_event)
                thread.start()
                logger.info("已启动事件消费线程 (topic=%s)", topic)

    def _consume_loop(self, topic: str, stop_event: threading.Event) -> None:
        """
        后台消费循环

        使用 XREAD 阻塞式读取 Stream 中的新消息，分发给已注册的处理器。

        参数:
            topic: 事件主题
            stop_event: 停止信号
        """
        stream_name = self._stream_name(topic)
        # 使用 "$" 表示只消费订阅之后的新消息
        last_id = "$"

        while not stop_event.is_set() and not self._closed:
            if self._redis is None:
                break
            try:
                # 阻塞式读取，超时 1 秒以便定期检查停止信号
                response = self._redis.xread(
                    {stream_name: last_id},
                    count=10,
                    block=1000,
                )
                if not response:
                    continue

                for _stream, messages in response:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        event = self._deserialize_event(msg_id, fields)
                        # 复制处理器列表，避免回调中修改列表导致异常
                        with self._lock:
                            handlers = list(self._handlers.get(topic, []))
                        for handler in handlers:
                            try:
                                handler(event)
                            except Exception as e:
                                logger.error(
                                    "事件处理器执行失败 (topic=%s, msg_id=%s): %s",
                                    topic, msg_id, e,
                                )
            except Exception as e:
                if not stop_event.is_set() and not self._closed:
                    logger.error("XREAD 消费异常 (topic=%s): %s", topic, e)
                    time.sleep(1)  # 出错后等待重试

    def _deserialize_event(
        self,
        msg_id: str,
        fields: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        反序列化 Redis Stream 消息为事件字典

        参数:
            msg_id: Redis Stream 消息 ID
            fields: Stream 消息字段字典

        返回:
            事件字典，包含 event_id / topic / timestamp / message_id / data
        """
        data = fields.get("data", "{}")
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            parsed = {}

        return {
            "event_id": fields.get("event_id", ""),
            "topic": fields.get("topic", ""),
            "timestamp": fields.get("timestamp", ""),
            "message_id": msg_id,
            "data": parsed,
        }

    def unsubscribe(
        self,
        topic: str,
        handler: Callable[[Dict[str, Any]], None],
    ) -> None:
        """
        取消订阅

        从指定主题的处理器列表中移除处理器。
        当主题无处理器时，停止对应的消费线程。

        参数:
            topic: 事件主题
            handler: 已注册的处理器
        """
        with self._lock:
            handlers = self._handlers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

            # 无处理器时停止消费线程
            if not handlers and topic in self._consumer_threads:
                thread, stop_event = self._consumer_threads.pop(topic)
                stop_event.set()
                thread.join(timeout=2)

    def close(self) -> None:
        """关闭连接，停止所有消费线程，清理资源"""
        self._closed = True

        # 停止所有消费线程
        with self._lock:
            threads = list(self._consumer_threads.values())
            self._consumer_threads.clear()
            self._handlers.clear()

        for thread, stop_event in threads:
            stop_event.set()
            thread.join(timeout=2)

        if self._redis is not None:
            self._redis.close()
            self._redis = None


# 模块级事件总线单例
_event_bus_instance: Optional[EventBus] = None


def create_event_bus(config: Optional[Dict[str, Any]] = None) -> EventBus:
    """
    工厂函数：根据配置创建事件总线实例

    参数:
        config: 事件总线配置字典（可选，None 时使用默认配置）

    返回:
        EventBus 实例（RedisStreamBus，未装 redis 或连接失败时内部降级内存模式）
    """
    return RedisStreamBus(config or {})


def get_event_bus() -> EventBus:
    """
    模块级便捷函数：获取事件总线单例

    返回:
        EventBus 单例实例
    """
    global _event_bus_instance
    if _event_bus_instance is None:
        _event_bus_instance = create_event_bus()
    return _event_bus_instance


# ============================================================
# 通知事件主题（跨模块通信契约，v6.78 拆分 runtime↔api 反向依赖）
# ============================================================
# 业务侧（coordinator/agents）不再直接 import api.notifications_api，
# 改经 publish_event 发布事件；API 层（M1）注册订阅后调用原落库函数。
EVENT_NOTIFY_CREATE = "notification.create"           # 创建普通通知
EVENT_NOTIFY_APPROVAL = "notification.approval"       # 创建审批待办通知
EVENT_NOTIFY_EXPIRE = "notification.workflow_expire"  # 失效审批待办
EVENT_NOTIFY_TEMP_GRANT_RESULT = "notification.temp_grant_result"  # 跨部门临时授权结果（TG-07，审批通过/驳回/超时推送申请人）


def publish_event(topic: str, payload: dict, source: str = "") -> str:
    """发布标准业务事件（替代业务层直接调用 API 层函数）。

    参数:
        topic: 事件主题（EVENT_NOTIFY_* 常量）
        payload: 事件载荷字典（与订阅 handler 的参数一一对应）
        source: 事件来源标识（如 coordinator / sales_agent）

    返回:
        事件 ID

    设计说明:
        框架层/Agent 层只发布事件，不感知消费方（通知落库等）；API 层在
        组装期 register_notification_handlers 订阅，实现单向依赖。
    """
    return get_event_bus().publish(topic, {
        "event_type": topic,
        "source": source,
        "payload": payload or {},
    })
