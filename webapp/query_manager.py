"""只读 SQL 查询执行器。

设计要点（对应排查页需求）：
- 最多 N 条并发查询（信号量名额），满额时 submit() 返回 None，由上层明确拒绝；
- 每条查询独立只读连接（file:...?mode=ro），并设置 authorizer 只放行 SELECT 所需动作，
  禁止 ATTACH / PRAGMA / 写操作 / load_extension，扩展加载同时被显式关闭；
- 超时由定时器触发 conn.interrupt() 实现，用户取消走同一路径 —— 真正中断 SQLite 计算，
  而不是仅仅隐藏结果；
- worker 线程在"关闭连接、释放名额"之后才对外落定终态，因此调用方看到 timeout/cancelled
  时，计算必然已经停止、名额必然已经释放，其他查询和数据库文件不受影响；
- 结果只有在完整取回后才挂载到 Job 上，失败时不会暴露半份结果。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote

# authorizer 放行的动作：只读查询所需的最小集合
_ALLOWED_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,    # 21 SELECT 语句本体
        sqlite3.SQLITE_READ,      # 20 读取表/列
        sqlite3.SQLITE_FUNCTION,  # 31 调用 SQL 函数（load_extension 单独拦截）
        sqlite3.SQLITE_RECURSIVE, # 33 递归 CTE / 视图展开
        sqlite3.SQLITE_PRAGMA,    # 19 pragma 函数形式（如 pragma_table_info）只读访问；
                                  #    pragma 函数无法修改值，且文件层 mode=ro 兜底
    }
)

_BLOCKED_FUNCTIONS = frozenset({"load_extension"})

TERMINAL_STATES = frozenset({"done", "error", "timeout", "cancelled"})


def readonly_authorizer(action, arg1, arg2, dbname, source):
    """SQLite authorizer：只放行只读动作，其余一律拒绝。"""
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_UPDATE:
        # pragma 表值函数（pragma_table_info 等）内部会对 sqlite_master 触发
        # SQLITE_UPDATE 记账动作，仅放行这一种；真实 UPDATE 用户表仍被拒绝，
        # 而 sqlite_master 本身未经 writable_schema 也不可写（且文件为只读）。
        if arg1 == "sqlite_master":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _json_safe(value):
    if isinstance(value, bytes):
        return "0x" + value.hex()
    return value


class Job:
    """一条查询的生命周期：queued -> running -> done/error/timeout/cancelled"""

    def __init__(self, db_path, sql):
        self.id = uuid.uuid4().hex
        self.db_path = db_path
        self.sql = sql
        self.status = "queued"
        self.columns = None
        self.rows = None
        self.truncated = False
        self.error = None
        self.created_at = time.monotonic()
        self.started_at = None
        self.finished_at = None
        self.conn = None
        self.thread = None
        self.timer = None
        self.cancel_reason = None  # 'user' | 'timeout'
        self.lock = threading.Lock()

    def cancel(self, reason):
        """请求终止（reason: 'user'/'timeout'）。只打标记并中断连接，终态由 worker 落定。"""
        with self.lock:
            if self.status in TERMINAL_STATES:
                return False
            if self.cancel_reason is None:
                self.cancel_reason = reason
            conn = self.conn
        if conn is not None:
            try:
                conn.interrupt()  # 真正中止 SQLite 正在执行的语句
            except Exception:
                pass
        return True

    def public(self):
        with self.lock:
            end = self.finished_at if self.finished_at is not None else time.monotonic()
            data = {
                "id": self.id,
                "status": self.status,
                "sql": self.sql,
                "truncated": self.truncated,
                "error": self.error,
                "elapsed_ms": int((end - self.started_at) * 1000) if self.started_at else 0,
            }
            if self.status == "done":
                data["columns"] = self.columns
                data["rows"] = self.rows
                data["row_count"] = len(self.rows)
            return data


class QueryManager:
    def __init__(self, max_concurrent=2, timeout=3.0, max_rows=1000, job_ttl=600):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.max_rows = max_rows
        self.job_ttl = job_ttl
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._jobs = {}
        self._lock = threading.Lock()

    # ---------------- 对外接口 ----------------

    def submit(self, db_path, sql):
        """提交查询；名额已满时返回 None（不排队，由上层明确拒绝）。"""
        if not self._slots.acquire(blocking=False):
            return None
        try:
            self._prune()
            job = Job(db_path, sql)
            with self._lock:
                self._jobs[job.id] = job
            t = threading.Thread(
                target=self._run, args=(job,), name=f"query-{job.id[:8]}", daemon=True
            )
            job.thread = t
            t.start()
            return job
        except Exception:
            self._slots.release()
            raise

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def cancel_and_wait(self, job_id, wait=10.0):
        """取消并等待 worker 真正退出后才返回，保证返回时计算已停止、名额已释放。"""
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel("user")
        t = job.thread
        if t is not None:
            t.join(timeout=wait)
        return job

    # ---------------- 内部实现 ----------------

    def _prune(self):
        now = time.monotonic()
        with self._lock:
            stale = [
                jid
                for jid, j in self._jobs.items()
                if j.finished_at is not None and now - j.finished_at > self.job_ttl
            ]
            for jid in stale:
                del self._jobs[jid]

    @staticmethod
    def _status_for(reason):
        return "timeout" if reason == "timeout" else "cancelled"

    @staticmethod
    def connect_readonly(db_path):
        """以只读方式打开库，并装上只读 authorizer、关闭扩展加载。"""
        uri = "file:" + quote(os.path.abspath(db_path)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.enable_load_extension(False)
        conn.set_authorizer(readonly_authorizer)
        return conn

    def _run(self, job):
        conn = None
        outcome = ("error", {"error": "内部错误：查询未执行"})
        try:
            with job.lock:
                early = job.cancel_reason
                if early is None:
                    job.status = "running"
                    job.started_at = time.monotonic()
            if early is not None:
                outcome = (self._status_for(early), {})
            else:
                conn = self.connect_readonly(job.db_path)
                with job.lock:
                    job.conn = conn
                    early = job.cancel_reason
                if early is not None:
                    outcome = (self._status_for(early), {})
                else:
                    timer = threading.Timer(self.timeout, job.cancel, args=("timeout",))
                    timer.daemon = True
                    job.timer = timer
                    timer.start()
                    outcome = self._execute(job, conn)
        except Exception as exc:  # 连接失败等
            outcome = ("error", {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            # 顺序很关键：先停计时器、关连接、释放名额，最后才对外落定终态。
            if job.timer is not None:
                job.timer.cancel()
            with job.lock:
                job.conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._slots.release()
            status, payload = outcome
            with job.lock:
                job.status = status
                job.error = payload.get("error")
                job.columns = payload.get("columns")
                job.rows = payload.get("rows")
                job.truncated = payload.get("truncated", False)
                job.finished_at = time.monotonic()

    def _execute(self, job, conn):
        try:
            cur = conn.cursor()
            cur.execute(job.sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            # 多取一行用于判断是否截断；失败时抛异常，不会把半份结果挂到 Job 上
            raw = cur.fetchmany(self.max_rows + 1)
            truncated = len(raw) > self.max_rows
            rows = [[_json_safe(v) for v in row] for row in raw[: self.max_rows]]
        except Exception as exc:
            with job.lock:
                reason = job.cancel_reason
            if reason is not None:
                return (self._status_for(reason), {})
            return ("error", {"error": f"{type(exc).__name__}: {exc}"})
        with job.lock:
            reason = job.cancel_reason
        if reason is not None:
            # 取消/超时与执行完成发生竞态：按终止状态返回并丢弃结果
            return (self._status_for(reason), {})
        return ("done", {"columns": columns, "rows": rows, "truncated": truncated})
