"""
文件存储（业务软件层 re-export + 部署逻辑）
============================================
框架能力：FileStorageBase 抽象基类 + S3Storage 默认实现（S3 协议兼容
MinIO/TOS/OSS，boto3 未装自动降级本地文件系统，含路径穿越防护）由AI工厂管家框架运行时
（prog/runtime）提供。
业务侧保留：get_file_storage() 从统一配置加载器读取 file_storage 接口配置。
"""
from prog.runtime.file_storage import FileStorageBase, S3Storage


def get_file_storage() -> S3Storage:
    """
    模块级便捷函数：获取文件存储单例（从统一配置加载器读取接口配置）

    返回:
        S3Storage 单例实例
    """
    from prog.config.config_loader import get_config_loader
    return S3Storage.get_instance(
        get_config_loader().get_interface_config("file_storage"))


__all__ = ["FileStorageBase", "S3Storage", "get_file_storage"]
