"""
System 系统监控API模块
======================

文件用途：
    实现系统状态监控API，提供系统状态、Agent状态、部署模式、
    学习流程触发等接口。

技术规格章节：
    - §1.1.3 Coordinator Agent（Agent状态监控）
    - §1 系统部署（部署模式信息）
    - 学习流程 L0-L3（触发学习流程接口）

接口列表：
    - GET /api/system/health: 健康检查
    - GET /api/system/config: 系统配置（脱敏）
    - GET /api/system/status: 系统状态（数据库/Redis/Milvus/LLM连通性）
    - GET /api/system/version: 版本信息

设计说明：
    - 系统状态含LLM连接、数据库连接、各组件健康度
    - 部署模式区分全功能模式/降级模式（无LLM时）
    - /config 仅返回是否已配置，不回传内网 host/port 等拓扑信息
"""

import os
import time
from typing import Any, Dict

from flask import Blueprint, request
from prog.utils.api_response import api_response, error_response
from prog.utils.auth_decorators import require_role

system_bp = Blueprint('system', __name__, url_prefix='/api/system')

# 进程启动时间（用于计算运行时长）
_START_TIME = time.time()

# 系统版本信息
_VERSION = os.environ.get('APP_VERSION', '1.0.0')


def _check_database() -> Dict[str, Any]:
    """检查数据库连通性。"""
    try:
        from prog.core.database import get_database
        db = get_database()
        db.execute("SELECT 1")
        return {"status": "connected"}
    except Exception as e:
        return {"status": "disconnected", "error": str(e)}


def _check_redis() -> Dict[str, Any]:
    """检查Redis连通性。"""
    r = None
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
                           socket_connect_timeout=2)
        r.ping()
        return {"status": "connected"}
    except Exception as e:
        return {"status": "disconnected", "error": str(e)}
    finally:
        # W5：连接用完即关，避免连接泄漏
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def _check_milvus() -> Dict[str, Any]:
    """检查Milvus连通性。"""
    try:
        from pymilvus import connections, utility
        host = os.environ.get('MILVUS_HOST', 'localhost')
        port = os.environ.get('MILVUS_PORT', '19530')
        alias = "system_health_check"
        connections.connect(alias=alias, host=host, port=port, timeout=2)
        # 列举集合验证连通
        utility.list_collections(using=alias)
        try:
            connections.disconnect(alias)
        except Exception:
            pass
        return {"status": "connected", "host": host, "port": port}
    except Exception as e:
        return {"status": "disconnected", "error": str(e)}


# --------------------------------------------------------
# 健康检查
# --------------------------------------------------------
@system_bp.route('/health', methods=['GET'])
def health():
    """GET /api/system/health 健康检查。"""
    try:
        return api_response(code=0, data={
            "status": "ok", "timestamp": int(time.time()),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 系统配置（脱敏）
# --------------------------------------------------------
@system_bp.route('/config', methods=['GET'])
def get_config():
    """GET /api/system/config 系统配置（脱敏）。"""
    try:
        # 敏感字段脱敏：仅显示是否存在/已配置，不回传内网 host/port 等拓扑信息
        config = {
            "app_name": "AI工厂管家",
            "version": _VERSION,
            "deployment_mode": os.environ.get('DEPLOYMENT_MODE', 'local'),
            "database": {
                "host_configured": bool(os.environ.get('DB_HOST')),
                "port_configured": bool(os.environ.get('DB_PORT')),
                "database": os.environ.get('DB_NAME', 'ai_factory'),
                "user_env": "DB_USER",
                "password_configured": bool(os.environ.get('DB_PASSWORD')),
            },
            "redis": {
                "url_configured": bool(os.environ.get('REDIS_URL')),
            },
            "milvus": {
                "host_configured": bool(os.environ.get('MILVUS_HOST')),
                "port_configured": bool(os.environ.get('MILVUS_PORT')),
            },
            "llm": {
                "provider": os.environ.get('LLM_PROVIDER', 'doubao'),
                "base_url_configured": bool(os.environ.get('LLM_BASE_URL')),
                "model": os.environ.get('LLM_MODEL', ''),
                "api_key_configured": bool(
                    os.environ.get('LLM_API_KEY') or os.environ.get('ARK_API_KEY')),
            },
            "auth": {
                "jwt_secret_configured": bool(os.environ.get('JWT_SECRET')),
                "jwt_expire_seconds": int(os.environ.get('JWT_EXPIRE_SECONDS', '86400')),
            },
        }
        return api_response(code=0, data=config)
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 系统状态（数据库/Redis/Milvus连通性）
# --------------------------------------------------------
@system_bp.route('/status', methods=['GET'])
def get_status():
    """GET /api/system/status 系统状态（数据库/Redis/Milvus/LLM）。"""
    try:
        components = {
            "database": _check_database(),
            "redis": _check_redis(),
            "milvus": _check_milvus(),
        }

        # v6.67.6：LLM 健康状态（进程级记录，欠费/限流/鉴权失败时提示客户，
        # 前端"🧠 AI已接入"标签展示 hint）
        try:
            from prog.core.llm_provider import get_llm_health
            lh = get_llm_health()
            components["llm"] = {
                "status": "connected" if lh.get("ok") else (lh.get("code") or "error"),
                "hint": lh.get("hint", ""),
                "error": lh.get("error", ""),
                "at": lh.get("at", 0),
            }
        except Exception:
            components["llm"] = {"status": "connected", "hint": ""}

        # 整体健康度：任一关键组件异常则为 degraded
        statuses = [c.get("status") for c in components.values()]
        if all(s == "connected" for s in statuses):
            overall = "healthy"
        elif any(s == "connected" for s in statuses):
            overall = "degraded"
        else:
            overall = "down"

        return api_response(code=0, data={
            "overall": overall,
            "components": components,
            "version": _VERSION,
            "uptime_seconds": int(time.time() - _START_TIME),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# 版本信息
# --------------------------------------------------------
@system_bp.route('/version', methods=['GET'])
def get_version():
    """GET /api/system/version 版本信息。"""
    try:
        import sys
        return api_response(code=0, data={
            "version": _VERSION,
            "app_name": "AI工厂管家（AI Factory Manager）",
            "python_version": sys.version.split()[0],
            "deployment_mode": os.environ.get('DEPLOYMENT_MODE', 'local'),
            "build_time": os.environ.get('APP_BUILD_TIME', ''),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@system_bp.route('/log-level', methods=['GET', 'PUT'])
@require_role('admin')
def log_level():
    """GET/PUT /api/system/log-level 运行时日志级别（L3，admin only）。

    GET 返回当前 root logger 级别；PUT body {"level": "DEBUG|INFO|WARNING|ERROR"}
    动态调整，生产即时生效无需重启。
    """
    import logging
    root_logger = logging.getLogger()
    if request.method == "GET":
        return api_response(code=0, data={"level": logging.getLevelName(
            root_logger.getEffectiveLevel())})
    try:
        data = request.get_json(silent=True) or {}
        level_name = str(data.get("level", "")).upper()
        if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return api_response(code=400, msg=f"level 仅支持 DEBUG/INFO/WARNING/ERROR/CRITICAL，收到 {level_name!r}"), 400
        root_logger.setLevel(getattr(logging, level_name))
        return api_response(code=0, data={"level": level_name})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert system_bp is not None, "system_bp 未定义"
    hello_world(__name__, "system_bp 定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
