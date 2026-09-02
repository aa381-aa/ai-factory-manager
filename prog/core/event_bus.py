"""
事件总线（业务软件层 re-export + 部署逻辑）
============================================
框架能力：EventBus 抽象基类 + RedisStreamBus 默认实现（Redis Streams
XADD/XREAD、MAXLEN 裁剪、未装 redis 自动降级内存 pub/sub）由AI工厂管家框架运行时
（prog/runtime）提供。
业务侧保留：VolcEventBus（火山引擎占位实现）与部署模式选择（local/volcano）。
"""
import logging
import uuid
from typing import Any, Callable, Dict, Optional

from prog.runtime.event_bus import EventBus, EventBusBase, RedisStreamBus

logger = logging.getLogger(__name__)


class VolcEventBus(EventBus):
    """
    火山引擎部署的事件总线实现（占位）

    火山引擎环境中事件总线通过火山引擎 MQ for Redis 或 DTS 等托管服务实现。
    当前为占位实现，事件仅记录日志，不进行实际投递。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化火山引擎事件总线（占位）

        参数:
            config: 火山引擎 Redis 配置字典
        """
        super().__init__(config)
        logger.info("VolcEventBus 初始化（占位实现，事件仅记录日志）")

    def publish(self, topic: str, message: Dict[str, Any]) -> str:
        """发布事件（占位实现，仅记录日志）"""
        event_id = message.get("event_id", str(uuid.uuid4()))
        logger.info(
            "[VolcEventBus] 发布事件: topic=%s, event_id=%s, message=%s",
            topic, event_id, message,
        )
        return event_id

    def subscribe(self, topic: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """订阅事件（占位实现，仅记录日志）"""
        logger.warning(
            "[VolcEventBus] 订阅功能未实现（占位）: topic=%s", topic
        )

    def unsubscribe(self, topic: str, handler: Callable[[Dict[str, Any]], None]) -> None:
        """取消订阅（占位实现，仅记录日志）"""
        logger.warning(
            "[VolcEventBus] 取消订阅功能未实现（占位）: topic=%s", topic
        )

    def close(self) -> None:
        """关闭连接（占位实现）"""
        logger.info("[VolcEventBus] 关闭连接（占位）")


# 模块级事件总线单例
_event_bus_instance: Optional[EventBus] = None


def create_event_bus(config: Optional[Dict[str, Any]] = None) -> EventBus:
    """
    工厂函数：根据部署配置创建事件总线实例

    选择逻辑：
        1. config 显式传入时，根据 config.event_bus_type 选择实现
        2. config 为 None 时，从统一配置加载器读取部署模式：
           - local 模式 -> RedisStreamBus（框架提供）
           - volcano 模式 -> VolcEventBus（业务占位）

    参数:
        config: 事件总线配置字典（可选）

    返回:
        EventBus 实例
    """
    if config is None:
        from prog.config.config_loader import get_config_loader
        loader = get_config_loader()
        config = loader.get_interface_config("event_bus")
        bus_type = loader.get_deployment_mode()
    else:
        bus_type = config.get("event_bus_type", "local")

    if bus_type == "volcano":
        return VolcEventBus(config)
    return RedisStreamBus(config)


def get_event_bus() -> EventBus:
    """模块级便捷函数：获取事件总线单例"""
    global _event_bus_instance
    if _event_bus_instance is None:
        _event_bus_instance = create_event_bus()
    return _event_bus_instance


__all__ = ["EventBus", "EventBusBase", "RedisStreamBus", "VolcEventBus",
           "create_event_bus", "get_event_bus"]
