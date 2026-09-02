"""
数据库技能模块

文件用途：
    提供AI工厂管家所需的数据库操作技能，封装通用的增删改查、
    聚合查询、表导出、表结构查询等能力，供SkillRegistry注册和调用。

对应技术规格章节：
    - §1.3 MCP工具中心 - 数据库技能
    - §1.3.3 MCP技能注册机制
    - §1.8.1 Database 统一接口层

替代demo：
    替代 demo data_manager.py 中直接操作JSON文件的低级数据访问。
    本模块基于 DatabaseManager 提供统一的数据库操作技能。

依赖：
    - core/database.py：DatabaseManager 统一数据库接口层
"""

import csv
import io
import json
import re
from typing import Any, Dict, List, Optional


class DbSkills:
    """数据库技能。

    封装通用的数据库CRUD、聚合查询、表导出等操作。
    当未注入 db 实例时所有操作返回错误信息，不抛异常。

    属性：
        _db: DatabaseManager 实例（可选）
    """

    # 允许查询的白名单表
    _READ_ONLY_TABLES = {
        "products", "customers", "orders", "inventory", "work_orders",
        "drawings", "qc_records", "production_lines", "bom",
        "workflow_instances", "workflow_configs", "notifications",
        "operation_logs", "training_data", "business_rules", "intent_rules",
    }
    # v6.96 P0-2：aggregate 聚合表达式白名单——仅允许标准聚合函数单列/全列
    _AGGREGATE_RE = re.compile(
        r"^(COUNT|SUM|AVG|MIN|MAX)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*|\*)\s*\)$",
        re.IGNORECASE)
    # 合法 SQL 标识符（分组字段/别名）
    _IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    def __init__(self, db=None) -> None:
        """初始化数据库技能。

        参数：
            db: DatabaseManager 实例；为 None 时所有操作返回错误
        """
        self._db = db

    def _ensure_db(self) -> Optional[str]:
        """检查数据库实例是否可用。

        返回：
            None 表示可用，否则返回错误信息字符串
        """
        if self._db is None:
            return "数据库未初始化，请先注入 DatabaseManager 实例"
        return None

    def query(
        self,
        table: str,
        filters: Optional[Dict[str, Any]] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """通用查询。

        参数：
            table: 表名
            filters: 过滤条件字典（等值匹配）
            fields: 查询字段列表（None 表示全部）
            limit: 返回记录上限

        返回：
            {"success": True, "data": [...], "error": None} 或
            {"success": False, "data": None, "error": "..."}
        """
        err = self._ensure_db()
        if err:
            return {"success": False, "data": None, "error": err}
        if table not in self._READ_ONLY_TABLES:
            return {"success": False, "data": None, "error": f"表 '{table}' 不在允许查询的白名单中"}
        try:
            rows = self._db.query_many(
                table, filters=filters, columns=fields, limit=limit
            )
            return {"success": True, "data": rows, "error": None}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def insert(self, table: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """通用插入。

        参数：
            table: 表名
            data: 待插入数据字典

        返回：
            {"success": True, "data": {"id": pk}, "error": None} 或错误
        """
        return {"success": False, "data": None, "error": "db_skills 禁止写操作（安全锁定）"}

    def update(
        self, table: str, data: Dict[str, Any], filters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """通用更新。

        参数：
            table: 表名
            data: 待更新字段字典
            filters: 过滤条件字典

        返回：
            {"success": True, "data": {"affected": N}, "error": None} 或错误
        """
        return {"success": False, "data": None, "error": "db_skills 禁止写操作（安全锁定）"}

    def delete(self, table: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        """通用删除。

        参数：
            table: 表名
            filters: 过滤条件字典

        返回：
            {"success": True, "data": {"affected": N}, "error": None} 或错误
        """
        return {"success": False, "data": None, "error": "db_skills 禁止写操作（安全锁定）"}

    def aggregate(
        self,
        table: str,
        group_by: str,
        aggregations: Dict[str, str],
    ) -> Dict[str, Any]:
        """聚合查询。

        参数：
            table: 表名
            group_by: 分组字段名
            aggregations: 聚合字段映射，如 {"total": "SUM(quantity)", "count": "COUNT(*)"}

        返回：
            {"success": True, "data": [...], "error": None} 或错误
        """
        err = self._ensure_db()
        if err:
            return {"success": False, "data": None, "error": err}
        if table.lower() not in self._READ_ONLY_TABLES:
            return {"success": False, "data": None, "error": f"表 '{table}' 不在允许查询的白名单中"}
        try:
            # v6.96 P0-2：字段白名单 + 表达式格式白名单，杜绝 f-string 直拼注入
            # （原实现直接拼用户 group_by/aggregations 进 SQL，仅靠子串黑名单防不住）
            columns = self._get_table_columns(table)
            if group_by not in columns:
                return {"success": False, "data": None,
                        "error": f"分组字段 '{group_by}' 不在表 '{table}' 的列白名单中"}
            agg_parts = []
            for alias, expr in aggregations.items():
                if not self._IDENTIFIER_RE.match(str(alias)):
                    return {"success": False, "data": None,
                            "error": f"聚合别名 '{alias}' 非法（仅允许字母/数字/下划线）"}
                m = self._AGGREGATE_RE.match(str(expr).strip())
                if not m:
                    return {"success": False, "data": None,
                            "error": f"聚合表达式 '{expr}' 非法：仅允许 COUNT/SUM/AVG/MIN/MAX(单列或*)"}
                agg_col = m.group(2)
                if agg_col != "*" and agg_col not in columns:
                    return {"success": False, "data": None,
                            "error": f"聚合列 '{agg_col}' 不在表 '{table}' 的列白名单中"}
                agg_parts.append(f"{m.group(1).upper()}({agg_col}) AS {alias}")
            # 经上述白名单校验后，group_by/agg_parts 均为已核实标识符，安全拼接
            cols_sql = f"{group_by}, " + ", ".join(agg_parts)
            sql = f"SELECT {cols_sql} FROM {table} GROUP BY {group_by} ORDER BY {group_by}"
            result = self._db.execute(sql)
            data = [dict(r._mapping) for r in result.fetchall()]
            return {"success": True, "data": data, "error": None}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def _get_table_columns(self, table: str) -> set:
        """查询真实表列名集合（P0-2 字段白名单依据）。"""
        schema = self.get_table_schema(table)
        if not schema.get("success") or not schema.get("data"):
            return set()
        return {row["column_name"] for row in schema["data"]}

    def export_table(self, table: str, format: str = "json") -> Dict[str, Any]:
        """导出表数据。

        参数：
            table: 表名
            format: 导出格式（json / csv），默认 json

        返回：
            {"success": True, "data": "导出内容字符串", "error": None} 或错误
        """
        err = self._ensure_db()
        if err:
            return {"success": False, "data": None, "error": err}
        if table not in self._READ_ONLY_TABLES:
            return {"success": False, "data": None, "error": f"表 '{table}' 不在允许查询的白名单中"}
        try:
            rows = self._db.query_many(table)
            if format.lower() == "csv":
                # 生成 CSV 格式
                if not rows:
                    return {"success": True, "data": "", "error": None}
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
                return {"success": True, "data": output.getvalue(), "error": None}
            else:
                # 默认 JSON 格式
                return {
                    "success": True,
                    "data": json.dumps(rows, ensure_ascii=False, default=str),
                    "error": None,
                }
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}

    def get_table_schema(self, table: str) -> Dict[str, Any]:
        """获取表结构。

        参数：
            table: 表名

        返回：
            {"success": True, "data": [...], "error": None} 或错误；
            data 为列信息列表，每项含 column_name / data_type / is_nullable / column_default
        """
        err = self._ensure_db()
        if err:
            return {"success": False, "data": None, "error": err}
        try:
            # 查询 information_schema 获取表结构
            sql = (
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_name = :table_name "
                "ORDER BY ordinal_position"
            )
            result = self._db.execute(sql, {"table_name": table})
            data = [dict(r._mapping) for r in result.fetchall()]
            return {"success": True, "data": data, "error": None}
        except Exception as e:
            return {"success": False, "data": None, "error": str(e)}


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world

    assert DbSkills is not None, "DbSkills 类未定义"
    # 验证无 db 时返回错误
    skills = DbSkills(db=None)
    result = skills.query("test_table")
    assert not result["success"], "无 db 时 query 应返回 success=False"
    assert result["error"], "无 db 时 query 应返回错误信息"
    hello_world(__name__, "无数据库降级验证通过")


from prog.core.debug import DEBUG

if DEBUG:
    _self_test()
