"""
配置校验器 - AI工厂管家

文件用途：
    对系统所有运行时配置进行完整性校验，在启动阶段提前发现配置缺失或错误，
    避免运行时因配置问题导致不可预期的故障。

对应技术规格章节：
    §A.0 三层变量加载机制（配置校验）
    §1.8.8 统一部署配置文件（deployment_config.json）

设计说明：
    1. ConfigValidator 接受 ConfigLoader 实例，从中读取各接口层配置进行校验
    2. 支持按类别校验（database / llm / redis / milvus / minio）
    3. validate_all() 一次性校验全部配置，返回 {category: [errors]} 结构
    4. check_env_vars() 检查必需环境变量是否设置
    5. get_validation_report() 生成人类可读的校验报告，便于启动日志输出
    6. 部署模式校验支持 local / volcano / hybrid 三种模式

使用示例:
    validator = ConfigValidator()
    report = validator.get_validation_report()
    print(report)

    errors = validator.validate_all()
    if errors:
        # 配置存在问题，按类别处理
        for category, msgs in errors.items():
            ...

校验规则：
    - database: host / port / database / user / password 非空
    - llm: api_key / base_url / model 非空
    - redis: host / port 非空
    - milvus: host / port / collection 非空
    - minio: endpoint / bucket 非空
    - 部署模式: 必须为 local / volcano / hybrid 之一
"""

import os
from typing import Dict, List, Optional

from prog.config.config_loader import ConfigLoader
from prog.core.debug import DEBUG


