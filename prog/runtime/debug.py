"""
Debug 中央控制模块
==================
文件用途：
    统一管理框架的 DEBUG 开关，控制各模块自检代码的执行。
    发行版通过环境变量 RUNTIME_DEBUG=0（或不设置）关闭所有自检代码。

使用方法：
    1. 各模块在文件末尾添加 _self_test() 函数，验证基座正确性
    2. 通过 `from prog.runtime.debug import DEBUG` 引入开关
    3. `if DEBUG: _self_test()` 控制自检执行
    4. pytest 测试中通过 set_debug(True) 临时开启

发行版配置：
    - 环境变量 RUNTIME_DEBUG 未设置或为 "0"：DEBUG=False，所有自检代码跳过
    - 环境变量 RUNTIME_DEBUG=1：DEBUG=True，模块导入时执行自检
    - 生产部署时不设置 RUNTIME_DEBUG

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - DEBUG 全局开关（环境变量 RUNTIME_DEBUG=1 开启）+ set_debug() 运行时切换 + hello_world() 自检输出，供框架模块与使用方自检代码使用（SPEC §5.7 DEBUG 开关，工程基础设施非业务规格内容）
    对外接口（方法/API）：
        - DEBUG：全局开关（模块导入时由环境变量 RUNTIME_DEBUG 确定，运行中可经 set_debug() 修改）（SPEC §5.7）
        - set_debug(value)：运行时修改 DEBUG 开关（供 pytest 临时开启）（SPEC §5.7）
        - hello_world(module_name, info="")：打印模块自检通过信息（SPEC §5.7）
    错误处理要求：
        - 生产部署不设置 RUNTIME_DEBUG：DEBUG=False，所有自检代码跳过（SPEC §5.7）
"""

import os
import sys


def _get_debug_flag() -> bool:
    """从环境变量读取 DEBUG 开关

    Returns:
        bool: True=调试模式（执行自检），False=发行模式（跳过自检）
    """
    return os.environ.get('RUNTIME_DEBUG', '0') == '1'


# 全局 DEBUG 开关（模块导入时确定，运行中可通过 set_debug() 修改）
DEBUG: bool = _get_debug_flag()


def set_debug(value: bool) -> None:
    """运行时修改 DEBUG 开关（供 pytest 使用）

    Args:
        value: True=开启自检，False=关闭自检
    """
    global DEBUG
    DEBUG = value


def hello_world(module_name: str, info: str = "") -> None:
    """打印模块自检通过信息

    Args:
        module_name: 模块名称（通常传 __name__）
        info: 额外信息（如验证的类名/函数名）
    """
    suffix = f" - {info}" if info else ""
    print(f"[OK] {module_name} 基座验证通过{suffix}")


def self_test():
    """debug 模块自身自检"""
    hello_world(__name__, f"DEBUG={DEBUG}, Python={sys.version.split()[0]}")
