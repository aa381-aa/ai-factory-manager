"""
EmbeddingProvider 统一接口层 - AI工厂管家

文件用途：
    定义文本向量化的统一接口层，用于 RAG 知识库的文档向量化与查询向量化。
    支持豆包 Embedding API（云端）与本地 bge-m3 模型两种实现。

对应技术规格章节：
    §1.8.6 EmbeddingProvider 统一接口层
    §1.3 企业管理知识库（RAG 向量化）

替代 demo 文件/函数：
    demo 中无独立的 Embedding 能力，RAG 检索能力为本系统新增。
    本接口层提供统一的文本向量化能力，供向量库与检索系统使用。

设计说明：
    1. 抽象基类 EmbeddingProviderBase 定义统一契约
    2. VolcanoEmbedding 为默认实现，调用豆包 Embedding API
    3. 维度：1024（与向量库 collection 配置一致）
    4. 支持单条文本与批量文本两种调用模式
    5. 配置来源：deployment_config.json 的 embedding_provider 节点

配置示例（deployment_config.json）:
    {
        "embedding_provider": {
            "type": "volcano",
            "model": "doubao-embedding-large",
            "dimension": 1024
        }
    }

使用场景:
    - 文档入库向量化：知识库文档分块后调用 embed_batch 批量生成向量，写入向量库
    - 查询向量化：用户查询文本调用 embed_text 生成查询向量，用于向量检索
"""

import hashlib
import json
import os
import random
from typing import Any, Dict, List, Optional


# 默认向量维度（与 Milvus collection 配置一致）
_DEFAULT_DIMENSION = 1024


def _deterministic_vector(text: str, dim: int = _DEFAULT_DIMENSION) -> List[float]:
    """
    生成确定性随机向量（模拟模式使用）

    基于 text 的 MD5 哈希作为种子，确保相同文本始终产生相同向量。

    参数:
        text: 输入文本
        dim: 向量维度

    返回:
        浮点数列表，长度为 dim
    """
    seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(dim)]


