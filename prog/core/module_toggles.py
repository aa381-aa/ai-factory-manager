"""
模块开关（业务软件层 re-export）
================================
框架能力：ModuleToggleManager 模块开关单例 + @require_module 装饰器（模块关闭时
自动降级返回统一响应）与 DEFAULT_TOGGLES 默认清单由AI工厂管家框架运行时（prog/runtime）提供。
业务侧各模块关闭时的降级策略见 §4.3.2。
本文件仅作 re-export。
"""
from prog.runtime.module_toggles import ModuleToggleManager, require_module, DEFAULT_TOGGLES

__all__ = ["ModuleToggleManager", "require_module", "DEFAULT_TOGGLES"]
