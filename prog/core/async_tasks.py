"""
S10 消息队列/异步任务骨架（可商用部署功能补充建议 · S10）
==========================================================
文件用途：
    提供统一的异步任务提交入口 submit_task 与任务装饰器 async_task_decorator：
    - Celery 可用（import celery 成功且 CELERY_BROKER_URL 已设置）时入队；
    - 否则降级为进程内 ThreadPoolExecutor（单例）执行；
    - 所有路径包 try/except，失败记日志，绝不阻断调用方。

示例任务：
    run_export_report(kind)          报表导出（骨架）
    run_vectorize_document(doc_id)   文档向量化（骨架）
"""

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

_logger = logging.getLogger("prog.async_tasks")

#: 已注册的异步任务表（装饰器注册，submit_task 按名查找）
_TASKS: dict = {}

#: 线程池单例（Celery 不可用时的降级执行器）
_executor: ThreadPoolExecutor = None
_executor_lock = threading.Lock()

#: Celery app 单例（延迟初始化）
_celery_app = None
_celery_app_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """获取线程池单例（并发安全）。"""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                # ASYNC_MAX_WORKERS：线程池最大并发数（集中登记于 .env「异步任务」分组）
                try:
                    _max_workers = max(1, int(os.environ.get("ASYNC_MAX_WORKERS", "4")))
                except ValueError:
                    _max_workers = 4
                _executor = ThreadPoolExecutor(
                    max_workers=_max_workers, thread_name_prefix="async-task")
    return _executor


def _celery_available() -> bool:
    """Celery 可用性：CELERY_BROKER_URL 已设置且 import celery 成功。"""
    if not os.environ.get("CELERY_BROKER_URL", "").strip():
        return False
    try:
        import celery  # noqa: F401
        return True
    except Exception:
        return False


def _get_celery_app():
    """获取 Celery app 单例（延迟初始化，失败返回 None）。"""
    global _celery_app
    if _celery_app is None:
        with _celery_app_lock:
            if _celery_app is None:
                try:
                    from celery import Celery
                    _celery_app = Celery(
                        "ai_factory",
                        broker=os.environ.get("CELERY_BROKER_URL", ""),
                    )
                except Exception as e:
                    _logger.error("Celery app 初始化失败：%s", e)
                    _celery_app = None
    return _celery_app


def submit_task(name: str, *args, **kwargs):
    """提交异步任务。

    参数:
        name: 任务名（须已通过 async_task_decorator 注册）
        args/kwargs: 透传给任务函数

    返回:
        Celery 路径返回 AsyncResult；线程池路径返回 None（fire-and-forget）；
        未知任务返回 None 并记 warning。
    """
    fn = _TASKS.get(name)
    if fn is None:
        _logger.warning("未知异步任务 %r（可用：%s）", name, ", ".join(_TASKS) or "-")
        return None
    # Celery 路径：入队（失败回退线程池）
    if _celery_available():
        try:
            app = _get_celery_app()
            if app is not None:
                task = app.task(fn, name=name)
                return task.delay(*args, **kwargs)
        except Exception as e:
            _logger.error("Celery 入队失败（%s），回退线程池执行 %r", e, name)
    # 线程池路径：单例提交，失败记日志不抛出
    try:
        _get_executor().submit(fn, *args, **kwargs)
    except Exception as e:
        _logger.error("线程池提交任务 %r 失败：%s", name, e)
    return None


def async_task_decorator(fn):
    """异步任务装饰器：将函数注册到 _TASKS 表，调用时走 submit_task 异步执行。

    用法：
        @async_task_decorator
        def run_export_report(kind): ...

        submit_task("run_export_report", "orders")   # 或直接 run_export_report("orders")
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        return submit_task(fn.__name__, *args, **kwargs)

    _TASKS[fn.__name__] = fn
    return wrapper


# --------------------------------------------------------
# 示例任务
# --------------------------------------------------------
@async_task_decorator
def run_export_report(kind: str = "orders") -> None:
    """异步导出报表（骨架）：接入现有导出逻辑或 pass。

    实施时调用现有报表生成逻辑（如 reportlab / data_api 导出），
    完成后经 notifications 通知创建人。
    """
    try:
        _logger.info("异步导出任务开始 kind=%s", kind)
        # TODO: 接入现有导出逻辑（报表生成 + 文件落库 + 通知）
    except Exception as e:
        _logger.error("异步导出失败 kind=%s：%s", kind, e)


@async_task_decorator
def run_vectorize_document(doc_id: str) -> None:
    """异步文档向量化（骨架）：调用现有向量化逻辑或 pass。

    实施时读取文件分块 -> embedding -> 写入向量库（MilvusVectorStore），
    失败由现有降级路径兜底（关键词检索）。
    """
    try:
        _logger.info("异步文档向量化开始 doc_id=%s", doc_id)
        try:
            from prog.llm.knowledge_base import KnowledgeBase
            kb = KnowledgeBase.get_instance()
            if hasattr(kb, "vectorize_document"):
                kb.vectorize_document(doc_id)
            # 无该方法时保持骨架（pass）
        except Exception as e:
            _logger.warning("文档向量化调用降级 doc_id=%s：%s", doc_id, e)
    except Exception as e:
        _logger.error("异步文档向量化失败 doc_id=%s：%s", doc_id, e)
