"""
训练/对话数据增量上报器
======================
用途：
    将本地训练样本与对话记录增量上报到公共数据库（checkpoint 游标幂等）。

采集范围（仅基础数据，不含合规审计数据）：
    - training_data          用户审批通过后的训练样本（approved=TRUE）
    - conversation_messages  对话记录（脱敏后）

机制：
    - checkpoint：本地 community_upload_checkpoint 表记录各源 last_id
    - 幂等：按 > last_id 增量抽取，上报成功推进游标
    - 脱敏：复用 prog.llm.desensitizer（若不可用则跳过脱敏仅上报业务字段）
    - 定时：由 scheduler 注册（默认 5 分钟），或手动调用 upload_once()
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CHECKPOINT_TABLE = "community_upload_checkpoint"
_SOURCES = ("training_data", "conversation_messages")


def _get_db():
    """本地库连接。"""
    try:
        from prog.runtime.database import get_database
        return get_database()
    except Exception:  # noqa: BLE001
        return None


def _get_community():
    """公共库连接器。"""
    from community.db_connector import CommunityDBConnector
    return CommunityDBConnector.get_instance()


def _get_checkpoint(source: str) -> int:
    """读取上报游标。"""
    db = _get_db()
    if db is None:
        return 0
    try:
        row = db.query_one(_CHECKPOINT_TABLE, {"source": source})
        if row:
            return int(row.get("last_id") or 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _set_checkpoint(source: str, last_id: int) -> None:
    """推进上报游标。"""
    db = _get_db()
    if db is None:
        return
    try:
        existing = db.query_one(_CHECKPOINT_TABLE, {"source": source})
        if existing:
            db.update(_CHECKPOINT_TABLE, {"last_id": last_id},
                      {"source": source})
        else:
            db.insert(_CHECKPOINT_TABLE,
                      {"source": source, "last_id": last_id})
    except Exception:  # noqa: BLE001
        pass


def _collect_training_data(last_id: int, limit: int = 100) -> tuple:
    """抽取已审批训练样本。"""
    db = _get_db()
    if db is None:
        return [], 0
    rows = db.query_many(
        "training_data",
        {"approved": True},
        order_by="id ASC",
        limit=limit,
    ) or []
    rows = [r for r in rows if int(r.get("id") or 0) > last_id]
    new_last = max((int(r.get("id") or 0) for r in rows), default=last_id)
    return rows, new_last


def _collect_conversations(last_id: int, limit: int = 100) -> tuple:
    """抽取对话记录（脱敏）。"""
    db = _get_db()
    if db is None:
        return [], 0
    rows = db.query_many(
        "conversation_messages", {},
        order_by="message_id ASC",
        limit=limit,
    ) or []
    rows = [r for r in rows
            if int(r.get("message_id") or 0) > last_id]
    new_last = max((int(r.get("message_id") or 0) for r in rows),
                   default=last_id)
    return rows, new_last


def _mask_row(row: Dict[str, Any], keep: List[str]) -> Dict[str, Any]:
    """脱敏：仅保留业务字段（复用 Desensitizer 若可用）。"""
    out = {k: row.get(k) for k in keep if k in row}
    try:
        from prog.llm.desensitizer import Desensitizer
        d = Desensitizer()
        for k in ("user_input", "ai_output", "content", "message_content"):
            if k in out and isinstance(out[k], str):
                out[k] = d.desensitize(out[k])
    except Exception:  # noqa: BLE001
        pass
    return out


def upload_once() -> Dict[str, int]:
    """执行一次增量上报，返回各源上报条数。"""
    community = _get_community()
    if community is None:
        return {"skipped": True}
    result: Dict[str, int] = {}
    for source in _SOURCES:
        last_id = _get_checkpoint(source)
        if source == "training_data":
            rows, new_last = _collect_training_data(last_id)
            keep = ["id", "agent_type", "intent", "user_input",
                    "ai_output", "user_correction", "final_output"]
        else:
            rows, new_last = _collect_conversations(last_id)
            keep = ["message_id", "session_id", "intent",
                    "user_input", "message_content"]
        masked = [_mask_row(r, keep) for r in rows]
        ok = community.upload(source, masked) if masked else True
        if ok and new_last > last_id:
            _set_checkpoint(source, new_last)
        result[source] = len(masked)
    return result


def register_upload_task(scheduler) -> None:
    """注册定时上报任务（默认 5 分钟，可经 env 调整间隔分钟）。"""
    if scheduler is None:
        return
    interval = int(os.environ.get("COMMUNITY_UPLOAD_INTERVAL", "5"))
    if interval <= 0:
        return
    from prog.runtime.scheduler import ScheduledTask
    scheduler.register(ScheduledTask(
        task_id="community_upload",
        handler=upload_once,
        schedule_expr=f"cron:*/{interval} * * * *",
    ))
