"""
缓存管理器（业务软件层 re-export）
==================================
框架能力：CacheManager 缓存管理器（Redis 单例 / 未装 redis 自动降级内存字典，
命名空间键管理 + 会话/LLM 缓存/限流计数）由AI工厂管家框架运行时（prog/runtime）提供。
本文件仅作 re-export。
"""
from prog.runtime.cache import CacheManager, get_cache

__all__ = ["CacheManager", "get_cache"]
