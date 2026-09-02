"""
数据驱动的规则引擎
==================
文件用途：
    将业务规则逻辑从硬编码 Python 类迁移为配置驱动的 DSL 执行引擎。
    规则定义存储在 business_rules.config_json 的 "engine_steps" 字段中，
    引擎按步骤顺序执行，返回 RuleResult。

设计要点：
    1. DSL 设计：engine_steps 为步骤列表，每步含 id/action/参数。
    2. 内置 Action（安全铁律）：fetch/lookup/filter/compare/branch/
       block/pass/warn/route_approval，不可被训练移除或修改。
    3. 表达式引擎：先做 ${param} 替换，再用 ast 模块安全求值
       （自定义 NodeVisitor，禁止 eval/exec）。
    4. 安全优先：任何步骤异常时默认返回 block。

对应技术规格：
    - §2.6 规则引擎（数据驱动 DSL + 安全沙箱求值）

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 数据驱动的规则引擎：规则逻辑从硬编码迁移为 business_rules.config_json 的 engine_steps DSL 执行（来源：SPEC §3.3 / 业务规格书 v6.02 统一规则引擎架构）
        - 内置 Action（安全铁律）：fetch/lookup/filter/compare/pluck/to_map/range_lookup/set_diff/aggregate/branch/block/pass/warn/route_approval，不可被训练移除或修改（来源：业务规格书 v6.02 / v6.05）
        - 表达式安全求值：${param} 占位符替换 + ast.NodeVisitor 自定义沙箱（禁止 eval/exec、函数/方法白名单）（来源：业务规格书 v6.02 / SPEC §3.3）
        - 规则执行结果复用 RuleResult 三态（pass/warn/block）契约（来源：模块拆分方案 契约4）
    对外接口（方法/API）：
        - RuleEngine.execute(rule_def, context, params, rule_name='')：执行规则定义（engine_steps），返回 RuleResult（来源：SPEC §3.3 / 模块拆分方案 契约4）
        - RuleEngine.BUILTIN_ACTIONS：不可变内置 action 集合（frozenset）（来源：业务规格书 v6.02）
        - _SafeExprEvaluator.evaluate(expr_str)：沙箱求值表达式（仅白名单节点/函数/字符串方法）（来源：业务规格书 v6.02）
    错误处理要求：
        - 任何步骤执行异常：默认返回 block（is_hard=True），安全优先（来源：SPEC §3.3 / 业务规格书 v6.02）
        - 未知 action / 参数缺失：抛 ValueError，由 execute() 捕获转为 block（来源：业务规格书 v6.02）
        - 表达式含不允许的语法节点/函数调用：抛 ValueError 拒绝（来源：业务规格书 v6.02）
"""

import ast
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from prog.runtime.rule_registry import RuleResult
from prog.runtime.logger import Logger


# ============================================================
# 安全表达式求值器
# ============================================================

