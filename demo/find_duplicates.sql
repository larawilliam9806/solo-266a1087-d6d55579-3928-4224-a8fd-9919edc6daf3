-- ============================================================
-- 传感器重复采样排查 SQL（配合 demo_sensors.db 使用）
-- 背景：批次重试导致同一 (sensor_id, ts) 被重复写入。
-- 全部均为只读查询，可直接在排查页执行。
-- ============================================================

-- 1) 重复采样汇总：每个传感器每秒应只有 1 条，n > 1 即为重复
SELECT sensor_id, ts, COUNT(*) AS n, GROUP_CONCAT(id) AS sample_ids
FROM sensor_samples
GROUP BY sensor_id, ts
HAVING COUNT(*) > 1
ORDER BY n DESC, sensor_id, ts;

-- 2) 重复采样明细：展开每组重复，核对 value 抖动与 batch_id / ingested_at 差异
SELECT s.id, s.sensor_id, s.ts, s.value, s.batch_id, s.ingested_at
FROM sensor_samples s
JOIN (
    SELECT sensor_id, ts
    FROM sensor_samples
    GROUP BY sensor_id, ts
    HAVING COUNT(*) > 1
) d ON d.sensor_id = s.sensor_id AND d.ts = s.ts
ORDER BY s.sensor_id, s.ts, s.id;

-- 3) 按批次统计：确认重复来自哪个批次（演示库中为 B-20261110-RETRY）
SELECT batch_id,
       COUNT(*) AS rows_total,
       COUNT(DISTINCT sensor_id || '|' || ts) AS distinct_samples,
       COUNT(*) - COUNT(DISTINCT sensor_id || '|' || ts) AS duplicate_rows
FROM sensor_samples
GROUP BY batch_id
ORDER BY duplicate_rows DESC;

-- 4) 待清理的重复行：每组保留最小 id，其余为应去重删除的行（只读预览，不执行删除）
WITH ranked AS (
    SELECT id, sensor_id, ts, value, batch_id,
           ROW_NUMBER() OVER (PARTITION BY sensor_id, ts ORDER BY id) AS rn
    FROM sensor_samples
)
SELECT * FROM ranked WHERE rn > 1 ORDER BY sensor_id, ts;

-- 5) 修复入库逻辑后的验证：应返回 0 行
SELECT sensor_id, ts, COUNT(*) AS n
FROM sensor_samples
GROUP BY sensor_id, ts
HAVING COUNT(*) > 1;