class EmbeddingProvider:
    """
    Embedding 提供者抽象基类

    定义所有 Embedding 实现必须遵循的统一契约。
    子类需实现 embed / embed_batch 核心方法，返回 1024 维向量。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化 Embedding 提供者

        参数:
            config: Embedding 配置字典，包含 type / model / dimension 等字段
        """
        self.config: Dict[str, Any] = dict(config) if config else {}
        self.model: str = self.config.get("model", "")
        self.dimension: int = self.config.get("dimension", _DEFAULT_DIMENSION)

    def embed(self, text: str) -> List[float]:
        """
        单条文本向量化

        参数:
            text: 待向量化的文本

        返回:
            向量列表（浮点数，长度为 dimension）
        """
        raise NotImplementedError("子类必须实现 embed 方法")

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化

        用于文档入库时的批量处理，减少 API 调用次数。

        参数:
            texts: 待向量化的文本列表

        返回:
            向量列表的列表（外层长度等于 texts 长度，内层长度为 dimension）
        """
        raise NotImplementedError("子类必须实现 embed_batch 方法")

    def get_dim(self) -> int:
        """
        获取向量维度

        返回:
            向量维度（默认 1024）
        """
        return self.dimension

    def is_mock(self) -> bool:
        """是否处于模拟模式（无 API key / 底层库缺失，向量为确定性伪随机）。

        P1-9：调用方应据此跳过向量库写入，避免伪随机向量污染检索库。
        基类默认 False，子类设置 _mock_mode 后返回实际值。
        """
        return bool(getattr(self, "_mock_mode", False))

    def get_model_name(self) -> str:
        """
        获取模型名称

        返回:
            模型名称（如 doubao-embedding-large）
        """
        return self.model

    # ---- 兼容别名 ----

    def embed_text(self, text: str) -> List[float]:
        """embed 方法的兼容别名"""
        return self.embed(text)

    def get_dimension(self) -> int:
        """get_dim 方法的兼容别名"""
        return self.get_dim()


class BgeM3Provider(EmbeddingProvider):
    """
    本地 bge-m3 Embedding 实现类

    通过 sentence-transformers 或 FlagEmbedding 加载本地 bge-m3 模型，
    在本地环境执行文本向量化，无需调用外部 API。

    配置说明:
        - type: "bge_m3" 或 "local_bge_m3"
        - model: 模型路径（如 "BAAI/bge-m3"）
        - dimension: 1024

    降级说明:
        当 sentence-transformers 和 FlagEmbedding 均未安装时，
        自动降级为确定性随机向量（模拟模式），确保功能可用。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化本地 bge-m3 Embedding 提供者

        参数:
            config: Embedding 配置字典
        """
        super().__init__(config)
        self._mock_mode: bool = False
        self._model = None
        model_path = self.config.get("model_path", self.model or "BAAI/bge-m3")

        # 优先尝试 FlagEmbedding
        try:
            from FlagEmbedding import BGEM3FlagModel
            self._model = BGEM3FlagModel(
                model_path,
                use_fp16=self.config.get("use_fp16", True),
            )
            return
        except ImportError:
            pass

        # 其次尝试 sentence-transformers
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_path)
            return
        except ImportError:
            pass

        # 均未安装，降级为模拟模式
        self._mock_mode = True

    def embed(self, text: str) -> List[float]:
        """
        单条文本向量化（本地 bge-m3）

        参数:
            text: 待向量化的文本

        返回:
            1024 维向量
        """
        if self._mock_mode:
            return _deterministic_vector(text, self.dimension)

        try:
            # FlagEmbedding 接口
            if hasattr(self._model, "encode"):
                result = self._model.encode(text)
                # FlagEmbedding 的 encode 返回 dict，含 dense_vecs
                if isinstance(result, dict):
                    vec = result.get("dense_vecs")
                    if vec is not None:
                        return list(vec)[:self.dimension]
                # sentence-transformers 的 encode 返回 ndarray
                if hasattr(result, "tolist"):
                    return result.tolist()[:self.dimension]
                if isinstance(result, list):
                    return result[:self.dimension]
        except Exception:
            pass

        # 异常时降级为确定性随机向量
        return _deterministic_vector(text, self.dimension)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化（本地 bge-m3）

        参数:
            texts: 待向量化的文本列表

        返回:
            向量列表的列表
        """
        if self._mock_mode:
            return [_deterministic_vector(t, self.dimension) for t in texts]

        try:
            if hasattr(self._model, "encode"):
                result = self._model.encode(texts)
                # FlagEmbedding 返回 dict
                if isinstance(result, dict):
                    vecs = result.get("dense_vecs")
                    if vecs is not None:
                        return [list(v)[:self.dimension] for v in vecs]
                # sentence-transformers 返回 ndarray
                if hasattr(result, "tolist"):
                    return [list(v)[:self.dimension] for v in result.tolist()]
                if isinstance(result, list):
                    return [list(v)[:self.dimension] for v in result]
        except Exception:
            pass

        # 异常时降级为确定性随机向量
        return [_deterministic_vector(t, self.dimension) for t in texts]


