#!/usr/bin/env python3
"""生成传感器演示库 demo_sensors.db。

场景：边缘网关按批次上报采样，入库服务在批次超时后重试，
导致同一 (sensor_id, ts) 被重复写入（多数 2 条，个别 3 条），
value 有微小抖动、batch_id / ingested_at 不同 —— 典型的"重复采样"现场。

用法：python3 make_demo_db.py [输出路径]   （默认写到本脚本同目录）
"""
import math
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE sensors (
    sensor_id TEXT PRIMARY KEY,
    location  TEXT NOT NULL,
    metric    TEXT NOT NULL,
    unit      TEXT NOT NULL
);

CREATE TABLE sensor_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id   TEXT NOT NULL REFERENCES sensors(sensor_id),
    ts          TEXT NOT NULL,   -- 采样时间 ISO8601（秒级）
    value       REAL NOT NULL,
    ingested_at TEXT NOT NULL,   -- 入库时间
    batch_id    TEXT NOT NULL    -- 入库批次
);

CREATE INDEX idx_samples_sensor_ts ON sensor_samples(sensor_id, ts);
CREATE INDEX idx_samples_batch     ON sensor_samples(batch_id);
"""

# sensor_id, 位置, 指标, 单位, 基线, 振幅
SENSORS = [
    ("S-1001", "车间A", "temperature", "°C", 22.0, 3.0),
    ("S-1002", "车间A", "humidity", "%RH", 55.0, 6.0),
    ("S-2001", "车间B", "temperature", "°C", 24.5, 2.0),
    ("S-2002", "车间B", "pressure", "kPa", 101.3, 0.4),
    ("S-3001", "冷库", "temperature", "°C", -18.0, 1.2),
    ("S-3002", "冷库", "humidity", "%RH", 70.0, 4.0),
]

BATCH_NORMAL = "B-20261110-A"
BATCH_RETRY = "B-20261110-RETRY"

SAMPLES_PER_SENSOR = 2160   # 6 小时 × 每 10 秒一条
DUP_TIMES_PER_SENSOR = 45   # 每个传感器被重复入库的时刻数
TRIPLE_PER_SENSOR = 5       # 其中被重复两次（共 3 条）的时刻数


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def build(path):
    random.seed(20261110)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO sensors VALUES (?,?,?,?)",
            [(sid, loc, metric, unit) for sid, loc, metric, unit, _b, _a in SENSORS],
        )
        start = datetime(2026, 11, 10, 0, 0, 0)
        retry_ingest = iso(start + timedelta(minutes=5))  # 重试批次 5 分钟后补传

        total = 0
        for sid, _loc, _metric, _unit, base, amp in SENSORS:
            rows = []
            for i in range(SAMPLES_PER_SENSOR):
                ts = start + timedelta(seconds=10 * i)
                value = round(base + amp * math.sin(i / 120.0) + random.uniform(-0.4, 0.4), 3)
                rows.append((sid, iso(ts), value, iso(ts + timedelta(seconds=2)), BATCH_NORMAL))
            # 重试批次造成的重复采样：同一 (sensor_id, ts) 再写一遍，值轻微抖动
            for n, idx in enumerate(random.sample(range(SAMPLES_PER_SENSOR), DUP_TIMES_PER_SENSOR)):
                orig = rows[idx]
                rows.append((orig[0], orig[1], round(orig[2] + random.uniform(-0.05, 0.05), 3),
                             retry_ingest, BATCH_RETRY))
                if n < TRIPLE_PER_SENSOR:  # 个别时刻重试了两次
                    rows.append((orig[0], orig[1], round(orig[2] + random.uniform(-0.08, 0.08), 3),
                                 retry_ingest, BATCH_RETRY))
            random.shuffle(rows)
            conn.executemany(
                "INSERT INTO sensor_samples (sensor_id, ts, value, ingested_at, batch_id) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
            total += len(rows)
        conn.commit()

        dup_groups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM sensor_samples "
            "GROUP BY sensor_id, ts HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        dup_rows = conn.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) AS n FROM sensor_samples "
            "GROUP BY sensor_id, ts HAVING n > 1)"
        ).fetchone()[0]
        print(f"已生成 {path}")
        print(f"  传感器 {len(SENSORS)} 个，采样总行数 {total}")
        print(f"  重复采样组 {dup_groups} 个，多出重复行 {dup_rows} 行")
    finally:
        conn.close()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "demo_sensors.db"
    )
    build(out)
