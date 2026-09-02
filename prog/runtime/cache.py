"""
缓存管理器模块
==============

文件用途：
    封装 Redis 连接与常用缓存操作，提供带命名空间的键管理。
    提供会话缓存、LLM 响应缓存、限流计数器等通用能力。
    与 event_bus.py 共享 Redis 连接配置，但使用独立的连接实例。

设计说明：
    1. CacheManager 单例类封装 Redis 连接与常用操作
    2. 未安装 redis 或连接失败时降级为内存字典模拟（带 TTL 清理）
    3. 提供命名空间的 key 设计，避免键冲突：
        - session 命名空间：会话缓存
        - llm_cache 命名空间：相同 prompt 的 LLM 响应缓存
        - rate_limit 命名空间：滑动窗口限流计数
    4. 支持通用的 get / set / delete / expire / exists / incr 操作

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - CacheManager 单例：Redis 连接（可选导入）或内存字典降级（带 TTL 清理），所有 key 自动加前缀（默认 runtime:，可参数化）（SPEC §5.3 缓存管理器，来源映射 §1.8.5 共享 Redis 基础设施 / §1.7 会话缓存）
        - 基础操作 get/set/delete/exists/expire/incr，值统一 JSON 序列化（支持复杂对象）（SPEC §5.3）
        - 命名空间封装：session（会话缓存）、llm_cache（相同 prompt 响应缓存）、rate_limit（固定窗口限流）（SPEC §5.3）
    对外接口（方法/API）：
        - CacheManager.get_instance(config=None) -> CacheManager：单例（SPEC §5.3）
        - CacheManager.get(key, namespace=None) / set(key, value, ttl=None, namespace=None) / delete / exists / expire / incr：基础操作（SPEC §5.3）
        - CacheManager.set_session(user_id, data, ttl=86400) / get_session(user_id) / delete_session(user_id)：会话缓存命名空间（登出删除）（SPEC §5.3）
        - CacheManager.set_llm_cache(prompt_hash, response, ttl=3600) / get_llm_cache(prompt_hash)：LLM 响应缓存（相同 prompt 避免重复调用成本）（SPEC §5.3）
        - CacheManager.check_rate_limit(user_id, limit=100, window=60) -> bool：固定窗口限流检查（超限返回 False 拒绝请求）（SPEC §5.3）
    错误处理要求：
        - redis 未安装或连接失败：降级内存字典模拟（带 TTL 清理）（SPEC §5.3）
"""

import json
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# C2：空对象缓存标记（防穿透）——builder 返回 None 时缓存该标记，
# get_or_build 读取到标记直接返回 None，避免每次请求都回源重建
_EMPTY_MARKER: Dict[str, bool] = {"__cache_empty__": True}


def _is_empty_marker(value: Any) -> bool:
    """判断缓存值是否为空对象标记。"""
    return isinstance(value, dict) and value.get("__cache_empty__") is True

# 尝试导入 redis，未安装时降级为内存字典模拟
try:
    import redis as _redis_module
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    logger.warning("redis 模块未安装，CacheManager 将降级为内存字典模拟")