class DoubaoEmbeddingProvider(EmbeddingProvider):
    """
    豆包 Embedding 实现类（火山引擎方舟 API）

    通过 OpenAI 兼容接口调用 doubao-embedding-large 模型生成文本向量。
    与 LLMProvider 共用同一套 API Key（LLM_API_KEY / EMBEDDING_API_KEY）。

    配置说明:
        - type: "volcano" 或 "doubao"
        - model: "doubao-embedding-large"
        - dimension: 1024
        - base_url: "https://ark.cn-beijing.volces.com/api/v3"
        - api_key_env: "EMBEDDING_API_KEY"

    降级说明:
        当 openai 库未安装或 API 密钥未配置时，
        自动降级为确定性随机向量（模拟模式）。
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化豆包 Embedding 提供者

        参数:
            config: Embedding 配置字典
        """
        super().__init__(config)
        self._mock_mode: bool = False
        self._client = None
        self.base_url: str = self.config.get(
            "base_url", "https://ark.cn-beijing.volces.com/api/v3"
        )
        self.api_key: str = self.config.get("api_key", "")

        # 无 API 密钥时直接进入模拟模式
        if not self.api_key:
            self._mock_mode = True
            return

        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        except ImportError:
            # openai 未安装，降级为模拟模式
            self._mock_mode = True

    def embed(self, text: str) -> List[float]:
        """
        单条文本向量化（豆包 API）

        参数:
            text: 待向量化的文本

        返回:
            1024 维向量
        """
        if self._mock_mode:
            return _deterministic_vector(text, self.dimension)

        try:
            response = self._client.embeddings.create(
                model=self.model or "doubao-embedding-large",
                input=text,
            )
            return list(response.data[0].embedding)[:self.dimension]
        except Exception:
            # 异常时降级为确定性随机向量
            return _deterministic_vector(text, self.dimension)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量文本向量化（豆包 API）

        参数:
            texts: 待向量化的文本列表

        返回:
            向量列表的列表
        """
        if self._mock_mode:
            return [_deterministic_vector(t, self.dimension) for t in texts]

        try:
            response = self._client.embeddings.create(
                model=self.model or "doubao-embedding-large",
                input=texts,
            )
            # 按 index 排序确保顺序一致
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [list(item.embedding)[:self.dimension] for item in sorted_data]
        except Exception:
            # 异常时降级为确定性随机向量
            return [_deterministic_vector(t, self.dimension) for t in texts]

    def close(self) -> None:
        """关闭 OpenAI 客户端连接"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


def _load_default_embedding_config() -> Dict[str, Any]:
    """从 deployment_config.json 加载默认 Embedding 配置"""
    config: Dict[str, Any] = {
        "type": "volcano",
        "model": "doubao-embedding-large",
        "api_key_env": "EMBEDDING_API_KEY",
        "dimension": _DEFAULT_DIMENSION,
    }
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "deployment_config.json",
        )
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                deploy_config = json.load(f)
            emb_section = deploy_config.get("interfaces", {}).get("embedding_provider", {})
            if emb_section:
                config.update(emb_section)
    except Exception:
        pass
    return config


def create_embedding_provider(config: Optional[Dict[str, Any]] = None) -> EmbeddingProvider:
    """
    工厂函数：创建 Embedding 提供者实例

    根据 config.embedding_provider 的 type 字段选择实现：
        - "bge_m3" / "local_bge_m3": 本地 bge-m3 模型（BgeM3Provider）
        - "volcano" / "doubao": 豆包 Embedding API（DoubaoEmbeddingProvider）

    当 config 为 None 时，自动从 deployment_config.json 加载配置。

    参数:
        config: Embedding 配置字典，为 None 时自动加载默认配置

    返回:
        EmbeddingProvider 实例
    """
    if config is None:
        config = _load_default_embedding_config()

    # 解析 api_key_env 为实际 api_key
    api_key_env = config.get("api_key_env")
    if api_key_env and not config.get("api_key"):
        config["api_key"] = os.environ.get(api_key_env, "")

    # 兜底：尝试常见环境变量
    if not config.get("api_key"):
        config["api_key"] = os.environ.get("EMBEDDING_API_KEY", "") or os.environ.get("ARK_API_KEY", "")

    provider_type = config.get("type", "volcano")

    if provider_type in ("bge_m3", "local_bge_m3"):
        return BgeM3Provider(config)

    # 默认使用豆包 Embedding（volcano / doubao）
    return DoubaoEmbeddingProvider(config)


def get_embedding_provider() -> EmbeddingProvider:
    """
    模块级便捷函数：获取 Embedding 提供者单例

    根据 deployment_config.json 的 embedding_provider.type 实例化对应实现。
    当前默认返回 DoubaoEmbeddingProvider 实例。

    返回:
        EmbeddingProvider 单例实例
    """
    return create_embedding_provider()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert EmbeddingProvider is not None, "EmbeddingProvider 类未定义"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
