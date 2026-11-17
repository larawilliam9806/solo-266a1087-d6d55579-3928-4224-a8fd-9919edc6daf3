#!/usr/bin/env python3
"""关键功能自测：只读约束、并发名额、超时、取消、截断、失败不留半份结果。

用法：
    python3 tests/selftest.py <base_url> <demo_db_path> <server_upload_dir>

需要被测服务已在运行（UPLOAD_DIR 指向 <server_upload_dir>）。
仅用标准库，可用 python3 -I 运行。
"""
import hashlib
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

SLOW_SQL = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt "
    "WHERE x < 300000000) SELECT COUNT(*), SUM(x) FROM cnt"
)
DUP_SQL = (
    "SELECT sensor_id, ts, COUNT(*) AS n FROM sensor_samples "
    "GROUP BY sensor_id, ts HAVING COUNT(*) > 1 ORDER BY n DESC, sensor_id, ts"
)

failures = []
checks = [0]


def check(name, cond, detail=""):
    checks[0] += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Client:
    """带独立 cookie 会话的 HTTP 客户端。"""

    def __init__(self, base):
        self.base = base
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(self, method, path, payload=None):
        data = headers = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def upload(self, path, filename="demo.db", content=None):
        if content is None:
            with open(path, "rb") as f:
                content = f.read()
        boundary = "----selftest-boundary-9f2c"
        body = b"\r\n".join(
            [
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
                b"Content-Type: application/octet-stream",
                b"",
                content,
                f"--{boundary}--".encode(),
                b"",
            ]
        )
        req = urllib.request.Request(
            self.base + "/upload",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with self.opener.open(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode())
            except Exception:
                return e.code, {}

    def run_query(self, sql):
        return self.call("POST", "/query", {"sql": sql})

    def wait_final(self, job_id, timeout=20.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            st, j = self.call("GET", f"/query/{job_id}")
            assert st == 200, f"status {st}"
            if j["status"] in ("done", "error", "timeout", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("查询未在预期时间内到达终态")


def main():
    base, demo_db, upload_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    demo_hash = sha256(demo_db)
    c = Client(base)

    print("== 页面与上传 ==")
    with c.opener.open(base + "/", timeout=10) as resp:
        html = resp.read().decode()
    check("首页可访问且为排查页", resp.status == 200 and "只读 SQL" in html)

    st, j = c.upload(None, "evil.db", b"not a sqlite database at all........")
    check("非 SQLite 文件被拒绝", st == 400, f"got {st} {j}")

    st, j = c.upload(demo_db, "demo_sensors.db")
    check("演示库上传成功", st == 200 and j.get("ok"), f"got {st} {j}")

    st, j = c.call("GET", "/schema")
    tables = {t["name"]: t for t in j.get("tables", [])} if st == 200 else {}
    check("表结构包含 sensor_samples", "sensor_samples" in tables)
    if "sensor_samples" in tables:
        cols = {col["name"] for col in tables["sensor_samples"]["columns"]}
        check("列结构完整", {"id", "sensor_id", "ts", "value", "batch_id"} <= cols, str(cols))
        check("行数统计合理", (tables["sensor_samples"]["row_count"] or 0) > 12000)

    print("== 只读约束 ==")
    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    total_before = j["rows"][0][0] if j["status"] == "done" else None
    check("SELECT 正常执行", j["status"] == "done" and total_before, str(j))

    for bad in ["DELETE FROM sensor_samples", "UPDATE sensor_samples SET value=0",
                "PRAGMA user_version=1", "ATTACH DATABASE '/tmp/evil.db' AS e",
                "DROP TABLE sensor_samples", "INSERT INTO sensors VALUES ('X','x','y','z')"]:
        st, j = c.run_query(bad)
        check(f"非只读语句被 400 拒绝: {bad.split()[0]}", st == 400, f"got {st} {j}")

    st, j = c.run_query("WITH d AS (SELECT 1) DELETE FROM sensor_samples")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("WITH 伪装的 DELETE 被 authorizer 拦截", j["status"] == "error" and "not authorized" in j.get("error", ""), str(j))
    else:
        check("WITH 伪装的 DELETE 被 authorizer 拦截", False, f"got {st} {j}")

    st, j = c.run_query("SELECT load_extension('/tmp/evil.so')")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("load_extension 被拦截", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT 1; SELECT 2")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("多语句被拒绝", j["status"] == "error", str(j))

    st, j = c.run_query("SELECT COUNT(*) AS n FROM sensor_samples")
    j = c.wait_final(j["job_id"])
    check("写尝试后数据未变", j["status"] == "done" and j["rows"][0][0] == total_before,
          f"before={total_before} after={j.get('rows')}")

    st, j = c.run_query("SELECT name, type FROM pragma_table_info('sensor_samples') ORDER BY cid")
    j = c.wait_final(j["job_id"]) if st == 200 else {"status": "?"}
    check("pragma 函数只读访问可用", j["status"] == "done" and j["row_count"] == 6, str(j)[:200])

    print("== 重复采样排查 SQL ==")
    st, j = c.run_query(DUP_SQL)
    j = c.wait_final(j["job_id"])
    check("重复汇总查询成功", j["status"] == "done", str(j)[:200])
    if j["status"] == "done":
        check("找到 270 组重复", j["row_count"] == 270, f"got {j['row_count']}")
        check("最大重复次数为 3", j["rows"][0][2] == 3, str(j["rows"][0]))
        check("未截断", j["truncated"] is False)

    print("== 行数上限与截断 ==")
    st, j = c.run_query(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5000) SELECT x FROM c"
    )
    j = c.wait_final(j["job_id"])
    check("超过 1000 行被截断", j["status"] == "done" and j["row_count"] == 1000 and j["truncated"] is True,
          f"rows={j.get('row_count')} truncated={j.get('truncated')}")

    print("== 失败不留半份结果 ==")
    st, j = c.run_query("SELECT * FROM no_such_table")
    j = c.wait_final(j["job_id"])
    check("错误查询无结果字段", j["status"] == "error" and "rows" not in j and "columns" not in j, str(j))

    print("== 超时自动终止 ==")
    t0 = time.monotonic()
    st, j = c.run_query(SLOW_SQL)
    check("慢查询被受理", st == 200, f"got {st} {j}")
    j = c.wait_final(j["job_id"])
    wall = time.monotonic() - t0
    check("慢查询 3s 左右自动终止", j["status"] == "timeout" and 2.0 <= wall <= 8.0,
          f"status={j['status']} wall={wall:.2f}s elapsed_ms={j['elapsed_ms']}")

    print("== 并发名额与取消 ==")
    c2, c3 = Client(base), Client(base)
    st, _ = c2.upload(demo_db, "demo_sensors.db")
    assert st == 200
    st, _ = c3.upload(demo_db, "demo_sensors.db")
    assert st == 200

    st1, r1 = c2.run_query(SLOW_SQL)
    st2, r2 = c3.run_query(SLOW_SQL)
    check("两条慢查询均被受理", st1 == 200 and st2 == 200, f"{st1} {st2}")
    time.sleep(0.6)
    st3, r3 = c.run_query("SELECT 1")
    check("第三条查询被 429 明确拒绝", st3 == 429 and "满" in r3.get("error", ""), f"got {st3} {r3}")

    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r1['job_id']}/cancel")
    cancel_wall = time.monotonic() - t0
    check("取消接口返回 cancelled 且响应迅速", j.get("status") == "cancelled" and cancel_wall < 2.5,
          f"status={j.get('status')} wall={cancel_wall:.2f}s")

    st, j = c.run_query("SELECT 1 AS ok")
    check("取消后名额已释放（新查询立即受理）", st == 200, f"got {st} {j}")
    if st == 200:
        j = c.wait_final(j["job_id"])
        check("新查询正常完成", j["status"] == "done" and j["rows"][0][0] == 1, str(j))

    j2 = c3.wait_final(r2["job_id"])
    check("另一条查询不受取消影响（自然超时终止）", j2["status"] == "timeout", j2["status"])

    print("== 手动取消进行中的查询 ==")
    st, r = c2.run_query(SLOW_SQL)
    check("慢查询再次被受理", st == 200, f"got {st}")
    time.sleep(0.7)
    t0 = time.monotonic()
    st, j = c2.call("POST", f"/query/{r['job_id']}/cancel")
    wall = time.monotonic() - t0
    check("取消生效且真实停止计算", j.get("status") == "cancelled" and wall < 2.5 and j.get("elapsed_ms", 9999) < 2500,
          f"status={j.get('status')} wall={wall:.2f}s elapsed={j.get('elapsed_ms')}ms")

    print("== 数据库文件未被修改 ==")
    uploaded = [os.path.join(upload_dir, f) for f in os.listdir(upload_dir) if f.endswith(".db")]
    check("服务端存在上传副本", len(uploaded) >= 3, str(uploaded))
    for p in uploaded:
        check(f"副本哈希未变 {os.path.basename(p)}", sha256(p) == demo_hash)

    print("== 内置演示库入口 ==")
    c4 = Client(base)
    st, j = c4.call("POST", "/use-demo")
    check("use-demo 可用", st == 200 and j.get("ok"), f"got {st} {j}")
    st, j = c4.call("GET", "/schema")
    check("演示库表结构可读", st == 200 and any(t["name"] == "sensor_samples" for t in j.get("tables", [])))

    print()
    print(f"共 {checks[0]} 项检查，失败 {len(failures)} 项")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
