"""
FileStorage 文件存储模块
========================

文件用途：
    定义文件存储访问的统一接口层，使用 S3 协议实现本地 MinIO 与云端 S3
    兼容存储（TOS/OSS 等）的统一抽象。
    用于知识库文档、训练数据、附件等文件的存储与分发。

设计说明：
    1. 抽象基类 FileStorageBase 定义统一契约
    2. S3Storage 为默认实现，通过 boto3 Python SDK 访问 S3 兼容存储
    3. 未安装 boto3 或连接失败时降级为本地文件系统模拟
       （默认根目录 <temp>/runtime_storage/<bucket>/，可通过 config.local_path 配置）
    4. 支持预签名 URL（默认过期 3600 秒）
    5. 防止路径穿越：本地模式校验对象路径不得越出存储根目录

配置示例:
    {
        "endpoint": "127.0.0.1:9000",
        "bucket": "my-bucket",
        "secure": false,
        "access_key": "...",
        "secret_key": "..."
    }

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - FileStorageBase 抽象基类 + S3Storage 默认实现（boto3 可选导入，兼容 MinIO/TOS/OSS 等 S3 兼容存储），用于知识库文档、训练数据、附件等文件存储与分发（SPEC §5.5 文件存储，来源映射 §1.8.4 FileStorage 统一接口层）
        - 未装 boto3 或连接失败时降级本地文件系统模拟（默认 <temp>/runtime_storage/<bucket>/，config.local_path 可配置；含路径穿越防护）（SPEC §5.5）
        - upload/download/delete/exists/list_objects（含分页）（SPEC §5.5）
        - get_presigned_url(expires=3600)：预签名 URL（本地模式返回 file:// 协议 URL）（SPEC §5.5）
    对外接口（方法/API）：
        - FileStorageBase.upload(object_name, data, length, content_type=None, metadata=None) -> str：上传文件（SPEC §5.5）
        - FileStorageBase.download(object_name) -> bytes：下载文件（SPEC §5.5）
        - FileStorageBase.get_presigned_url(object_name, expires=3600, method="GET") -> str：预签名 URL（SPEC §5.5）
        - FileStorageBase.delete(object_name) -> bool / exists(object_name) -> bool：删除/存在性检查（SPEC §5.5）
        - FileStorageBase.list_objects(prefix=None, recursive=True) -> list：列举对象（含分页）（SPEC §5.5）
        - S3Storage.get_instance(config=None) -> S3Storage：单例（SPEC §5.5）
    错误处理要求：
        - boto3 未安装或连接失败：降级本地文件系统模拟（SPEC §5.5）
        - 路径穿越防护：本地模式校验对象路径不得越出存储根目录，非法路径拒绝（SPEC §5.5）
"""

import os
import tempfile
import warnings
from typing import BinaryIO, Optional


