"""
配置加载器 - AI工厂管家

文件用途：
    实现技术规格 §A.0 定义的三层变量加载机制，统一管理系统所有运行时配置。
    替代 demo 中散落在各模块的硬编码配置读取逻辑。

对应技术规格章节：
    §1.8.8 统一部署配置文件（deployment_config.json）
    §A.0 三层变量加载机制

三层变量加载机制（优先级由低到高）：
    1. 系统默认值（DEFAULT_CONFIG）：代码内置的安全默认值，保证无配置也能启动
    2. deployment_config.json：项目根目录的部署配置文件，定义部署模式与各接口层参数
    3. 环境变量（最高优先级）：覆盖上述两层中同名的配置，用于注入敏感信息（密钥、密码、主机等）

设计要点：
    - 部署模式检测：通过 deployment_config.json 的 deployment_mode 字段（local / volcano）
    - 环境变量解析：所有以 _env 后缀命名的字段（如 api_key_env: "LLM_API_KEY"）
      表示从同名环境变量读取实际值，加载时替换为环境变量的值
    - 配置缓存：首次加载后缓存完整配置树，避免重复文件 IO
    - 单例模式：全系统共享同一个 ConfigLoader 实例

替代 demo 文件/函数：
    替代 demo 中 llm_config.json 的直接读取，以及各模块 os.environ.get 的散落调用
"""

import os
import json
import warnings
from typing import Any, Dict, Optional


