"""
轻量任务调度器（v6.83 通用能力第1档）
====================================

文件用途：
    实现进程内轻量调度器：注册的定时任务在独立守护线程中每分钟轮询一次，
    到点执行并把执行状态/最后运行日期持久化到 scheduled_tasks 表
    （无数据库时降级为进程内存记账，重启后可能重复触发当日任务）。

设计说明：
    1. 零第三方依赖（仅标准库 threading/time/re/datetime/dataclasses），
       不引入 apscheduler，避免调度线程阻塞业务。
    2. 任务通过 register() 注册（任务ID + 处理器回调），
       enabled/schedule_expr/last_run_date 以 scheduled_tasks 表为准
       （管理员可改库停用/调整时间）；表行缺失时首次注册自动补行。
    3. 调度表达式支持两种格式：
       - 每日定点 "HH:MM"（24 小时制，兼容 "8:30"）——原有格式；
       - 简化 cron "cron:minute hour * * *"（C1：空格分隔 5 段，minute/hour
         支持数字精确匹配或 * 恒匹配，其余 3 段忽略，按每日最近触发时间执行）。
       同一任务同一自然日只执行一次（last_run_date 去重，防重复通知）。
    4. 处理器执行异常不中断调度循环：状态记为 error，异常摘要写入
       last_run_message 便于排查。
    5. 调度线程为 daemon 守护线程：随进程退出自动结束，不阻塞服务关闭。

对应 SPEC.md §「定时任务」说明。

功能清单（规格/变更对照 · 模块拆分方案 M0）：
    应实现功能：
        - 轻量任务调度器：TaskScheduler 单例 + daemon 守护线程每分钟轮询，零第三方依赖（不引 apscheduler）（业务规格书 v6.83 通用能力第1档 / CHANGELOG v39，M0 清单含 scheduler）
        - 调度表达式仅支持每日定点 "HH:MM"（24 小时制，兼容 "8:30"），last_run_date 同日去重防重复通知（业务规格书 v6.83）
        - enabled/schedule_expr/last_run_date 以 scheduled_tasks 表为准（管理员改库即可停用/调时）；表行缺失首次注册自动补行（业务规格书 v6.83 / CHANGELOG v39）
        - 调度任务执行 new_trace/clear_trace 独立 trace_id（v6.84 §4.7.2 排障关联），日报 handler 内写库/审计钩子带上 trace_id（CHANGELOG v40）
    对外接口（方法/API）：
        - ScheduledTask(task_id, handler, schedule_expr="HH:MM", enabled=True)：定时任务定义（handler 为无参回调，返回执行摘要文本）（业务规格书 v6.83）
        - TaskScheduler.get_instance(db=None) -> TaskScheduler：单例（业务规格书 v6.83）
        - TaskScheduler.register(task)：注册任务（重复注册同 task_id 覆盖处理器）并补建 DB 配置行（业务规格书 v6.83）
        - TaskScheduler.start() / stop()：启动/停止调度守护线程（幂等）（业务规格书 v6.83）
        - TaskScheduler.run_due()：立即执行一轮到期检查（供测试/手动触发）（业务规格书 v6.83）
    错误处理要求：
        - 处理器执行异常不中断调度循环：状态记 error + 异常摘要写入 last_run_message（业务规格书 v6.83 / CHANGELOG v39）
        - DB 不可用：降级进程内存记账（重启后可能重复触发当日任务）（业务规格书 v6.83）
        - 调度表达式非法（HH:MM 格式不可解析）：本轮跳过该任务（规格书未明确）
"""

from __future__ import annotations

import datetime
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from prog.runtime.database import get_database

