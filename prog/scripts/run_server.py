"""
生产服务器启动脚本

文件用途：
    生产环境启动AI工厂管家Flask服务，替代demo的run_server.py。
    根据操作系统选择WSGI服务器（Windows用Waitress，Linux用Gunicorn）。

对应技术规格章节：
    - §1.3 系统整体架构（Web层 + Agent层 + 数据层）
    - §A.0 系统配置

替代demo：
    替代 demo run_server.py 直接 app.run() 开发服务器启动的现状。
    demo的开发服务器无法用于生产，无并发能力且无优雅退出。

启动步骤说明：
    1. 加载配置
       - 从config.py或环境变量读取（DB/Redis/Milvus/LLM配置）
       - 区分dev/prod环境
    2. 初始化Flask app与CORS
    3. 注册Blueprint
       - /api/chat（对话）
       - /api/data（数据CRUD）
       - /api/llm（LLM配置）
       - /api/monitor（监控）
       - /api/auth（登录鉴权）
    4. 初始化核心组件
       - Database（PostgreSQL连接池）
       - VectorStore（Milvus客户端）
       - EventBus（事件总线）
       - SkillRegistry（注册默认文件技能）
       - HookEngine（注册默认Hook）
       - SessionManager（Redis会话）
    5. 启动WSGI服务器
       - Windows: Waitress（多线程，listen=*:5000）
       - Linux: Gunicorn（多worker + uvicorn/gevent worker）
       - 优雅退出：监听SIGTERM，关闭连接池与Milvus连接

运行：
    python prog/scripts/run_server.py --host 0.0.0.0 --port 5000 --workers 4
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

# 路径引导：支持 `python prog/scripts/run_server.py` 直接运行
# （脚本目录 prog/scripts 不在包路径内，需将项目根加入 sys.path）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from prog.config.config_loader import ConfigLoader

# v6.99.1 配置加载顺序修复：.env 必须在本模块导入任何“导入时快照环境变量”的
# 框架模块（prog.runtime.debug 的 DEBUG 在模块导入时读取 RUNTIME_DEBUG）之前写入
# os.environ——否则 .env 中的 RUNTIME_DEBUG=1 不生效（DEBUG 恒 False → A-2 门禁
# 在登录入口拦截弱 JWT_SECRET 返回 500）。config_loader 的 load_config() 在 main()
# 中才执行，晚于本模块顶部导入，故在此提前调用其 _load_env_file()。
ConfigLoader.get_instance()._load_env_file()

from prog.runtime.debug import DEBUG
from prog.config.config_validator import ConfigValidator
from prog.scripts.deploy_check import (
    check_dependencies,
    install_dependencies,
    probe_and_hint_services,
)

_logger = logging.getLogger("prog.run_server")

# 配置校验错误类别 -> 需交互补齐的环境变量
ENV_FIX_MAP = {
    "llm": ["LLM_API_KEY"],
    "database": ["DB_USER", "DB_PASSWORD"],
    "minio": ["MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"],
}


def _write_env_file(path: str, updates: Dict[str, str]) -> None:
    """将配置项写入 .env（已存在则更新对应行，否则追加），保留原有注释/顺序"""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    for key, value in updates.items():
        marker = f"{key}="
        replaced = False
        for i, line in enumerate(lines):
            if line.startswith(marker):
                lines[i] = f"{marker}{value}\n"
                replaced = True
                break
        if not replaced:
            lines.append(f"{marker}{value}\n")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _interactive_fix_config(errors: Dict[str, List[str]], env_path: str,
                            loader: ConfigLoader) -> bool:
    """正式模式配置缺失时的交互式引导：逐项提示输入并写入 .env，复验通过返回 True。

    非交互终端（无 tty）时直接返回 False，保持 fail-fast 语义。
    """
    if not sys.stdin.isatty():
        return False
    _logger.info("检测到配置缺失，是否进入交互式配置引导？（y/n）")
    try:
        if input().strip().lower() not in ("y", "yes"):
            return False
    except EOFError:
        return False

    env_vars = []
    for category in ("llm", "database", "minio"):
        if category in errors:
            env_vars.extend(ENV_FIX_MAP.get(category, []))
    env_vars = sorted(set(env_vars))
    if not env_vars:
        return False

    updates: Dict[str, str] = {}
    for var in env_vars:
        hint = ""
        if var == "LLM_API_KEY":
            hint = "（火山引擎方舟 API Key）"
        elif var.endswith(("PASSWORD", "SECRET_KEY", "ROOT_PASSWORD")):
            hint = "（将明文写入 .env，请妥善保管）"
        try:
            value = input(f"请输入 {var}{hint}（回车跳过）: ").strip()
        except EOFError:
            break
        if value:
            updates[var] = value

    if not updates:
        _logger.warning("未输入任何配置，放弃引导")
        return False

    _write_env_file(env_path, updates)
    for key, value in updates.items():
        os.environ.setdefault(key, value)
    loader.load_config(force=True)
    errors_after = ConfigValidator(loader).validate_all()
    if errors_after:
        print("[ERROR] 补齐后仍有配置缺失：")
        print(ConfigValidator(loader).get_validation_report())
        return False
    print("[OK] 配置补齐并通过校验")
    return True


def load_config(env: str = "prod") -> dict:
    """加载配置。

    参数：
        env: 环境（'dev'/'prod'）

    返回：
        配置字典
    """
    # 从统一配置加载器获取配置
    try:
        from prog.config.config_loader import get_config_loader
        loader = get_config_loader()
        deployment_config = loader.load_config()
    except Exception:
        deployment_config = {}

    # 从Settings获取Flask应用配置
    try:
        from prog.config.settings import Settings
        app_config = {
            key: getattr(Settings, key)
            for key in dir(Settings)
            if not key.startswith("_") and key.isupper()
        }
    except Exception:
        app_config = {
            "APP_ENV": env,
            "SECRET_KEY": os.environ.get("APP_SECRET_KEY", "dev-secret-key-change-me"),
            "JSON_AS_ASCII": False,
        }

    # 合并配置
    config: Dict[str, Any] = {
        "env": env,
        "app": app_config,
        "deployment": deployment_config,
    }
    return config


def _register_error_handlers(app: object) -> None:
    """S4/S5：全局错误处理——404/405/413/500/未捕获异常统一 JSON 响应。

    - 生产（APP_ENV=production 且非 DEBUG）：响应不含内部异常细节（str(e) 可能
      泄露 DB 连接串/SQL/路径），详细堆栈仅写入服务端日志（S4）。
    - 其他 HTTPException（如 abort(400)）：透传其状态码，避免被 500 兜底误吞（S5）。
    """
    from flask import jsonify
    from werkzeug.exceptions import HTTPException
    from prog.runtime.trace import get_trace_id

    _logger = logging.getLogger("prog.run_server")

    def _payload(code: int, msg: str) -> dict:
        return {"code": code, "msg": msg, "trace_id": get_trace_id()}

    @app.errorhandler(404)
    def _err_404(e):
        return jsonify(_payload(404, "资源不存在")), 404

    @app.errorhandler(405)
    def _err_405(e):
        return jsonify(_payload(405, "请求方法不允许")), 405

    @app.errorhandler(413)
    def _err_413(e):
        return jsonify(_payload(413, "请求体超过大小限制")), 413

    @app.errorhandler(500)
    def _err_500(e):
        _logger.error("500 内部错误", exc_info=(type(e), e, e.__traceback__))
        return jsonify(_payload(500, "内部错误")), 500

    @app.errorhandler(Exception)
    def _err_exception(e):
        # 非 404/405/413 的 HTTP 异常（abort 主动抛出）透传原状态码，不做 500 兜底
        if isinstance(e, HTTPException):
            return jsonify(_payload(e.code, e.name or "请求错误")), e.code
        _logger.error("未捕获异常", exc_info=(type(e), e, e.__traceback__))
        debug = app.config.get("APP_ENV") in ("development", "dev") or getattr(app, "debug", False)
        msg = f"内部错误: {e}" if debug else "内部错误"
        return jsonify(_payload(500, msg)), 500


def _register_health_routes(app: object) -> None:
    """S6：匿名健康探针——K8s/部署脚本无需 token 即可访问。

    - GET /health：liveness，仅返回进程存活与时间戳，不依赖外部组件。
    - GET /ready：readiness——数据库为硬门禁（不可用即 503 未就绪）；
      Redis/Milvus 为软依赖（应用设计为降级内存/关键词检索仍可用），
      不可用时返回 200 但 checks 中标记 degraded，便于排障与告警区分。
    路由不挂 /api 前缀，认证中间件天然不拦截；AUTH_EXEMPT_PATHS 亦已加入。
    """
    from flask import jsonify
    from datetime import datetime

    @app.route("/health")
    def _health():
        return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

    @app.route("/ready")
    def _ready():
        checks = {"database": False, "redis": False, "milvus": False}
        try:
            from prog.core.database import get_database
            _db = get_database()
            if _db is not None:
                _db.query_one("inventory", {"product_code": "__ready_probe__"})
                checks["database"] = True
        except Exception:
            checks["database"] = False
        try:
            from prog.runtime.cache import get_cache
            checks["redis"] = get_cache().ping()
        except Exception:
            checks["redis"] = False
        try:
            from prog.core.vector_store import MilvusVectorStore
            _vs = MilvusVectorStore.get_instance()
            # 实时探测：内存模式 connect() 恒 True；pymilvus 模式失败置 _connected=False
            if not getattr(_vs, "_connected", False):
                _vs.connect()
            checks["milvus"] = bool(getattr(_vs, "_connected", False))
        except Exception:
            checks["milvus"] = False
        db_ready = checks["database"]
        soft_down = [k for k, v in checks.items() if k != "database" and not v]
        status = "ok" if all(checks.values()) else ("degraded" if db_ready else "not_ready")
        return jsonify({
            "status": status,
            "checks": checks,
            "soft_dependencies_down": soft_down,
            "timestamp": datetime.now().isoformat(),
        }), 200 if db_ready else 503


# O1：/metrics 指标采集（自包含实现，Prometheus 文本格式，零额外依赖）。
# 指标：请求总数 / 4xx / 5xx、HTTP 延迟直方图；安装 prometheus-client 后
# 可平滑切换为标准库采集，本实现输出格式保持兼容。
_METRICS_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0)


def _register_metrics(app: object) -> None:
    """O1：注册 /metrics 端点与请求级指标打点。"""
    import threading
    import time as _t
    _metrics = {
        "requests_total": 0, "requests_4xx": 0, "requests_5xx": 0,
        "latency_sum": 0.0, "latency_count": 0,
    }
    _bucket_hits = [0] * (len(_METRICS_BUCKETS) + 1)
    _lock = threading.Lock()

    @app.before_request
    def _metrics_begin():
        from flask import g
        g.metrics_start = _t.perf_counter()

    @app.after_request
    def _metrics_record(resp):
        from flask import g
        start = getattr(g, "metrics_start", None)
        if start is None:
            return resp
        elapsed = _t.perf_counter() - start
        code = resp.status_code
        with _lock:
            _metrics["requests_total"] += 1
            if code >= 500:
                _metrics["requests_5xx"] += 1
            elif code >= 400:
                _metrics["requests_4xx"] += 1
            _metrics["latency_sum"] += elapsed
            _metrics["latency_count"] += 1
            for i, lim in enumerate(_METRICS_BUCKETS):
                if elapsed <= lim:
                    _bucket_hits[i] += 1
                    break
            else:
                _bucket_hits[-1] += 1
        return resp

    @app.get("/metrics")
    def _metrics_view():
        with _lock:
            total = _metrics["requests_total"]
            s4xx = _metrics["requests_4xx"]
            s5xx = _metrics["requests_5xx"]
            lat_sum = _metrics["latency_sum"]
            lat_cnt = _metrics["latency_count"] or 1
            hits = list(_bucket_hits)
        lines = [
            "# HELP ai_factory_requests_total 已处理请求总数",
            "# TYPE ai_factory_requests_total counter",
            f"ai_factory_requests_total {total}",
            f"ai_factory_requests_4xx {s4xx}",
            f"ai_factory_requests_5xx {s5xx}",
            "# HELP ai_factory_http_latency_seconds HTTP 请求延迟（秒）",
            "# TYPE ai_factory_http_latency_seconds summary",
            f"ai_factory_http_latency_seconds_sum {lat_sum:.6f}",
            f"ai_factory_http_latency_seconds_count {lat_cnt}",
            "# HELP ai_factory_http_latency_seconds_bucket HTTP 请求延迟直方图",
            "# TYPE ai_factory_http_latency_seconds_bucket histogram",
        ]
        acc = 0
        for i, lim in enumerate(_METRICS_BUCKETS):
            acc += hits[i]
            lines.append(
                f'ai_factory_http_latency_seconds_bucket{{le="{lim:.2f}"}} {acc}')
        lines.append(f'ai_factory_http_latency_seconds_bucket{{le="+Inf"}} {total}')
        return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}


def _register_rate_limit(app: object) -> None:
    """S2：HTTP 全局限流——复用 CacheManager.check_rate_limit（Redis 固定窗口，
    连接失败降级内存字典，不引入 flask-limiter 新依赖）。

    规则（对应《可商用部署功能补充建议》S2）：
        - 全局：100/min·IP
        - /api/auth/login：5/min·IP（防爆破）
        - /api/llm/chat：10/min·user（认证中间件已注入 g.user_id）
    健康探针/静态资源豁免；限流基础设施自身异常不阻断请求（限流失效不拖垮核心业务）。
    """
    @app.before_request
    def _rate_limit_before():
        try:
            from flask import jsonify, request, g
            from prog.config.settings import Settings
            if not getattr(Settings, "ENABLE_RATE_LIMIT", True):
                return None
            # 测试模式豁免：pytest Flask 客户端共享进程内内存降级 CacheManager，
            # 限流计数跨用例/跨模块持续累积，会导致全量 429；生产请求不受影响。
            if getattr(app, "testing", False):
                return None
            path = request.path
            if path in ("/health", "/ready") or path.startswith("/static/"):
                return None
            from prog.runtime.cache import get_cache
            cache = get_cache()
            ip = request.remote_addr or "unknown"
            if not cache.check_rate_limit(f"ip:{ip}", limit=100, window=60):
                return jsonify({"code": 429, "msg": "请求过于频繁，请稍后再试"}), 429
            if path == "/api/auth/login":
                if not cache.check_rate_limit(f"login:{ip}", limit=5, window=60):
                    return jsonify({"code": 429, "msg": "登录尝试过于频繁，请稍后再试"}), 429
            elif path == "/api/llm/chat":
                uid = getattr(g, "user_id", None) or ip
                if not cache.check_rate_limit(f"chat:{uid}", limit=10, window=60):
                    return jsonify({"code": 429, "msg": "请求过于频繁，请稍后再试"}), 429
        except Exception:
            pass
        return None


def create_app(config: dict) -> object:
    """创建并配置Flask app。

    参数：
        config: 配置字典

    返回：
        配置完成的Flask app实例
    """
    from flask import Flask
    from flask_cors import CORS

    # static_folder=None：禁用 Flask 内置 static 路由，由 _register_frontend_routes 提供
    app = Flask(__name__, static_folder=None)

    # 加载应用配置
    app_config = config.get("app", {})
    app.config.update(app_config)

    # S3：全局请求体上限 10MB（files_api 上传端点用局部 100MB 覆盖），
    # 避免超大请求体拖垮单 worker，同时给业务上传留出局部放宽空间
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    # S4/S5：统一 JSON 错误响应——404/405/413/500/未捕获异常兜底，
    # 生产环境不向客户端泄露 str(e)（可能含 DB 连接串/SQL/路径），trace_id 进响应
    _register_error_handlers(app)

    # S6：匿名健康探针 /health（liveness）与 /ready（readiness），K8s/部署脚本免鉴权
    _register_health_routes(app)

    # O1：/metrics 指标端点（Prometheus 文本格式，零额外依赖）
    _register_metrics(app)

    # S2：HTTP 全局限流（复用 CacheManager.check_rate_limit，Redis 降级内存字典）
    _register_rate_limit(app)

    # S7：CORS 完整接线——白名单来源 + 方法/头/凭证显式传递；
    # 生产环境 origins="*" 且启用凭证时降级为同源（浏览器规范禁止 * + credentials 并存）
    cors_origins = app_config.get("CORS_ORIGINS", "*")
    cors_methods = app_config.get("CORS_METHODS") or ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    cors_headers = app_config.get("CORS_ALLOW_HEADERS") or ["Content-Type", "Authorization"]
    cors_credentials = bool(app_config.get("CORS_SUPPORTS_CREDENTIALS", False))
    if cors_credentials and cors_origins == "*":
        logging.warning("CORS_SUPPORTS_CREDENTIALS=True 时 origins 不能为 *，已降级为同源（跨域被拒）")
        cors_origins = []
    CORS(app, origins=cors_origins, methods=cors_methods,
         allow_headers=cors_headers, supports_credentials=cors_credentials)

    # S10：安全响应头--X-Content-Type-Options 防 MIME 嗅探 / X-Frame-Options
    # 防 点击劫持 / HSTS 强制 HTTPS / CSP 限制资源来源
    @app.after_request
    def _add_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-XSS-Protection", "1; mode=block")
        # HSTS 仅生产环境 + HTTPS（开发环境不设，避免 localhost 缓存）
        if app_config.get("APP_ENV") == "production":
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains")
        # CSP：仅允许同源资源 + 内联样式（前端兼容）
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'")
        return resp

    # P8：ProxyFix--信任反向代理（Nginx/ALB）转发的 X-Forwarded-* 头，
    # 使 request.remote_addr 取真实客户端 IP（供限流/审计使用）
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=2, x_proto=1, x_host=1, x_prefix=1)

    # 初始化核心组件
    components = init_core_components(config)
    # 将组件存储在app上，供Blueprint注册时使用
    app.extensions = app.extensions or {}
    app.extensions["components"] = components

    # 注册Blueprint
    register_blueprints(app)

    # S8：Webhook 出站分发器就绪（业务事件 -> HMAC-SHA256 签名 POST -> 重试 3 次 -> 死信；
    # 分发失败静默，绝不阻断业务）
    try:
        from prog.core.webhook_dispatcher import dispatch_event
        print("[INFO] Webhook 出站分发器已就绪（S8：HMAC-SHA256 签名 + 3 次重试 + 死信）", flush=True)
    except Exception as e:
        print(f"[WARN] Webhook 分发器初始化失败：{e}")

    # 通知事件订阅：M1 侧消费 M0（coordinator/agents）发布的通知事件，
    # 替代业务层直接调用 notifications_api（拆分 runtime↔api 反向依赖）
    try:
        from prog.api.notifications_api import register_notification_handlers
        register_notification_handlers()
        print("[INFO] 通知事件订阅注册完成", flush=True)
    except Exception as e:
        print(f"[WARN] 通知事件订阅注册失败：{e}")

    # A4：请求级超时告警——before_request 记录起点，after_request 超过阈值
    # （默认 30s，可用 REQUEST_TIMEOUT_SECONDS 配置）记 WARNING，便于发现慢接口
    # 与卡死调用；LLM 流式接口（/api/chat/stream SSE 长连接）除外。
    # 置于 trace 接线之前注册，使计时覆盖认证中间件 + 视图 + 响应序列化全链路。
    _req_timeout_seconds = float(app_config.get("REQUEST_TIMEOUT_SECONDS", 30))
    _stream_paths = ("/api/chat/stream",)

    @app.before_request
    def _request_timeout_start():
        try:
            from flask import g as _g
            _g._req_start = time.time()
        except Exception:
            pass

    @app.after_request
    def _request_timeout_check(resp):
        try:
            from flask import g as _g, request as _req
            if _req.path in _stream_paths:
                return resp
            start = getattr(_g, "_req_start", None)
            if start is None:
                return resp
            elapsed = time.time() - start
            if elapsed > _req_timeout_seconds:
                logging.getLogger("prog.run_server").warning(
                    "请求超时告警：method=%s path=%s elapsed=%.2fs (阈值 %ds)",
                    _req.method, _req.path, elapsed, int(_req_timeout_seconds))
        except Exception:
            pass
        return resp

    # v6.84 + A5：trace_id 请求级接线（规格书 §4.7.2）——在认证中间件之前注册，
    # 使未鉴权请求（/health /ready /api/auth/login 等）也携带 trace_id，
    # 贯穿协调器→Agent→LLM→数据库全链路（审核链 chain_id 复用 trace_id）；
    # 请求结束清理，避免线程复用串号
    @app.before_request
    def _request_begin_trace():
        try:
            from prog.runtime.trace import new_trace
            new_trace()
        except Exception:
            pass

    @app.teardown_request
    def _request_end_trace(exc=None):
        try:
            from prog.runtime.trace import clear_trace
            clear_trace()
        except Exception:
            pass

    # 认证中间件：业务 API 统一校验 Bearer token 并注入 g（身份唯一来源）
    try:
        from prog.api.auth import register_auth_middleware
        register_auth_middleware(app)
    except Exception as e:
        _logger.warning(f"注册认证中间件失败：{e}")

    # 前端页面（登录页/主界面，静态文件）
    _register_frontend_routes(app)

    # 无数据库环境下启动时预热探测数据库连接：失败自动进入熔断，
    # 避免首个用户请求承担 ~2s 连接超时（连接失败后熔断期内查询即时降级）
    try:
        from prog.core.database import get_database
        _db = get_database()
        _row = _db.query_one("inventory", {"product_code": "__warmup__"})
        print(f"[INFO] 数据库预热探测成功 engine={_db._engine.url}", flush=True)
    except Exception as _e:
        print(f"[WARN] 数据库预热探测失败：{type(_e).__name__}: {_e}", flush=True)

    # C2：缓存启动预热钩子（可商用部署补充）——keys_builders（{key: builder}）
    # 由各业务模块注册（预热关键配置/规则等热点数据）；未注册时为空字典跳过。
    # CacheManager.warmup 对单 key 失败仅记日志，不阻断启动；此处位于
    # register_scheduler()（main 中调用）之前。
    try:
        from prog.runtime.cache import get_cache
        get_cache().warmup({})
        print("[INFO] 缓存预热钩子执行完成（C2：get_or_build/set_empty/TTL 抖动）",
              flush=True)
    except Exception as _e:
        print(f"[WARN] 缓存预热跳过：{type(_e).__name__}: {_e}", flush=True)

    return app


def _register_frontend_routes(app: object) -> None:
    """注册前端静态页面路由。

    静态目录：prog/static/；根路径 / 返回登录页/主界面（index.html）。
    页面侧校验登录态：打开页面先调 /api/auth/me 验证 Token，未登录/失效则提示登录。
    """
    from flask import send_from_directory

    static_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

    @app.route("/")
    def _frontend_index():
        return send_from_directory(static_dir, "index.html", max_age=0)

    @app.route("/static/<path:filename>")
    def _frontend_static(filename: str):
        return send_from_directory(static_dir, filename, max_age=0)


def register_blueprints(app: object) -> None:
    """注册所有Blueprint路由。"""
    from flask import Blueprint

    components = app.extensions.get("components", {})
    coordinator = components.get("coordinator")
    sales_agent = components.get("sales_agent")
    database = components.get("database")

    # 1. 注册对话Blueprint
    try:
        from prog.api.chat import chat_bp, register_chat_routes
        register_chat_routes(chat_bp, coordinator)
        app.register_blueprint(chat_bp)
    except Exception as e:
        print(f"[WARN] 注册chat_bp失败：{e}")

    # 2. 注册订单Blueprint
    try:
        from prog.api.orders import register_orders_routes
        orders_bp = Blueprint("orders", __name__, url_prefix="/api/orders")
        register_orders_routes(orders_bp, sales_agent, database)
        app.register_blueprint(orders_bp)
    except Exception as e:
        _logger.warning(f"注册orders_bp失败：{e}")

    # 3. 注册其他Blueprint（如已实现）
    _try_register_blueprint(app, "prog.api.auth", "auth_bp", "auth", "/api/auth")
    _try_register_blueprint(app, "prog.api.data_api", "data_bp", "data", "/api/data")
    _try_register_blueprint(app, "prog.api.import_api", "import_bp", "import", "/api/import")
    _try_register_blueprint(app, "prog.api.system_api", "system_bp", "system", "/api/system")
    _try_register_blueprint(app, "prog.api.llm_api", "llm_bp", "llm", "/api/llm")
    _try_register_blueprint(app, "prog.api.inventory_api", "inventory_bp", "inventory", "/api/inventory")
    _try_register_blueprint(app, "prog.api.audit_api", "audit_bp", "audit", "/api/audit")
    _try_register_blueprint(app, "prog.api.notifications_api", "notifications_bp", "notifications", "/api/notifications")
    _try_register_blueprint(app, "prog.api.webhook_api", "webhook_bp", "webhook", "/api/webhooks")
    _try_register_blueprint(app, "prog.api.training", "training_bp", "training", "/api/training")
    _try_register_blueprint(app, "prog.api.files_api", "files_bp", "files", "/api/files")
    _try_register_blueprint(app, "prog.api.mcp_api", "mcp_bp", "mcp", "/api/mcp")
    # S13：AI 治理——模型注册/A-B 流量切分（088 迁移 model_registry）
    _try_register_blueprint(app, "prog.api.model_api", "model_bp", "models", "/api/models")

    # 3.0 可商用部署补充：S3 主数据（客户/供应商）与 C7 合同生命周期
    _try_register_blueprint(app, "prog.api.master_data_api", "master_bp", "master", "/api/master")
    _try_register_blueprint(app, "prog.api.contract_api", "contract_bp", "contract", "/api/contracts")

    # 3.1 规格书 §A.1 补齐域 Blueprint（退货/质检/生产/采购/HR/财务/知识，薄封装 Agent 能力）
    _try_register_blueprint(app, "prog.api.returns_api", "returns_bp", "returns", "/api/returns")
    _try_register_blueprint(app, "prog.api.hr_api", "hr_bp", "hr", "/api")
    _try_register_blueprint(app, "prog.api.qc_api", "qc_bp", "qc", "/api")
    _try_register_blueprint(app, "prog.api.production_api", "production_bp", "production", "/api")
    _try_register_blueprint(app, "prog.api.purchase_api", "purchase_bp", "purchase", "/api")
    _try_register_blueprint(app, "prog.api.finance_api", "finance_bp", "finance", "/api/finance")
    _try_register_blueprint(app, "prog.api.knowledge_api", "knowledge_bp", "knowledge", "/api/knowledge")

    # 3.2 用户管理Blueprint（A.3：创建/禁用/重置密码/解锁/角色变更审批）
    _try_register_blueprint(app, "prog.api.user_api", "user_bp", "user", "/api/users")

    # 3.3 可商用部署补充项（M5 打印 / S7 SSO / M10 DSAR / C5 数据治理 /
    #     L2 固定资产 / T3 OpenAPI 文档）
    _try_register_blueprint(app, "prog.api.print_api", "print_bp", "print", "/api/print")
    _try_register_blueprint(app, "prog.api.sso_api", "sso_bp", "sso", "/api/auth/sso")
    _try_register_blueprint(app, "prog.api.dsar_api", "dsar_bp", "dsar", "/api/dsar")
    _try_register_blueprint(app, "prog.api.data_governance_api", "dg_bp", "governance", "/api/governance")
    _try_register_blueprint(app, "prog.api.asset_api", "asset_bp", "asset", "/api/assets")
    _try_register_blueprint(app, "prog.api.openapi_api", "openapi_bp", "openapi", "")

    # 4. 注册意图规则管理Blueprint（注入 coordinator 用于热更新）
    try:
        from prog.api.intent_rules_api import register_intent_rules_routes
        intent_rules_bp = Blueprint("intent_rules", __name__, url_prefix="/api/intent-rules")
        register_intent_rules_routes(intent_rules_bp, coordinator)
        app.register_blueprint(intent_rules_bp)
    except Exception as e:
        print(f"[WARN] 注册intent_rules_bp失败：{e}")


def _try_register_blueprint(app: object, module_path: str,
                            bp_var: str, bp_name: str,
                            url_prefix: str) -> None:
    """尝试注册一个Blueprint，失败时静默跳过。

    参数：
        app: Flask应用实例
        module_path: 模块路径
        bp_var: Blueprint变量名
        bp_name: Blueprint名称
        url_prefix: URL前缀
    """
    try:
        import importlib
        from flask import Blueprint
        module = importlib.import_module(module_path)
        # 优先使用模块中已定义的Blueprint
        bp = getattr(module, bp_var, None)
        if bp is None:
            # 模块中未定义Blueprint，尝试创建并注册路由
            bp = Blueprint(bp_name, module_path, url_prefix=url_prefix)
            register_fn = getattr(module, f"register_{bp_name}_routes", None)
            if register_fn:
                register_fn(bp)
            else:
                return  # 无路由注册函数，跳过
        app.register_blueprint(bp)
    except ImportError:
        pass  # 模块未实现，跳过
    except Exception as e:
        print(f"[WARN] 注册{bp_name}_bp失败：{e}")


def init_core_components(config: dict) -> dict:
    """初始化核心组件。

    返回：
        组件实例字典 {database, llm_engine, sales_agent, coordinator, ...}
    """
    components: Dict[str, Any] = {}

    # 1. 初始化数据库（PostgreSQL连接池）
    try:
        from prog.core.database import DatabaseManager
        from prog.config.config_loader import get_config_loader
        # 使用统一配置加载器解析 _env 后缀（user/password 实际值），
        # 避免直接传原始配置导致空用户名/密码连接失败触发熔断
        db_config = get_config_loader().get_interface_config("database")
        database = DatabaseManager.get_instance(db_config)
        components["database"] = database
        # v6.78.2：显式注册到框架注入点（runtime.database）——不再依赖
        # create_app 预热探测（get_database()）副作用触发，消除时序依赖：
        # 若预热失败（熔断降级），框架组件（审核链归档/规则配置加载）仍
        # 能拿到同一 DatabaseManager，避免静默降级内存模式。
        try:
            from prog.runtime.database import set_database as _rt_set_db
            _rt_set_db(database)
        except Exception:
            pass
        _logger.info(f"数据库初始化完成 host={db_config.get('host')} "
                     f"user={'***' if db_config.get('password') else '(空)'} "
                     f"db={db_config.get('database')}")
    except Exception as e:
        _logger.warning(f"数据库初始化失败：{e}")
        components["database"] = None

    # 2. 初始化LLM引擎（注入LLMProvider启用安全门控+Function Calling）
    try:
        from prog.core.llm_provider import create_llm_provider
        from prog.llm.engine import LLMEngine
        llm_provider = create_llm_provider()
        llm_engine = LLMEngine(llm_provider=llm_provider)
        components["llm_engine"] = llm_engine
    except Exception as e:
        print(f"[WARN] LLM引擎初始化失败：{e}")
        components["llm_engine"] = None

    # 2.1 初始化意图识别专用 LLM 引擎（v6.78.3 双模型架构）
    #     intent_llm_provider 节点外部可配（deployment_config.json）：
    #     语义理解用强模型（thinking 开启，reasoning 流式推前端），
    #     与对话回复的快模型（llm_engine）独立。节点缺失时回退复用
    #     llm_engine，保持单模型兼容。
    intent_llm_engine = None
    try:
        from prog.core.llm_provider import create_llm_provider
        from prog.llm.engine import LLMEngine
        intent_provider = create_llm_provider(section="intent_llm_provider")
        if intent_provider.model:
            intent_llm_engine = LLMEngine(llm_provider=intent_provider)
            components["intent_llm_engine"] = intent_llm_engine
            print(f"[INFO] 意图识别LLM引擎初始化完成 model={intent_provider.model} "
                  f"thinking={intent_provider.thinking}", flush=True)
        else:
            components["intent_llm_engine"] = None
    except Exception as e:
        print(f"[WARN] 意图识别LLM引擎初始化失败：{e}（回退复用 llm_engine）")
        components["intent_llm_engine"] = None

    # 3. 初始化销售Agent
    try:
        from prog.agents.sales_agent import SalesAgent
        sales_agent = SalesAgent(
            llm_provider=components.get("llm_engine"),
            database=components.get("database"),
        )
        components["sales_agent"] = sales_agent
    except Exception as e:
        print(f"[WARN] 销售Agent初始化失败：{e}")
        components["sales_agent"] = None

    # 4. 初始化其他领域Agent（如已实现）
    for agent_name, agent_class_path in [
        ("production", "prog.agents.production_agent.ProductionAgent"),
        ("warehouse", "prog.agents.warehouse_agent.WarehouseAgent"),
        ("technical", "prog.agents.technical_agent.TechnicalAgent"),
        ("finance", "prog.agents.finance_agent.FinanceAgent"),
        ("qc", "prog.agents.qc_agent.QCAgent"),
        ("hr", "prog.agents.hr_agent.HRAgent"),
    ]:
        try:
            import importlib
            module_path, class_name = agent_class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            agent_class = getattr(module, class_name)
            agent = agent_class(
                llm_provider=components.get("llm_engine"),
                database=components.get("database"),
            )
            components[agent_name + "_agent"] = agent
        except Exception:
            pass  # Agent未实现，跳过

    # 5. 初始化知识助手（如已实现）
    try:
        from prog.agents.knowledge_assistant import KnowledgeAssistant
        # v6.30：初始化进程级知识库（图纸/工艺/训练内容等同步落库，
        # 辅助质量分析/流程查询 RAG 检索），并从 knowledge_documents 加载已入库文档
        from prog.llm.knowledge_base import KnowledgeBase
        # P0 接线：初始化向量库与 Embedding 提供方并注入知识库，
        # 使 RAG 真正走向量检索；组件不可用时降级为内存 TF-IDF 关键词检索。
        embedding_provider = None
        vector_store = None
        try:
            from prog.core.embedding_provider import create_embedding_provider
            from prog.core.vector_store import MilvusVectorStore
            embedding_provider = create_embedding_provider()
            vector_store = MilvusVectorStore.get_instance()
            print(f"[INFO] RAG 向量检索组件就绪 embedding_type="
                  f"{embedding_provider.__class__.__name__} "
                  f"vector_store={vector_store.__class__.__name__}", flush=True)
        except Exception as e:
            print(f"[WARN] 向量库/Embedding 初始化失败：{e}"
                  f"（RAG 降级为关键词检索）")
        knowledge_base = KnowledgeBase.get_instance(
            db=components.get("database"),
            vector_store=vector_store,
            embedding_provider=embedding_provider,
        )
        knowledge_base.load_from_db()
        components["knowledge_base"] = knowledge_base
        # 知识库未命中兜底：默认 LLM 自身知识回答；KB_WEB_SEARCH_ENABLED=1 时先联网检索
        kb_web_search = os.environ.get("KB_WEB_SEARCH_ENABLED", "").lower() in ("1", "true", "yes")
        knowledge_assistant = KnowledgeAssistant(
            llm_provider=components.get("llm_engine"),
            knowledge_base=knowledge_base,
            web_search_enabled=kb_web_search,
        )
        components["knowledge_assistant"] = knowledge_assistant
    except Exception:
        components["knowledge_assistant"] = None

    # 开源版：已移除 KbSink/Desensitizer（知识自动沉淀，属商业版能力）

    # 6. 初始化协调Agent
    try:
        from prog.runtime.coordinator import CoordinatorAgent
        # 构建Agent路由表
        agents = {}
        if components.get("sales_agent"):
            agents["sales"] = components["sales_agent"]
        if components.get("production_agent"):
            agents["production"] = components["production_agent"]
        if components.get("warehouse_agent"):
            agents["warehouse"] = components["warehouse_agent"]
        if components.get("technical_agent"):
            agents["technical"] = components["technical_agent"]
        if components.get("finance_agent"):
            agents["finance"] = components["finance_agent"]
        if components.get("qc_agent"):
            agents["qc"] = components["qc_agent"]
        if components.get("hr_agent"):
            agents["hr"] = components["hr_agent"]

        coordinator = CoordinatorAgent(
            agents=agents,
            knowledge_assistant=components.get("knowledge_assistant"),
            llm_engine=components.get("llm_engine"),
            intent_llm_engine=components.get("intent_llm_engine"),
        )
        components["coordinator"] = coordinator
    except Exception as e:
        _logger.warning(f"协调Agent初始化失败：{e}")
        components["coordinator"] = None

    return components


def run_waitress(app: object, host: str, port: int, threads: int) -> None:
    """Windows下使用Waitress启动。"""
    try:
        from waitress import serve
        print(f"[INFO] Waitress启动：{host}:{port}（线程数={threads}）")
        # send_bytes=1：最小化输出缓冲，使SSE流式响应能及时推送到客户端
        serve(app, host=host, port=port, threads=threads, send_bytes=1)
    except ImportError:
        print("[ERROR] waitress未安装，请执行 pip install waitress")
        # 兜底：使用Flask开发服务器
        app.run(host=host, port=port, debug=False)


def run_gunicorn(app: object, host: str, port: int, workers: int) -> None:
    """Linux下使用Gunicorn启动。"""
    try:
        import gunicorn.app.base

        class StandaloneApplication(gunicorn.app.base.BaseApplication):
            """Gunicorn独立应用封装。"""

            def __init__(self, app_obj, options=None):
                self.application = app_obj
                self.options = options or {}
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                return self.application

        options = {
            "bind": f"{host}:{port}",
            "workers": workers,
            "timeout": 120,
        }
        print(f"[INFO] Gunicorn启动：{host}:{port}（worker数={workers}）")
        StandaloneApplication(app, options).run()
    except ImportError:
        print("[ERROR] gunicorn未安装，请执行 pip install gunicorn")
        # 兜底：使用Flask开发服务器
        app.run(host=host, port=port, debug=False)


def register_scheduler() -> None:
    """注册并启动轻量任务调度器（库存/订单/质量日报 + D3 备份 + 公共/特殊提醒）。

    从 main() 与 wsgi.py 统一调用，避免调度器注册逻辑散落两处。
    调度线程为 daemon 守护线程，随进程退出自动结束，不阻塞服务关闭。
    """
    try:
        from prog.runtime.scheduler import ScheduledTask, TaskScheduler
        from prog.scripts.report_tasks import (
            inventory_daily_report,
            order_daily_report,
            quality_daily_report,
        )
        scheduler = TaskScheduler.get_instance()
        for _tid, _handler in (
            ("inventory_daily", inventory_daily_report),
            ("order_daily", order_daily_report),
            ("quality_daily", quality_daily_report),
        ):
            scheduler.register(ScheduledTask(task_id=_tid, handler=_handler))
        # D3：每日 02:00 数据库备份（075 迁移种子行，处理器 backup_db.run_backup）
        try:
            from prog.scripts.backup_db import run_backup
            scheduler.register(ScheduledTask(
                task_id="db_backup_daily", handler=run_backup,
                schedule_expr="02:00"))
        except Exception as e:
            _logger.warning(f"数据库备份任务注册失败：{e}")
        # 公共/特殊提醒任务（DB 行驱动，075 迁移 task_type 列）
        try:
            _n_remind = scheduler.load_reminder_tasks()
            if _n_remind:
                print(f"[INFO] 已加载 {_n_remind} 个提醒任务（公共/特殊）")
        except Exception as e:
            print(f"[WARN] 提醒任务加载失败：{e}")
        # C6：数据哈希巡检（063 触发器已计算哈希；每日定时校验防篡改，
        # 发现篡改记 ERROR 告警日志，不阻塞调度）
        try:
            import logging as _lg

            def _data_hash_inspection() -> None:
                _logger = _lg.getLogger("prog.scheduler")
                try:
                    from prog.scripts import verify_data_hash
                    _code = verify_data_hash.main()
                    if _code == 1:
                        _logger.error("数据哈希巡检发现篡改记录（详见 verify_data_hash 输出）")
                    else:
                        _logger.info("数据哈希巡检完成，未发现篡改")
                except Exception as _e:
                    _logger.error("数据哈希巡检失败: %s", _e, exc_info=True)

            scheduler.register(ScheduledTask(
                task_id="data_hash_inspection", handler=_data_hash_inspection,
                schedule_expr="04:00"))
        except Exception as e:
            print(f"[WARN] 数据哈希巡检任务注册失败：{e}")
        # C8：AP/AR 子账与总账对账（每日 01:30 / 01:45；表缺失/DB 不可达
        # 优雅降级返回跳过状态，不抛异常）
        try:
            from prog.scripts.reconcile_tasks import (
                ap_ar_reconciliation,
                ledger_reconciliation,
            )
            scheduler.register(ScheduledTask(
                task_id="ap_ar_reconciliation", handler=ap_ar_reconciliation,
                schedule_expr="01:30"))
            scheduler.register(ScheduledTask(
                task_id="ledger_reconciliation", handler=ledger_reconciliation,
                schedule_expr="01:45"))
        except Exception as e:
            print(f"[WARN] 财务对账任务注册失败：{e}")
        # M9/L9：审计数据归档（每日 03:00；WORM 表只导出不删除，
        # 导出成功置 archive_enabled=FALSE 防重复导出）
        try:
            from prog.scripts.audit_archive import archive_expired_audit
            scheduler.register(ScheduledTask(
                task_id="audit_archive_daily", handler=archive_expired_audit,
                schedule_expr="03:00"))
        except Exception as e:
            print(f"[WARN] 审计归档任务注册失败：{e}")
        scheduler.start()
        print("[INFO] 轻量任务调度器已启动（库存/订单/质量日报，默认 08:30/09:00/09:30；备份 02:00）")
    except Exception as e:
        print(f"[WARN] 轻量任务调度器启动失败：{e}")


def _ensure_local_redis(is_debug: bool) -> None:
    """本地开发：Redis 未运行时自动拉起 tools/redis/redis-server.exe（仅 dev）。

    用户需求：启动服务器时同时启动 Redis。本项目 Redis 客户端（缓存/事件总线/
    会话）不可用时降级内存模式，但真实 Redis 可提供进程外共享缓存与事件分发。
    规则（保守，绝不阻断启动）：
        - 仅 --env dev 生效（正式环境由运维负责 Redis，不自动拉起）
        - 仅当 127.0.0.1:6379 不可达（Redis 已在运行时跳过）
        - 仅当 tools/redis/redis-server.exe 存在（未找到仅告警）
        - 启动失败仅告警（缓存/事件总线/会话继续降级内存模式）
    """
    if not is_debug:
        return
    try:
        import socket
        with socket.create_connection(("127.0.0.1", 6379), timeout=1):
            return  # Redis 已在运行
    except Exception:
        pass
    redis_dir = os.path.join(_PROJECT_ROOT, "tools", "redis")
    redis_exe = os.path.join(redis_dir, "redis-server.exe")
    if not os.path.exists(redis_exe):
        print("[WARN] Redis 未运行且未找到 tools/redis/redis-server.exe，"
              "缓存/事件总线/会话降级内存模式（可先运行 "
              "./prog/deploy-dev-windows.ps1 -StartServices）")
        return
    try:
        conf = os.path.join(redis_dir, "redis.windows.conf")
        subprocess.Popen(
            [redis_exe, conf],
            cwd=redis_dir,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"[INFO] Redis 未运行，已自动启动（{redis_exe}，127.0.0.1:6379）")
    except Exception as e:
        print(f"[WARN] Redis 自动启动失败：{e}（缓存/事件总线/会话继续降级内存模式）")


def main() -> None:
    """主入口。

    解析参数：
        --host: 监听地址（默认0.0.0.0）
        --port: 监听端口（默认5000）
        --workers: worker数（Linux Gunicorn，默认4）
        --threads: 线程数（Windows Waitress，默认8）
        --env: 环境（dev/prod，默认跟随 DEBUG：RUNTIME_DEBUG=1 时 dev，否则 prod）

    配置校验门（§A.0 三层变量加载机制）：
        - dev（开发模式）：依赖/配置缺失时自动尝试修复，仍缺失则打印 WARN 继续启动，
          允许内存降级；
        - prod（正式模式）：依赖缺失自动安装，配置缺失进入交互式引导（非交互终端
          保持 fail-fast），均失败则打印报告并 exit(1)，确保不在残缺配置下对外服务。
    根据sys.platform选择WSGI服务器启动。
    """
    parser = argparse.ArgumentParser(description="AI工厂管家Flask服务启动")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认0.0.0.0）")
    parser.add_argument("--port", type=int, default=5000, help="监听端口（默认5000）")
    parser.add_argument("--workers", type=int, default=4, help="Gunicorn worker数（默认4）")
    parser.add_argument("--threads", type=int, default=8, help="Waitress线程数（默认8）")
    parser.add_argument("--env", default="dev" if DEBUG else "prod",
                        choices=["dev", "prod"],
                        help="环境（默认跟随DEBUG：开发=dev，正式=prod）")
    args = parser.parse_args()
    is_debug = args.env == "dev"
    prog_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(prog_root, ".env")

    # 1. 依赖自检 + 自动安装：缺失时自动 pip install，失败打印手动命令
    #    正式模式：安装失败视为启动失败（fail-fast）；开发模式：提示后继续
    missing_deps = check_dependencies()
    if missing_deps:
        print(f"[WARN] 缺失运行依赖: {', '.join(missing_deps)}")
        if install_dependencies(missing_deps):
            missing_deps = check_dependencies()
        if missing_deps and not is_debug:
            print("[ERROR] 正式部署模式：依赖安装失败，无法启动")
            sys.exit(1)

    # 2. 配置校验门：dev 宽松（WARN 不阻断），prod 严格（交互引导 / fail-fast）
    loader = ConfigLoader.get_instance()
    validator = ConfigValidator(loader)
    errors = validator.validate_all()
    if errors:
        report = validator.get_validation_report()
        total = sum(len(v) for v in errors.values())
        if is_debug:
            _logger.warning(f"配置校验发现 {total} 个问题"
                            f"（开发模式：打印日志，继续启动，允许内存降级）")
            _logger.warning(report)
        else:
            if not _interactive_fix_config(errors, env_path, loader):
                _logger.error(f"配置校验发现 {total} 个问题，正式部署模式拒绝启动：")
                _logger.error(report)
                sys.exit(1)

    # 3. 服务探测提示（local 模式，缺失仅提示，不阻断）
    if loader.get_deployment_mode() == "local":
        probe_and_hint_services()

    # S11：正式部署模式强制强 SECRET_KEY（兜底未设 APP_ENV=production 的 --env prod 路径）
    if not is_debug:
        from prog.config.settings import Settings as _SettingsCls
        _sk = _SettingsCls.SECRET_KEY
        if _sk == "dev-secret-key-change-me" or len(_sk) < 32:
            print("[ERROR] 正式部署模式：SECRET_KEY 强度不足，"
                  "请设置 APP_SECRET_KEY（≥32 字节随机密钥）后重启，拒绝启动")
            sys.exit(1)

    # 加载配置
    config = load_config(args.env)
    print(f"[INFO] 配置加载完成，环境={args.env}")

    # 用户需求：dev 模式下 Redis 未运行时自动拉起 tools/redis/redis-server.exe，
    # 使缓存/事件总线/会话使用真实 Redis（在 create_app 连接前启动）
    _ensure_local_redis(is_debug)

    # 创建Flask app
    app = create_app(config)
    print(f"[INFO] Flask应用创建完成")

    # 5. 启动轻量任务调度器（v6.83 通用能力第1档：库存/订单/质量日报 -> notifications）
    #    调度线程为 daemon 守护线程，随进程退出自动结束，不阻塞服务关闭。
    #    时间与启停可在 scheduled_tasks 表调整（schedule_expr / enabled）。
    #    可商用部署补充：
    #      - D3：db_backup_daily 每日 02:00 数据库备份（pg_dump + 对象存储，保留 30 天）
    #      - 公共/特殊提醒任务：scheduled_tasks 表 task_type='public'（提醒所有人）
    #        / 'targeted'（提醒指定人，空则创建人）的行，启动时装配处理器到点发通知
    register_scheduler()

    # 根据环境选择WSGI服务器
    if args.env == "dev":
        # 开发模式：Flask 内置服务器（支持 SSE 流式响应，无缓冲）
        print(f"[INFO] Flask开发服务器启动：{args.host}:{args.port}（开发模式，支持SSE流式）")
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    elif sys.platform == "win32":
        run_waitress(app, args.host, args.port, args.threads)
    else:
        run_gunicorn(app, args.host, args.port, args.workers)


if __name__ == "__main__":
    main()