class _SafeExprEvaluator(ast.NodeVisitor):
    """基于 ast.NodeVisitor 的安全表达式求值器。

    仅允许以下 AST 节点类型：
        - Expression          : 顶层表达式
        - BinOp               : 算术运算（+ - * / % **）
        - UnaryOp             : 一元运算（- + not）
        - BoolOp              : 逻辑运算（and / or）
        - Compare             : 比较运算（< > <= >= == !=）
        - Constant            : 数字 / 字符串 / 布尔值 / None
        - Name                : 变量引用（从 locals 查找）
        - Attribute           : 属性访问（dict 支持 key 访问）
        - Subscript           : 下标访问
        - List / Tuple / Dict : 容器字面量
        - Call                : 函数调用（仅允许 len / sum / abs）

    禁止的节点类型（命中即抛 ValueError）：
        - Import / ImportFrom : 导入模块
        - Lambda              : 匿名函数
        - Assign / AugAssign  : 赋值
        - Attribute（写）      : 属性赋值
        - Call（非白名单函数）  : 任意函数调用
        - 其他语句/表达式节点   : Comprehension / IfExp / GeneratorExp 等
    """

    # 允许调用的内置函数白名单
    _ALLOWED_FUNCS: Dict[str, Any] = {
        "len": len,
        "sum": sum,
        "abs": abs,
        "min": min,
        "max": max,
        "int": int,
        "float": float,
        "str": str,
    }

    # 允许调用的字符串方法白名单（仅对 str 实例开放，防止属性链攻击）
    _ALLOWED_STR_METHODS: frozenset = frozenset({
        "lower", "upper", "strip", "lstrip", "rstrip",
        "startswith", "endswith", "contains",
    })

    def __init__(self, variables: Dict[str, Any]):
        """初始化求值器。

        Args:
            variables: 变量上下文字典，表达式中的 Name 节点从此处查找。
        """
        self._vars = variables

    # ----------------------------------------------------------
    # 公共入口
    # ----------------------------------------------------------
    def evaluate(self, expr_str: str) -> Any:
        """解析并安全求值表达式字符串。

        Args:
            expr_str: 表达式字符串（如 "item.unit_price < item.cost_price * 1.15"）

        Returns:
            求值结果（可为 bool / 数字 / 字符串 / 列表 / 字典等）

        Raises:
            SyntaxError: 表达式语法错误
            ValueError: 表达式包含不允许的语法节点
            NameError: 引用了未定义的变量
        """
        tree = ast.parse(expr_str.strip(), mode="eval")
        return self.visit(tree.body)

    # ----------------------------------------------------------
    # AST 节点访问方法
    # ----------------------------------------------------------
    def visit_Expression(self, node: ast.Expression) -> Any:
        """顶层表达式节点"""
        return self.visit(node.body)

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        """二元算术运算：+ - * / % **"""
        left = self.visit(node.left)
        right = self.visit(node.right)
        # DB DECIMAL 列（cost_price/credit_limit/quantity 等）与 float 参数
        # 混合运算会抛 TypeError，统一转 float 保证规则表达式可计算
        if isinstance(left, Decimal):
            left = float(left)
        if isinstance(right, Decimal):
            right = float(right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
        raise ValueError(f"不支持的二元运算符: {type(node.op).__name__}")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        """一元运算：- + not"""
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.Not):
            return not operand
        raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        """逻辑运算：and / or（支持短路求值）"""
        if not node.values:
            raise ValueError("逻辑运算缺少操作数")
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = self.visit(value_node)
                if not result:
                    return result
            return result
        # ast.Or
        result = False
        for value_node in node.values:
            result = self.visit(value_node)
            if result:
                return result
        return result

    def visit_Compare(self, node: ast.Compare) -> bool:
        """比较运算：< > <= >= == != in not in（支持链式比较如 a < b < c）"""
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            if isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            else:
                raise ValueError(f"不支持的比较运算符: {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True

    def visit_Constant(self, node: ast.Constant) -> Any:
        """常量节点：数字 / 字符串 / 布尔值 / None"""
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        """变量引用：从上下文字典中查找"""
        name = node.id
        # Python 内置常量
        if name == "True":
            return True
        if name == "False":
            return False
        if name == "None":
            return None
        # 从变量上下文查找
        if name in self._vars:
            return self._vars[name]
        raise NameError(f"未定义的变量: {name}")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        """属性访问：dict 支持 key 访问；对象仅允许公开属性访问（S2：拒绝 _/__ 私有与
        魔术属性，防止 __class__.__bases__.__subclasses__ 等属性链遍历逃逸沙箱）"""
        value = self.visit(node.value)
        attr = node.attr
        if isinstance(value, dict):
            # dict 的 key 访问无对象属性逃逸风险，保持宽松（key 缺失返回 None）
            return value.get(attr)
        if attr.startswith("_"):
            raise ValueError(f"不允许访问私有/魔术属性: {attr}")
        return getattr(value, attr, None)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        """下标访问：如 items[0] 或 data["key"]"""
        value = self.visit(node.value)
        # Python 3.9+：node.slice 直接是表达式（ast.Index 自 3.9 已移除，S2）
        index = self.visit(node.slice)
        return value[index]

    def visit_List(self, node: ast.List) -> list:
        """列表字面量"""
        return [self.visit(elt) for elt in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> tuple:
        """元组字面量"""
        return tuple(self.visit(elt) for elt in node.elts)

    def visit_Dict(self, node: ast.Dict) -> dict:
        """字典字面量"""
        return {
            self.visit(k): self.visit(v)
            for k, v in zip(node.keys, node.values)
        }

    def visit_Call(self, node: ast.Call) -> Any:
        """函数调用：仅允许白名单内置函数（len/sum/abs/min/max/int/float/str）
        与白名单字符串方法（lower/strip/startswith/contains 等）。

        安全约束：
            - 内置函数按名称白名单校验
            - 字符串方法仅对 str 实例开放（node.func.value 求值结果必须为 str），
              方法名必须在 _ALLOWED_STR_METHODS 白名单内
            - 禁止关键字参数
            - 任意对象的方法调用（如 obj.__class__ 链）一律拒绝
        """
        # 禁止关键字参数（str 方法如 startswith 也仅用位置参数）
        if node.keywords:
            raise ValueError("不允许使用关键字参数")

        args = [self.visit(arg) for arg in node.args]

        # 1. 内置函数白名单：len / sum / abs / min / max / int / float / str
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name not in self._ALLOWED_FUNCS:
                raise ValueError(f"不允许调用的函数: {func_name}")
            func = self._ALLOWED_FUNCS[func_name]
            return func(*args)

        # 2. 字符串方法白名单：仅对 str 实例开放
        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            if method_name not in self._ALLOWED_STR_METHODS:
                raise ValueError(f"不允许调用的方法: {method_name}")
            obj = self.visit(node.func.value)
            if not isinstance(obj, str):
                raise ValueError(
                    f"方法 {method_name} 仅支持字符串类型调用"
                    f"（实际类型: {type(obj).__name__}）")
            method = getattr(obj, method_name, None)
            if method is None:
                raise ValueError(f"字符串无该方法: {method_name}")
            try:
                return method(*args)
            except Exception as e:
                raise ValueError(
                    f"字符串方法 {method_name} 调用失败: {e}")

        # 3. 其他调用形式一律拒绝
        raise ValueError("仅允许调用内置函数或白名单字符串方法")

    def generic_visit(self, node: ast.AST) -> Any:
        """兜底：遇到任何未显式处理的节点类型即拒绝"""
        raise ValueError(f"不允许的语法节点: {type(node).__name__}")


# ============================================================
# 规则引擎
# ============================================================

# ${param} 占位符正则：匹配 ${variable_name}
_PARAM_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


class RuleEngine:
    """数据驱动的规则引擎。

    从 business_rules.config_json 读取 engine_steps 定义，
    按步骤顺序执行，返回 RuleResult。

    安全保证：
        - engine 内置 action 不可被训练修改或移除
        - 规则定义只能选择使用哪些 action、传入什么参数
        - 表达式在受限沙箱中求值，禁止导入/执行任意代码
        - 执行失败时默认 block（安全优先）

    线程安全说明：
        _vars 为实例级状态，每次 execute() 调用会重置。
        如需并发执行，请为每次调用创建独立的 RuleEngine 实例。
    """

    # 不可变的安全 action 集合（frozenset 确保运行时不可修改）
    BUILTIN_ACTIONS: frozenset = frozenset({
        "fetch", "lookup", "filter", "compare", "pluck",
        "to_map", "range_lookup", "set_diff", "aggregate",
        "branch", "block", "pass", "warn", "route_approval",
    })

    def __init__(self, database: Any = None):
        """初始化规则引擎。

        Args:
            database: 可选的数据库访问层（鸭子类型：提供 query_one/query_many）。
                      为 None 时尝试从 prog.runtime.database.get_database() 获取，
                      均不可用时 lookup action 降级返回空。
        """
        self._db = database
        self._vars: Dict[str, Any] = {}
        self._context: Dict[str, Any] = {}
        self._params: Dict[str, Any] = {}
        self._logger = Logger.get_logger("runtime.rule_engine")

    # ----------------------------------------------------------
    # 公共接口
    # ----------------------------------------------------------
    def execute(self, rule_def: dict, context: dict, params: dict,
                rule_name: str = "") -> "RuleResult":
        """执行规则定义，返回 RuleResult。

        Args:
            rule_def: 规则定义（含 engine_steps）
            context: Agent 上下文（含 data, user, slots 等）
            params: 从 business_rules 表加载的可训练参数
            rule_name: 规则名（用于结果标识）

        Returns:
            RuleResult: 规则执行结果。任何步骤异常时返回 block（安全优先）。
        """
        try:
            # 每次执行重置变量上下文
            self._vars = {}
            self._context = context or {}
            self._params = params or {}

            steps: List[dict] = rule_def.get("engine_steps", []) if rule_def else []

            if not steps:
                self._logger.debug(
                    "规则无 engine_steps | rule=%s | 直接通过", rule_name,
                )
                return RuleResult(
                    status=RuleResult.STATUS_PASS,
                    rule_name=rule_name,
                    message="规则未定义执行步骤，默认通过",
                )

            self._logger.debug(
                "开始执行规则 | rule=%s | steps=%d", rule_name, len(steps),
            )

            for step in steps:
                result = self._execute_step(step)
                if result is not None:
                    # 终态 action（block/pass/warn/route_approval）返回结果
                    if not result.rule_name:
                        result.rule_name = rule_name
                    self._logger.debug(
                        "规则执行完成 | rule=%s | status=%s",
                        rule_name, result.status,
                    )
                    return result

            # 所有步骤执行完毕，未触发终态 action -> 默认通过
            self._logger.debug(
                "规则步骤执行完毕未触发终态 | rule=%s | 默认通过", rule_name,
            )
            return RuleResult(
                status=RuleResult.STATUS_PASS,
                rule_name=rule_name,
                message="规则执行完成，未触发终态动作",
            )

        except Exception as e:
            # 安全优先：任何异常都返回 block
            self._logger.error(
                "规则执行异常，安全阻断 | rule=%s | error=%s",
                rule_name, e, exc_info=True,
            )
            return RuleResult(
                status=RuleResult.STATUS_BLOCK,
                rule_name=rule_name,
                message=f"规则执行异常，安全阻断: {e}",
                is_hard=True,
            )

    # ----------------------------------------------------------
    # 步骤分发
    # ----------------------------------------------------------
    def _execute_step(self, step: dict) -> Optional[RuleResult]:
        """执行单个步骤，返回 RuleResult 或 None。

        Args:
            step: 步骤定义字典（含 action 和参数）

        Returns:
            - 终态 action（block/pass/warn/route_approval）返回 RuleResult
            - 非终态 action（fetch/lookup/filter/compare/branch）返回 None

        Raises:
            ValueError: 未知 action 或参数缺失
        """
        action = step.get("action", "")
        if action not in self.BUILTIN_ACTIONS:
            raise ValueError(
                f"未知的 action: {action!r}，"
                f"允许的 action: {sorted(self.BUILTIN_ACTIONS)}"
            )

        # v6.49：统一递归替换步骤内 ${param} 占位符（expr/condition 按表达式字面量，
        # message/approver_role 等按纯值），分支子步骤递归执行时幂等。
        step = self._substitute_params_in_step(step)

        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            raise ValueError(f"action {action!r} 尚未实现")

        return handler(step)

    # ----------------------------------------------------------
    # 内置 Action 处理器
    # ----------------------------------------------------------
    def _action_fetch(self, step: dict) -> None:
        """fetch: 从上下文获取数据。

        参数：
            source : dot 路径（如 "data.items" -> context["data"]["items"]）
            as     : 存储到 _vars 的变量名
        """
        source: str = step.get("source", "")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("fetch action 需要 'as' 参数")

        # 支持 "params.xxx" 前缀：从可训练参数（config_json）中按 dot 路径取
        if source.startswith("params."):
            value = self._resolve_dot_path(
                source[len("params."):], self._params)
        else:
            value = self._resolve_dot_path(source, self._context)
        self._vars[var_name] = value

        result_summary = self._summarize(value)
        self._logger.debug(
            "step fetch | id=%s | source=%s | as=%s | result=%s",
            step.get("id", ""), source, var_name, result_summary,
        )
        return None

    def _action_lookup(self, step: dict) -> None:
        """lookup: 查询数据库表。

        参数：
            table       : 数据库表名
            key_field   : 过滤字段名
            source_key  : 查找值来源（字段名或变量名）
            target_field: 要获取的目标字段
            fields      : 要获取的目标字段列表（mode=many 时）
            mode        : 查询模式，"one"（默认，单条）/"many"（列表）
            merge_into  : 合并目标变量名（指向列表时逐项充实）
            as          : 存储结果的变量名

        三种模式：
            1. 单值模式（默认）：以 _vars[source_key] 为键 query_one，
               结果存入 _vars[as]。
            2. 合并模式（merge_into 指向列表）：遍历列表中每个 item，
               以 item[source_key] 为键 query_one，将 target_field 写回 item。
            3. 列表模式（mode="many"）：以 _vars[source_key] 为键 query_many，
               结果列表存入 _vars[as]（BOM 标准明细、排产物料需求等）。
        """
        table: str = step.get("table", "")
        key_field: str = step.get("key_field", "")
        source_key: str = step.get("source_key", "")
        target_field: str = step.get("target_field", "")
        fields: Optional[list] = step.get("fields")
        mode: str = step.get("mode", "one")
        merge_into: str = step.get("merge_into", "")
        as_var: str = step.get("as", "")

        db = self._get_db()
        if db is None:
            self._logger.debug(
                "step lookup | id=%s | 跳过（无数据库可用）", step.get("id", ""),
            )
            return None

        # 合并模式：merge_into 指向 _vars 中的列表
        if merge_into and isinstance(self._vars.get(merge_into), list):
            items: List[Any] = self._vars[merge_into]
            enriched = 0
            for item in items:
                lookup_value = self._get_item_field(item, source_key)
                if lookup_value is None:
                    continue
                row = db.query_one(table, {key_field: lookup_value},
                                   [target_field])
                if row:
                    value = row.get(target_field)
                    self._set_item_field(item, target_field, value)
                    enriched += 1
            self._logger.debug(
                "step lookup | id=%s | table=%s | merge_into=%s | enriched=%d/%d",
                step.get("id", ""), table, merge_into, enriched, len(items),
            )
            return None

        # 查找值来源：变量或 context dot 路径
        lookup_value = self._vars.get(source_key)
        if lookup_value is None:
            lookup_value = self._resolve_dot_path(source_key, self._context)

        # 列表模式：query_many
        if mode == "many":
            result = []
            if lookup_value is not None:
                # W11：需显式括号——`fields or ([target_field] if target_field else None)`
                # 无括号时按 `(fields or [target_field]) if target_field else None` 解析，
                # target_field 为空但 fields 存在时 select_fields 被误置 None（字段丢失）
                select_fields = fields or ([target_field] if target_field else None)
                try:
                    result = db.query_many(
                        table, {key_field: lookup_value},
                        select_fields) or []
                except Exception as e:
                    self._logger.debug(
                        "step lookup(many) | id=%s | table=%s | error=%s",
                        step.get("id", ""), table, e,
                    )
                    result = []
            target_var = as_var or merge_into
            if target_var:
                self._vars[target_var] = result
            self._logger.debug(
                "step lookup(many) | id=%s | table=%s | key=%s=%s | rows=%d",
                step.get("id", ""), table, key_field, lookup_value, len(result),
            )
            return None

        # 单值模式：target_field 单字段返回标量；fields 多字段返回整行 dict
        result = None
        if lookup_value is not None:
            select_fields = fields or ([target_field] if target_field else None)
            row = db.query_one(table, {key_field: lookup_value},
                               select_fields)
            if row:
                if fields:
                    result = row
                elif target_field:
                    result = row.get(target_field)
                else:
                    result = row

        target_var = as_var or merge_into
        if target_var:
            self._vars[target_var] = result

        self._logger.debug(
            "step lookup | id=%s | table=%s | key=%s=%s | target=%s | result=%s",
            step.get("id", ""), table, key_field, lookup_value,
            target_field, result,
        )
        return None

    def _action_filter(self, step: dict) -> None:
        """filter: 对列表过滤，返回满足条件的元素列表。

        参数：
            source_var : 源列表变量名（从 _vars 取）
            expr       : 过滤表达式（item 为循环变量）
            as         : 存储过滤结果列表的变量名
        """
        source_var: str = step.get("source_var", "")
        expr: str = step.get("expr", "")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("filter action 需要 'as' 参数")

        if source_var not in self._vars:
            raise ValueError(
                f"filter 的 source_var {source_var!r} 不存在于变量上下文中"
            )
        source_list = self._vars[source_var]
        if not isinstance(source_list, list):
            raise ValueError(
                f"filter 的 source_var {source_var!r} 不是列表"
                f"（实际类型: {type(source_list).__name__}）"
            )

        # 先做 ${param} 替换
        expr = self._substitute_params(expr)

        filtered: List[Any] = []
        for item in source_list:
            # 每次迭代将 item 注入变量上下文
            eval_vars = dict(self._vars)
            eval_vars["item"] = item
            evaluator = _SafeExprEvaluator(eval_vars)
            if evaluator.evaluate(expr):
                filtered.append(item)

        self._vars[var_name] = filtered

        self._logger.debug(
            "step filter | id=%s | source_var=%s | as=%s | "
            "matched=%d/%d",
            step.get("id", ""), source_var, var_name,
            len(filtered), len(source_list),
        )
        return None

    def _action_compare(self, step: dict) -> None:
        """compare: 单值比较，expr 求值后返回 bool。

        参数：
            expr : 表达式（求值为 bool）
            as   : 存储布尔结果的变量名（可选）
        """
        expr: str = step.get("expr", "")
        var_name: str = step.get("as", "")

        expr = self._substitute_params(expr)
        evaluator = _SafeExprEvaluator(dict(self._vars))
        result = bool(evaluator.evaluate(expr))

        if var_name:
            self._vars[var_name] = result

        self._logger.debug(
            "step compare | id=%s | expr=%s | result=%s | as=%s",
            step.get("id", ""), expr, result, var_name,
        )
        return None

    def _action_pluck(self, step: dict) -> None:
        """pluck: 从列表中提取指定字段，生成新列表。

        参数：
            source_var : 源列表变量名（从 _vars 取）
            field      : 要提取的字段名
            as         : 存储结果列表的变量名

        用途：将 [{material_code:"A",...}, {material_code:"B",...}]
              提取为 ["A", "B"]，供后续 in/not in 判断使用。
        """
        source_var: str = step.get("source_var", "")
        field: str = step.get("field", "")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("pluck action 需要 'as' 参数")
        if source_var not in self._vars:
            raise ValueError(
                f"pluck 的 source_var {source_var!r} 不存在于变量上下文中"
            )

        source_list = self._vars[source_var]
        if not isinstance(source_list, list):
            raise ValueError(
                f"pluck 的 source_var {source_var!r} 不是列表"
                f"（实际类型: {type(source_list).__name__}）"
            )

        result = [self._get_item_field(item, field) for item in source_list]
        self._vars[var_name] = result

        self._logger.debug(
            "step pluck | id=%s | source_var=%s | field=%s | as=%s | count=%d",
            step.get("id", ""), source_var, field, var_name, len(result),
        )
        return None

    def _action_to_map(self, step: dict) -> None:
        """to_map: 将列表转换为 {key: value} 映射字典。

        参数：
            source_var : 源列表变量名（从 _vars 取）
            key_field  : 用作 key 的字段名
            value_field: 用作 value 的字段名（缺省时值为整个 item）
            as         : 存储结果字典的变量名

        用途：BOM 一致性校验中构建 {material_code: quantity} 映射，
              排产物料需求中构建 {material_code: required_qty} 映射。
        """
        source_var: str = step.get("source_var", "")
        key_field: str = step.get("key_field", "")
        value_field: str = step.get("value_field", "")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("to_map action 需要 'as' 参数")
        if source_var not in self._vars:
            raise ValueError(
                f"to_map 的 source_var {source_var!r} 不存在于变量上下文中"
            )
        source_list = self._vars[source_var]
        if not isinstance(source_list, list):
            raise ValueError(
                f"to_map 的 source_var {source_var!r} 不是列表"
                f"（实际类型: {type(source_list).__name__}）"
            )

        result: Dict[Any, Any] = {}
        for item in source_list:
            key = self._get_item_field(item, key_field)
            if key is None:
                continue
            if value_field:
                result[str(key)] = self._get_item_field(item, value_field)
            else:
                result[str(key)] = item

        self._vars[var_name] = result
        self._logger.debug(
            "step to_map | id=%s | source_var=%s | key=%s | value=%s | "
            "as=%s | entries=%d",
            step.get("id", ""), source_var, key_field, value_field or "*",
            var_name, len(result),
        )
        return None

    def _action_range_lookup(self, step: dict) -> None:
        """range_lookup: 按区间查表（AQL 样本量查表等）。

        参数：
            value      : 待匹配的值（变量名，从 _vars 取）
            table_var  : 区间表变量名（从 _vars 取）
            key_format : 兼容参数（保留供调用方传入）：实现按表结构自动分派——
                         dict 按 "lo-hi" 字符串 key 遍历、list/tuple 按 [lo, hi, sample] 条目遍历
            out_field  : table_var 为 dict 时，命中后提取的字段名
            default    : 未命中时的默认值（缺省 None）
            as         : 存储结果的变量名

        支持两种表结构（对应 QC 的 aql_sample_table 两种格式）：
            - dict：{"51-90": {"sample": 13, ...}}
            - list：[(2, 8, 2), (9, 15, 3), ...]
        """
        value: str = step.get("value", "")
        table_var: str = step.get("table_var", "")
        key_format: str = step.get("key_format", "lo-hi")
        out_field: str = step.get("out_field", "")
        default: Any = step.get("default")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("range_lookup action 需要 'as' 参数")

        match_value = self._vars.get(value)
        table = self._vars.get(table_var)
        if match_value is None or table is None:
            self._vars[var_name] = default
            return None

        try:
            match_value = int(match_value)
        except (ValueError, TypeError):
            self._vars[var_name] = default
            return None

        result = None
        if isinstance(table, dict):
            # dict 格式：{"lo-hi": value}
            for key, val in table.items():
                parts = str(key).split("-")
                if len(parts) != 2:
                    continue
                try:
                    lo, hi = int(parts[0]), int(parts[1])
                except ValueError:
                    continue
                if lo <= match_value <= hi:
                    result = val
                    break
            if isinstance(result, dict) and out_field:
                result = result.get(out_field)
        elif isinstance(table, (list, tuple)):
            # list 格式：[(lo, hi, sample), ...]
            for entry in table:
                try:
                    lo, hi, sample = int(entry[0]), int(entry[1]), entry[2]
                except (ValueError, TypeError, IndexError):
                    continue
                if lo <= match_value <= hi:
                    result = sample
                    break
        else:
            self._vars[var_name] = default
            return None

        if result is None:
            result = default
        self._vars[var_name] = result
        self._logger.debug(
            "step range_lookup | id=%s | value=%s=%s | table=%s | result=%s",
            step.get("id", ""), value, match_value, table_var, result,
        )
        return None

    def _action_set_diff(self, step: dict) -> None:
        """set_diff: 集合差集，返回 a - b（a 中有而 b 中没有的元素）。

        参数：
            a_var : 集合 A 变量名（从 _vars 取）
            b_var : 集合 B 变量名（从 _vars 取）
            as    : 存储差集列表的变量名

        用途：BOM 缺项 = BOM 码集 - 订单码集；多项 = 订单码集 - BOM 码集。
        """
        a_var: str = step.get("a_var", "")
        b_var: str = step.get("b_var", "")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("set_diff action 需要 'as' 参数")
        if a_var not in self._vars or b_var not in self._vars:
            raise ValueError(
                f"set_diff 的变量 {a_var!r}/{b_var!r} 不存在于变量上下文中"
            )

        try:
            set_a = set(self._vars[a_var] or [])
            set_b = set(self._vars[b_var] or [])
        except TypeError as e:
            raise ValueError(f"set_diff 的元素必须可哈希（当前不可哈希: {e}）")
        diff = sorted(set_a - set_b)

        self._vars[var_name] = diff
        self._logger.debug(
            "step set_diff | id=%s | a=%s(%d) | b=%s(%d) | diff=%d",
            step.get("id", ""), a_var, len(set_a), b_var, len(set_b),
            len(diff),
        )
        return None

    def _action_aggregate(self, step: dict) -> None:
        """aggregate: 对列表做聚合统计。

        参数：
            source_var : 源列表变量名（从 _vars 取）
            field      : 聚合字段名（op=count 时可为空）
            op         : 聚合操作：sum / count / max / min / avg
            as         : 存储结果的变量名

        用途：排产物料缺口统计、订单数量汇总等。
        """
        source_var: str = step.get("source_var", "")
        field: str = step.get("field", "")
        op: str = step.get("op", "count")
        var_name: str = step.get("as", "")
        if not var_name:
            raise ValueError("aggregate action 需要 'as' 参数")
        if source_var not in self._vars:
            raise ValueError(
                f"aggregate 的 source_var {source_var!r} 不存在于变量上下文中"
            )
        source_list = self._vars[source_var]
        if not isinstance(source_list, list):
            raise ValueError(
                f"aggregate 的 source_var {source_var!r} 不是列表"
                f"（实际类型: {type(source_list).__name__}）"
            )

        if op == "count":
            result = len(source_list)
        else:
            values = []
            for item in source_list:
                val = self._get_item_field(item, field)
                try:
                    values.append(float(val))
                except (TypeError, ValueError):
                    continue
            if op == "sum":
                result = sum(values)
            elif op == "avg":
                result = sum(values) / len(values) if values else 0
            elif op == "max":
                result = max(values) if values else 0
            elif op == "min":
                result = min(values) if values else 0
            else:
                raise ValueError(f"不支持的聚合操作: {op}")

        self._vars[var_name] = result
        self._logger.debug(
            "step aggregate | id=%s | source_var=%s | field=%s | op=%s | "
            "as=%s | result=%s",
            step.get("id", ""), source_var, field, op, var_name, result,
        )
        return None

    def _action_branch(self, step: dict) -> Optional[RuleResult]:
        """branch: 条件分支，condition 求值后执行 then 或 else 子步骤。

        参数：
            condition : 条件表达式（求值为 bool）
            then      : 条件为真时执行的子步骤（dict 或 list[dict]）
            else      : 条件为假时执行的子步骤（dict 或 list[dict]）

        子步骤可以是任何内置 action。若子步骤为终态 action，
        则返回其 RuleResult；否则返回 None 继续主流程。
        """
        condition: str = step.get("condition", "")
        condition = self._substitute_params(condition)
        evaluator = _SafeExprEvaluator(dict(self._vars))
        cond_result = bool(evaluator.evaluate(condition))

        sub_step = step.get("then") if cond_result else step.get("else")

        self._logger.debug(
            "step branch | id=%s | condition=%s | result=%s | branch=%s",
            step.get("id", ""), condition, cond_result,
            "then" if cond_result else "else",
        )

        if sub_step is None:
            return None

        # 支持单个步骤（dict）或多个步骤（list[dict]）
        if isinstance(sub_step, list):
            for s in sub_step:
                result = self._execute_step(s)
                if result is not None:
                    return result
            return None
        return self._execute_step(sub_step)

    def _action_block(self, step: dict) -> RuleResult:
        """block: 阻断操作，返回 STATUS_BLOCK。

        参数：
            message : 阻断原因说明

        说明：
            block 恒为硬阻断（is_hard=True，安全铁律），不接受配置覆盖为软阻断
            （软性提示请使用 warn action）。
        """
        message: str = step.get("message", "规则拦截")

        self._logger.debug(
            "step block | id=%s | message=%s | is_hard=True(安全铁律)",
            step.get("id", ""), message,
        )
        return RuleResult(
            status=RuleResult.STATUS_BLOCK,
            message=message,
            is_hard=True,
        )

    def _action_pass(self, step: dict) -> RuleResult:
        """pass: 通过，返回 STATUS_PASS。

        参数：
            message : 通过说明
        """
        message: str = step.get("message", "规则通过")

        self._logger.debug(
            "step pass | id=%s | message=%s", step.get("id", ""), message,
        )
        return RuleResult(
            status=RuleResult.STATUS_PASS,
            message=message,
        )

    def _action_warn(self, step: dict) -> RuleResult:
        """warn: 警告，返回 STATUS_WARN + requires_approval=True。

        参数：
            message : 警告说明
            is_hard : 是否硬规则（默认 False）
        """
        message: str = step.get("message", "规则警告")
        is_hard: bool = step.get("is_hard", False)

        self._logger.debug(
            "step warn | id=%s | message=%s", step.get("id", ""), message,
        )
        return RuleResult(
            status=RuleResult.STATUS_WARN,
            message=message,
            requires_approval=True,
            is_hard=is_hard,
        )

    def _action_route_approval(self, step: dict) -> RuleResult:
        """route_approval: 路由到审批，设置 requires_approval + approver_role。

        参数：
            message       : 审批说明
            approver_role : 审批角色（如 "manager" / "general_manager"）
            is_hard       : 是否硬规则（默认 False）
        """
        message: str = step.get("message", "需要审批")
        approver_role: str = step.get("approver_role", "")
        is_hard: bool = step.get("is_hard", False)

        self._logger.debug(
            "step route_approval | id=%s | approver_role=%s | message=%s",
            step.get("id", ""), approver_role, message,
        )
        return RuleResult(
            status=RuleResult.STATUS_WARN,
            message=message,
            requires_approval=True,
            approver_role=approver_role,
            is_hard=is_hard,
        )

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------
    # 需按表达式字面量（repr）替换的字段：比较/分支条件/过滤表达式
    _EXPR_FIELDS = frozenset({"expr", "condition"})

    def _substitute_params_in_step(self, value: Any,
                                   expr_field: bool = False) -> Any:
        """递归替换步骤结构内全部字符串的 ${param} 占位符（幂等）。

        - expr/condition 字段按表达式字面量替换（repr，字符串加引号）
        - 其余字段（message/approver_role 等）按纯值替换（字符串不加引号）
        """
        if isinstance(value, str):
            if expr_field:
                return self._substitute_params(value)
            return self._substitute_params_plain(value)
        if isinstance(value, dict):
            return {k: self._substitute_params_in_step(
                v, k in self._EXPR_FIELDS) for k, v in value.items()}
        if isinstance(value, list):
            return [self._substitute_params_in_step(v) for v in value]
        return value

    def _substitute_params_plain(self, text: str) -> str:
        """替换 ${param} 占位符为纯值（str 不加引号，供 message/approver_role 等字段）。"""
        def _replacer(match: re.Match) -> str:
            """${param} 匹配替换回调（纯值模式）。

            参数：
                match: 正则匹配对象（group(1)=参数名，可含 dot 路径）
            返回：
                str: 参数纯值（str 原样返回）；未找到返回 ""（空串，
                     供 message/approver_role 等展示字段静默留空）
            查找顺序：_params 顶层 -> _params dot 路径 -> _context dot 路径
            """
            param_name = match.group(1)
            value = self._params.get(param_name)
            if value is None and "." in param_name:
                value = self._resolve_dot_path(param_name, self._params)
            if value is None:
                value = self._resolve_dot_path(param_name, self._context)
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            # 非字符串标量按 str 注入展示字段（避免 repr 的 Python 字面量引号污染 message/approver_role 等）
            return str(value)
        return _PARAM_PATTERN.sub(_replacer, text)

    def _get_db(self) -> Any:
        """获取数据库实例。

        优先使用构造函数注入的 self._db，
        为 None 时降级到 prog.runtime.database.get_database()，
        均不可用时返回 None。

        Returns:
            数据库对象或 None
        """
        if self._db is not None:
            return self._db
        try:
            from prog.runtime.database import get_database
            return get_database()
        except Exception:
            return None

    def _resolve_dot_path(self, path: str, source: Any) -> Any:
        """通过 dot 路径访问嵌套数据。

        例如 "data.items" -> source["data"]["items"]

        Args:
            path: dot 路径字符串
            source: 数据源（dict 或对象）

        Returns:
            路径对应的值，路径不存在时返回 None
        """
        if not path:
            return source

        current = source
        for part in path.split("."):
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                # 支持列表索引路径段（如 items.0.name），越界返回 None
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                current = getattr(current, part, None)
        return current

    def _substitute_params(self, expr: str) -> str:
        """替换表达式中的 ${param} 占位符。

        从 self._params 中查找参数值，使用 repr() 转换为 Python 字面量。
        支持 dot 路径嵌套参数（如 ${tolerance.quantity_deviation}）。
        未找到的参数替换为 None。

        Args:
            expr: 含 ${param} 占位符的表达式字符串

        Returns:
            替换后的表达式字符串
        """
        def _replacer(match: re.Match) -> str:
            """${param} 匹配替换回调（表达式字面量模式）。

            参数：
                match: 正则匹配对象（group(1)=参数名，可含 dot 路径）
            返回：
                str: 参数 repr 字面量（expr/condition 求值需保持 Python
                     语义，如字符串带引号）；未找到返回 "None"
            查找顺序：_params 顶层 -> _params dot 路径 -> _context dot 路径
            """
            param_name = match.group(1)
            # 1. 顶层参数
            value = self._params.get(param_name)
            # 2. dot 路径嵌套参数（如 tolerance.quantity_deviation）
            if value is None and "." in param_name:
                value = self._resolve_dot_path(param_name, self._params)
            # 3. context 中按 dot 路径查找
            if value is None:
                value = self._resolve_dot_path(param_name, self._context)
            if value is None:
                return "None"
            return repr(value)

        return _PARAM_PATTERN.sub(_replacer, expr)

    @staticmethod
    def _get_item_field(item: Any, field: str) -> Any:
        """从 item 中获取字段值（支持 dict 和对象）。

        Args:
            item: 数据项（dict 或对象）
            field: 字段名

        Returns:
            字段值，不存在时返回 None
        """
        if isinstance(item, dict):
            return item.get(field)
        return getattr(item, field, None)

    @staticmethod
    def _set_item_field(item: Any, field: str, value: Any) -> None:
        """设置 item 的字段值（支持 dict 和对象）。

        Args:
            item: 数据项（dict 或对象）
            field: 字段名
            value: 字段值
        """
        if isinstance(item, dict):
            item[field] = value
        else:
            setattr(item, field, value)

    @staticmethod
    def _summarize(value: Any, max_len: int = 80) -> str:
        """生成值的概要字符串（用于日志）。

        Args:
            value: 任意值
            max_len: 最大字符串长度

        Returns:
            概要字符串
        """
        if value is None:
            return "None"
        if isinstance(value, (list, tuple)):
            return f"{type(value).__name__}[{len(value)}]"
        if isinstance(value, dict):
            return f"dict[{len(value)}]"
        text = repr(value)
        if len(text) > max_len:
            return text[:max_len] + "..."
        return text


__all__ = ["RuleEngine"]