__all__ = ["ScheduledTask", "TaskScheduler"]

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# C1：简化 cron 表达式——"cron:minute hour dom month dow"（空格分隔 5 段，
# minute/hour 支持数字精确匹配、* 恒匹配、*/N 步进（v6.99.2，TG-08 临时
# 授权 5 分钟兜底通知用），dom/month/dow 忽略按每日执行）
_CRON_RE = re.compile(
    r"^cron:\s*(\*|\d{1,2}|\*/\d{1,2})\s+(\*|\d{1,2}|\*/\d{1,2})\s+(\*|\d{1,2}|\*/\d{1,2})\s+(\*|\d{1,2}|\*/\d{1,2})\s+(\*|\d{1,2}|\*/\d{1,2})\s*$",
    re.IGNORECASE)

# P5：分布式锁租约有效期（秒）--调度线程每次 tick 续约，超过该时间未续约则
# 视为 worker 失活，其他 worker 可抢占。需 > tick_interval（60s）留足续约余量。
_LEASE_TTL_SECONDS = 90


@dataclass
class ScheduledTask:
    """定时任务定义。

    属性：
        task_id: 任务唯一ID（与 scheduled_tasks.task_id 对应，如 inventory_daily）
        handler: 无参处理器，返回执行摘要文本（供 last_run_message 记录）
        schedule_expr: 默认调度表达式 "HH:MM"（DB 行存在时以 DB 为准）
        enabled: 默认启用状态（DB 行存在时以 DB 为准）
        task_type: 任务类型（075 迁移列）--system 内置任务（代码注册处理器）；
                   public 公共提醒（到点通知所有 active 用户）；
                   targeted 特殊提醒（仅通知 target_users 指定人，空则回退创建人）
    """
    task_id: str
    handler: Callable[[], Any]
    schedule_expr: str = "08:30"
    enabled: bool = True
    task_type: str = "system"