class FileStorageBase:
    """
    文件存储抽象基类

    定义所有文件存储实现必须遵循的统一契约。
    """

    def __init__(self, config: dict) -> None:
        """
        初始化文件存储

        参数:
            config: 文件存储配置字典，包含 endpoint / bucket / secure 等字段
        """
        raise NotImplementedError

    def upload(
        self,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        上传文件

        参数:
            object_name: 对象存储路径（如 "docs/doc_001.pdf"）
            data: 二进制文件流
            length: 文件字节数
            content_type: MIME 类型（如 "application/pdf"）
            metadata: 自定义元数据字典

        返回:
            上传对象的完整路径
        """
        raise NotImplementedError

    def download(self, object_name: str) -> bytes:
        """
        下载文件

        参数:
            object_name: 对象存储路径

        返回:
            文件二进制内容
        """
        raise NotImplementedError

    def get_presigned_url(
        self,
        object_name: str,
        expires: int = 3600,
        method: str = "GET",
    ) -> str:
        """
        获取预签名 URL

        用于生成临时可分享的文件访问链接，无需暴露存储凭证。

        参数:
            object_name: 对象存储路径
            expires: 过期时间（秒，默认 3600）
            method: HTTP 方法（GET 下载 / PUT 上传）

        返回:
            预签名 URL 字符串
        """
        raise NotImplementedError

    def delete(self, object_name: str) -> bool:
        """
        删除文件

        参数:
            object_name: 对象存储路径

        返回:
            True 表示删除成功
        """
        raise NotImplementedError

    def exists(self, object_name: str) -> bool:
        """
        检查文件是否存在

        参数:
            object_name: 对象存储路径

        返回:
            True 表示文件存在
        """
        raise NotImplementedError

    def list_objects(
        self,
        prefix: Optional[str] = None,
        recursive: bool = True,
    ) -> list:
        """
        列举对象

        参数:
            prefix: 路径前缀过滤
            recursive: 是否递归列举子目录

        返回:
            对象信息字典列表
        """
        raise NotImplementedError


class S3Storage(FileStorageBase):
    """
    S3 协议文件存储实现

    通过 boto3 SDK 访问 S3 兼容存储服务（MinIO / TOS / OSS 等）。

    降级模式：
        当 boto3 未安装时，自动降级为本地文件系统模拟，
        将文件存储在 <temp>/runtime_storage/<bucket>/ 中。
    """

    # 类级单例实例
    _instance: Optional["S3Storage"] = None

    def __init__(self, config: dict) -> None:
        """
        初始化 S3 文件存储

        参数:
            config: 已解析环境变量的文件存储配置字典，包含：
                    - endpoint: S3 端点
                    - access_key: 访问密钥
                    - secret_key: 秘密密钥
                    - bucket: Bucket 名称（默认 "runtime"）
                    - secure: 是否启用 HTTPS
                    - local_path: 本地降级模式根目录（可选）
        """
        self._config = config or {}
        self._bucket = self._config.get("bucket", "runtime")
        self._secure = self._config.get("secure", False)
        endpoint = self._config.get("endpoint", "127.0.0.1:9000")

        # 读取访问密钥：优先从配置读取，其次从环境变量读取
        self._access_key = (
            self._config.get("access_key")
            or os.environ.get("S3_ACCESS_KEY")
            or os.environ.get("MINIO_ROOT_USER")
            or ""
        )
        self._secret_key = (
            self._config.get("secret_key")
            or os.environ.get("S3_SECRET_KEY")
            or os.environ.get("MINIO_ROOT_PASSWORD")
            or ""
        )

        # 尝试导入 boto3，未安装时降级为本地文件系统模拟
        try:
            import boto3
            from botocore.exceptions import ClientError

            self._boto3 = boto3
            self._ClientError = ClientError
            # 构建 endpoint_url，补全协议前缀
            if endpoint.startswith("http://") or endpoint.startswith("https://"):
                endpoint_url = endpoint
            else:
                endpoint_url = ("https://" if self._secure else "http://") + endpoint
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
            )
            self._mode = "s3"
            # 检查并创建 Bucket（服务不可达时仅告警，不阻断初始化）
            self._ensure_bucket()
        except ImportError:
            # boto3 未安装，降级为本地文件系统模拟
            self._boto3 = None
            self._client = None
            self._ClientError = None
            self._mode = "local"
            self._local_base = self._config.get("local_path") or os.path.join(
                tempfile.gettempdir(), "runtime_storage"
            )
            self._local_root = os.path.join(self._local_base, self._bucket)
            os.makedirs(self._local_root, exist_ok=True)
            warnings.warn(
                "boto3 未安装，FileStorage 降级为本地文件系统模拟模式"
            )

    @classmethod
    def get_instance(cls, config: Optional[dict] = None) -> "S3Storage":
        """
        获取单例实例

        参数:
            config: 配置字典（仅在首次初始化时需要，None 时使用默认配置）

        返回:
            S3Storage 单例
        """
        if cls._instance is None:
            with cls._instance_lock:
                # double-checked locking：锁内二次校验，避免并发首次调用重复创建
                if cls._instance is None:
                    cls._instance = cls(config or {})
        return cls._instance

    def _ensure_bucket(self) -> None:
        """
        检查 Bucket 是否存在，不存在则自动创建

        仅在 S3 模式下执行；服务不可达时仅告警，不抛出异常。
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except self._ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                self._client.create_bucket(Bucket=self._bucket)
            else:
                warnings.warn(f"Bucket 检查失败，跳过自动创建: {e}")
        except Exception as e:
            warnings.warn(f"文件存储服务不可达，跳过 Bucket 初始化: {e}")

    def _local_path(self, object_name: str) -> str:
        """
        计算本地模式下对象的完整文件路径

        参数:
            object_name: 对象存储路径

        返回:
            本地文件系统完整路径
        """
        # 统一路径分隔符
        safe_name = object_name.replace("\\", "/")
        path = os.path.join(self._local_root, safe_name)
        # 防止路径穿越攻击
        if not os.path.abspath(path).startswith(os.path.abspath(self._local_root)):
            raise ValueError(f"非法的对象路径: {object_name}")
        return path

    @staticmethod
    def _read_body(data) -> bytes:
        """
        从二进制流或字节对象中读取内容

        参数:
            data: 二进制文件流或字节对象

        返回:
            字节内容
        """
        if hasattr(data, "read"):
            return data.read()
        return data

    def upload(
        self,
        object_name: str,
        data: BinaryIO,
        length: int,
        content_type: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """
        上传文件

        参数:
            object_name: 对象存储路径
            data: 二进制文件流
            length: 文件字节数
            content_type: MIME 类型（如 "application/pdf"）
            metadata: 自定义元数据字典

        返回:
            上传对象的完整路径
        """
        if self._mode == "s3":
            body = self._read_body(data)
            args = {
                "Bucket": self._bucket,
                "Key": object_name,
                "Body": body,
            }
            if content_type:
                args["ContentType"] = content_type
            if metadata:
                args["Metadata"] = metadata
            self._client.put_object(**args)
        else:
            # 本地文件系统模拟
            path = self._local_path(object_name)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            body = self._read_body(data)
            with open(path, "wb") as f:
                f.write(body)
        return object_name

    def download(self, object_name: str) -> bytes:
        """
        下载文件

        参数:
            object_name: 对象存储路径

        返回:
            文件二进制内容
        """
        if self._mode == "s3":
            response = self._client.get_object(
                Bucket=self._bucket, Key=object_name
            )
            return response["Body"].read()
        else:
            # 本地文件系统模拟
            path = self._local_path(object_name)
            if not os.path.exists(path):
                raise FileNotFoundError(f"对象不存在: {object_name}")
            with open(path, "rb") as f:
                return f.read()

    def get_presigned_url(
        self,
        object_name: str,
        expires: int = 3600,
        method: str = "GET",
    ) -> str:
        """
        获取预签名 URL

        用于生成临时可分享的文件访问链接，无需暴露存储凭证。

        参数:
            object_name: 对象存储路径
            expires: 过期时间（秒，默认 3600）
            method: HTTP 方法（GET 下载 / PUT 上传）

        返回:
            预签名 URL 字符串
        """
        if self._mode == "s3":
            method_map = {"GET": "get_object", "PUT": "put_object"}
            operation = method_map.get(method.upper(), "get_object")
            return self._client.generate_presigned_url(
                operation,
                Params={"Bucket": self._bucket, "Key": object_name},
                ExpiresIn=expires,
            )
        else:
            # 本地模式返回 file:// 协议 URL
            path = self._local_path(object_name)
            return f"file://{os.path.abspath(path)}"

    def delete(self, object_name: str) -> bool:
        """
        删除文件

        参数:
            object_name: 对象存储路径

        返回:
            True 表示删除成功
        """
        if self._mode == "s3":
            self._client.delete_object(Bucket=self._bucket, Key=object_name)
            return True
        else:
            # 本地文件系统模拟
            path = self._local_path(object_name)
            if os.path.exists(path):
                os.remove(path)
                return True
            return False

    def exists(self, object_name: str) -> bool:
        """
        检查文件是否存在

        参数:
            object_name: 对象存储路径

        返回:
            True 表示文件存在
        """
        if self._mode == "s3":
            try:
                self._client.head_object(
                    Bucket=self._bucket, Key=object_name
                )
                return True
            except self._ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    return False
                raise
        else:
            # 本地文件系统模拟
            path = self._local_path(object_name)
            return os.path.exists(path)

    def list_objects(
        self,
        prefix: Optional[str] = None,
        recursive: bool = True,
    ) -> list:
        """
        列举对象

        参数:
            prefix: 路径前缀过滤
            recursive: 是否递归列举子目录

        返回:
            对象信息字典列表，每项含 name / size / last_modified 字段
        """
        if self._mode == "s3":
            results = []
            kwargs = {"Bucket": self._bucket}
            if prefix:
                kwargs["Prefix"] = prefix
            if not recursive:
                kwargs["Delimiter"] = "/"
            while True:
                response = self._client.list_objects_v2(**kwargs)
                for obj in response.get("Contents", []):
                    results.append(
                        {
                            "name": obj["Key"],
                            "size": obj["Size"],
                            "last_modified": obj.get("LastModified"),
                        }
                    )
                # 处理分页
                if response.get("IsTruncated"):
                    kwargs["ContinuationToken"] = response.get(
                        "NextContinuationToken"
                    )
                else:
                    break
            return results
        else:
            # 本地文件系统模拟
            results = []
            for root, dirs, files in os.walk(self._local_root):
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(
                        full_path, self._local_root
                    ).replace("\\", "/")
                    if prefix is None or rel_path.startswith(prefix):
                        results.append(
                            {
                                "name": rel_path,
                                "size": os.path.getsize(full_path),
                                "last_modified": os.path.getmtime(full_path),
                            }
                        )
                if not recursive:
                    # 非递归模式仅处理顶层目录
                    dirs.clear()
            return results


def get_file_storage() -> S3Storage:
    """
    模块级便捷函数：获取文件存储单例

    返回:
        S3Storage 单例实例
    """
    return S3Storage.get_instance()
