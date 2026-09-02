"""
DEBUG 开关（业务软件层 re-export）
==================================
框架能力：DEBUG 全局开关（环境变量 RUNTIME_DEBUG=1 开启）+ set_debug() 运行时
切换 + hello_world()/self_test() 自检输出由AI工厂管家框架运行时（prog/runtime）提供。
本文件仅作 re-export。
"""
from prog.runtime.debug import DEBUG, set_debug, hello_world, self_test

__all__ = ["DEBUG", "set_debug", "hello_world", "self_test"]
