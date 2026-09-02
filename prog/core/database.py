"""
Database 统一接口层 - AI工厂管家

文件用途：
    定义 PostgreSQL 数据库访问的统一接口层，封装连接池管理与事务上下文。

对应技术规格章节：
    §1.8.1 Database 统一接口层
    §1.4.2 训练数据存储（JSONB 字段）

设计说明：
    1. 使用 SQLAlchemy 2.0 ORM + psycopg2 驱动连接 PostgreSQL
    2. 连接池管理通过 SQLAlchemy create_engine 实现：
        - local 模式：pool_size=10, max_overflow=20（适合开发与小规模部署）
        - volcano 模式：pool_size=20, max_overflow=40（适合高并发生产环境）
    3. DatabaseManager 为单例类，全系统共享同一连接池
    4. 支持 JSONB 字段类型，用于存储训练数据等半结构化数据（§1.4.2）
    5. 提供事务上下文管理器，保证操作的原子性

配置示例（deployment_config.json）:
    {
        "database": {
            "local": {
                "host": "127.0.0.1", "port": 5432,
                "database": "ai_factory",
                "user_env": "DB_USER", "password_env": "DB_PASSWORD",
                "pool_size": 10, "max_overflow": 20
            },
            "volcano": {
                "host_env": "RDS_HOST", "port": 5432,
                "database": "ai_factory",
                "user_env": "RDS_USER", "password_env": "RDS_PASSWORD",
                "pool_size": 20, "max_overflow": 40,
                "ssl_mode": "require"
            }
        }
    }
"""

import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional


class DatabaseManager:
    """
    数据库管理器单例类

    封装 SQLAlchemy engine 与 session factory，提供统一的数据库访问入口。

    属性:
        _engine: SQLAlchemy Engine 实例（管理连接池）
        _session_factory: SQLAlchemy sessionmaker 实例
        _config: 当前部署模式的数据库配置

    设计说明:
        - 单例模式：全系统共享同一连接池，避免连接泄漏
        - 连接池参数根据部署模式动态配置
        - 支持 with 语法的事务上下文
    """

    _instance: Optional["DatabaseManager"] = None

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        初始化数据库管理器

        参数:
            config: 数据库配置字典，已根据部署模式选定 local 或 volcano 子配置，
                    并解析了 _env 后缀字段，包含：
                    - host: 数据库主机
                    - port: 端口
                    - database: 数据库名
                    - user: 用户名
                    - password: 密码
                    - pool_size: 连接池大小
                    - max_overflow: 连接池溢出上限
                    - ssl_mode: SSL 模式（volcano 模式为 require）
        """
        self._config = config
        self._engine = self._create_engine()
        # 连接失败熔断：失败后 60s 内快速失败，避免每次查询重复 2s 连接超时
        self._fuse_open_until = 0.0
        # 延迟导入 sqlalchemy.orm，避免骨架阶段依赖未安装导致模块加载失败
        from sqlalchemy.orm import sessionmaker
        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False
        )
        # v6.71 审计日志：当前操作者（供认证中间件调用设置）
        # A-5 修复：改用线程本地存储——实例级 _current_user 在多线程并发
        # 请求下会串扰（A 请求的设置被 B 覆盖，审计操作人记错）
        self._user_local = threading.local()

    def _connection_ok(self) -> bool:
        """连接熔断检查：熔断期内直接判定不可用（快速失败）。"""
        return time.time() >= self._fuse_open_until

    def _mark_connection_down(self, seconds: int = 60) -> None:
        """标记连接不可用，进入熔断期（无数据库环境下降级为内存/mock）。"""
        self._fuse_open_until = time.time() + seconds

    @staticmethod
    def _decode_conn_error(exc: Exception) -> str:
        """将连接异常转为可读消息。

        v6.78.3：libpq 返回的原始错误消息可能使用服务器系统编码（如 GBK 中文），
        psycopg2 按 utf-8 解码直接抛 UnicodeDecodeError，掩盖真实原因
        （如 "pg_hba.conf 没有该 IP 记录"）。此处取原始字节按 GBK 兜底解码，
        还原可读错误；无法解码时退回默认消息。
        """
        try:
            args = getattr(exc, "args", ()) or ()
            if len(args) >= 2 and isinstance(args[1], bytes):
                raw = args[1]
            elif isinstance(exc, UnicodeDecodeError) and exc.object:
                raw = bytes(exc.object)
            else:
                raw = b""
            if raw:
                return raw.decode("gbk", "replace").strip()
        except Exception:
            pass
        return f"{type(exc).__name__}: {exc}"

    @contextmanager
    def _connect(self) -> Generator[Any, None, None]:
        """统一连接入口：熔断检查 + 连接失败自动标记（后续查询快速失败）。"""
        if not self._connection_ok():
            raise ConnectionError("数据库连接不可用（熔断期内）")
        try:
            with self._engine.connect() as conn:
                yield conn
        except Exception as exc:
            # v6.57：仅连接类异常熔断（连接失败/超时等）；业务异常
            # （如表不存在 ProgrammingError / UndefinedTable）正常抛出由
            # 调用方处理，避免一次业务错误让整个数据库熔断 60s 造成后续
            # 全部查询快速失败（"流程实例不存在"等假象）。
            from sqlalchemy.exc import OperationalError as _OpErr
            if isinstance(exc, (ConnectionError, _OpErr)):
                self._mark_connection_down()
                print(f"[database] 连接失败进入熔断：{self._decode_conn_error(exc)}", flush=True)
            raise

    @classmethod
    def get_instance(cls, config: Optional[Dict[str, Any]] = None) -> "DatabaseManager":
        """
        获取单例实例

        参数:
            config: 配置字典（仅在首次初始化时需要）

        返回:
            DatabaseManager 单例
        """
        if cls._instance is None:
            if config is None:
                # 未显式传入配置时，从统一配置加载器获取数据库接口配置
                from prog.config.config_loader import get_config_loader
                config = get_config_loader().get_interface_config("database")
            cls._instance = cls(config)
        return cls._instance

    def _create_engine(self) -> Any:
        """
        创建 SQLAlchemy Engine

        根据配置创建带连接池的 engine：
            - local 模式：pool_size=10, max_overflow=20
            - volcano 模式：pool_size=20, max_overflow=40, 启用 SSL

        返回:
            SQLAlchemy Engine 实例
        """
        # 延迟导入 sqlalchemy，避免骨架阶段依赖未安装导致模块加载失败
        from sqlalchemy import create_engine

        cfg = self._config
        user = cfg.get("user", "")
        password = cfg.get("password", "")
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 5432)
        database = cfg.get("database", "ai_factory")

        # 连接 URL 格式: postgresql+psycopg2://user:password@host:port/database
        url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"

        pool_size = cfg.get("pool_size", 10)
        max_overflow = cfg.get("max_overflow", 20)

        # volcano 模式启用 SSL
        connect_args: Dict[str, Any] = {}
        if cfg.get("ssl_mode") == "require":
            connect_args["sslmode"] = "require"
        # v6.78.3：强制客户端编码 UTF-8——远程库若按 GBK 字节流返回（如库本身
        # 编码非 UTF8），psycopg2 按 utf-8 解码直接 UnicodeDecodeError（0xd6 等
        # 高字节），触发 DB 熔断、few-shot 注入 0 条致语义退化。显式指定
        # client_encoding 让服务端在连接建立时即按 UTF-8 转码。
        connect_args["client_encoding"] = "utf8"
        # 连接超时：DB 未部署/不可达时快速失败（配合 _mark_connection_down 熔断），
        # 避免首次连接长时间阻塞（如远程库不可达时单次尝试达数十秒）
        connect_args["connect_timeout"] = cfg.get("connect_timeout", 10)

        engine = create_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # 连接前检测有效性，避免使用已断开的连接
            pool_recycle=1800,   # D5：连接空闲 30 分钟回收，避免长连接被服务端/中间件断开后复用失效
            pool_timeout=10,     # D5：池满时等待 10s 超时报错，避免无限阻塞拖垮请求
            connect_args=connect_args,
        )
        return engine

    def get_session(self) -> Any:
        """
        获取数据库会话

        返回:
            SQLAlchemy Session 实例，调用方负责在用完后关闭
        """
        return self._session_factory()

    def _audit_write(self, action: str, table: str, data: dict = None, filters: dict = None) -> None:
        """写操作审计钩子（v6.71）：自动记录 insert/update/delete/execute 到 operation_logs。

        审计降级：写入异常时静默丢弃，不阻断主业务流程。
        审计表自身（operation_logs）不递归审计。
        """
        if table == "operation_logs":
            return  # 避免递归
        try:
            import json as _json
            from datetime import datetime
            details = {"table": table}
            if data:
                # 仅记录字段名，不记录值（避免敏感数据泄露到日志）
                details["fields"] = list(data.keys()) if isinstance(data, dict) else []
            if filters:
                details["filters"] = {k: str(v)[:50] for k, v in filters.items()} if isinstance(filters, dict) else {}
            # v6.74：无操作者时 user_id 写 NULL（而非 "system"）——users 表无
            # system 用户，写 "system" 违反 fk_operation_logs_user 外键导致审计
            # 静默丢失（try/except 吞掉）；NULL 不违反外键，保证无登录上下文
            # （MCP stdio / 后台任务）写库时审计仍留痕
            row = {
                "user_id": getattr(getattr(self, "_user_local", None), "user", "") or None,
                "action": f"db_{action}",
                "details": details,
                "extra_data": {"timestamp": datetime.now().isoformat()},
            }
            # 直接用底层连接写入，不走 self.insert（避免递归）
            from sqlalchemy import text
            with self._connect() as conn:
                conn.execute(text(
                    "INSERT INTO operation_logs (user_id, action, details, extra_data) "
                    "VALUES (:user_id, :action, :details, :extra_data)"
                ), {
                    "user_id": row["user_id"],
                    "action": row["action"],
                    "details": _json.dumps(details, ensure_ascii=False, default=str),
                    "extra_data": _json.dumps(row["extra_data"], ensure_ascii=False, default=str),
                })
                conn.commit()
        except Exception as e:
            # O4：审计降级必须告警（不阻断业务，但不可静默吞错）
            import logging
            _audit_logger = logging.getLogger("prog.core.database")
            _audit_logger.error("审计写入失败（降级不阻断业务）: %s", e,
                                exc_info=True)

    def set_current_user(self, user_id: str) -> None:
        """设置当前操作者（供认证中间件调用；线程本地，接口语义不变）。"""
        self._user_local.user = user_id

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        执行原生 SQL 语句

        参数:
            sql: SQL 语句（支持命名参数占位符）
            params: 参数字典

        返回:
            执行结果
        """
        # 延迟导入 sqlalchemy.text
        from sqlalchemy import text

        with self._connect() as conn:
            result = conn.execute(text(sql), params or {})
            conn.commit()
            _sql_lower = sql.lower()
            if any(kw in _sql_lower for kw in ("insert", "update", "delete", "drop", "truncate", "alter")):
                self._audit_write("execute", "", {"sql": sql[:200]})
            return result

    @contextmanager
    def transaction(self) -> Generator[Any, None, None]:
        """
        事务上下文管理器

        用法:
            with db.transaction() as session:
                session.add(obj)
                # 退出 with 块时自动提交，异常时自动回滚

        返回:
            Session 上下文生成器
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def query_one(
        self,
        table: str,
        filters: Dict[str, Any],
        columns: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        查询单条记录

        参数:
            table: 表名
            filters: 过滤条件字典
            columns: 查询列名列表（None 表示全部列）

        返回:
            单条记录字典，无结果时返回 None
        """
        from sqlalchemy import text

        # D14 修复：SELECT 列名白名单校验（防 SQL 注入）
        self._validate_columns(columns)
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table}"
        params: Dict[str, Any] = {}
        if filters:
            where = " AND ".join(f"{k} = :{k}" for k in filters)
            sql += f" WHERE {where}"
            params = dict(filters)
        sql += " LIMIT 1"

        with self._connect() as conn:
            row = conn.execute(text(sql), params).fetchone()
            return dict(row._mapping) if row else None

    def query_many(
        self,
        table: str,
        filters: Optional[Dict[str, Any]] = None,
        columns: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询多条记录

        参数:
            table: 表名
            filters: 过滤条件字典
            columns: 查询列名列表
            limit: 返回记录上限
            offset: 偏移量（分页）
            order_by: 排序字段

        返回:
            记录字典列表
        """
        from sqlalchemy import text

        # D14 修复：SELECT 列名 / ORDER BY 白名单校验（防 SQL 注入）
        self._validate_columns(columns)
        self._validate_order_by(order_by)
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table}"
        params: Dict[str, Any] = {}
        if filters:
            where = " AND ".join(f"{k} = :{k}" for k in filters)
            sql += f" WHERE {where}"
            params = dict(filters)
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        with self._connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
            return [dict(r._mapping) for r in rows]

    def count(self, table: str, filters: Optional[Dict[str, Any]] = None) -> int:
        """统计记录数（D12：COUNT(*) 聚合，避免全表拉取后 len() 计数）。

        参数:
            table: 表名
            filters: 过滤条件字典

        返回:
            匹配记录总数（int）
        """
        from sqlalchemy import text

        sql = f"SELECT COUNT(*) AS cnt FROM {table}"
        params: Dict[str, Any] = {}
        if filters:
            where = " AND ".join(f"{k} = :{k}" for k in filters)
            sql += f" WHERE {where}"
            params = dict(filters)
        with self._connect() as conn:
            row = conn.execute(text(sql), params).fetchone()
            return int(row[0]) if row else 0

    # 条件过滤支持的操作符（v6.65 查询附加词）——字段名白名单防注入
    _OP_SQL = {
        "eq": "=", "ne": "<>", "gt": ">", "gte": ">=",
        "lt": "<", "lte": "<=", "like": "LIKE", "between": "BETWEEN",
        "in": "IN",
    }
    _FIELD_NAME_RE = None
    _ORDER_BY_RE = None

    @classmethod
    def _validate_columns(cls, columns: Optional[List[str]]) -> None:
        """D14 修复：SELECT 列名白名单校验（防 SQL 注入）。

        非法列名抛 ValueError，阻断拼入 SQL。仅校验 SELECT 列；
        filters 键已由参数化占位符（:k）保证安全。
        """
        if not columns:
            return
        if cls._FIELD_NAME_RE is None:
            import re
            cls._FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
        for c in columns:
            c = str(c).strip()
            if not cls._FIELD_NAME_RE.match(c):
                raise ValueError(f"非法查询列名：{c}")

    @classmethod
    def _validate_order_by(cls, order_by: Optional[str]) -> None:
        """D14 修复：ORDER BY 白名单校验（防 SQL 注入）。

        支持「列名[.表前缀][ ASC|DESC]」以逗号分隔的多字段排序；
        非法排序表达式抛 ValueError。
        """
        if not order_by:
            return
        if cls._ORDER_BY_RE is None:
            import re
            cls._ORDER_BY_RE = re.compile(
                r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?(\s+(ASC|DESC))?$",
                re.IGNORECASE)
        for part in str(order_by).split(","):
            if not cls._ORDER_BY_RE.match(part.strip()):
                raise ValueError(f"非法排序字段：{part}")

    def query_filtered(
        self,
        table: str,
        conditions: List[Dict[str, Any]],
        columns: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """条件过滤查询（v6.65）：支持比较/范围/模糊操作符。

        与 query_many（等值过滤）的区别：conditions 为操作符条件列表，
        每项 {"field": "列名", "op": "gt|gte|lt|lte|eq|ne|like|between",
              "value": 值}。字段名白名单校验（^[a-zA-Z_][a-zA-Z0-9_]*$）
        防止配置注入；between 的 value 为 [lo, hi] 两元组/列表。

        参数:
            table: 表名
            conditions: 操作符条件列表
            columns: 查询列名列表（None 表示全部列）
            limit: 返回记录上限
            offset: 偏移量（分页）
            order_by: 排序字段（如 "created_at DESC"）

        返回:
            记录字典列表
        """
        from sqlalchemy import text

        # D14 修复：SELECT 列名 / ORDER BY 白名单校验（防 SQL 注入）
        self._validate_columns(columns)
        self._validate_order_by(order_by)
        cols = ", ".join(columns) if columns else "*"
        sql = f"SELECT {cols} FROM {table}"
        params: Dict[str, Any] = {}
        if conditions:
            if DatabaseManager._FIELD_NAME_RE is None:
                import re
                DatabaseManager._FIELD_NAME_RE = re.compile(
                    r"^[a-zA-Z_][a-zA-Z0-9_]*$")
            where_parts = []
            for i, c in enumerate(conditions):
                field = str(c.get("field", ""))
                if not DatabaseManager._FIELD_NAME_RE.match(field):
                    raise ValueError(f"非法查询字段名：{field}")
                op = str(c.get("op", "eq"))
                op_sql = DatabaseManager._OP_SQL.get(op)
                if op_sql is None:
                    raise ValueError(f"不支持的操作符：{op}")
                pname = f"f{i}"
                if op == "between":
                    vals = c.get("value")
                    if not (isinstance(vals, (list, tuple)) and len(vals) == 2):
                        raise ValueError("between 条件的 value 须为 [lo, hi]")
                    where_parts.append(
                        f"{field} BETWEEN :{pname}_0 AND :{pname}_1")
                    params[f"{pname}_0"] = vals[0]
                    params[f"{pname}_1"] = vals[1]
                elif op == "in":
                    vals = c.get("value")
                    if not isinstance(vals, (list, tuple)) or not vals:
                        raise ValueError("in 条件的 value 须为非空列表")
                    placeholders = ", ".join(
                        f":{pname}_{j}" for j in range(len(vals)))
                    where_parts.append(f"{field} IN ({placeholders})")
                    for j, v in enumerate(vals):
                        params[f"{pname}_{j}"] = v
                else:
                    where_parts.append(f"{field} {op_sql} :{pname}")
                    params[pname] = c.get("value")
            if where_parts:
                sql += " WHERE " + " AND ".join(where_parts)
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"

        with self._connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
            return [dict(r._mapping) for r in rows]

    def insert(self, table: str, data: Dict[str, Any]) -> Any:
        """
        插入单条记录

        参数:
            table: 表名
            data: 待插入数据字典

        返回:
            新插入记录的主键
        """
        from sqlalchemy import text
        import json as _json

        # JSONB 列（metadata 等）值为 dict/list 时自动序列化为 JSON 字符串，
        # 避免 psycopg2 "can't adapt type 'dict'" 错误
        _data = {
            k: (_json.dumps(v, ensure_ascii=False, default=str)
                if isinstance(v, (dict, list)) else v)
            for k, v in data.items()
        }

        cols = list(_data.keys())
        col_str = ", ".join(cols)
        val_str = ", ".join(f":{c}" for c in cols)
        # 使用 RETURNING * 返回新插入的完整行，便于获取主键
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({val_str}) RETURNING *"

        with self._connect() as conn:
            result = conn.execute(text(sql), _data)
            conn.commit()
            self._audit_write("insert", table, data)
            row = result.fetchone()
            if row:
                row_dict = dict(row._mapping)
                # 返回第一个字段值作为主键（大多数表主键为首列）
                return next(iter(row_dict.values()))
            return None

    def update(
        self,
        table: str,
        data: Dict[str, Any],
        filters: Dict[str, Any],
    ) -> int:
        """
        更新记录

        参数:
            table: 表名
            data: 待更新字段字典
            filters: 过滤条件字典

        返回:
            受影响的行数
        """
        from sqlalchemy import text
        import json as _json

        # JSONB 列值为 dict/list 时自动序列化为 JSON 字符串（与 insert 一致）
        _data = {
            k: (_json.dumps(v, ensure_ascii=False, default=str)
                if isinstance(v, (dict, list)) else v)
            for k, v in data.items()
        }

        # 使用 set_ 前缀避免与过滤条件参数名冲突
        set_clause = ", ".join(f"{k} = :set_{k}" for k in _data)
        params: Dict[str, Any] = {f"set_{k}": v for k, v in _data.items()}
        where = " AND ".join(f"{k} = :{k}" for k in filters)
        params.update(filters)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where}"

        with self._connect() as conn:
            result = conn.execute(text(sql), params)
            conn.commit()
            self._audit_write("update", table, data, filters)
            return result.rowcount

    def delete(self, table: str, filters: Dict[str, Any]) -> int:
        """
        删除记录

        参数:
            table: 表名
            filters: 过滤条件字典

        返回:
            受影响的行数
        """
        from sqlalchemy import text

        where = " AND ".join(f"{k} = :{k}" for k in filters)
        sql = f"DELETE FROM {table} WHERE {where}"

        with self._connect() as conn:
            result = conn.execute(text(sql), filters)
            conn.commit()
            self._audit_write("delete", table, None, filters)
            return result.rowcount

    def save_jsonb(
        self,
        table: str,
        record_id: Any,
        jsonb_column: str,
        json_data: Dict[str, Any],
    ) -> bool:
        """
        保存 JSONB 字段数据（§1.4.2 训练数据存储）

        PostgreSQL 的 JSONB 字段用于存储训练数据等半结构化数据，
        支持高效的 JSON 查询与索引。

        参数:
            table: 表名
            record_id: 记录主键
            jsonb_column: JSONB 列名
            json_data: 待保存的 JSON 数据

        返回:
            True 表示保存成功
        """
        from sqlalchemy import text

        # 将 JSON 数据序列化后通过 CAST 转为 jsonb 类型，直接覆盖原值
        sql = (
            f"UPDATE {table} SET {jsonb_column} = CAST(:json_data AS jsonb) "
            f"WHERE id = :record_id"
        )
        params = {
            "json_data": json.dumps(json_data, ensure_ascii=False),
            "record_id": record_id,
        }

        with self._connect() as conn:
            result = conn.execute(text(sql), params)
            conn.commit()
            return result.rowcount > 0

    def query_jsonb(
        self,
        table: str,
        jsonb_column: str,
        json_path: str,
        json_value: Any,
    ) -> List[Dict[str, Any]]:
        """
        查询 JSONB 字段

        利用 PostgreSQL 的 JSONB 查询能力，按 JSON 路径过滤记录。

        参数:
            table: 表名
            jsonb_column: JSONB 列名
            json_path: JSON 路径表达式（如 "training_data.status"）
            json_value: 待匹配的值

        返回:
            匹配的记录列表
        """
        from sqlalchemy import text

        # 拆分 JSON 路径，若首段与列名相同则跳过
        parts = json_path.split(".")
        if parts and parts[0] == jsonb_column:
            parts = parts[1:]
        if not parts:
            return []

        # 构建 JSONB 路径表达式：中间层级用 -> ，末层用 ->> 返回文本
        expr = jsonb_column
        for i, p in enumerate(parts):
            if i == len(parts) - 1:
                expr = f"{expr} ->> '{p}'"
            else:
                expr = f"{expr} -> '{p}'"

        sql = f"SELECT * FROM {table} WHERE {expr} = :json_value"
        params = {"json_value": str(json_value)}

        with self._connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
            return [dict(r._mapping) for r in rows]


def get_database() -> DatabaseManager:
    """
    模块级便捷函数：获取数据库管理器单例

    同步注册到开源框架的数据库注入点（runtime.database），
    使框架组件（审核链归档 / 规则配置加载）读取同一数据库实例。

    返回:
        DatabaseManager 单例实例
    """
    db = DatabaseManager.get_instance()
    try:
        import prog.runtime.database as _rt_db
        _rt_db.set_database(db)
    except Exception:
        pass
    return db


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、核心类定义、基本结构完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert DatabaseManager is not None, "DatabaseManager 类未定义"
    hello_world(__name__, "核心类定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