class CacheManager:
    """
    Redis 缓存管理器单例类

    封装 Redis 连接与常用缓存操作，提供带命名空间的键管理。

    属性:
        _redis: Redis 客户端实例
        _prefix: 全局键前缀（默认 "runtime:"）

    设计说明:
        - 单例模式：全系统共享同一 Redis 连接
        - 命名空间：所有 key 自动添加前缀，避免与其他系统冲突
        - 序列化：值统一使用 JSON 序列化，支持复杂对象
    """

    _instance: Optional["CacheManager"] = None
    # 单例创建锁（保护 _instance 的创建，double-checked locking）
    _singleton_lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> "CacheManager":
        """单例模式：确保全系统只有一个 CacheManager 实例"""
        # double-checked locking：先无锁检查避免每次获取锁
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: dict) -> None:
        """
        初始化缓存管理器

        参数:
            config: Redis 配置字典，包含 host / port / db / tls / password / prefix 等字段
        """
        # 单例已初始化时跳过，避免重复初始化
        if getattr(self, "_initialized", False):
            return

        self._config = config or {}
        self._prefix = self._config.get("prefix", "runtime:")
        self._redis = None
        # 内存降级模式的存储：key -> JSON 序列化后的值
        self._memory_store: dict = {}
        # 内存降级模式的 TTL 记录：key -> 过期时间戳
        self._memory_ttls: dict = {}
        # 内存降级模式的锁（保护 _memory_store / _memory_ttls 的并发读写）
        self._memory_lock = threading.Lock()
        # C2 缓存击穿防护：per-key 单飞锁（同一 key 并发未命中时仅重建一次）
        self._build_locks: Dict[str, threading.Lock] = {}
        self._build_locks_guard = threading.Lock()
        self._initialized = False

        self._init_redis()
        self._initialized = True

    def _init_redis(self) -> None:
        """初始化 Redis 连接，未安装 redis 或连接失败时降级为内存模式"""
        if not _REDIS_AVAILABLE:
            logger.warning("redis 模块不可用，使用内存字典模拟缓存")
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
            logger.warning("Redis 连接失败 (%s)，降级为内存字典模拟", e)
            self._redis = None

    def ping(self) -> bool:
        """S6：连通性探测——Redis 在线返回 True；未连接/连接失效/内存降级返回 False。

        供 /ready 就绪探针使用（见 run_server._register_health_routes）。
        """
        if self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    @classmethod
    def get_instance(cls, config: Optional[dict] = None) -> "CacheManager":
        """
        获取单例实例

        参数:
            config: Redis 配置字典（仅在首次初始化时需要，None 时使用默认配置）

        返回:
            CacheManager 单例
        """
        if cls._instance is None:
            cls._instance = cls(config or {})
        return cls._instance

    def _make_key(self, key: str, namespace: Optional[str] = None) -> str:
        """
        构建带前缀的键

        参数:
            key: 原始键名
            namespace: 命名空间（如 session / llm_cache / rate_limit）

        返回:
            完整的 Redis 键名，格式为 prefix:namespace:key 或 prefix:key
        """
        if namespace:
            return f"{self._prefix}{namespace}:{key}"
        return f"{self._prefix}{key}"

    def get(self, key: str, namespace: Optional[str] = None) -> Any:
        """
        获取缓存值

        参数:
            key: 缓存键（不含前缀）
            namespace: 命名空间

        返回:
            反序列化后的值，不存在时返回 None
        """
        full_key = self._make_key(key, namespace)

        if self._redis is not None:
            value = self._redis.get(full_key)
            if value is None:
                return None
            return json.loads(value)

        # 内存降级模式
        with self._memory_lock:
            self._cleanup_expired()
            if full_key not in self._memory_store:
                return None
            return json.loads(self._memory_store[full_key])

    def set(self, key: str, value: Any, ttl: Optional[int] = None,
            namespace: Optional[str] = None) -> bool:
        """
        设置缓存值

        参数:
            key: 缓存键
            value: 待缓存值（自动 JSON 序列化）
            ttl: 过期时间（秒，None 表示永不过期）
            namespace: 命名空间

        返回:
            True 表示设置成功
        """
        full_key = self._make_key(key, namespace)
        serialized = json.dumps(value, ensure_ascii=False, default=str)

        if self._redis is not None:
            if ttl is not None:
                return self._redis.set(full_key, serialized, ex=ttl)
            return self._redis.set(full_key, serialized)

        # 内存降级模式
        self._memory_store[full_key] = serialized
        if ttl is not None:
            self._memory_ttls[full_key] = time.time() + ttl
        elif full_key in self._memory_ttls:
            del self._memory_ttls[full_key]
        return True

    def delete(self, key: str, namespace: Optional[str] = None) -> int:
        """
        删除缓存

        参数:
            key: 缓存键
            namespace: 命名空间

        返回:
            删除的键数量
        """
        full_key = self._make_key(key, namespace)

        if self._redis is not None:
            return self._redis.delete(full_key)

        # 内存降级模式
        existed = full_key in self._memory_store
        self._memory_store.pop(full_key, None)
        self._memory_ttls.pop(full_key, None)
        return 1 if existed else 0

    def exists(self, key: str, namespace: Optional[str] = None) -> bool:
        """
        检查键是否存在

        参数:
            key: 缓存键
            namespace: 命名空间

        返回:
            True 表示存在
        """
        full_key = self._make_key(key, namespace)

        if self._redis is not None:
            return self._redis.exists(full_key) > 0

        # 内存降级模式
        self._cleanup_expired()
        return full_key in self._memory_store

    def expire(self, key: str, ttl: int, namespace: Optional[str] = None) -> bool:
        """
        设置过期时间

        参数:
            key: 缓存键
            ttl: 过期时间（秒）
            namespace: 命名空间

        返回:
            True 表示设置成功
        """
        full_key = self._make_key(key, namespace)

        if self._redis is not None:
            return self._redis.expire(full_key, ttl)

        # 内存降级模式
        with self._memory_lock:
            if full_key not in self._memory_store:
                return False
            self._memory_ttls[full_key] = time.time() + ttl
            return True

    def incr(self, key: str, amount: int = 1,
             namespace: Optional[str] = None) -> int:
        """
        自增计数器

        用于限流计数器等场景。

        参数:
            key: 计数器键
            amount: 自增量（默认 1）
            namespace: 命名空间

        返回:
            自增后的值
        """
        full_key = self._make_key(key, namespace)

        if self._redis is not None:
            return self._redis.incrby(full_key, amount)

        # 内存降级模式
        self._cleanup_expired()
        current = int(self._memory_store.get(full_key, "0"))
        new_value = current + amount
        self._memory_store[full_key] = str(new_value)
        return new_value

    # -------------------------------------------------------------------------
    # 通用命名空间封装方法
    # -----------------------------------------------------------------

    def set_session(self, user_id: str, data: dict, ttl: int = 86400) -> bool:
        """
        设置会话缓存

        键格式：session:<user_id>
        存储会话信息与用户权限快照。

        参数:
            user_id: 用户 ID
            data: 会话数据字典
            ttl: 过期时间（秒，默认 24 小时）

        返回:
            True 表示设置成功
        """
        return self.set(user_id, data, ttl=ttl, namespace="session")

    def get_session(self, user_id: str) -> Optional[dict]:
        """
        获取会话缓存

        参数:
            user_id: 用户 ID

        返回:
            会话数据字典，不存在时返回 None
        """
        return self.get(user_id, namespace="session")

    def delete_session(self, user_id: str) -> int:
        """
        删除会话缓存（登出时调用）

        参数:
            user_id: 用户 ID

        返回:
            删除的键数量
        """
        return self.delete(user_id, namespace="session")

    def set_llm_cache(
        self,
        prompt_hash: str,
        response: dict,
        ttl: int = 3600,
    ) -> bool:
        """
        设置 LLM 响应缓存

        键格式：llm_cache:<hash>
        缓存相同 prompt 的 LLM 响应，避免重复调用成本。
        hash 为 prompt + model + temperature 的哈希值。

        参数:
            prompt_hash: Prompt 哈希
            response: LLM 响应字典
            ttl: 过期时间（秒，默认 1 小时）

        返回:
            True 表示设置成功
        """
        return self.set(prompt_hash, response, ttl=ttl, namespace="llm_cache")

    def get_llm_cache(self, prompt_hash: str) -> Optional[dict]:
        """
        获取 LLM 响应缓存

        参数:
            prompt_hash: Prompt 哈希

        返回:
            缓存的响应字典，不存在时返回 None
        """
        return self.get(prompt_hash, namespace="llm_cache")

    def check_rate_limit(
        self,
        user_id: str,
        limit: int = 100,
        window: int = 60,
    ) -> bool:
        """
        限流检查

        键格式：rate_limit:<user_id>:<window>
        基于固定窗口的请求计数，防止滥用。

        参数:
            user_id: 用户 ID
            limit: 窗口内最大请求数（默认 100）
            window: 时间窗口（秒，默认 60）

        返回:
            True 表示未超限（允许请求），False 表示已超限（拒绝请求）
        """
        key = f"{user_id}:{window}"
        current = self.incr(key, namespace="rate_limit")
        if current == 1:
            # 首次请求，设置窗口过期时间
            self.expire(key, window, namespace="rate_limit")
        return current <= limit

    # -------------------------------------------------------------------------
    # C2 缓存防护（可商用部署补充：击穿/穿透/雪崩）
    # -----------------------------------------------------------------

    def _get_build_lock(self, key: str, namespace: Optional[str]) -> threading.Lock:
        """获取 per-key 单飞锁（进程内按完整 key 复用，避免重建同一 key 时并发放大）。"""
        full_key = self._make_key(key, namespace)
        with self._build_locks_guard:
            lock = self._build_locks.get(full_key)
            if lock is None:
                lock = threading.Lock()
                self._build_locks[full_key] = lock
            return lock

    @staticmethod
    def _jitter_ttl(ttl: Optional[float]) -> Optional[float]:
        """C2：TTL 随机抖动（±10%）——同一批 key 的过期时刻错开，防缓存雪崩。

        使用 random.uniform(0.9, 1.1)；ttl 为 None（永不过期）或非法值时原样返回。
        """
        if ttl is None:
            return None
        try:
            return ttl * random.uniform(0.9, 1.1)
        except (TypeError, ValueError):
            return ttl

    def set_empty(self, key: str, ttl: Optional[int] = None,
                  namespace: str = "cache") -> bool:
        """C2 空对象缓存（防穿透）：builder 返回 None 时缓存空标记。

        后续 get_or_build 命中空标记直接返回 None，避免高频请求反复回源
        （DB 查无、远端服务空结果等"穿透"场景）。

        参数:
            key: 缓存键
            ttl: 过期时间（秒，None 表示永不过期）
            namespace: 命名空间（默认 "cache"）

        返回:
            True 表示设置成功
        """
        return self.set(key, _EMPTY_MARKER, ttl=ttl, namespace=namespace)

    def get_or_build(self, key: str, builder: Callable[[], Any],
                     ttl: Optional[int] = None, namespace: str = "cache") -> Any:
        """C2 缓存击穿防护（singleflight 简化版）。

        先查缓存，未命中时加进程内 per-key 锁重建（同一 key 的并发请求
        只有一个会执行 builder，其余等待后复用结果）；builder 返回 None
        时缓存空标记（防穿透）；ttl 自动 ±10% 随机抖动（防雪崩）。

        参数:
            key: 缓存键
            builder: 未命中时的回源构建函数（无参，返回待缓存值）
            ttl: 基准过期时间（秒，自动抖动；None 表示永不过期）
            namespace: 命名空间（默认 "cache"）

        返回:
            builder 的返回值；builder 返回 None 时返回 None（已缓存空标记）
        """
        # 1) 常规命中直接返回（含空标记 → 返回 None）
        value = self.get(key, namespace=namespace)
        if value is not None:
            return None if _is_empty_marker(value) else value

        # 2) 未命中：per-key 单飞锁内重建（double-check，等待期间他人可能已完成）
        lock = self._get_build_lock(key, namespace)
        with lock:
            value = self.get(key, namespace=namespace)
            if value is not None:
                return None if _is_empty_marker(value) else value
            try:
                value = builder()
            except Exception as e:
                # 回源异常不缓存（避免把临时故障固化），向上抛由调用方处理
                logger.exception("缓存重建失败 key=%s: %s", key, e)
                raise
            if value is None:
                self.set_empty(key, ttl, namespace=namespace)
                return None
            self.set(key, value, ttl=self._jitter_ttl(ttl), namespace=namespace)
            return value

    def warmup(self, keys_builders: Optional[Dict[str, Callable[[], Any]]],
               ttl: Optional[int] = 3600, namespace: str = "cache") -> None:
        """C2 启动预热：遍历 {key: builder} 逐个 get_or_build。

        预热关键配置/规则等热点数据，避免启动后首个请求承担回源延迟。
        单个 key 构建失败仅记 WARNING，不阻断其余预热与系统启动。

        参数:
            keys_builders: {缓存键: 构建函数} 映射（可为空字典）
            ttl: 预热缓存过期时间（秒，默认 1 小时，自动抖动）
            namespace: 命名空间（默认 "cache"）
        """
        for key, builder in (keys_builders or {}).items():
            try:
                self.get_or_build(key, builder, ttl=ttl, namespace=namespace)
            except Exception as e:
                logger.warning("缓存预热失败 key=%s: %s", key, e)

    def close(self) -> None:
        """关闭 Redis 连接，清理内存降级存储"""
        if self._redis is not None:
            self._redis.close()
            self._redis = None
        with self._memory_lock:
            self._memory_store.clear()
            self._memory_ttls.clear()

    def _cleanup_expired(self) -> None:
        """清理内存降级模式中已过期的键（调用者需持有 _memory_lock）"""
        if not self._memory_ttls:
            return
        now = time.time()
        # 迭代时使用快照，避免在迭代过程中修改字典
        expired = [k for k, exp in list(self._memory_ttls.items()) if exp <= now]
        for k in expired:
            self._memory_store.pop(k, None)
            self._memory_ttls.pop(k, None)


def get_cache() -> CacheManager:
    """
    模块级便捷函数：获取缓存管理器单例

    返回:
        CacheManager 单例实例
    """
    return CacheManager.get_instance()
