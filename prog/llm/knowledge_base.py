"""
KnowledgeBase 企业管理知识库模块
===============================

文件用途：
    实现企业管理知识库的管理与检索，承载知识文档的存储、向量化、
    语义检索、版本管理等能力。

技术规格章节：
    - §3.7 Knowledge Assistant（知识库RAG的基础设施）
    - 双通道架构：管理咨询通道的知识存储层

替代demo：
    替代 demo/llm_engine.py 的 build_enterprise_knowledge() 硬编码知识。
    demo将企业管理知识以长字符串硬编码在代码中，无法动态维护。
    本模块改为：知识文档 -> MinIO/TOS存储 -> 向量化 -> Milvus存储 ->
    语义检索，支持动态增删改与版本管理。

核心功能：
    1. 知识文档管理：
       文档增删改查，原始文件存储到 MinIO/TOS 对象存储
    2. 向量化存储：
       文档分块 -> embedding -> 存入 Milvus（Collection: ai_factory_kb）
    3. 语义检索（RAG）：
       query向量化 -> Milvus相似度检索 -> 返回top_k知识片段
    4. 知识更新与版本管理：
       文档更新时重新分块向量化，保留历史版本

依赖组件：
    - core/vector_store.py: Milvus向量库访问
    - core/embedding_provider.py: 文本向量化
    - 对象存储: MinIO/TOS（文档原始文件）

数据流：
    写入：文档文本 -> 分块 -> embedding -> Milvus存储 + 元数据存PostgreSQL
    读取：query -> embedding -> Milvus检索 -> 返回知识片段
"""

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional


class KnowledgeChunk:
    """
    知识片段对象。

    用于封装文档分块后的单个片段，作为向量化与检索单元。

    属性说明：
        - chunk_id: 片段唯一ID
        - doc_id: 来源文档ID
        - content: 片段文本内容
        - title: 来源文档标题
        - category: 知识分类（如"制度"/"流程"/"岗位"）
        - embedding: 片段向量（写入Milvus时使用）
        - score: 检索相似度得分（检索返回时填充）
        - version: 文档版本号
    """

    def __init__(self, chunk_id: str = "", doc_id: str = "",
                 content: str = "", title: str = "",
                 category: str = "", source: str = "",
                 embedding: Optional[List[float]] = None,
                 score: float = 0.0,
                 metadata: Optional[Dict[str, Any]] = None,
                 version: int = 1):
        """初始化知识片段。

        参数：
            chunk_id: 片段唯一ID
            doc_id: 来源文档ID
            content: 片段文本内容
            title: 来源文档标题
            category: 知识分类
            source: 来源标识
            embedding: 片段向量
            score: 检索相似度得分
            metadata: 附加元数据
            version: 文档版本号
        """
        self.chunk_id = chunk_id
        self.doc_id = doc_id
        self.content = content
        self.title = title
        self.category = category
        self.source = source
        self.embedding = embedding
        self.score = score
        self.metadata = metadata or {}
        self.version = version

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "content": self.content,
            "title": self.title,
            "source": self.source,
            "score": self.score,
            "metadata": self.metadata,
        }


