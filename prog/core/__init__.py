"""
core 模块 - AI工厂管家核心统一接口层

本模块实现技术规格 §1.8 定义的六大统一接口层，替代 demo 中的直接调用方式：
    - llm_provider.py   : LLMProvider（§1.8.2）
    - database.py       : Database（§1.8.1）
    - vector_store.py   : VectorStore（§1.8.3）
    - file_storage.py   : FileStorage（§1.8.4）
    - event_bus.py      : EventBus（§1.8.5）
    - embedding_provider.py : EmbeddingProvider（§1.8.6）
    - cache.py          : Redis 缓存封装

所有接口通过 deployment_config.json + 环境变量加载配置，支持 local / volcano 双部署。
"""
