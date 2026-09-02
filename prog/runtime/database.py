"""
数据库层（可选外部依赖注入点）
==============================
框架不强依赖数据库。业务侧通过 `set_database()` 注册一个提供
`query_one / query_many / insert / update` 接口的数据库对象，或直接
覆写 `get_database()` 返回该对象（鸭子类型）。

未注册时 `get_database()` 返回 None，框架组件（审核链归档 / 规则配置
加载等）自动降级：审核链使用内存链存储、规则引擎返回默认配置。

对应 SPEC.md §2.3「数据库层」注入说明。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 数据库层注入点（鸭子类型）：业务侧通过 set_database() 注册提供 query_one/query_many/insert/update 接口的数据库对象（SPEC §2.3 与外部系统的边界）
        - 框架不强依赖数据库，未注入时自动降级：审核链降级内存链存储、流程引擎降级内存实例、规则/权限/SOD/模块开关使用内置默认配置（SPEC §2.3）
        - 组装期显式注册（v6.78.2 风险1收敛）：run_server 获取 DatabaseManager 实例后显式 set_database(database) 注册框架注入点，消除预热探测时序耦合（CHANGELOG v39）
    对外接口（方法/API）：
        - set_database(db)：注册数据库对象（鸭子类型：提供 query_one/query_many/insert/update）（SPEC §2.3）
        - get_database() -> Any：获取注册的数据库对象，未注册时返回 None（框架组件自动降级）（SPEC §2.3）
    错误处理要求：
        - 未注册数据库：get_database() 返回 None，框架组件自动降级运行（审核链内存链存储、规则引擎返回默认配置）（SPEC §2.3）
"""
from typing import Any

_database: Any = None


def set_database(db: Any) -> None:
    """注册数据库对象（鸭子类型：提供 query_one/query_many/insert/update）"""
    global _database
    _database = db


def get_database() -> Any:
    """获取注册的数据库对象，未注册时返回 None（框架组件自动降级）"""
    return _database


__all__ = ["set_database", "get_database"]