def _parse_hhmm(expr: str) -> Optional[int]:
    """解析 "HH:MM" 为当日分钟数；格式非法返回 None。"""
    m = _HHMM_RE.match((expr or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _cron_field_match(field: str, value: int) -> bool:
    """简化 cron 字段匹配：* 恒匹配；*/N 步进；数字精确匹配。"""
    field = (field or "").strip()
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            step = int(field[2:])
            return step > 0 and value % step == 0
        except ValueError:
            return False
    return field.isdigit() and int(field) == value


def _parse_expr(expr: str) -> Optional[tuple]:
    """解析调度表达式（C1）。

    支持两种格式：
        - "HH:MM"（或 "H:MM"）-> ("daily", hour, minute)
        - "cron:minute hour * * *" -> ("cron", (minute_field, hour_field, ...))
    非法/无法解析返回 None（调用方本轮跳过该任务，兼容旧格式语义）。

    返回：
        Optional[tuple]：("daily", hour, minute) 或 ("cron", fields 5 元组)
    """
    text = (expr or "").strip()
    if not text:
        return None
    # 旧格式：每日定点 HH:MM
    m = _HHMM_RE.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return ("daily", hour, minute)
        return None
    # C1：简化 cron（cron:minute hour dom month dow）
    cm = _CRON_RE.match(text)
    if cm:
        minute_field, hour_field = cm.group(1), cm.group(2)
        # v6.99.2：支持 */N 步进（校验步长 N 在范围内），数字仍按原范围校验
        for _f, _max in ((minute_field, 59), (hour_field, 23)):
            if _f == "*":
                continue
            if _f.startswith("*/"):
                try:
                    _n = int(_f[2:])
                    if not (1 <= _n <= _max):
                        return None
                except ValueError:
                    return None
            else:
                _v = int(_f)
                if not (0 <= _v <= _max):
                    return None
        return ("cron", tuple(cm.groups()))
    return None


def _next_run(expr: str, now: datetime.datetime) -> Optional[datetime.datetime]:
    """计算调度表达式 expr 的下一次触发时间（C1）。

    - "HH:MM"：返回当日该时刻（若已过点则由 last_run_date 同日去重兜底，
      保持原 _run_due 的 now >= due 判定语义不变）。
    - "cron:minute hour * * *"：返回 now 起最近一个 minute/hour 均匹配的
      时刻（秒/微秒清零；偏移上限 1 天+1 分钟，防异常字段死循环）。

    返回：
        Optional[datetime.datetime]：下一次触发时间；表达式非法返回 None。
    """
    parsed = _parse_expr(expr)
    if parsed is None:
        return None
    kind = parsed[0]
    if kind == "daily":
        _, hour, minute = parsed
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if kind == "cron":
        minute_field, hour_field = parsed[1][0], parsed[1][1]
        # 从当前分钟开始找最近匹配（允许 offset=0：当前分钟即触发）
        for offset in range(24 * 60 + 1):
            t = now + datetime.timedelta(minutes=offset)
            if (_cron_field_match(minute_field, t.minute)
                    and _cron_field_match(hour_field, t.hour)):
                return t.replace(second=0, microsecond=0)
        return None
    return None


class TaskScheduler:
    """轻量任务调度器（单例，进程内守护线程）。

    用法：
        scheduler = TaskScheduler.get_instance()
        scheduler.register(ScheduledTask(task_id="inventory_daily", handler=fn))
        scheduler.start()
    """

    _instance: Optional["TaskScheduler"] = None

    def __init__(self, db: Any = None, tick_interval: int = 60) -> None:
        """初始化任务调度器。

        参数：
            db: 数据库访问层（可空；None 时延时从 runtime 注册处获取，
                仍不可用则降级内存记账 _memory_last_run）
            tick_interval: 守护线程轮询间隔（秒），下限 10s
        装配：_tasks 任务注册表（task_id -> ScheduledTask）、
              _thread 守护线程（start 时创建）、_stop_event 停止信号、
              _lock 线程锁（注册/轮询/记账互斥）
        """
        # 未显式注入时延后取 runtime 注册的数据库（可空，降级内存记账）
        self._db = db
        self._tick_interval = max(10, int(tick_interval))
        self._tasks: Dict[str, ScheduledTask] = {}
        # 内存兜底记账：DB 不可用/无行时的 last_run_date（YYYY-MM-DD）
        self._memory_last_run: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # P5：分布式锁--多 worker 环境下仅持锁 worker 运行调度
        self._worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._has_lease = False

    @classmethod
    def get_instance(cls, db: Any = None) -> "TaskScheduler":
        """获取单例实例。"""
        if cls._instance is None:
            cls._instance = cls(db)
        return cls._instance

    # --------------------------------------------------------
    # 注册与生命周期
    # --------------------------------------------------------
    def register(self, task: ScheduledTask) -> None:
        """注册任务（重复注册同 task_id 覆盖处理器），并补建 DB 配置行。"""
        with self._lock:
            self._tasks[task.task_id] = task
        self._ensure_row(task)

    def start(self) -> None:
        """启动调度守护线程（幂等）。

        P5：多 worker 环境下通过 DB 分布式锁确保仅持锁 worker 运行调度。
        DB 不可用时降级为单实例模式（开发/测试环境无多 worker 问题）。
        """
        if self._thread is not None and self._thread.is_alive():
            return
        # P5：尝试抢锁--失败说明已有其他 worker 持锁，本 worker 不启动调度
        if not self._acquire_lease():
            print(f"[INFO] 调度器锁已被其他 worker 持有，本 worker ({self._worker_id}) 跳过调度")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="task-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止调度线程（幂等，最多等待一个 tick）。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._tick_interval + 5, 10))
            self._thread = None
        # P5：释放锁，允许其他 worker 抢占
        self._release_lease()

    # --------------------------------------------------------
    # P5：分布式锁（scheduler_lease 表）
    # --------------------------------------------------------
    def _acquire_lease(self) -> bool:
        """尝试抢占调度器锁（DB 不可用时降级为单实例，返回 True）。

        抢锁逻辑：INSERT ... ON CONFLICT DO UPDATE WHERE expires_at < NOW()
        --仅当锁已过期时才能抢占成功（rowcount=1），否则失败（rowcount=0）。
        """
        db = self._db_now()
        if db is None:
            return True  # 无 DB 环境（开发/测试）：无锁运行
        try:
            from sqlalchemy import text
            with db._connect() as conn:
                result = conn.execute(text(
                    "INSERT INTO scheduler_lease (id, worker_id, acquired_at, expires_at) "
                    "VALUES (1, :wid, NOW(), NOW() + INTERVAL '1 second' * :ttl) "
                    "ON CONFLICT (id) DO UPDATE "
                    "SET worker_id = :wid, acquired_at = NOW(), "
                    "    expires_at = NOW() + INTERVAL '1 second' * :ttl "
                    "WHERE scheduler_lease.expires_at < NOW()"
                ).bindparams(wid=self._worker_id, ttl=_LEASE_TTL_SECONDS))
                conn.commit()
                acquired = result.rowcount > 0
                if acquired:
                    self._has_lease = True
                    print(f"[INFO] 调度器锁已获取 ({self._worker_id})")
                return acquired
        except Exception:
            # 表缺失/DB 异常：降级为单实例（避免无锁环境完全不运行调度）
            return True

    def _renew_lease(self) -> bool:
        """续约调度器锁（仅持锁 worker 调用）。续约失败说明锁被抢占，应停止调度。"""
        if not self._has_lease:
            return False
        db = self._db_now()
        if db is None:
            return True
        try:
            from sqlalchemy import text
            with db._connect() as conn:
                result = conn.execute(text(
                    "UPDATE scheduler_lease SET expires_at = NOW() + INTERVAL '1 second' * :ttl "
                    "WHERE id = 1 AND worker_id = :wid"
                ).bindparams(wid=self._worker_id, ttl=_LEASE_TTL_SECONDS))
                conn.commit()
                if result.rowcount == 0:
                    # 锁已被其他 worker 抢占
                    self._has_lease = False
                    return False
                return True
        except Exception:
            return True  # DB 异常时不因续约失败停止调度

    def _release_lease(self) -> None:
        """释放调度器锁（stop 时调用，允许其他 worker 立即抢占）。"""
        if not self._has_lease:
            return
        db = self._db_now()
        if db is None:
            return
        try:
            from sqlalchemy import text
            with db._connect() as conn:
                conn.execute(text(
                    "UPDATE scheduler_lease SET expires_at = NOW() - INTERVAL '1 hour' "
                    "WHERE id = 1 AND worker_id = :wid"
                ).bindparams(wid=self._worker_id))
                conn.commit()
        except Exception:
            pass
        self._has_lease = False

    # --------------------------------------------------------
    # 配置持久化（scheduled_tasks 表）
    # --------------------------------------------------------
    def _db_now(self) -> Any:
        """延时获取数据库句柄（首次调用探测，失败缓存 None 不重复尝试）。

        返回：
            Any: 数据库访问层；不可用时 None（调用方降级内存记账）
        """
        if self._db is not None:
            return self._db
        try:
            self._db = get_database()
        except Exception:
            self._db = None
        return self._db

    def _ensure_row(self, task: ScheduledTask) -> None:
        """任务配置行缺失时补建默认行（幂等，冲突忽略）。"""
        db = self._db_now()
        if db is None:
            return
        try:
            row = db.query_one("scheduled_tasks", {"task_id": task.task_id})
            if row is None:
                db.insert("scheduled_tasks", {
                    "task_id": task.task_id,
                    "task_name": task.task_id,
                    "schedule_expr": task.schedule_expr,
                    "enabled": task.enabled,
                    "task_type": task.task_type,
                })
        except Exception:
            pass  # 表缺失/DB 异常：降级内存记账

    def _read_config(self, task_id: str) -> Dict[str, Any]:
        """读取任务运行配置（DB 优先，缺省用注册值+内存记账）。"""
        out: Dict[str, Any] = {}
        db = self._db_now()
        if db is not None:
            try:
                row = db.query_one("scheduled_tasks", {"task_id": task_id})
                if row:
                    out["enabled"] = bool(row.get("enabled", True))
                    out["schedule_expr"] = row.get("schedule_expr") or ""
                    lr = row.get("last_run_date")
                    out["last_run_date"] = (
                        lr.isoformat() if hasattr(lr, "isoformat") else str(lr or ""))
                    return out
            except Exception:
                pass
        # DB 不可用/无行：回退注册默认 + 内存记账
        task = self._tasks.get(task_id)
        if task is not None:
            out["enabled"] = task.enabled
            out["schedule_expr"] = task.schedule_expr
        out["last_run_date"] = self._memory_last_run.get(task_id, "")
        return out

    def _mark_run(self, task_id: str, run_date: str,
                  status: str, message: str) -> None:
        """记录本次执行结果（内存 + DB 双写）。"""
        with self._lock:
            self._memory_last_run[task_id] = run_date
        db = self._db_now()
        if db is None:
            return
        try:
            db.update("scheduled_tasks", {
                "last_run_date": run_date,
                "last_run_status": status,
                "last_run_message": (message or "")[:500],
                "updated_at": datetime.datetime.now(),
            }, {"task_id": task_id})
        except Exception:
            pass

    # --------------------------------------------------------
    # 调度循环
    # --------------------------------------------------------
    def _loop(self) -> None:
        """守护线程主循环：每 tick 轮询一次到期任务。

        P5：每次 tick 续约调度器锁，续约失败（锁被其他 worker 抢占）则
        停止本 worker 的调度循环，避免多 worker 重复执行任务。
        """
        while not self._stop_event.wait(self._tick_interval):
            # P5：续约锁--失败说明锁已被抢占，退出调度循环
            if not self._renew_lease():
                print(f"[WARN] 调度器锁已丢失 ({self._worker_id})，停止本 worker 调度")
                break
            try:
                self._run_due()
            except Exception:
                pass  # 单次轮询异常不影响后续

    def run_due(self) -> None:
        """立即执行一轮到期检查（供测试/手动触发调用，线程安全）。"""
        self._run_due()

    def _run_due(self) -> None:
        """轮询所有注册任务，执行到点且当日未跑的任务。

        判定链（逐任务）：enabled -> 当日未执行（last_run_date）-> 表达式
        可解析（HH:MM / 简化 cron，C1 _next_run）-> now >= 触发时间；
        满足则调 _execute 执行。
        无参数、无返回；异常由 _execute 内消化，本轮循环不中断。
        """
        today = datetime.date.today().isoformat()
        now = datetime.datetime.now()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            cfg = self._read_config(task.task_id)
            if not cfg.get("enabled", True):
                continue
            if cfg.get("last_run_date") == today:
                continue  # 同一自然日只执行一次
            expr = cfg.get("schedule_expr") or task.schedule_expr
            # C1：统一走 _next_run——HH:MM 与简化 cron 同一判定入口；
            # 表达式非法返回 None，本轮跳过该任务（兼容旧格式）
            next_run = _next_run(expr, now)
            if next_run is None:
                continue  # 调度表达式非法：本轮跳过
            if now < next_run:
                continue  # 未到点
            self._execute(task, today)

    def _execute(self, task: ScheduledTask, run_date: str) -> None:
        """执行单个调度任务并记账。

        参数：
            task: 待执行任务（取 task.handler() 无参调用）
            run_date: 执行日期 YYYY-MM-DD（同日去重与 _mark_run 记账用）
        流程：新建独立 trace_id（v6.84 §4.7.2 排障关联）-> 调用 handler ->
              成功/异常分别记账 status（success/error）与消息摘要；
              handler 异常不影响调度循环。
        """
        status, message = "success", ""
        # v6.84：调度任务执行独立 trace_id（规格书 §4.7.2）--日报 handler
        # 内写库/审计钩子带上 trace_id，便于跨模块排障关联
        try:
            from prog.runtime.trace import new_trace, clear_trace
        except Exception:
            new_trace = None
            clear_trace = None
        if new_trace is not None:
            new_trace()
        try:
            message = str(task.handler() or "")
        except Exception as e:
            status = "error"
            message = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            if clear_trace is not None:
                clear_trace()
        self._mark_run(task.task_id, run_date, status, message)

    # --------------------------------------------------------
    # 公共/特殊提醒任务（DB 行驱动，075 迁移 task_type 列）
    # --------------------------------------------------------
    def load_reminder_tasks(self) -> int:
        """从 scheduled_tasks 加载提醒任务（public/targeted）并注册处理器。

        用户/管理员在 scheduled_tasks 表插入 task_type='public'（公共提醒，
        通知所有 active 用户）或 'targeted'（特殊提醒，通知 target_users
        指定人，为空回退创建人 created_by）的行即可创建定时提醒；启动时
        由本方法装配处理器，到点按行内 title/content 发送通知。

        返回:
            注册的提醒任务数（DB 不可用/表缺失返回 0）
        """
        db = self._db_now()
        if db is None:
            return 0
        try:
            rows = db.query_filtered(
                "scheduled_tasks",
                [{"field": "task_type", "op": "in",
                  "value": ["public", "targeted"]}]) or []
        except Exception:
            return 0  # 表缺失（075 未执行）/DB 异常：跳过提醒任务加载
        count = 0
        for row in rows:
            task_id = row.get("task_id") or ""
            if not task_id:
                continue
            self.register(ScheduledTask(
                task_id=task_id,
                handler=_make_reminder_handler(task_id),
                schedule_expr=str(row.get("schedule_expr") or "08:30"),
                enabled=bool(row.get("enabled", True)),
                task_type=str(row.get("task_type") or "targeted"),
            ))
            count += 1
        return count


def _make_reminder_handler(task_id: str) -> Callable[[], str]:
    """构造提醒任务处理器闭包（执行时读自身行，目标以最新 DB 配置为准）。

    分发规则（公共/特殊任务区分）：
        - task_type='public'：通知所有 active 用户（提醒所有人）
        - task_type='targeted'：仅通知 target_users（JSONB 数组）指定用户；
          未指定任何用户时回退通知创建人 created_by
    """
    def _handler() -> str:
        from prog.api.notifications_api import create_notification
        db = get_database()
        if db is None:
            return "数据库不可用，提醒跳过"
        row = db.query_one("scheduled_tasks", {"task_id": task_id}) or {}
        task_type = str(row.get("task_type") or "targeted")
        title = str(row.get("title") or row.get("task_name") or "定时提醒")
        content = str(row.get("content") or title)
        ntype = "info"

        targets: list = []
        if task_type == "public":
            # 公共任务：提醒所有 active 用户
            try:
                users = db.query_many(
                    "users", {"status": "active"}, columns=["user_id"]) or []
                targets = [u["user_id"] for u in users if u.get("user_id")]
            except Exception:
                targets = []
            if not targets:
                return "无 active 用户，公共提醒未发送"
        else:
            # 特殊任务：提醒指定人；未指定回退创建人
            raw = row.get("target_users")
            if isinstance(raw, str):
                try:
                    import json as _json
                    raw = _json.loads(raw)
                except Exception:
                    raw = []
            if isinstance(raw, (list, tuple)):
                targets = [str(t) for t in raw if t]
            if not targets:
                creator = str(row.get("created_by") or "")
                targets = [creator] if creator else []
            if not targets:
                return "未指定提醒对象（target_users/created_by 均为空），提醒未发送"

        sent = 0
        for uid in targets:
            if create_notification(ntype, title, content, target_user=uid,
                                   extra={"scheduled_task": task_id}):
                sent += 1
        scope = "公共（全部 active 用户）" if task_type == "public" else "指定对象"
        return f"提醒已发送 {sent}/{len(targets)} 人（{scope}）：{title}"

    return _handler