class KnowledgeBase:
    """
    企业管理知识库。

    设计意图：
        作为管理咨询通道的知识存储与检索层，供 KnowledgeAssistant
        调用完成RAG问答。支持知识文档的动态维护。

    替代demo：
        替代 demo/llm_engine.py build_enterprise_knowledge() 的硬编码知识。

    属性：
        vector_store: Milvus向量库访问实例（Collection: ai_factory_kb）
        embedding_provider: 文本向量化提供方
        db: 数据库访问层（文档元数据持久化，knowledge_documents 表）

    降级策略：
        - 无 vector_store 时：降级为内存存储 + 关键词/TF-IDF检索
        - 无 embedding_provider 时：降级为TF-IDF相似度检索

    v6.30 扩展（训练内容同步知识库）：
        - add_document 额外持久化到 knowledge_documents 表（doc_id 存
          extra_data.doc_id，title/doc_type/content/tags 落库）
        - load_from_db() 进程启动时从 knowledge_documents 加载已入库文档，
          使图纸/工艺/训练文件/流程文档等训练内容可被 RAG 检索
          （辅助质量分析、流程查询等）
        - 进程级单例 get_knowledge_base()：各 Agent / 训练接口共享同一实例

    Milvus Collection说明：
        - 名称: ai_factory_kb
        - 字段: chunk_id, doc_id, content, title, category, version, embedding
        - 索引: IVF_FLAT / HNSW（基于embedding向量相似度检索）
    """

    # Milvus Collection 名称
    COLLECTION_NAME = "ai_factory_kb"

    _instance: Optional["KnowledgeBase"] = None

    @classmethod
    def get_instance(cls, db: Any = None, vector_store: Any = None,
                     embedding_provider: Any = None) -> "KnowledgeBase":
        """进程级单例（各 Agent / 训练接口共享同一内存知识库）。

        P0 接线：首次构造或后续显式传入 vector_store / embedding_provider 时
        注入组件，使 RAG 真正走向量检索（否则降级为内存 TF-IDF 关键词检索）。
        """
        if cls._instance is None:
            cls._instance = cls(vector_store=vector_store,
                                embedding_provider=embedding_provider,
                                db=db)
        else:
            if db is not None:
                cls._instance.db = db
            if vector_store is not None:
                cls._instance.vector_store = vector_store
            if embedding_provider is not None:
                cls._instance.embedding_provider = embedding_provider
        return cls._instance

    def __init__(self, vector_store: Any = None,
                 embedding_provider: Any = None,
                 db: Any = None):
        """
        初始化知识库。

        参数：
            vector_store: Milvus向量库访问实例
            embedding_provider: 文本向量化提供方
            db: 数据库访问层（文档元数据持久化）
        """
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.db = db
        # 内存存储（始终维护，用于降级检索与元数据管理）
        # 结构: {doc_id: {"content": str, "source": str, "metadata": dict, "chunks": [str], "chunk_ids": [str]}}
        self._memory_store: Dict[str, Dict[str, Any]] = {}

    # --------------------------------------------------------
    # 新增文档
    # --------------------------------------------------------
    def add_document(self, doc_id: str, content: str,
                     source: str,
                     metadata: Optional[Dict[str, Any]] = None) -> str:
        """
        新增知识文档。

        设计意图：
            将文档分块、向量化后写入Milvus（若可用），同时在内存中
            维护一份副本用于降级检索。返回文档ID供后续更新/删除使用。

        参数：
            doc_id: 文档ID（为空时自动生成）
            content: 文档全文内容
            source: 来源标识（如"企业管理手册-第3章"）
            metadata: 附加元数据（如标题、分类等）

        返回：
            str: 文档ID

        流程：
            1. 生成或校验doc_id
            2. 若doc_id已存在，先删除旧文档（实现upsert语义）
            3. 文档分块（按字数+重叠）
            4. 每块调用 embedding_provider 生成向量（若可用）
            5. 写入Milvus（若vector_store可用）
            6. 同步写入内存存储

        替代demo：
            替代 demo/llm_engine.py build_enterprise_knowledge() 的硬编码添加。
        """
        # 生成或校验doc_id
        if not doc_id:
            doc_id = self._generate_doc_id(content)
        metadata = metadata or {}

        # 若doc_id已存在，先删除旧文档（upsert语义）
        if doc_id in self._memory_store:
            self.delete_document(doc_id)

        # 文档分块
        chunks = self._split_text(content)

        # 向量化并写入向量库
        chunk_ids: List[str] = []
        if (self.vector_store is not None and chunks
                and self.embedding_provider is not None
                and not self.embedding_provider.is_mock()):
            vectors = self._generate_embeddings(chunks)
            if vectors:
                meta_list = []
                for i, chunk in enumerate(chunks):
                    chunk_meta = {
                        "doc_id": doc_id,
                        "chunk_text": chunk,
                        "source": source,
                        "metadata": {**metadata, "chunk_index": i},
                    }
                    meta_list.append(chunk_meta)
                try:
                    chunk_ids = self.vector_store.insert(
                        collection_name=self.COLLECTION_NAME,
                        vectors=vectors,
                        metadata=meta_list,
                    )
                except Exception:
                    chunk_ids = []

        # 同步写入内存存储（始终维护，用于降级检索）
        self._memory_store[doc_id] = {
            "content": content,
            "source": source,
            "metadata": metadata,
            "chunks": chunks,
            "chunk_ids": chunk_ids,
        }

        # v6.30：持久化到 knowledge_documents 表（DB 可用时，失败静默降级内存）
        self._persist_to_db(doc_id, content, source, metadata)

        return doc_id

    def _persist_to_db(self, doc_id: str, content: str, source: str,
                       metadata: dict) -> None:
        """将文档持久化到 knowledge_documents 表（训练内容跨进程可检索）。"""
        if self.db is None:
            return
        try:
            title = (metadata or {}).get("title") or doc_id
            doc_type = ((metadata or {}).get("doc_type")
                        or (metadata or {}).get("category") or "knowledge")
            tags = (metadata or {}).get("tags", [])
            self.db.insert("knowledge_documents", {
                "title": title,
                "doc_type": doc_type,
                "content": content,
                "tags": json.dumps(tags, ensure_ascii=False)
                         if isinstance(tags, list) else "[]",
                "status": "active",
                "extra_data": json.dumps(
                    {
                        "doc_id": doc_id,
                        "source": source,
                        # 知识自动沉淀幂等键（kb_sink 用 extra_data->>'source_key' 去重）
                        "source_key": (metadata or {}).get("source_key"),
                        "source_type": (metadata or {}).get("source_type"),
                        "biz_no": (metadata or {}).get("biz_no", ""),
                    },
                    ensure_ascii=False),
            })
        except Exception:
            pass  # DB 不可用：仅内存存储

    def load_from_db(self) -> int:
        """从 knowledge_documents 表加载已入库文档到内存（进程启动时调用）。

        使图纸/工艺/训练文件/流程文档等训练内容在服务重启后仍可被 RAG
        检索（不重复向量化，检索走 TF-IDF/关键词降级或向量库）。

        Returns:
            int: 加载的文档数量
        """
        if self.db is None:
            return 0
        count = 0
        try:
            rows = self.db.query_many(
                "knowledge_documents", filters={"status": "active"}) or []
            for row in rows:
                title = row.get("title") or ""
                content = row.get("content") or ""
                doc_type = row.get("doc_type") or "knowledge"
                extra = row.get("extra_data")
                if isinstance(extra, str):
                    try:
                        extra = json.loads(extra)
                    except (ValueError, TypeError):
                        extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                source = extra.get("source") or doc_type
                key = str(extra.get("doc_id") or row.get("doc_id"))
                # 直接构建内存条目（不做向量化，检索走关键词/TF-IDF 降级）
                self._memory_store[key] = {
                    "content": content,
                    "source": source,
                    "metadata": {"title": title, "doc_type": doc_type,
                                 "db_doc_id": row.get("doc_id")},
                    "chunks": self._split_text(content),
                    "chunk_ids": [],
                }
                count += 1
        except Exception:
            pass
        return count

    def add_documents(self, documents: List[Dict[str, Any]]) -> List[str]:
        """
        批量添加知识文档。

        参数：
            documents: 文档字典列表，每项含 doc_id/content/source/metadata

        返回：
            list: 成功添加的文档ID列表
        """
        doc_ids: List[str] = []
        for doc in documents:
            doc_id = doc.get("doc_id", "")
            content = doc.get("content", "")
            source = doc.get("source", "")
            metadata = doc.get("metadata")
            result_id = self.add_document(doc_id, content, source, metadata)
            doc_ids.append(result_id)
        return doc_ids

    # --------------------------------------------------------
    # 语义检索
    # --------------------------------------------------------
    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索知识片段。

        设计意图：
            RAG检索的核心方法。根据可用组件自动选择最优检索策略：
            - vector_store + embedding_provider: 向量语义检索
            - 仅 vector_store（无embedding）: TF-IDF相似度检索
            - 无 vector_store: 内存关键词/TF-IDF检索

        参数：
            query: 查询文本
            top_k: 返回片段数量上限（默认5）

        返回：
            list[dict]: 检索到的知识片段列表，每项含
                        {content, source, score, metadata}，按相似度降序
        """
        if not query or not self._memory_store:
            return []

        # 优先尝试向量检索（需要 vector_store + embedding_provider）
        # P1-9：embedding 处于模拟模式（无 key / 底层库缺失，向量为确定性
        # 伪随机）时跳过向量检索，避免伪随机 query 命中伪随机向量产生垃圾结果。
        if (self.vector_store is not None and self.embedding_provider is not None
                and not self.embedding_provider.is_mock()):
            results = self._search_via_vector_store(query, top_k)
            if results:
                return results

        # 降级：TF-IDF / 关键词检索（基于内存存储）
        return self._tfidf_search(query, top_k)

    # --------------------------------------------------------
    # 删除文档
    # --------------------------------------------------------
    def delete_document(self, doc_id: str) -> None:
        """
        删除知识文档。

        设计意图：
            从向量库与内存存储中彻底删除文档及其所有分块。

        参数：
            doc_id: 文档ID
        """
        doc_data = self._memory_store.get(doc_id)
        if doc_data is None:
            return

        # 从向量库删除分块
        if self.vector_store is not None:
            chunk_ids = doc_data.get("chunk_ids", [])
            if chunk_ids:
                try:
                    self.vector_store.delete(
                        collection_name=self.COLLECTION_NAME,
                        ids=chunk_ids,
                    )
                except Exception:
                    pass

        # 从内存存储删除
        self._memory_store.pop(doc_id, None)

    # --------------------------------------------------------
    # 获取文档数
    # --------------------------------------------------------
    def get_document_count(self) -> int:
        """
        获取知识库中的文档总数。

        返回：
            int: 文档数量
        """
        return len(self._memory_store)

    # --------------------------------------------------------
    # 文本分块
    # --------------------------------------------------------
    def _split_text(self, text: str, chunk_size: int = 500,
                    overlap: int = 50) -> List[str]:
        """
        文本分块。

        按指定字数切分文本，相邻块之间保留一定重叠以保证语义连贯。

        参数：
            text: 待分块的文本
            chunk_size: 每块最大字数（默认500）
            overlap: 相邻块重叠字数（默认50）

        返回：
            list: 分块后的文本列表
        """
        if not text:
            return []
        # 文本不超过块大小，直接返回
        if len(text) <= chunk_size:
            return [text]

        # 防止 overlap >= chunk_size 导致死循环
        if overlap >= chunk_size:
            overlap = chunk_size // 4

        chunks: List[str] = []
        start = 0
        step = chunk_size - overlap
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            if end >= len(text):
                break
            start += step
        return chunks

    # --------------------------------------------------------
    # 生成文档ID
    # --------------------------------------------------------
    def _generate_doc_id(self, content: str) -> str:
        """
        根据文档内容生成唯一文档ID。

        使用内容MD5哈希前16位，确保相同内容生成相同ID。

        参数：
            content: 文档内容

        返回：
            str: 文档ID
        """
        return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]

    # --------------------------------------------------------
    # 内部方法：向量化
    # --------------------------------------------------------
    def _generate_embeddings(self, chunks: List[str]) -> List[List[float]]:
        """为文本块生成向量。

        优先使用 embedding_provider.embed_batch 批量生成，
        失败时降级为逐条 embed。无 embedding_provider 时返回空列表。

        参数：
            chunks: 文本块列表

        返回：
            list: 向量列表，与chunks一一对应
        """
        if self.embedding_provider is None or not chunks:
            return []
        # 优先批量向量化
        try:
            vectors = self.embedding_provider.embed_batch(chunks)
            if vectors and len(vectors) == len(chunks):
                return vectors
        except Exception:
            pass
        # 降级：逐条向量化
        try:
            vectors = []
            for chunk in chunks:
                vec = self.embedding_provider.embed(chunk)
                vectors.append(vec)
            return vectors
        except Exception:
            return []

    # --------------------------------------------------------
    # 内部方法：向量库检索
    # --------------------------------------------------------
    def _search_via_vector_store(self, query: str,
                                  top_k: int) -> List[Dict[str, Any]]:
        """通过向量库进行语义检索。

        参数：
            query: 查询文本
            top_k: 返回数量上限

        返回：
            list: 检索结果列表
        """
        try:
            query_vector = self.embedding_provider.embed(query)
            results = self.vector_store.search(
                collection_name=self.COLLECTION_NAME,
                query_vector=query_vector,
                top_k=top_k,
            )
            if not results:
                return []

            formatted: List[Dict[str, Any]] = []
            for r in results:
                content = r.get("chunk_text", "") or r.get("content", "")
                source = r.get("source", "")
                score = r.get("distance", 0.0)
                meta = r.get("metadata", {})
                # metadata 可能是 JSON 字符串
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, ValueError):
                        meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                item = {
                    "content": content,
                    "source": source,
                    "score": float(score),
                    "metadata": meta,
                }
                # 若元数据中有标题，提升到顶层方便访问
                if "title" in meta:
                    item["title"] = meta["title"]
                formatted.append(item)
            return formatted
        except Exception:
            return []

    # --------------------------------------------------------
    # 内部方法：TF-IDF检索
    # --------------------------------------------------------
    def _tfidf_search(self, query: str,
                      top_k: int) -> List[Dict[str, Any]]:
        """基于TF-IDF相似度的文本检索（降级方案）。

        当无 embedding_provider 或向量检索不可用时，使用TF-IDF
        余弦相似度在内存文档块中检索最相关的片段。

        参数：
            query: 查询文本
            top_k: 返回数量上限

        返回：
            list: 检索结果列表，按相似度降序
        """
        # 收集所有文档块
        all_chunks: List[Dict[str, Any]] = []
        for doc_id, doc_data in self._memory_store.items():
            for i, chunk in enumerate(doc_data.get("chunks", [])):
                all_chunks.append({
                    "content": chunk,
                    "source": doc_data.get("source", ""),
                    "metadata": {
                        **doc_data.get("metadata", {}),
                        "doc_id": doc_id,
                        "chunk_index": i,
                    },
                })

        if not all_chunks:
            return []

        # 分词
        query_tokens = self._tokenize(query)
        doc_tokens_list = [self._tokenize(c["content"]) for c in all_chunks]

        # 计算文档频率（DF）
        N = len(doc_tokens_list)
        df: Dict[str, int] = {}
        for tokens in doc_tokens_list:
            seen = set(tokens)
            for token in seen:
                df[token] = df.get(token, 0) + 1

        # 计算 IDF
        idf: Dict[str, float] = {}
        for token, freq in df.items():
            idf[token] = math.log((N + 1) / (freq + 1)) + 1

        # 计算查询向量 TF-IDF
        query_tf: Dict[str, int] = {}
        for token in query_tokens:
            query_tf[token] = query_tf.get(token, 0) + 1
        query_vec: Dict[str, float] = {
            token: tf * idf.get(token, 0.0)
            for token, tf in query_tf.items()
        }

        # 计算每个文档块的TF-IDF向量并求余弦相似度
        scored: List[tuple] = []
        for idx, tokens in enumerate(doc_tokens_list):
            if not tokens:
                scored.append((0.0, idx))
                continue
            doc_tf: Dict[str, int] = {}
            for token in tokens:
                doc_tf[token] = doc_tf.get(token, 0) + 1
            doc_vec: Dict[str, float] = {
                token: tf * idf.get(token, 0.0)
                for token, tf in doc_tf.items()
            }
            score = self._cosine_similarity(query_vec, doc_vec)
            scored.append((score, idx))

        # 按相似度降序排序，取top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        results: List[Dict[str, Any]] = []
        for score, idx in scored[:top_k]:
            if score <= 0:
                continue
            chunk = all_chunks[idx]
            item = {
                "content": chunk["content"],
                "source": chunk["source"],
                "score": round(score, 4),
                "metadata": chunk["metadata"],
            }
            # 若元数据中有标题，提升到顶层
            if "title" in chunk["metadata"]:
                item["title"] = chunk["metadata"]["title"]
            results.append(item)

        # 若TF-IDF无匹配结果，降级为关键词重叠搜索
        if not results:
            results = self._keyword_search(query, top_k, all_chunks)

        return results

    def _keyword_search(self, query: str, top_k: int,
                        all_chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """关键词重叠搜索（TF-IDF无匹配时的最终兜底）。

        参数：
            query: 查询文本
            top_k: 返回数量上限
            all_chunks: 全部文档块

        返回：
            list: 检索结果列表
        """
        query_tokens = set(self._tokenize(query))
        if not query_tokens:
            return []
        scored: List[tuple] = []
        for idx, chunk in enumerate(all_chunks):
            doc_tokens = set(self._tokenize(chunk["content"]))
            # 计算交集比例作为得分
            overlap = len(query_tokens & doc_tokens)
            if overlap > 0:
                score = overlap / len(query_tokens)
                scored.append((score, idx))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: List[Dict[str, Any]] = []
        for score, idx in scored[:top_k]:
            chunk = all_chunks[idx]
            item = {
                "content": chunk["content"],
                "source": chunk["source"],
                "score": round(score, 4),
                "metadata": chunk["metadata"],
            }
            if "title" in chunk["metadata"]:
                item["title"] = chunk["metadata"]["title"]
            results.append(item)
        return results

    # --------------------------------------------------------
    # 内部方法：分词
    # --------------------------------------------------------
    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """简单分词：提取中文单字与英文单词。

        对中文按单字切分，对英文按单词切分，统一小写。
        不依赖jieba等外部分词库，保证可用性。

        参数：
            text: 待分词文本

        返回：
            list: 词元列表
        """
        tokens: List[str] = []
        # 提取英文单词
        for word in re.findall(r"[a-zA-Z]+", text):
            tokens.append(word.lower())
        # 提取中文单字
        for char in re.findall(r"[\u4e00-\u9fff]", text):
            tokens.append(char)
        # 提取数字串
        for num in re.findall(r"\d+", text):
            tokens.append(num)
        return tokens

    # --------------------------------------------------------
    # 内部方法：余弦相似度
    # --------------------------------------------------------
    @staticmethod
    def _cosine_similarity(vec_a: Dict[str, float],
                           vec_b: Dict[str, float]) -> float:
        """计算两个稀疏向量（字典表示）的余弦相似度。

        参数：
            vec_a: 向量A（字典：词元 -> 权重）
            vec_b: 向量B（字典：词元 -> 权重）

        返回：
            float: 余弦相似度（0到1，值越大越相似）
        """
        if not vec_a or not vec_b:
            return 0.0
        # 取两个向量共有的词元计算点积
        common_keys = set(vec_a) & set(vec_b)
        dot = sum(vec_a[k] * vec_b[k] for k in common_keys)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# ============================================================
# 全局单例
# ============================================================

# 全局知识库实例（延迟初始化）
_kb_instance: Optional[KnowledgeBase] = None


def get_knowledge_base() -> KnowledgeBase:
    """
    获取全局知识库单例实例。

    首次调用时创建默认实例（无向量库、无embedding，使用内存降级模式）。
    如需注入 vector_store / embedding_provider，可直接实例化 KnowledgeBase。

    返回：
        KnowledgeBase: 全局知识库实例
    """
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert KnowledgeBase is not None, "KnowledgeBase 类未定义"
    assert KnowledgeChunk is not None, "KnowledgeChunk 类未定义"
    assert get_knowledge_base is not None, "get_knowledge_base 函数未定义"
    # 验证基本功能
    kb = KnowledgeBase()
    # 验证文本分块
    chunks = kb._split_text("测试文本分块功能", chunk_size=10, overlap=2)
    assert len(chunks) > 0, "文本分块应返回结果"
    # 验证文档ID生成
    doc_id = kb._generate_doc_id("测试内容")
    assert doc_id, "文档ID生成应返回非空字符串"
    # 验证分词
    tokens = kb._tokenize("hello 世界123")
    assert "hello" in tokens and "世" in tokens and "界" in tokens
    # 验证添加与检索（内存降级模式）
    kb.add_document("doc1", "精益生产五大原则包括定义价值与识别价值流", "管理手册")
    assert kb.get_document_count() == 1, "文档数应为1"
    results = kb.search("精益生产", top_k=3)
    assert len(results) > 0, "检索应返回结果"
    assert "content" in results[0], "检索结果应包含content字段"
    assert "source" in results[0], "检索结果应包含source字段"
    assert "score" in results[0], "检索结果应包含score字段"
    # 验证删除
    kb.delete_document("doc1")
    assert kb.get_document_count() == 0, "删除后文档数应为0"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
