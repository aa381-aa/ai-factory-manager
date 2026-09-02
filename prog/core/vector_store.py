"""
VectorStore 统一接口层 - AI工厂管家

文件用途：
    定义向量数据库访问的统一接口层，用于 RAG 知识库的向量存储与检索。
    通过 pymilvus 连接 Milvus 向量数据库。

对应技术规格章节：
    §1.8.3 VectorStore 统一接口层
    §1.3 企业管理知识库（RAG）

替代 demo 文件/函数：
    demo 中无独立的向量存储实现，知识库检索能力为本系统新增。
    本接口层替代 demo 中缺失的语义检索能力。

设计说明：
    1. 抽象基类 VectorStoreBase 定义统一契约，便于未来扩展其他向量库
    2. MilvusVectorStore 为默认实现，通过 pymilvus 连接 Milvus
    3. Collection 设计：ai_factory_kb（企业管理知识库）
    4. 索引参数：IVF_FLAT + COSINE 距离 + nlist=1024
        - IVF_FLAT：倒排文件索引，适合中等规模向量集，精度与速度均衡
        - COSINE：余弦距离，适合文本语义相似度计算
        - nlist=1024：聚类中心数，影响查询精度与速度
    5. Embedding 维度：1024（bge-m3 / doubao-embedding-large）

配置示例（deployment_config.json）:
    {
        "vector_store": {
            "local": {
                "host": "127.0.0.1", "port": 19530,
                "collection": "ai_factory_kb"
            },
            "volcano": {
                "host_env": "MILVUS_HOST", "port": 19530,
                "collection": "ai_factory_kb", "tls": true
            }
        }
    }
"""

import math
import uuid
import warnings
from typing import Any, Dict, List, Optional


