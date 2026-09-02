"""
Webhook 出站分发器（S8）
========================
文件用途：
    业务事件 → webhooks 订阅匹配（events JSONB 数组）→ HMAC-SHA256 签名
    POST → webhook_deliveries 投递记录（sent / failed / dead 死信）。

设计要点：
    - 零阻断：所有 DB/网络异常捕获，绝不向上抛出（webhook 分发不阻断业务）。
    - 表不存在（083 迁移未执行）或 DB 不可达时静默跳过（log warning）。
    - 惰性 import：函数内 import requests / get_database，避免模块加载期循环依赖。
    - 重试：失败立即重试最多 3 次（间隔 1s），全部失败标记 dead（死信）。
    - 事件匹配：webhooks.events 为 JSONB 数组，query_many 后 Python 侧过滤。

对应迁移：
    - migrations/083_webhooks.sql（webhooks / webhook_deliveries 表）
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List

_logger = logging.getLogger("prog.webhook_dispatcher")

_MAX_ATTEMPTS = 3          # 每次事件分发最多尝试 3 次（含首次）
_RETRY_INTERVAL = 1.0      # 失败重试间隔（秒）
_REQUEST_TIMEOUT = 5       # POST 超时（秒）

#: 环境变量可覆盖的重试/超时配置（解析失败回退默认值）
#: 集中登记于 prog/.env 与 .env.example 的「通知通道（S1）」分组（webhook 事件分发 S8）
for _env_key, _attr, _default in (
    ("WEBHOOK_DISPATCH_MAX_ATTEMPTS", "_MAX_ATTEMPTS", _MAX_ATTEMPTS),
    ("WEBHOOK_DISPATCH_RETRY_INTERVAL", "_RETRY_INTERVAL", _RETRY_INTERVAL),
    ("WEBHOOK_DISPATCH_REQUEST_TIMEOUT", "_REQUEST_TIMEOUT", _REQUEST_TIMEOUT),
):
    _raw = os.environ.get(_env_key, "").strip()
    if _raw:
        try:
            _parsed = int(float(_raw)) if _env_key == "WEBHOOK_DISPATCH_MAX_ATTEMPTS" else float(_raw)
            if _parsed > 0:
                globals()[_attr] = _parsed
        except ValueError:
            _logger.warning("环境变量 %s 解析失败，使用默认值 %s", _env_key, _default)


def dispatch_event(event: str, payload: dict) -> None:
    """分发业务事件到所有匹配的活跃 webhook（S8）。

    参数：
        event: 事件名（如 "order.created" / "qc.record_created"）
        payload: 事件载荷（JSON 可序列化字典）

    返回：
        无。任何异常均被吞掉（webhook 分发不阻断业务），仅记日志。
    """
    try:
        from prog.core.database import get_database
        db = get_database()
        if db is None:
            return
        hooks = db.query_many("webhooks", {"is_active": True}) or []
    except Exception as e:  # 表不存在（迁移未执行）/ DB 不可达：静默跳过
        _logger.warning("webhook 分发跳过：webhooks 表不可用（迁移未执行或 DB 不可达）: %s", e)
        return

    for hook in hooks:
        try:
            _deliver(db, hook, event, payload)
        except Exception:
            # 单 webhook 分发失败不阻断其他订阅（_deliver 内部已兜底，此为双保险）
            _logger.exception("webhook 分发异常 webhook_id=%s event=%s",
                              hook.get("webhook_id"), event)


def _deliver(db: Any, hook: Dict[str, Any], event: str, payload: dict) -> None:
    """向单个 webhook 投递事件：签名 POST + 失败重试（最多 3 次）+ 投递记录。"""
    webhook_id = hook.get("webhook_id")
    if event not in _hook_events(hook):
        return

    body = json.dumps(
        {"event": event, "payload": payload,
         "ts": datetime.now().isoformat()},
        ensure_ascii=False, default=str)
    url = str(hook.get("url") or "").strip()
    if not url:
        _record_delivery(db, webhook_id, event, payload,
                         "dead", _MAX_ATTEMPTS, "webhook url 为空")
        return

    signature = _sign(str(hook.get("secret") or ""), body)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "User-Agent": "AI-Factory-Webhook/1.0",
    }

    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            import requests
            resp = requests.post(url, data=body, headers=headers,
                                 timeout=_REQUEST_TIMEOUT)
            if 200 <= resp.status_code < 300:
                _record_delivery(db, webhook_id, event, payload,
                                 "sent", attempt, None)
                return
            last_error = f"HTTP {resp.status_code}"
        except Exception as e:  # 网络/连接/超时异常
            last_error = f"{type(e).__name__}: {e}"

        # 本次尝试失败：写 failed 记录；未达上限则间隔 1s 重试，全失败写 dead（死信）
        _record_delivery(db, webhook_id, event, payload,
                         "failed" if attempt < _MAX_ATTEMPTS else "dead",
                         attempt, last_error)
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_INTERVAL)


def _hook_events(hook: Dict[str, Any]) -> List[str]:
    """解析 webhooks.events（JSONB 数组，psycopg2 可能返回 str 或 list）。"""
    events = hook.get("events")
    if isinstance(events, list):
        return [str(e) for e in events]
    if isinstance(events, str):
        try:
            parsed = json.loads(events)
            if isinstance(parsed, list):
                return [str(e) for e in parsed]
        except Exception:
            pass
    return []


def _sign(secret: str, body: str) -> str:
    """HMAC-SHA256 签名：hexdigest 放入 X-Webhook-Signature 请求头。"""
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _record_delivery(db: Any, webhook_id: Any, event: str, payload: dict,
                     status: str, attempt: int, last_error: Any) -> None:
    """写 webhook_deliveries 投递记录；表不存在/写入失败仅记日志，不影响分发。"""
    try:
        db.insert("webhook_deliveries", {
            "webhook_id": webhook_id,
            "event": event,
            "payload": payload,
            "status": status,
            "attempt": attempt,
            "last_error": last_error,
        })
    except Exception as e:
        _logger.warning("webhook_deliveries 写入失败（仅记日志）: %s", e)
