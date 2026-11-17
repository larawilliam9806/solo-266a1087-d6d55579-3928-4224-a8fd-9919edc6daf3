"""传感器入库排查页：浏览器上传 SQLite 副本，只读执行 SELECT/WITH 查询。"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file, session

from query_manager import QueryManager

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(REPO_ROOT, "uploads"))
DEMO_DB = os.environ.get("DEMO_DB", os.path.join(REPO_ROOT, "demo", "demo_sensors.db"))
SQLITE_MAGIC = b"SQLite format 3\x00"

os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "300")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

manager = QueryManager(
    max_concurrent=int(os.environ.get("MAX_CONCURRENT", "2")),
    timeout=float(os.environ.get("QUERY_TIMEOUT", "3")),
    max_rows=int(os.environ.get("MAX_ROWS", "1000")),
)

_UID_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------- 会话与上传文件 ----------------

def _current_uid():
    uid = session.get("uid")
    return uid if uid and _UID_RE.match(uid) else None


def _ensure_uid():
    uid = _current_uid()
    if uid is None:
        uid = uuid.uuid4().hex
        session["uid"] = uid
    return uid


def _db_path(uid):
    return os.path.join(UPLOAD_DIR, uid + ".db")


def _current_db():
    uid = _current_uid()
    if not uid:
        return None
    path = _db_path(uid)
    return path if os.path.isfile(path) else None


# ---------------- SQL 文本预检（纵深防御的第一层，真正的硬约束在 authorizer） ----------------

def first_keyword(sql):
    """跳过空白与注释，返回第一个 SQL 关键字（大写）。"""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            i = s.find("\n")
            s = "" if i < 0 else s[i + 1 :]
        elif s.startswith("/*"):
            i = s.find("*/")
            if i < 0:
                return ""
            s = s[i + 2 :]
        else:
            break
    m = re.match(r"[A-Za-z]+", s)
    return m.group(0).upper() if m else ""


# ---------------- 页面与基础接口 ----------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        max_rows=manager.max_rows,
        timeout=manager.timeout,
        max_concurrent=manager.max_concurrent,
    )


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.get("/api/current")
def api_current():
    path = _current_db()
    if not path:
        return jsonify({"loaded": False})
    return jsonify({"loaded": True, "size": os.path.getsize(path)})


@app.post("/upload")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "请选择要上传的 SQLite 文件"}), 400
    uid = _ensure_uid()
    path = _db_path(uid)
    tmp = path + ".part"
    try:
        f.save(tmp)
        with open(tmp, "rb") as fh:
            magic = fh.read(16)
        if magic != SQLITE_MAGIC:
            os.remove(tmp)
            return jsonify({"error": "文件不是有效的 SQLite 数据库（缺少 SQLite 文件头）"}), 400
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return jsonify({"ok": True, "name": f.filename, "size": os.path.getsize(path)})


@app.post("/use-demo")
def use_demo():
    if not os.path.isfile(DEMO_DB):
        return jsonify({"error": "内置演示库不存在，请先运行 demo/make_demo_db.py 生成"}), 404
    uid = _ensure_uid()
    shutil.copyfile(DEMO_DB, _db_path(uid))
    return jsonify({"ok": True, "name": "demo_sensors.db", "size": os.path.getsize(DEMO_DB)})


@app.get("/demo/download")
def demo_download():
    if not os.path.isfile(DEMO_DB):
        return jsonify({"error": "演示库不存在"}), 404
    return send_file(DEMO_DB, as_attachment=True, download_name="demo_sensors.db")


@app.post("/reset")
def reset():
    uid = _current_uid()
    if uid:
        try:
            os.remove(_db_path(uid))
        except OSError:
            pass
    session.clear()
    return jsonify({"ok": True})


# ---------------- 表结构 ----------------

@app.get("/schema")
def schema():
    path = _current_db()
    if not path:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    conn = manager.connect_readonly(path)
    try:
        conn.row_factory = sqlite3.Row
        objects = [
            dict(r)
            for r in conn.execute(
                "SELECT type, name, tbl_name AS parent, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        tables = []
        for obj in objects:
            if obj["type"] not in ("table", "view"):
                continue
            name = obj["name"]
            cols = [
                dict(r)
                for r in conn.execute(
                    'SELECT cid, name, type, "notnull", dflt_value, pk '
                    "FROM pragma_table_info(?)",
                    (name,),
                )
            ]
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            except sqlite3.Error:
                row_count = None
            tables.append(
                {
                    "name": name,
                    "type": obj["type"],
                    "sql": obj["sql"],
                    "columns": cols,
                    "row_count": row_count,
                }
            )
        indexes = [
            {"name": o["name"], "parent": o["parent"], "sql": o["sql"]}
            for o in objects
            if o["type"] == "index"
        ]
        return jsonify({"tables": tables, "indexes": indexes})
    finally:
        conn.close()


# ---------------- 查询执行 ----------------

@app.post("/query")
def start_query():
    path = _current_db()
    if not path:
        return jsonify({"error": "尚未上传数据库副本"}), 400
    data = request.get_json(silent=True) or {}
    sql = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "SQL 不能为空"}), 400
    if len(sql) > 100_000:
        return jsonify({"error": "SQL 过长（>100KB）"}), 400
    kw = first_keyword(sql)
    if kw not in ("SELECT", "WITH"):
        return jsonify({"error": f"仅允许 SELECT / WITH 只读查询（检测到：{kw or '无法解析'}）"}), 400
    job = manager.submit(path, sql)
    if job is None:
        return (
            jsonify(
                {
                    "error": f"查询位已满：最多同时运行 {manager.max_concurrent} 条查询，"
                    "请等待其完成或先取消进行中的查询"
                }
            ),
            429,
        )
    return jsonify({"job_id": job.id})


@app.get("/query/<job_id>")
def query_status(job_id):
    job = manager.get(job_id)
    if job is None:
        return jsonify({"error": "查询不存在或已过期"}), 404
    return jsonify(job.public())


@app.post("/query/<job_id>/cancel")
def query_cancel(job_id):
    job = manager.cancel_and_wait(job_id)
    if job is None:
        return jsonify({"error": "查询不存在或已过期"}), 404
    return jsonify(job.public())


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "文件过大，超过上传限制"}), 413


def _upload_reaper():
    """定期清理超过 TTL 的上传副本，避免卷内文件无限堆积。"""
    ttl = int(os.environ.get("UPLOAD_TTL_HOURS", "24")) * 3600
    while True:
        time.sleep(1800)
        try:
            now = time.time()
            for name in os.listdir(UPLOAD_DIR):
                if not name.endswith(".db"):
                    continue
                path = os.path.join(UPLOAD_DIR, name)
                try:
                    if now - os.path.getmtime(path) > ttl:
                        os.remove(path)
                except OSError:
                    pass
        except Exception:
            pass


if os.environ.get("UPLOAD_REAPER", "1") == "1":
    threading.Thread(target=_upload_reaper, daemon=True, name="upload-reaper").start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
