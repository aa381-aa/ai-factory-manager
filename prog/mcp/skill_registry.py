"""
技能注册中心模块

文件用途：
    MCP技能注册中心，统一管理所有技能实例的注册、发现与调用。
    提供按名称获取技能实例、列出已注册技能、统一调用入口。

对应技术规格章节：
    - §1.3.3 MCP技能注册机制
    - §1.3 MCP工具中心（注册中心作为技能调用的统一入口）

替代demo：
    替代 demo server.py 中技能散落各处、无统一注册与发现的现状。
    demo中文件处理、意图识别、HTML渲染等能力分散在 server.py 与 data_manager.py，
    本注册中心将其收敛为可插拔技能。

技能发现和加载机制说明：
    1. 启动时自动注册内置技能（file_skills, db_skills）。
    2. 支持运行时动态注册第三方技能（插件机制）。
    3. call_skill 统一返回 dict，包含 success / data / error 字段，
       技能不存在或方法执行异常时不抛异常，便于上层 Hook 链处理。
"""

from typing import Any, Dict, List, Optional

from .file_skills import FileSkills, SkillResult
from .db_skills import DbSkills


class SkillRegistry:
    """MCP技能注册中心（单例）。

    设计意图：
        集中管理技能实例，提供统一的注册、查询、调用接口，
        解耦技能调用方与技能实现方。

    属性：
        _skills: 技能名 -> 技能信息字典的映射
        _instance: 单例实例
    """

    _instance: Optional["SkillRegistry"] = None

    def __init__(self) -> None:
        """初始化技能注册表并自动注册内置技能。"""
        self._skills: Dict[str, Dict[str, Any]] = {}
        self._register_defaults()

    @classmethod
    def get_instance(cls) -> "SkillRegistry":
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_skill(self, name: str, skill_instance: Any, description: str = "") -> None:
        """注册技能。

        参数：
            name: 技能唯一名称
            skill_instance: 技能实例（任意对象，通过 call_skill 调用其方法）
            description: 技能描述
        """
        self._skills[name] = {
            "name": name,
            "instance": skill_instance,
            "description": description,
        }

    def unregister_skill(self, name: str) -> bool:
        """注销技能。

        参数：
            name: 技能名称

        返回：
            True 表示注销成功，False 表示技能不存在
        """
        return self._skills.pop(name, None) is not None

    def get_skill(self, name: str) -> Any:
        """获取技能实例。

        参数：
            name: 技能名称

        返回：
            技能实例；未注册时返回 None
        """
        info = self._skills.get(name)
        return info["instance"] if info else None

    def list_skills(self) -> List[Dict[str, Any]]:
        """列出所有已注册技能。

        返回：
            技能信息列表，每项包含 name / description
            （不含 instance，避免序列化问题）
        """
        return [
            {
                "name": s["name"],
                "description": s["description"],
            }
            for s in self._skills.values()
        ]

    def call_skill(self, name: str, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用技能方法。

        参数：
            name: 技能名称
            method: 技能实例上的方法名
            params: 传给方法的参数字典（将展开为关键字参数）

        返回：
            {"success": True, "data": ..., "error": None} 或
            {"success": False, "data": None, "error": "..."}；
            技能不存在、方法不存在或执行异常时均返回 success=False，不抛异常。
        """
        info = self._skills.get(name)
        if info is None:
            return {"success": False, "data": None, "error": f"技能 '{name}' 不存在"}
        instance = info["instance"]
        handler = getattr(instance, method, None)
        if handler is None or not callable(handler):
            return {
                "success": False,
                "data": None,
                "error": f"技能 '{name}' 无方法 '{method}'",
            }
        try:
            result = handler(**(params or {}))
            return {"success": True, "data": result, "error": None}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def _register_defaults(self) -> None:
        """注册内置默认技能（file_skills, db_skills）。"""
        self.register_skill(
            "file_skills",
            FileSkills(file_storage=None),
            "文件技能：文件读写、图纸管理、Excel解析、报告生成",
        )
        self.register_skill(
            "db_skills",
            DbSkills(db=None),
            "数据库技能：通用CRUD、聚合查询、表导出、表结构查询",
        )


# 默认全局注册中心实例（供全局调用，也可由应用自行实例化独立作用域）
default_registry = SkillRegistry.get_instance()


def get_skill_registry() -> SkillRegistry:
    """模块级便捷函数：获取技能注册中心单例。"""
    return SkillRegistry.get_instance()


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world

    assert SkillRegistry is not None, "SkillRegistry 类未定义"
    # 验证内置技能自动注册
    registry = SkillRegistry()
    skills = registry.list_skills()
    skill_names = [s["name"] for s in skills]
    assert "file_skills" in skill_names, "内置技能 file_skills 未注册"
    assert "db_skills" in skill_names, "内置技能 db_skills 未注册"
    # 验证 get_skill 返回实例
    fs = registry.get_skill("file_skills")
    assert fs is not None, "get_skill('file_skills') 返回 None"
    # 验证 call_skill 调用方法
    result = registry.call_skill("file_skills", "generate_report", {
        "template": "Hello {name}",
        "data": {"name": "World"},
    })
    assert result["success"], f"call_skill 失败: {result}"
    assert "World" in result["data"], f"报告生成结果不符预期: {result['data']}"
    hello_world(__name__, f"内置技能注册数={len(skills)}, call_skill验证通过")


from prog.core.debug import DEBUG

if DEBUG:
    _self_test()
