"""
数据库初始化脚本

文件用途：
    一键初始化AI工厂管家的全部数据存储依赖：PostgreSQL表结构、
    种子数据、Milvus向量库Collection、Redis基础键。

对应技术规格章节：
    - §1.7 数据库表结构设计
    - §1.4.2 训练数据存储（PostgreSQL JSONB）
    - §A.0 系统配置和错误码定义（种子数据）

替代demo：
    替代 demo 无显式初始化脚本、表结构与种子数据散落的现状。
    demo依赖data_manager.py运行时懒加载，缺乏可重复的初始化流程。

初始化步骤说明：
    1. 增量执行 migrations/ 目录下SQL文件（D4 版本管理）
       - schema_migrations 版本表记录已应用版本（文件名去 .sql 为版本号）
       - 已应用版本重跑自动跳过；存量库（users 表在）首次运行回填全部为已应用
       - 每文件独立事务，失败整体回滚并中断
    2. 插入种子数据（已包含在004/005 SQL中）
    3. 创建Milvus Collection（向量库，用于知识库与RAG）
       - collection: ai_factory_knowledge（dim=1024，IVF索引）
       - collection: ai_factory_training（dim=1024，用于训练样本召回）
    4. 初始化Redis
       - flushdb（仅开发环境）
       - 写入默认配置键（如 system:llm_config）

运行：
    python prog/scripts/init_db.py --reset
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

# 路径引导：支持 `python prog/scripts/init_db.py` 直接运行
# （脚本目录 prog/scripts 不在包路径内，需将项目根加入 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# D4 存量库回填上限：引入 schema_migrations 版本管理（随 075 一同发布）之前，
# 历史库最后执行的迁移为 074。存量库首次运行版本管理时只回填 ≤074 的文件；
# 075+ 的新迁移正常增量执行（避免回填把新迁移一并标记为已应用而跳过）。
_LEGACY_BACKFILL_MAX = 74


def _version_number(stem: str) -> "int | None":
    """取文件名前缀数字（"075_xxx" -> 75）；无数字前缀返回 None。"""
    head = stem.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def _applied_versions(cur: Any) -> set:
    """读取 schema_migrations 已应用版本集合（表不存在时自动建表）。"""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    VARCHAR(128) PRIMARY KEY,
            filename   VARCHAR(255) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    cur.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in cur.fetchall()}


def run_migrations(migrations_dir: str, db_config: Dict[str, Any],
                   reset: bool = False) -> None:
    """按版本号增量执行 migrations 目录下所有 SQL 文件（D4 版本管理）。

    参数：
        migrations_dir: migrations目录绝对路径
        db_config: 数据库连接配置（host/port/user/password/database）
        reset: 是否为 --reset 全新初始化（不做存量库回填）

    说明：
        1. 版本表 schema_migrations(version PK, filename, applied_at)：
           以迁移文件名（去 .sql）为版本号，重复编号文件（019/020/034 各两个）
           以完整文件名区分互不冲突。
        2. 存量库回填：版本表为空但业务表已存在（users 表在）时，视为
           001~074 已手工/历史执行过并回填标记，仅增量执行 075+ 新迁移；
           --reset 重建库不回填。
        3. 每个文件独立事务（执行 SQL + 写版本行同 commit），失败整体回滚
           并中断（替代原 autocommit 逐句提交、中途失败无法回滚）。
        4. 已应用版本直接跳过（验收：重跑 init_db 跳过已执行版本）。
    """
    migrations_path = Path(migrations_dir)
    if not migrations_path.exists():
        print(f"[ERROR] 迁移目录不存在: {migrations_dir}")
        return

    # 按文件名排序获取所有 .sql 文件
    sql_files = sorted(migrations_path.glob("*.sql"))
    if not sql_files:
        print(f"[WARN] 迁移目录中无SQL文件: {migrations_dir}")
        return

    print(f"[INFO] 发现 {len(sql_files)} 个迁移文件")

    # 使用 psycopg2 原生连接执行（simple query 协议）：
    # - 支持多语句一次执行，且不解析 :name / % 占位符，
    #   避免 JSONB 种子数据中的 ":1"/":true" 被 SQLAlchemy text() 误判为绑定参数
    import psycopg2

    conn = psycopg2.connect(
        host=db_config.get("host", "127.0.0.1"),
        port=db_config.get("port", 5432),
        user=db_config.get("user", ""),
        password=db_config.get("password", ""),
        dbname=db_config.get("database", "ai_factory"),
    )
    # 非 autocommit：每文件一个事务，失败可回滚（D4）
    conn.autocommit = False
    cur = conn.cursor()

    try:
        applied = _applied_versions(cur)
        conn.commit()

        if not applied and not reset:
            # 存量库回填：版本表为空但业务表已存在 -> 历史迁移全部标记已应用
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'users'")
            if cur.fetchone():
                print(f"[INFO] 检测到存量库（users 表已存在）：回填 001~{_LEGACY_BACKFILL_MAX} 为已应用")
                for sql_file in sql_files:
                    num = _version_number(sql_file.stem)
                    if num is None or num > _LEGACY_BACKFILL_MAX:
                        continue  # 版本管理发布之后的新迁移：正常增量执行
                    cur.execute(
                        "INSERT INTO schema_migrations (version, filename) "
                        "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                        (sql_file.stem, sql_file.name))
                conn.commit()
                applied = _applied_versions(cur)
                conn.commit()

        skipped = executed = 0
        for sql_file in sql_files:
            version = sql_file.stem
            if version in applied:
                skipped += 1
                print(f"[INFO] 跳过（已应用）: {sql_file.name}")
                continue
            print(f"[INFO] 执行: {sql_file.name} ...", end=" ")
            try:
                sql_content = sql_file.read_text(encoding="utf-8")
                cur.execute(sql_content)
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename) "
                    "VALUES (%s, %s)",
                    (version, sql_file.name))
                conn.commit()
                executed += 1
                print("OK")
            except Exception as e:
                conn.rollback()
                print(f"FAILED: {e}")
                print(f"[ERROR] 迁移失败于 {sql_file.name}，已回滚并中断")
                raise

        print(f"[OK] 迁移执行完成：新执行 {executed} 个，跳过已应用 {skipped} 个")
    finally:
        conn.close()


def create_milvus_collections(milvus_client: Any) -> None:
    """创建Milvus Collection。

    说明：
        创建 ai_factory_knowledge 与 ai_factory_training 两个Collection，
        维度1024，使用IVF索引 + cosine度量。
    """
    from pymilvus import Collection, CollectionSchema, FieldSchema, DataType, utility

    collections_config = [
        {
            "name": "ai_factory_knowledge",
            "description": "企业管理知识库向量集合",
        },
        {
            "name": "ai_factory_training",
            "description": "训练样本向量集合",
        },
    ]

    dim = 1024  # bge-m3 / Doubao Embedding 维度

    for config in collections_config:
        name = config["name"]
        desc = config["description"]

        # 检查是否已存在
        if utility.has_collection(name):
            print(f"[INFO] Milvus Collection '{name}' 已存在，跳过创建")
            continue

        print(f"[INFO] 创建 Milvus Collection: {name} ...", end=" ")

        # 定义字段
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]

        schema = CollectionSchema(fields=fields, description=desc)
        collection = Collection(name=name, schema=schema)

        # 创建IVF索引
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 1024},
        }
        collection.create_index(field_name="embedding", index_params=index_params)
        collection.load()

        print("OK")

    print("[OK] Milvus Collections 创建完成")


def init_redis(redis_client: Any, flush: bool = False) -> None:
    """初始化Redis。

    参数：
        redis_client: Redis 客户端实例
        flush: 是否清空当前db（仅开发环境使用）
    """
    if flush:
        redis_client.flushdb()
        print("[INFO] Redis db 已清空（开发模式）")

    # 写入默认配置键
    default_configs = {
        "system:llm_config": '{"model": "doubao-seed-1-6-250615", "temperature": 0.3, "max_tokens": 4096}',
        "system:cache_ttl": "3600",
        "system:rate_limit": "100",
    }

    for key, value in default_configs.items():
        redis_client.set(key, value)

    print(f"[OK] Redis 初始化完成，写入 {len(default_configs)} 个默认键")


def verify_initialization(db: Any, milvus_client: Any = None,
                          redis_client: Any = None) -> bool:
    """验证初始化结果（表存在、Collection存在、Redis可写）。

    参数：
        db: DatabaseManager 实例
        milvus_client: Milvus 客户端（可选）
        redis_client: Redis 客户端（可选）

    返回：
        True 表示全部验证通过
    """
    all_ok = True

    # 验证 PostgreSQL 表
    expected_tables = [
        "products", "customers", "orders", "inventory",
        "production_lines", "qc_records", "notifications", "operation_logs",
        "roles", "users",
        "bom", "process_routes", "work_orders",
        "business_rules", "workflow_configs",
    ]

    print("[INFO] 验证 PostgreSQL 表 ...")
    for table in expected_tables:
        try:
            result = db.query_one(
                "information_schema.tables",
                {"table_name": table, "table_schema": "public"},
            )
            if result:
                print(f"  ✅ {table}")
            else:
                print(f"  ❌ {table} (不存在)")
                all_ok = False
        except Exception as e:
            # 降级：直接查询
            try:
                db.execute(f"SELECT 1 FROM {table} LIMIT 1")
                print(f"  ✅ {table}")
            except Exception:
                print(f"  ❌ {table} (查询失败: {e})")
                all_ok = False

    # 验证 Milvus Collection
    if milvus_client is not None:
        print("[INFO] 验证 Milvus Collection ...")
        try:
            from pymilvus import utility
            for name in ["ai_factory_knowledge", "ai_factory_training"]:
                if utility.has_collection(name):
                    print(f"  ✅ {name}")
                else:
                    print(f"  ❌ {name} (不存在)")
                    all_ok = False
        except Exception as e:
            print(f"  ⚠️ Milvus 验证跳过: {e}")

    # 验证 Redis
    if redis_client is not None:
        print("[INFO] 验证 Redis ...")
        try:
            redis_client.set("_verify_init", "ok")
            val = redis_client.get("_verify_init")
            if val == b"ok" or val == "ok":
                print("  ✅ Redis 读写正常")
                redis_client.delete("_verify_init")
            else:
                print("  ❌ Redis 读写异常")
                all_ok = False
        except Exception as e:
            print(f"  ❌ Redis 连接失败: {e}")
            all_ok = False

    return all_ok


def main() -> None:
    """主入口。

    解析参数：
        --reset: 清空现有数据后重新初始化（危险，需二次确认）
        --skip-milvus: 跳过Milvus初始化
        --skip-redis: 跳过Redis初始化
    执行初始化并输出验证报告。
    """
    # Windows GBK 控制台兼容：✅/❌ emoji 在 cp936 下 UnicodeEncodeError 中断验证
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="AI工厂管家数据库初始化")
    parser.add_argument("--reset", action="store_true", help="清空现有数据后重新初始化")
    parser.add_argument("--skip-milvus", action="store_true", help="跳过Milvus初始化")
    parser.add_argument("--skip-redis", action="store_true", help="跳过Redis初始化")
    args = parser.parse_args()

    print("=" * 60)
    print("AI工厂管家 - 数据库初始化")
    print("=" * 60)

    # --reset 二次确认
    if args.reset:
        confirm = input("⚠️  --reset 将清空现有数据！确认输入 'YES': ")
        if confirm != "YES":
            print("[INFO] 用户取消操作")
            return

    # 获取项目根目录
    project_root = Path(__file__).parent.parent.parent
    migrations_dir = str(Path(__file__).parent.parent / "migrations")

    # 1. 初始化 PostgreSQL
    print("\n[1/4] 初始化 PostgreSQL ...")
    try:
        from prog.config.config_loader import ConfigLoader
        from prog.core.database import DatabaseManager

        config_loader = ConfigLoader()
        db_config = config_loader.get_interface_config("database")
        db = DatabaseManager(db_config)

        if args.reset:
            # 删除现有表（危险操作）
            print("[INFO] 清空现有表 ...")
            drop_sql = """
                DROP SCHEMA public CASCADE;
                CREATE SCHEMA public;
                GRANT ALL ON SCHEMA public TO postgres;
                GRANT ALL ON SCHEMA public TO public;
            """
            try:
                db.execute(drop_sql)
            except Exception as e:
                print(f"[WARN] 清空操作失败: {e}")

        run_migrations(migrations_dir, db_config, reset=args.reset)
    except Exception as e:
        print(f"[ERROR] PostgreSQL 初始化失败: {e}")
        print("[INFO] 请确认 PostgreSQL 已启动且配置正确")
        return

    # 2. 初始化 Milvus
    milvus_client = None
    if not args.skip_milvus:
        print("\n[2/4] 初始化 Milvus ...")
        try:
            from pymilvus import connections
            milvus_config = config_loader.get_minio_config()  # 复用部署配置中的host
            milvus_host = os.environ.get("MILVUS_HOST", "127.0.0.1")
            milvus_port = os.environ.get("MILVUS_PORT", "19530")
            connections.connect(host=milvus_host, port=milvus_port)
            create_milvus_collections(None)
            milvus_client = True  # 标记为已初始化
        except ImportError:
            print("[WARN] pymilvus 未安装，跳过 Milvus 初始化")
        except Exception as e:
            print(f"[WARN] Milvus 初始化失败: {e}")
    else:
        print("\n[2/4] 跳过 Milvus 初始化")

    # 3. 初始化 Redis
    redis_client = None
    if not args.skip_redis:
        print("\n[3/4] 初始化 Redis ...")
        try:
            import redis
            redis_host = os.environ.get("REDIS_HOST", "127.0.0.1")
            redis_port = int(os.environ.get("REDIS_PORT", "6379"))
            redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
            init_redis(redis_client, flush=args.reset)
        except ImportError:
            print("[WARN] redis 未安装，跳过 Redis 初始化")
        except Exception as e:
            print(f"[WARN] Redis 初始化失败: {e}")
    else:
        print("\n[3/4] 跳过 Redis 初始化")

    # 4. 验证初始化结果
    print("\n[4/4] 验证初始化结果 ...")
    all_ok = verify_initialization(db, milvus_client, redis_client)

    print("\n" + "=" * 60)
    if all_ok:
        print("✅ 数据库初始化完成！所有组件验证通过。")
    else:
        print("⚠️  数据库初始化完成，但部分验证未通过，请检查上方输出。")
    print("=" * 60)


if __name__ == "__main__":
    main()
