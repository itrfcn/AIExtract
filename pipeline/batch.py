"""批量调度：线程池并发 + 逐条状态持久化 + 失败重跑。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from core.config import settings
from . import store
from .extract import extract_record
from .providers import BaseProvider
from .schema import FieldSpec

ProgressCallback = Callable[[int, int, dict], None]


def _work_item(item: dict, specs: list[FieldSpec], provider: BaseProvider, threshold: float) -> dict:
    store.mark_running(item["item_id"])
    try:
        cells = extract_record(provider, item["text"], specs, threshold)
        store.mark_done(item["item_id"], cells)
        return {
            "item_id": item["item_id"],
            "status": "done",
            "source_name": item.get("source_name", ""),
            "record_id": item.get("record_id", ""),
            "name_hint": item.get("name_hint"),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮批次
        message = f"{type(exc).__name__}: {exc}"
        store.mark_failed(item["item_id"], message)
        return {
            "item_id": item["item_id"],
            "status": "failed",
            "source_name": item.get("source_name", ""),
            "record_id": item.get("record_id", ""),
            "name_hint": item.get("name_hint"),
            "error": message,
        }


def run_batch(
    job_id: str,
    specs: list[FieldSpec],
    provider: BaseProvider,
    concurrency: int = 4,
    threshold: float | None = None,
    progress_cb: ProgressCallback | None = None,
    only_failed: bool = False,
) -> dict:
    """执行任务中 pending（或 failed）的条目，返回最新统计。"""
    threshold = settings.confidence_threshold if threshold is None else threshold
    statuses = ["failed"] if only_failed else ["pending"]
    items = store.fetch_items(job_id, statuses)
    total = len(items)
    if total == 0:
        return store.job_stats(job_id)

    workers = max(1, min(16, int(concurrency)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract") as executor:
        futures = {
            executor.submit(_work_item, item, specs, provider, threshold): item
            for item in items
        }
        for index, future in enumerate(as_completed(futures), 1):
            outcome = future.result()
            if progress_cb:
                try:
                    progress_cb(index, total, outcome)
                except Exception:
                    pass

    store.update_job_status(job_id)
    return store.job_stats(job_id)


def retry_failed(job_id: str, specs: list[FieldSpec], provider: BaseProvider, **kwargs) -> dict:
    """把失败项重置为 pending 后重跑；此时 pending 里就是待重试的条目。"""
    retried = store.reset_failed(job_id)
    if retried == 0:
        return store.job_stats(job_id)
    return run_batch(job_id, specs, provider, only_failed=False, **kwargs)
