"""
LLM Config LLM配置API模块
=========================

文件用途：
    实现LLM配置管理API，提供配置查询、更新、连接测试、模型列表接口。

技术规格章节：
    - §2 LLM安全门控（LLM引擎配置由本模块管理）
    - §1.1.3 Coordinator Agent（Agent通过LLMEngine间接使用配置）

接口列表：
    - GET /api/llm/config: 获取当前LLM配置
    - PUT /api/llm/config: 更新LLM配置（仅管理员）
    - POST /api/llm/test: 测试LLM连接（复用已配置 base_url，禁止任意 URL 防 SSRF）
    - GET /api/llm/models: 获取可用模型列表
    - POST /api/llm/chat: 直接LLM对话
    - GET /api/llm/usage: Token使用统计（llm_usage 表未启用时返回空）

设计说明：
    - 配置含 api_key/base_url/model/temperature/max_tokens/timeout
    - 更新配置需管理员权限
    - api_key 返回时脱敏（仅显示首2+末4位），落盘时 Fernet 加密（fernet: 前缀，D6）
    - 测试连接发送一条简单消息验证可用性（base_url 仅允许取已配置值）
    - 模型列表按provider分组返回（deepseek/qwen/doubao/local）
"""

import json
import os
import time
from typing import Any

from flask import Blueprint, request, current_app
from prog.utils.api_response import api_response, error_response

from prog.utils.auth_decorators import require_role

llm_bp = Blueprint('llm', __name__, url_prefix='/api/llm')

# 可用模型列表（按provider分组，参考demo llm_engine.py的PROVIDERS预设）
LLM_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "get_key_url": "https://platform.deepseek.com/api_keys",
    },
    "doubao": {
        "name": "豆包（火山方舟）",
        "models": ["doubao-seed-1-6-250615", "doubao-seed-2-1-turbo-260628", "doubao-seed-2-0-mini-260428", "deepseek-v4-flash-ga-260731"],
        "default_model": "deepseek-v4-flash-ga-260731",
        "get_key_url": "https://console.volcengine.com/ark",
    },
    "qwen": {
        "name": "通义千问",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "default_model": "qwen-plus",
        "get_key_url": "https://dashscope.console.aliyun.com/apiKey",
    },
    "local": {
        "name": "本地部署",
        "models": ["local-llm"],
        "default_model": "local-llm",
        "get_key_url": "",
    },
}


def _get_db() -> Any:
    """延迟获取数据库实例，获取失败时返回None（降级为模拟数据）。"""
    try:
        from prog.core.database import get_database
        return get_database()
    except Exception:
        return None


def _get_llm_engine() -> Any:
    """获取LLM引擎实例：优先从app.extensions注入，否则按默认配置实例化。"""
    # 优先使用注入的引擎
    try:
        components = current_app.extensions.get('components', {}) or {}
        engine = components.get('llm_engine')
        if engine is not None:
            return engine
    except Exception:
        pass
    # 回退：按默认配置实例化
    try:
        from prog.llm.engine import LLMEngine
        return LLMEngine()
    except Exception:
        return None