class VectorStoreBase:
    """
    向量存储抽象基类

    定义所有向量库实现必须遵循的统一契约。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化向量存储

        参数:
            config: 向量库配置字典，包含 host / port / collection / tls 等字段
        """
        raise NotImplementedError

    def create_collection(
        self,
        collection_name: str,
        dimension: int = 1024,
        index_type: str = "IVF_FLAT",
        metric_type: str = "COSINE",
        nlist: int = 1024,
    ) -> bool:
        """
        创建 Collection

        参数:
            collection_name: Collection 名称
            dimension: 向量维度（默认 1024，对应 bge-m3 / doubao-embedding-large）
            index_type: 索引类型（默认 IVF_FLAT）
            metric_type: 距离度量类型（默认 COSINE）
            nlist: 聚类中心数（默认 1024）

        返回:
            True 表示创建成功
        """
        raise NotImplementedError

    def insert(
        self,
        collection_name: str,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
    ) -> List[str]:
        """
        插入向量数据

        参数:
            collection_name: Collection 名称
            vectors: 向量列表（每个向量为浮点数列表）
            metadata: 与向量一一对应的元数据字典列表

        返回:
            插入记录的主键 ID 列表
        """
        raise NotImplementedError

    def search(
        self,
        collection_name: str,
        query_vector: List[float],
        top_k: int = 10,
        filter_expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量相似度检索

        参数:
            collection_name: Collection 名称
            query_vector: 查询向量
            top_k: 返回最相似结果数（默认 10）
            filter_expr: 过滤表达式（Milvus 的布尔表达式）
            output_fields: 返回的字段列表

        返回:
            检索结果列表，每项含 id / distance / metadata 等字段
        """
        raise NotImplementedError

    def delete(
        self,
        collection_name: str,
        ids: List[str],
    ) -> int:
        """
        删除向量记录

        参数:
            collection_name: Collection 名称
            ids: 待删除记录的主键 ID 列表

        返回:
            删除的记录数
        """
        raise NotImplementedError

    def has_collection(self, collection_name: str) -> bool:
        """
        检查 Collection 是否存在

        参数:
            collection_name: Collection 名称

        返回:
            True 表示存在
        """
        raise NotImplementedError

    def drop_collection(self, collection_name: str) -> bool:
        """
        删除 Collection

        参数:
            collection_name: Collection 名称

        返回:
            True 表示删除成功
        """
        raise NotImplementedError


class MilvusVectorStore(VectorStoreBase):
    """
    Milvus 向量存储实现

    通过 pymilvus 连接 Milvus 向量数据库，支持本地部署与火山引擎 Milvus Cloud。

    降级模式：
        当 pymilvus 未安装时，自动降级为内存列表模拟，
        使用余弦相似度计算支持基本的 insert / search 操作。

    配置说明:
        - local 模式：直接连接 127.0.0.1:19530
        - volcano 模式：通过环境变量 MILVUS_HOST 配置主机，启用 TLS

    Collection 设计（ai_factory_kb）:
        字段:
            - id: 主键（VARCHAR）
            - embedding: 向量字段（FLOAT_VECTOR, dim=1024）
            - doc_id: 文档 ID（VARCHAR，关联 file_storage 中的文件）
            - chunk_text: 文本块内容（VARCHAR）
            - source: 来源标识（VARCHAR）
            - metadata: 元数据（JSON）
        索引:
            - 类型：IVF_FLAT
            - 度量：COSINE
            - nlist：1024
    """

    # 默认向量维度（bge-m3 / doubao-embedding-large）
    DEFAULT_DIMENSION = 1024

    # 类级单例实例
    _instance: Optional["MilvusVectorStore"] = None

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化 Milvus 向量存储

        参数:
            config: 已解析环境变量的 Milvus 配置字典
        """
        self._config = config
        self._host = config.get("host", "127.0.0.1")
        self._port = config.get("port", 19530)
        self._collection = config.get("collection", "ai_factory_kb")
        self._tls = config.get("tls", False)
        self._dimension = config.get("dimension", self.DEFAULT_DIMENSION)
        self._alias = "default"

        # 尝试导入 pymilvus，未安装时降级为内存列表模拟
        try:
            from pymilvus import connections, Collection, CollectionSchema
            from pymilvus import FieldSchema, DataType, utility

            self._connections = connections
            self._Collection = Collection
            self._CollectionSchema = CollectionSchema
            self._FieldSchema = FieldSchema
            self._DataType = DataType
            self._utility = utility
            self._mode = "milvus"
            self._connected = False
            # 尝试连接 Milvus：
            #  - 连接成功 → 正常使用 Milvus（保留完整 Milvus 代码路径，部署后自动生效）
            #  - 连接失败（Milvus 服务未部署）→ 降级为内存列表模拟，不阻断业务
            if not self.connect():
                self._init_memory_mode(
                    f"Milvus 服务不可达（{self._host}:{self._port}），"
                    "VectorStore 降级为内存列表模拟模式（Milvus 代码路径保留，部署后自动启用）"
                )
        except ImportError:
            # pymilvus 未安装，降级为内存列表模拟
            self._init_memory_mode(
                "pymilvus 未安装，VectorStore 降级为内存列表模拟模式"
            )

    def _init_memory_mode(self, reason: str) -> None:
        """降级为内存列表模拟模式

        内存模式下所有 Collection/插入/检索/删除均在进程内完成，
        业务代码无需感知底层向量库是否可用。
        """
        self._connections = None
        self._Collection = None
        self._CollectionSchema = None
        self._FieldSchema = None
        self._DataType = None
        self._utility = None
        self._mode = "memory"
        # 内存模式视为已连接，后续 has_collection/insert/search/delete 走内存分支
        self._connected = True
        # 内存存储结构：{collection_name: [record, ...]}
        self._memory_store: Dict[str, List[Dict[str, Any]]] = {}
        # 各 Collection 的维度记录
        self._collection_dims: Dict[str, int] = {}
        warnings.warn(reason)

    @classmethod
    def get_instance(cls, config: Optional[Dict[str, Any]] = None) -> "MilvusVectorStore":
        """
        获取单例实例

        参数:
            config: 配置字典（仅在首次初始化时需要）

        返回:
            MilvusVectorStore 单例
        """
        if cls._instance is None:
            if config is None:
                # 未显式传入配置时，从统一配置加载器获取向量库接口配置
                from prog.config.config_loader import get_config_loader

                config = get_config_loader().get_interface_config("vector_store")
            cls._instance = cls(config)
        return cls._instance

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """
        连接 Milvus 服务

        返回:
            True 表示连接成功（内存模式始终返回 True）
        """
        if self._mode == "memory":
            self._connected = True
            return True

        try:
            self._connections.connect(
                alias=self._alias,
                host=self._host,
                port=str(self._port),
                secure=self._tls,
            )
            self._connected = True
            return True
        except Exception as e:
            warnings.warn(f"Milvus 连接失败（{self._host}:{self._port}）: {e}")
            self._connected = False
            return False

    def close(self) -> None:
        """
        关闭 Milvus 连接

        内存模式下为空操作。
        """
        if self._mode == "milvus" and self._connected:
            try:
                self._connections.disconnect(self._alias)
            except Exception:
                pass
            self._connected = False

    # ------------------------------------------------------------------
    # Collection 管理
    # ------------------------------------------------------------------

    def create_collection(
        self,
        collection_name: str,
        dimension: int = 1024,
        index_type: str = "IVF_FLAT",
        metric_type: str = "COSINE",
        nlist: int = 1024,
    ) -> bool:
        """
        创建 Collection

        参数:
            collection_name: Collection 名称
            dimension: 向量维度（默认 1024）
            index_type: 索引类型（默认 IVF_FLAT）
            metric_type: 距离度量类型（默认 COSINE）
            nlist: 聚类中心数（默认 1024）

        返回:
            True 表示创建成功
        """
        if self._mode == "memory":
            # 内存模式：创建空列表与维度记录
            if collection_name not in self._memory_store:
                self._memory_store[collection_name] = []
                self._collection_dims[collection_name] = dimension
            return True

        # Milvus 模式：定义 Schema 并创建 Collection
        if self._utility.has_collection(collection_name):
            return True

        # 定义字段 Schema
        fields = [
            self._FieldSchema(
                name="id",
                dtype=self._DataType.VARCHAR,
                is_primary=True,
                max_length=256,
            ),
            self._FieldSchema(
                name="embedding",
                dtype=self._DataType.FLOAT_VECTOR,
                dim=dimension,
            ),
            self._FieldSchema(
                name="doc_id",
                dtype=self._DataType.VARCHAR,
                max_length=256,
            ),
            self._FieldSchema(
                name="chunk_text",
                dtype=self._DataType.VARCHAR,
                max_length=65535,
            ),
            self._FieldSchema(
                name="source",
                dtype=self._DataType.VARCHAR,
                max_length=256,
            ),
            self._FieldSchema(
                name="metadata",
                dtype=self._DataType.JSON,
            ),
        ]
        schema = self._CollectionSchema(
            fields=fields, description=f"AI工厂管家知识库 Collection: {collection_name}"
        )
        collection = self._Collection(
            name=collection_name, schema=schema, using=self._alias
        )
        # 创建向量索引
        collection.create_index(
            field_name="embedding",
            index_params={
                "index_type": index_type,
                "metric_type": metric_type,
                "params": {"nlist": nlist},
            },
        )
        return True

    def has_collection(self, collection_name: str) -> bool:
        """
        检查 Collection 是否存在

        参数:
            collection_name: Collection 名称

        返回:
            True 表示存在
        """
        if self._mode == "memory":
            return collection_name in self._memory_store
        return self._utility.has_collection(collection_name)

    def drop_collection(self, collection_name: str) -> bool:
        """
        删除 Collection

        参数:
            collection_name: Collection 名称

        返回:
            True 表示删除成功
        """
        if self._mode == "memory":
            if collection_name in self._memory_store:
                del self._memory_store[collection_name]
                self._collection_dims.pop(collection_name, None)
                return True
            return False
        if self._utility.has_collection(collection_name):
            self._utility.drop_collection(collection_name)
            return True
        return False

    def get_collection_stats(self, name: str) -> Dict[str, Any]:
        """
        获取 Collection 统计信息

        参数:
            name: Collection 名称

        返回:
            统计信息字典，含 row_count / dimension 等字段
        """
        if self._mode == "memory":
            records = self._memory_store.get(name, [])
            return {
                "name": name,
                "row_count": len(records),
                "dimension": self._collection_dims.get(name, self._dimension),
            }
        collection = self._Collection(name, using=self._alias)
        return {
            "name": name,
            "row_count": collection.num_entities,
            "dimension": self._dimension,
        }

    # ------------------------------------------------------------------
    # 数据操作
    # ------------------------------------------------------------------

    def insert(
        self,
        collection_name: str,
        vectors: List[List[float]],
        metadata: List[Dict[str, Any]],
    ) -> List[str]:
        """
        插入向量数据

        参数:
            collection_name: Collection 名称
            vectors: 向量列表（每个向量为浮点数列表）
            metadata: 与向量一一对应的元数据字典列表

        返回:
            插入记录的主键 ID 列表
        """
        if not vectors:
            return []

        # 自动创建 Collection（如不存在）
        if not self.has_collection(collection_name):
            dim = len(vectors[0]) if vectors else self._dimension
            self.create_collection(collection_name, dimension=dim)

        # 生成主键 ID 列表
        ids = [str(uuid.uuid4()) for _ in vectors]

        if self._mode == "memory":
            # 内存模式：追加记录到列表
            records = self._memory_store.setdefault(collection_name, [])
            for i, vec in enumerate(vectors):
                meta = metadata[i] if i < len(metadata) else {}
                records.append(
                    {
                        "id": ids[i],
                        "embedding": list(vec),
                        "doc_id": meta.get("doc_id", ""),
                        "chunk_text": meta.get("chunk_text", ""),
                        "source": meta.get("source", ""),
                        "metadata": meta,
                    }
                )
            return ids

        # Milvus 模式：批量插入
        collection = self._Collection(collection_name, using=self._alias)
        doc_ids = [m.get("doc_id", "") for m in metadata]
        chunk_texts = [m.get("chunk_text", "") for m in metadata]
        sources = [m.get("source", "") for m in metadata]
        metadatas = [m for m in metadata]
        collection.insert(
            [ids, vectors, doc_ids, chunk_texts, sources, metadatas]
        )
        collection.flush()
        return ids

    def search(
        self,
        collection_name: str,
        query_vector: List[float],
        top_k: int = 10,
        filter_expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量相似度检索

        参数:
            collection_name: Collection 名称
            query_vector: 查询向量
            top_k: 返回最相似结果数（默认 10）
            filter_expr: 过滤表达式（Milvus 的布尔表达式）
            output_fields: 返回的字段列表

        返回:
            检索结果列表，每项含 id / distance / metadata 等字段
        """
        # Collection 不存在时返回空结果
        if not self.has_collection(collection_name):
            return []

        # 默认返回字段
        if output_fields is None:
            output_fields = ["doc_id", "chunk_text", "source", "metadata"]

        if self._mode == "memory":
            # 内存模式：计算余弦相似度，返回 top_k 结果
            records = self._memory_store.get(collection_name, [])
            scored = []
            for record in records:
                score = self._cosine_similarity(query_vector, record["embedding"])
                scored.append((score, record))
            # 按相似度降序排序
            scored.sort(key=lambda x: x[0], reverse=True)
            results = []
            for score, record in scored[:top_k]:
                item = {
                    "id": record["id"],
                    "distance": score,
                }
                # 按请求字段组装结果
                for field in output_fields:
                    item[field] = record.get(field, "")
                results.append(item)
            return results

        # Milvus 模式：调用 Milvus search
        collection = self._Collection(collection_name, using=self._alias)
        collection.load()
        search_params = {
            "metric_type": "COSINE",
            "params": {"nprobe": 10},
        }
        results = collection.search(
            data=[query_vector],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            expr=filter_expr,
            output_fields=output_fields,
        )
        # 组装结果列表
        formatted = []
        if results:
            for hit in results[0]:
                entity = hit.entity.to_dict() if hasattr(hit, "entity") else {}
                item = {
                    "id": hit.id,
                    "distance": hit.distance,
                }
                for field in output_fields:
                    item[field] = entity.get(field, "")
                formatted.append(item)
        return formatted

    def delete(
        self,
        collection_name: str,
        ids: List[str],
    ) -> int:
        """
        删除向量记录

        参数:
            collection_name: Collection 名称
            ids: 待删除记录的主键 ID 列表

        返回:
            删除的记录数
        """
        if not ids:
            return 0

        if not self.has_collection(collection_name):
            return 0

        if self._mode == "memory":
            # 内存模式：按 ID 过滤删除
            records = self._memory_store.get(collection_name, [])
            id_set = set(ids)
            before = len(records)
            self._memory_store[collection_name] = [
                r for r in records if r["id"] not in id_set
            ]
            return before - len(self._memory_store[collection_name])

        # Milvus 模式：通过表达式删除
        collection = self._Collection(collection_name, using=self._alias)
        # 构建 id in [...] 过滤表达式
        id_list_str = ", ".join(f'"{i}"' for i in ids)
        expr = f'id in [{id_list_str}]'
        result = collection.delete(expr)
        collection.flush()
        # Milvus delete 返回 MutationResult，通过 delete_count 获取删除数
        return getattr(result, "delete_count", len(ids)) if result else len(ids)

    def delete_by_doc_id(self, collection_name: str, doc_id: str) -> int:
        """按文档ID删除其全部分块向量。

        P2 删除脱节修复：文档状态切为 archived/deprecated 或删除时，
        依据业务文档ID（doc_id）清理向量库，避免 RAG 检索仍命中已归档内容。

        参数:
            collection_name: Collection 名称
            doc_id: 业务文档ID（metadata.doc_id）

        返回:
            删除的记录数
        """
        if not doc_id:
            return 0
        if not self.has_collection(collection_name):
            return 0

        if self._mode == "memory":
            records = self._memory_store.get(collection_name, [])
            target_ids = [r["id"] for r in records
                          if r.get("doc_id") == doc_id]
            return self.delete(collection_name, target_ids)

        # Milvus 模式：通过 doc_id 字段表达式删除
        collection = self._Collection(collection_name, using=self._alias)
        expr = f'doc_id == "{doc_id}"'
        result = collection.delete(expr)
        collection.flush()
        return getattr(result, "delete_count", 0) if result else 0

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _cosine_similarity(
        vec_a: List[float], vec_b: List[float]
    ) -> float:
        """
        计算两个向量的余弦相似度

        参数:
            vec_a: 向量 A
            vec_b: 向量 B

        返回:
            余弦相似度（-1 到 1，值越大越相似）
        """
        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


def get_vector_store() -> MilvusVectorStore:
    """
    模块级便捷函数：获取向量存储单例

    返回:
        MilvusVectorStore 单例实例
    """
    return MilvusVectorStore.get_instance()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert VectorStoreBase is not None, "VectorStoreBase 类未定义"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
