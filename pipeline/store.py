"""SQLite 持久化：任务与逐条抽取状态（pending/running/done/failed）。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import settings
from .schema import FieldSpec

_DB_PATH: Path = settings.db_path
_INITIALIZED: set[str] = set()
_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    mode        TEXT NOT NULL,
    table_path  TEXT NOT NULL,
    sheet       TEXT NOT NULL,
    header_row  INTEGER NOT NULL,
    specs_json  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    total       INTEGER NOT NULL DEFAULT 0,
    meta_json   TEXT
);
CREATE TABLE IF NOT EXISTS items (
    item_id     TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    source_name TEXT,
    record_id   TEXT,
    name_hint   TEXT,
    text        TEXT NOT NULL,
    text_hash   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    result_json TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_job_status ON items(job_id, status);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_db_path(path: str | Path) -> None:
    """测试用：切换数据库文件并重置初始化状态。"""
    global _DB_PATH
    with _LOCK:
        _DB_PATH = Path(path)
        _INITIALIZED.discard(str(_DB_PATH))


def _connect() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


@contextmanager
def _connection():
    """每次操作独立连接，用完即关，避免 Windows 下 WAL 文件句柄泄漏。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _LOCK:
        if str(_DB_PATH) in _INITIALIZED and _DB_PATH.exists():
            return
        with _connection() as conn:
            conn.executescript(_SCHEMA)
        _INITIALIZED.add(str(_DB_PATH))


def _ensure_db() -> None:
    init_db()


def create_job(
    mode: str,
    table_path: str,
    sheet: str,
    header_row: int,
    specs: list[FieldSpec],
    meta: dict | None = None,
    job_id: str | None = None,
) -> str:
    _ensure_db()
    job_id = job_id or uuid.uuid4().hex[:12]
    now = _now()
    with _connection() as conn:
        conn.execute(
            "INSERT INTO jobs (job_id, created_at, updated_at, mode, table_path, sheet, header_row,"
            " specs_json, status, total, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, now, mode, table_path, sheet, int(header_row),
             json.dumps([s.model_dump() for s in specs], ensure_ascii=False),
             "pending", 0, json.dumps(meta or {}, ensure_ascii=False)),
        )
    return job_id


def add_items(job_id: str, items: list[dict]) -> list[str]:
    """items: [{source_name, record_id, name_hint, text}]"""
    _ensure_db()
    ids: list[str] = []
    now = _now()
    with _connection() as conn:
        for idx, item in enumerate(items):
            item_id = uuid.uuid4().hex[:12]
            text = item.get("text", "") or ""
            conn.execute(
                "INSERT INTO items (item_id, job_id, idx, source_name, record_id, name_hint, text,"
                " text_hash, status, attempts, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, job_id, idx, item.get("source_name", ""), item.get("record_id", ""),
                 item.get("name_hint"), text, hashlib.sha1(text.encode("utf-8")).hexdigest(),
                 "pending", 0, now),
            )
            ids.append(item_id)
        total = conn.execute("SELECT COUNT(*) FROM items WHERE job_id=?", (job_id,)).fetchone()[0]
        conn.execute("UPDATE jobs SET total=?, updated_at=? WHERE job_id=?", (total, now, job_id))
    return ids


def mark_running(item_id: str) -> None:
    _ensure_db()
    with _connection() as conn:
        conn.execute(
            "UPDATE items SET status='running', attempts=attempts+1, error=NULL, updated_at=? WHERE item_id=?",
            (_now(), item_id),
        )


def mark_done(item_id: str, result: dict) -> None:
    _ensure_db()
    with _connection() as conn:
        conn.execute(
            "UPDATE items SET status='done', result_json=?, error=NULL, updated_at=? WHERE item_id=?",
            (json.dumps(result, ensure_ascii=False), _now(), item_id),
        )


def mark_failed(item_id: str, error: str) -> None:
    _ensure_db()
    with _connection() as conn:
        conn.execute(
            "UPDATE items SET status='failed', error=?, updated_at=? WHERE item_id=?",
            (error[:500], _now(), item_id),
        )


def reset_failed(job_id: str) -> int:
    _ensure_db()
    with _connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM items WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
        conn.execute(
            "UPDATE items SET status='pending', error=NULL, updated_at=? WHERE job_id=? AND status='failed'",
            (_now(), job_id),
        )
    return int(count)


def fetch_items(job_id: str, statuses: list[str] | None = None) -> list[dict]:
    _ensure_db()
    sql = "SELECT * FROM items WHERE job_id=?"
    params: list = [job_id]
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        sql += f" AND status IN ({placeholders})"
        params.extend(statuses)
    sql += " ORDER BY idx ASC"
    with _connection() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        row["result"] = json.loads(row["result_json"]) if row.get("result_json") else None
    return rows


def get_job(job_id: str) -> dict | None:
    _ensure_db()
    with _connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["specs"] = [FieldSpec(**s) for s in json.loads(data["specs_json"])]
    data["meta"] = json.loads(data["meta_json"]) if data.get("meta_json") else {}
    return data


def job_stats(job_id: str) -> dict:
    _ensure_db()
    with _connection() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall()}
        total = conn.execute("SELECT COUNT(*) FROM items WHERE job_id=?", (job_id,)).fetchone()[0]
        job = conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    stats = {
        "total": int(total),
        "pending": int(counts.get("pending", 0)),
        "running": int(counts.get("running", 0)),
        "done": int(counts.get("done", 0)),
        "failed": int(counts.get("failed", 0)),
        "status": job["status"] if job else "unknown",
    }
    return stats


def update_job_status(job_id: str, status: str | None = None) -> str:
    _ensure_db()
    stats = job_stats(job_id)
    if status is None:
        if stats["running"] == 0 and stats["pending"] == 0:
            status = "done" if stats["failed"] == 0 else "partial"
        else:
            status = "running"
    with _connection() as conn:
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (status, _now(), job_id))
    return status


def list_jobs(limit: int = 20) -> list[dict]:
    _ensure_db()
    with _connection() as conn:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()]
        for job in jobs:
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM items WHERE job_id=? GROUP BY status", (job["job_id"],)
            ).fetchall()}
            job["counts"] = {k: int(v) for k, v in counts.items()}
            job["total"] = int(sum(counts.values()))
    return jobs