class ConfigValidator:
    """
    配置校验器

    对系统各接口层配置进行完整性校验，支持按类别校验与全量校验。

    属性:
        _config_loader: ConfigLoader 实例，提供配置读取能力

    设计说明:
        - 校验方法返回错误列表（空列表表示校验通过）
        - validate_all() 汇总所有类别校验结果
        - 不抛出异常，所有问题以错误消息形式返回
    """

    # 合法的部署模式
    VALID_DEPLOYMENT_MODES = ("local", "volcano", "hybrid")

    def __init__(self, config_loader: Optional[ConfigLoader] = None) -> None:
        """
        初始化配置校验器

        参数:
            config_loader: ConfigLoader 实例，未指定时使用全局单例
        """
        if config_loader is None:
            config_loader = ConfigLoader.get_instance()
        self._config_loader = config_loader

    # ------------------------------------------------------------------
    # 全量校验
    # ------------------------------------------------------------------

    def validate_all(self) -> Dict[str, List[str]]:
        """
        校验所有配置

        逐一校验部署模式及各接口层配置，汇总错误信息。

        返回:
            {category: [error_messages]} 字典，仅包含有错误的类别
            （空字典表示全部校验通过）
        """
        results: Dict[str, List[str]] = {}

        # 校验部署模式
        mode = self._config_loader.get_deployment_mode()
        if not self.validate_deployment_mode(mode):
            results["deployment_mode"] = [f"无效的部署模式: {mode}（合法值: local/volcano/hybrid）"]

        # 校验数据库配置
        db_config = self._config_loader.get_interface_config("database")
        errors = self.validate_database(db_config)
        if errors:
            results["database"] = errors

        # 校验 LLM 配置
        llm_config = self._config_loader.get_interface_config("llm_provider")
        errors = self.validate_llm(llm_config)
        if errors:
            results["llm"] = errors

        # 校验 Redis 配置（event_bus 共享 Redis 基础设施）
        redis_config = self._config_loader.get_interface_config("event_bus")
        errors = self.validate_redis(redis_config)
        if errors:
            results["redis"] = errors

        # 校验 Milvus 配置
        milvus_config = self._config_loader.get_interface_config("vector_store")
        errors = self.validate_milvus(milvus_config)
        if errors:
            results["milvus"] = errors

        # 校验 MinIO 配置（file_storage）
        minio_config = self._config_loader.get_interface_config("file_storage")
        errors = self.validate_minio(minio_config)
        if errors:
            results["minio"] = errors

        return results

    # ------------------------------------------------------------------
    # 分类校验方法
    # ------------------------------------------------------------------

    def validate_database(self, config: dict) -> List[str]:
        """
        校验数据库配置

        校验 host / port / database / user / password 字段非空。

        参数:
            config: 数据库配置字典

        返回:
            错误消息列表（空列表表示校验通过）
        """
        errors: List[str] = []
        required_fields = ["host", "port", "database", "user", "password"]
        for field in required_fields:
            value = config.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"数据库配置缺失必填字段: {field}")
        return errors

    def validate_llm(self, config: dict) -> List[str]:
        """
        校验 LLM 配置

        校验 api_key / base_url / model 字段非空。
        LLM 配置结构为 {"type": "...", "config": {...}}，实际参数在内层 config 字典中。

        参数:
            config: LLM 接口配置字典

        返回:
            错误消息列表（空列表表示校验通过）
        """
        errors: List[str] = []
        # LLM 配置的参数在 "config" 子字典中；若未嵌套则直接使用传入字典
        if isinstance(config, dict) and "config" in config and isinstance(config["config"], dict):
            llm_config = config["config"]
        else:
            llm_config = config if isinstance(config, dict) else {}

        required_fields = ["api_key", "base_url", "model"]
        for field in required_fields:
            value = llm_config.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"LLM配置缺失必填字段: {field}")
        return errors

    def validate_redis(self, config: dict) -> List[str]:
        """
        校验 Redis 配置

        校验 host / port 字段非空。Redis 与 EventBus 共享配置。

        参数:
            config: Redis 配置字典

        返回:
            错误消息列表（空列表表示校验通过）
        """
        errors: List[str] = []
        required_fields = ["host", "port"]
        for field in required_fields:
            value = config.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"Redis配置缺失必填字段: {field}")
        return errors

    def validate_milvus(self, config: dict) -> List[str]:
        """
        校验 Milvus 配置

        校验 host / port / collection 字段非空。

        参数:
            config: Milvus 配置字典

        返回:
            错误消息列表（空列表表示校验通过）
        """
        errors: List[str] = []
        required_fields = ["host", "port", "collection"]
        for field in required_fields:
            value = config.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"Milvus配置缺失必填字段: {field}")
        return errors

    def validate_minio(self, config: dict) -> List[str]:
        """
        校验 MinIO 配置

        校验 endpoint / bucket 字段非空。
        access_key / secret_key 通常从环境变量读取，这里一并检查。

        参数:
            config: MinIO 文件存储配置字典

        返回:
            错误消息列表（空列表表示校验通过）
        """
        errors: List[str] = []
        required_fields = ["endpoint", "bucket"]
        for field in required_fields:
            value = config.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"MinIO配置缺失必填字段: {field}")

        # 检查访问密钥：优先配置字段，其次环境变量
        access_key = (
            config.get("access_key")
            or os.environ.get("MINIO_ROOT_USER")
            or os.environ.get("TOS_ACCESS_KEY")
        )
        secret_key = (
            config.get("secret_key")
            or os.environ.get("MINIO_ROOT_PASSWORD")
            or os.environ.get("TOS_SECRET_KEY")
        )
        if not access_key:
            errors.append("MinIO配置缺失访问密钥（access_key 或环境变量 MINIO_ROOT_USER/TOS_ACCESS_KEY）")
        if not secret_key:
            errors.append("MinIO配置缺失秘密密钥（secret_key 或环境变量 MINIO_ROOT_PASSWORD/TOS_SECRET_KEY）")

        return errors

    # ------------------------------------------------------------------
    # 部署模式校验
    # ------------------------------------------------------------------

    def validate_deployment_mode(self, mode: str) -> bool:
        """
        校验部署模式

        参数:
            mode: 部署模式字符串

        返回:
            True 表示合法（local / volcano / hybrid），False 表示非法
        """
        return mode in self.VALID_DEPLOYMENT_MODES

    # ------------------------------------------------------------------
    # 环境变量检查
    # ------------------------------------------------------------------

    def check_env_vars(self, required_vars: List[str]) -> Dict[str, bool]:
        """
        检查环境变量是否设置

        参数:
            required_vars: 必需的环境变量名列表

        返回:
            {var_name: is_set} 字典，True 表示已设置，False 表示未设置
        """
        return {var: bool(os.environ.get(var)) for var in required_vars}

    # ------------------------------------------------------------------
    # 校验报告
    # ------------------------------------------------------------------

    def get_validation_report(self) -> str:
        """
        生成可读的校验报告

        调用 validate_all() 获取全量校验结果，格式化为人类可读的文本报告。

        返回:
            校验报告字符串（校验通过时提示全部通过，失败时列出各类别错误）
        """
        results = self.validate_all()
        mode = self._config_loader.get_deployment_mode()

        lines = [
            "=" * 50,
            "配置校验报告",
            f"部署模式: {mode}",
            "=" * 50,
        ]

        if not results:
            lines.append("✓ 所有配置校验通过")
        else:
            total_errors = sum(len(errs) for errs in results.values())
            lines.append(f"✗ 发现 {total_errors} 个配置问题：")
            for category, errors in results.items():
                lines.append(f"\n[{category}]")
                for err in errors:
                    lines.append(f"  - {err}")

        lines.append("=" * 50)
        return "\n".join(lines)


def get_config_validator() -> ConfigValidator:
    """
    模块级便捷函数：获取配置校验器实例

    返回:
        ConfigValidator 实例（使用全局 ConfigLoader 单例）
    """
    return ConfigValidator()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert ConfigValidator is not None, "ConfigValidator 类未定义"
    hello_world(__name__, "核心类定义完整")


if DEBUG:
    _self_test()