# --------------------------------------------------------
# 直接LLM对话（非Agent路由）
# --------------------------------------------------------
@llm_bp.route('/chat', methods=['POST'])
@require_role('admin')
def chat():
    """POST /api/llm/chat 直接LLM对话（非Agent路由）。

    S8：仅管理员可用--直接 LLM 对话绕过 Agent 路由与安全门控，
    普通用户应通过 /api/chat 走 Agent 路由（含意图校验/审核链）。
    """
    try:
        body = request.get_json(silent=True) or {}
        message = body.get('message') or body.get('prompt', '')
        if not message:
            return error_response(400, "message 为必填"), 400

        engine = _get_llm_engine()
        model = ""
        if engine is not None:
            try:
                config = getattr(engine, 'config', {}) or {}
                model = config.get('model', '')
                # 走安全门控的直接对话（context标记为direct，绕过Agent意图校验）
                context = {
                    'user_input': message,
                    'agent_type': 'direct',
                    'intent': 'direct_chat',
                }
                reply = engine.generate(message, context)
                # P1-1：读取 generate 回写的 need_confirm，向客户端下发二次确认标记
                need_confirm = bool(context.get('need_confirm'))
                return api_response(code=0, data={
                    "reply": reply, "model": model,
                    "need_confirm": need_confirm,
                })
            except Exception as e:
                return error_response(500, f"LLM调用失败：{str(e) if DEBUG else '内部错误'}"), 500

        # v6.58：移除模拟回复——LLM 引擎不可用时直接报错，不伪造数据
        return error_response(500, "LLM 引擎不可用，请检查模型配置"), 500
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# LLM 配置查询 / 更新 / 连接测试（v6.46 补全：模型名/API/密钥运行时可改）
# --------------------------------------------------------
def _mask_api_key(key: str) -> str:
    """脱敏：仅显示首2+末4位。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:2] + "*" * (len(key) - 6) + key[-4:]


def _obfuscate_api_key(key: str) -> str:
    """api_key 落盘加密（Fernet + fernet: 前缀，D6），避免明文密钥暴露在配置文件中。

    llm_provider._load_default_llm_config 加载时还原；旧配置（明文/b64: 混淆）兼容。

    幂等：已带 fernet: 前缀（本方案加密产物）时原样返回；旧版 b64: 产物
    自动升级重加密为 fernet:，避免 b64:b64:... 或 fernet:b64:... 层层叠加。
    """
    from prog.utils.crypto import encrypt_text, is_encrypted
    if isinstance(key, str) and is_encrypted(key):
        return key
    return encrypt_text(str(key))


def _deobfuscate_api_key(key: str) -> str:
    """还原 Fernet 加密的 api_key；透明兼容旧版 b64: 混淆与明文。"""
    from prog.utils.crypto import decrypt_text
    return decrypt_text(key)


def _get_current_role() -> str:
    """当前请求用户角色（认证中间件注入 g.user_role）。"""
    try:
        from flask import g
        return getattr(g, "user_role", "") or ""
    except Exception:
        return ""


def _config_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "deployment_config.json",
    )


def _read_deployment_config() -> dict:
    cfg = {}
    try:
        if os.path.exists(_config_path()):
            with open(_config_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
    except Exception:
        cfg = {}
    return cfg


def _write_llm_config_patch(patch: dict) -> dict:
    """将 LLM 配置补丁写回 deployment_config.json，返回完整 config。

    api_key 落盘前 Fernet 加密（D6），避免明文密钥暴露在配置文件中。
    同时将加密密钥指纹落地 system_configs（business_rules.encryption_key_id）。
    """
    from prog.utils.crypto import register_encryption_key_id
    register_encryption_key_id()  # D6：密钥指纹落库（失败静默）
    deploy = _read_deployment_config()
    llm_section = deploy.setdefault("interfaces", {}).setdefault("llm_provider", {})
    cfg = dict(llm_section.get("config", {}) or {})
    cfg.update(patch)
    if "api_key" in cfg and cfg["api_key"]:
        cfg["api_key"] = _obfuscate_api_key(str(cfg["api_key"]))
    llm_section["config"] = cfg
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(deploy, f, ensure_ascii=False, indent=2)
    return cfg


@llm_bp.route('/config', methods=['GET'])
def get_llm_config():
    """GET /api/llm/config 获取当前 LLM 配置（api_key 脱敏）。"""
    try:
        engine = _get_llm_engine()
        config = {}
        if engine is not None:
            config = dict(getattr(engine, "config", {}) or {})
        if not config:
            from prog.core.llm_provider import _load_default_llm_config
            config = _load_default_llm_config()
        api_key = config.get("api_key", "") or ""
        return api_response(code=0, data={
            "type": config.get("type", "openai_compatible"),
            "base_url": config.get("base_url", ""),
            "model": config.get("model", ""),
            "api_key": _mask_api_key(api_key),
            "has_api_key": bool(api_key),
            "temperature": config.get("temperature", 0.3),
            "max_tokens": config.get("max_tokens", 4096),
            "timeout": config.get("timeout", 60),
            "api_key_env": config.get("api_key_env", "LLM_API_KEY"),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@llm_bp.route('/config', methods=['PUT'])
def update_llm_config():
    """PUT /api/llm/config 更新 LLM 配置（仅管理员；写 deployment_config.json + 热更新运行中引擎）。

    body 可含：model / base_url / api_key / temperature / max_tokens /
    timeout / api_key_env / type。修改后无需重启即生效。
    """
    try:
        role = _get_current_role()
        if role != "admin":
            return error_response(403, "仅管理员可修改 LLM 配置"), 403
        body = request.get_json(silent=True) or {}
        allowed = ("model", "base_url", "api_key", "temperature",
                   "max_tokens", "timeout", "api_key_env", "type")
        patch = {k: v for k, v in body.items()
                 if k in allowed and v is not None and v != ""}
        if not patch:
            return error_response(400, "无可更新字段"), 400
        # S9：base_url 变更时校验 SSRF--禁止指向内网/保留地址
        if "base_url" in patch:
            from prog.utils.url_guard import validate_url
            try:
                validate_url(patch["base_url"], context="LLM base_url")
            except ValueError as ve:
                return error_response(400, str(ve)), 400
        # 数值字段类型校验
        for num_key in ("temperature", "max_tokens", "timeout"):
            if num_key in patch:
                try:
                    patch[num_key] = int(patch[num_key]) if num_key != "temperature" else float(patch[num_key])
                except (TypeError, ValueError):
                    return error_response(400, f"{num_key} 必须为数字"), 400
        cfg = _write_llm_config_patch(patch)
        # 热更新运行中的引擎：重建 provider + 更新 config
        engine = _get_llm_engine()
        updated = False
        if engine is not None:
            from prog.core.llm_provider import create_llm_provider
            new_config = dict(getattr(engine, "config", {}) or {})
            new_config.update(patch)
            # 仅当显式提供 api_key 时覆盖，否则保留原 key（落盘为混淆值，需还原）
            if "api_key" not in patch and not new_config.get("api_key"):
                new_config["api_key"] = _deobfuscate_api_key(cfg.get("api_key", ""))
            engine.llm_provider = create_llm_provider(new_config)
            engine.config = new_config
            updated = True
        safe = {k: v for k, v in cfg.items() if k != "api_key"}
        return api_response(code=0, data={
            "msg": "LLM 配置已更新" + ("（已热更新运行中的引擎）" if updated else "（重启后生效）"),
            "config": safe,
            "has_api_key": bool(cfg.get("api_key") or (engine and getattr(engine, "config", {}).get("api_key"))),
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


@llm_bp.route('/test', methods=['POST'])
def test_llm_connection():
    """POST /api/llm/test 测试 LLM 连接（body 可携带候选配置，缺省用当前配置）。

    W2 防 SSRF：base_url 不在可覆盖字段内——测试仅复用已配置（管理员经
    PUT /config 维护）的 base_url，不接受调用方传入任意 URL。
    """
    try:
        body = request.get_json(silent=True) or {}
        # 仅允许覆盖非网络地址类字段；base_url 固定取已配置值（防 SSRF）
        allowed = ("model", "api_key", "temperature", "max_tokens",
                   "timeout", "type", "api_key_env")
        candidate = {k: v for k, v in body.items()
                     if k in allowed and v is not None and v != ""}
        from prog.core.llm_provider import create_llm_provider
        if candidate:
            engine = _get_llm_engine()
            cur = dict(getattr(engine, "config", {}) or {})
            cur.update(candidate)
            provider = create_llm_provider(cur)
        else:
            provider = create_llm_provider()
        t0 = time.time()
        result = provider.chat(
            [{"role": "user", "content": "你好，请仅回复：OK"}],
            tools=None, temperature=0, max_tokens=16,
        )
        elapsed = round((time.time() - t0) * 1000, 1)
        content = result.get("content", "") or ""
        return api_response(code=0, data={
            "ok": True,
            "reply": content[:100],
            "elapsed_ms": elapsed,
        })
    except Exception as e:
        return error_response(500, f"连接测试失败：{str(e) if DEBUG else '内部错误'}"), 500


# --------------------------------------------------------
# 可用模型列表
# --------------------------------------------------------
@llm_bp.route('/models', methods=['GET'])
def get_models():
    """GET /api/llm/models 可用模型列表。"""
    try:
        return api_response(code=0, data={"providers": LLM_PROVIDERS})
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# --------------------------------------------------------
# Token使用统计
# --------------------------------------------------------
@llm_bp.route('/usage', methods=['GET'])
def get_usage():
    """GET /api/llm/usage Token使用统计（S12：真实查询 llm_usage 表）。"""
    try:
        days = request.args.get('days', 7, type=int)
        db = _get_db()

        enabled = False
        by_model = []
        trend = []
        if db:
            try:
                # S12：llm_usage 表已由迁移 080 创建；GROUP BY/日期分组需原生 SQL
                #（query_many 仅支持简单等值过滤），经 db._connect() + text 查询
                from sqlalchemy import text
                with db._connect() as conn:
                    rows = conn.execute(text(
                        "SELECT model, "
                        "SUM(prompt_tokens) AS prompt_tokens, "
                        "SUM(completion_tokens) AS completion_tokens, "
                        "SUM(total_tokens) AS total_tokens, "
                        "SUM(cost_yuan) AS cost_yuan, "
                        "COUNT(*) AS calls "
                        "FROM llm_usage GROUP BY model "
                        "ORDER BY total_tokens DESC"
                    )).fetchall()
                    for r in rows:
                        m = dict(r._mapping)
                        by_model.append({
                            "model": m.get("model", ""),
                            "prompt_tokens": int(m.get("prompt_tokens") or 0),
                            "completion_tokens": int(m.get("completion_tokens") or 0),
                            "total_tokens": int(m.get("total_tokens") or 0),
                            "cost_yuan": float(m.get("cost_yuan") or 0),
                            "calls": int(m.get("calls") or 0),
                        })
                    trend_rows = conn.execute(text(
                        "SELECT date(created_at) AS day, "
                        "SUM(total_tokens) AS total_tokens, "
                        "SUM(cost_yuan) AS cost_yuan, "
                        "COUNT(*) AS calls "
                        "FROM llm_usage "
                        "WHERE created_at >= CURRENT_DATE - (:days * INTERVAL '1 day') "
                        "GROUP BY date(created_at) ORDER BY day"
                    ), {"days": days}).fetchall()
                    for r in trend_rows:
                        m = dict(r._mapping)
                        trend.append({
                            "day": str(m.get("day") or ""),
                            "total_tokens": int(m.get("total_tokens") or 0),
                            "cost_yuan": float(m.get("cost_yuan") or 0),
                            "calls": int(m.get("calls") or 0),
                        })
                enabled = True
            except Exception:
                # 表不存在（迁移未执行）/DB 不可达：返回空统计，不 500
                by_model = []
                trend = []
                enabled = False

        # 汇总（按 model 分组结果聚合）
        total_prompt = sum(m.get('prompt_tokens', 0) or 0 for m in by_model)
        total_completion = sum(m.get('completion_tokens', 0) or 0 for m in by_model)
        total_tokens = sum(m.get('total_tokens', 0) or 0 for m in by_model)
        total_calls = sum(m.get('calls', 0) or 0 for m in by_model)
        total_cost = round(sum(m.get('cost_yuan', 0) or 0 for m in by_model), 6)

        return api_response(code=0, data={
            "days": days,
            "enabled": enabled,
            "items": by_model,
            "trend": trend,
            "summary": {
                "total_prompt_tokens": total_prompt,
                "total_completion_tokens": total_completion,
                "total_tokens": total_tokens,
                "total_calls": total_calls,
                "total_cost_yuan": total_cost,
            },
        })
    except Exception as e:
        return error_response(500, str(e) if DEBUG else "内部错误"), 500


# ============================================================
# DEBUG 自检（发行版自动跳过，仅在 PROG_DEBUG=1 时执行）
# 验证基座：模块导入、Blueprint定义、核心路由完整性
# ============================================================
def _self_test():
    """DEBUG模式自检：验证模块基座正确"""
    from prog.core.debug import hello_world
    assert llm_bp is not None, "llm_bp 未定义"
    hello_world(__name__, "llm_bp 定义完整")


from prog.core.debug import DEBUG
if DEBUG:
    _self_test()