class ConfigLoader:
    """
    配置加载器单例类

    负责按三层优先级合并配置，并提供按接口名查询的便捷方法。

    属性:
        _config_path: deployment_config.json 文件的绝对路径
        _config: 加载并合并后的完整配置树（缓存）
        _loaded: 是否已加载标记

    设计说明:
        - 单例通过类变量实现，全系统共享同一实例
        - 首次调用 load_config() 时执行实际加载，后续返回缓存
        - 环境变量解析发生在合并阶段，_env 字段被替换为实际值
    """

    # 类级单例实例
    _instance: Optional["ConfigLoader"] = None

    # 类级默认配置（第一层：系统默认值）
    # 仅包含保证系统无配置也能启动的安全默认值
    DEFAULT_CONFIG: Dict[str, Any] = {
        "deployment_mode": "local",
        "interfaces": {},
        "agent_matrix": {},
        "audit_engine": {"enabled": False, "layers": []},
    }

    def __init__(self, config_path: Optional[str] = None) -> None:
        """
        初始化配置加载器

        参数:
            config_path: deployment_config.json 的路径
                         未指定时默认为当前文件同级目录上层的 deployment_config.json
        """
        self._config_path = config_path or self._default_config_path()
        self._config: Dict[str, Any] = {}
        self._loaded: bool = False

    @classmethod
    def get_instance(cls) -> "ConfigLoader":
        """
        获取单例实例

        返回:
            ConfigLoader 单例，若不存在则创建
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def _default_config_path() -> str:
        """
        计算默认配置文件路径

        返回:
            deployment_config.json 的绝对路径（位于 prog 根目录）
        """
        # prog/config/config_loader.py -> prog/deployment_config.json
        current_dir = os.path.dirname(os.path.abspath(__file__))
        prog_root = os.path.dirname(current_dir)
        return os.path.join(prog_root, "deployment_config.json")

    def load_config(self, force: bool = False) -> Dict[str, Any]:
        """
        加载并合并配置（三层变量加载机制）

        加载顺序（后者覆盖前者）：
            0. .env 文件（prog/.env，仅补充未设置的环境变量）
            1. DEFAULT_CONFIG（系统默认值）
            2. deployment_config.json 文件内容
            3. 环境变量（解析所有 _env 后缀字段）

        参数:
            force: 是否强制重新加载（忽略缓存）

        返回:
            合并后的完整配置树
        """
        if self._loaded and not force:
            return self._config

        # 第零层：加载 .env 文件（deploy-dev-windows.ps1 / 交互引导写入的配置，
        # 仅补充未设置的环境变量，不覆盖系统环境变量）
        loaded_env = self._load_env_file()
        if loaded_env:
            warnings.warn(f"已从 .env 文件加载 {loaded_env} 项配置")

        # 第一层：系统默认值
        config = self._merge_config({}, self.DEFAULT_CONFIG)
        # 第二层：部署配置文件
        file_config = self._load_file_config()
        config = self._merge_config(config, file_config)
        # 第三层：环境变量（解析所有 _env 后缀字段）
        config = self._resolve_env_vars(config)

        self._config = config
        self._loaded = True
        return config

    @staticmethod
    def _default_env_path() -> str:
        """计算 .env 文件路径（位于 prog 根目录，与 deployment_config.json 同级）"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(os.path.dirname(current_dir), ".env")

    def _load_env_file(self, path: Optional[str] = None) -> int:
        """
        简易 .env 解析：KEY=VALUE，跳过注释/空行，仅补充未设置的环境变量。

        不依赖 python-dotenv（发行环境未强制安装，避免额外依赖）。
        已存在的系统环境变量优先，不被 .env 覆盖。

        参数:
            path: .env 文件路径，未指定时默认为 prog/.env

        返回:
            实际生效（写入 os.environ）的条目数
        """
        path = path or self._default_env_path()
        if not os.path.exists(path):
            return 0
        loaded = 0
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
                loaded += 1
        return loaded

    def _load_file_config(self) -> Dict[str, Any]:
        """
        从 deployment_config.json 读取配置

        返回:
            文件中的配置字典；文件不存在时返回空字典
        """
        if not os.path.exists(self._config_path):
            return {}
        with open(self._config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _merge_config(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """
        深度合并两个配置字典

        参数:
            base: 基础配置（低优先级）
            override: 覆盖配置（高优先级）

        返回:
            合并后的新字典（不修改入参）
        """
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                # 两个值均为字典时递归合并
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def _resolve_env_vars(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析所有 _env 后缀字段，从环境变量读取实际值

        规则：
            - 遇到 key 形如 "xxx_env" 时，读取环境变量 config[key] 的值
            - 例如 {"api_key_env": "LLM_API_KEY"} 解析为 {"api_key": os.environ["LLM_API_KEY"]}
            - 环境变量未设置时保留为 None 并记录警告

        参数:
            config: 待解析的配置字典

        返回:
            解析后的配置字典（_env 字段被替换为实际值）
        """
        result: Dict[str, Any] = {}
        for key, value in config.items():
            if isinstance(value, dict):
                # 递归解析嵌套字典
                result[key] = self._resolve_env_vars(value)
            elif isinstance(key, str) and key.endswith("_env") and isinstance(value, str):
                # 将 xxx_env: "ENV_NAME" 替换为 xxx: <环境变量值>
                actual_key = key[:-4]  # 去掉 _env 后缀
                env_value = os.environ.get(value)
                result[actual_key] = env_value
                if env_value is None:
                    warnings.warn(f"环境变量 {value} 未设置（配置键 {actual_key}）")
            else:
                result[key] = value
        return result

    def get_interface_config(self, interface_name: str) -> Dict[str, Any]:
        """
        获取指定接口层的配置

        根据 deployment_mode 自动选择 local 或 volcano 子配置并合并通用配置。

        参数:
            interface_name: 接口层名称
                            可选值：llm_provider / database / vector_store /
                                    file_storage / event_bus / embedding_provider

        返回:
            该接口层在当前部署模式下的完整配置字典
        """
        config = self.load_config()
        interfaces = config.get("interfaces", {})
        iface = interfaces.get(interface_name, {})

        # 对于含 local/volcano 子键的接口，按部署模式选择并合并通用配置
        mode = self.get_deployment_mode()
        if isinstance(iface, dict) and mode in iface:
            # 提取不属于 local/volcano 的通用配置项
            common = {
                k: v for k, v in iface.items() if k not in ("local", "volcano")
            }
            return self._merge_config(common, iface[mode])

        return iface

    def get_deployment_mode(self) -> str:
        """
        获取当前部署模式

        返回:
            "local" 或 "volcano"
        """
        config = self.load_config()
        return config.get("deployment_mode", "local")

    def get_agent_config(self, agent_name: str) -> Dict[str, Any]:
        """
        获取指定 Agent 的配置

        参数:
            agent_name: Agent 名称（如 sales_agent / production_agent 等）

        返回:
            Agent 配置字典（含 enabled / priority 等字段）
        """
        config = self.load_config()
        return config.get("agent_matrix", {}).get(agent_name, {})

    def get_audit_config(self) -> Dict[str, Any]:
        """
        获取审核引擎配置

        返回:
            审核引擎配置字典（含 enabled / layers 列表）
        """
        config = self.load_config()
        return config.get("audit_engine", {"enabled": False, "layers": []})


def get_config_loader() -> ConfigLoader:
    """
    模块级便捷函数：获取配置加载器单例

    返回:
        ConfigLoader 单例实例
    """
    return ConfigLoader.get_instance()
