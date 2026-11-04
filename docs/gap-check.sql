-- cams-record gap check (MariaDB 10.6+; all times UTC).
-- A gap is time between one segment's end and the next segment's start (same camera + quality)
-- longer than 1 s. missing_pct is gap time / 86400 s, so it is meaningful for complete UTC days.
-- Acceptance: missing_pct < 0.1 for every camera, quality and day (86.4 s/day).
WITH s AS (
  SELECT camera_id, quality, start_ts, end_ts,
         LAG(end_ts) OVER (PARTITION BY camera_id, quality ORDER BY start_ts) AS prev_end
  FROM segments
  WHERE missing = 0
    AND start_ts >= UTC_DATE() - INTERVAL 7 DAY
)
SELECT camera_id,
       quality,
       DATE(start_ts)                                                              AS day_utc,
       COUNT(*)                                                                    AS segments,
       ROUND(SUM(TIMESTAMPDIFF(MICROSECOND, start_ts, end_ts)) / 1e6 / 3600, 2)   AS recorded_h,
       SUM(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000)               AS gaps,
       ROUND(SUM(IF(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000,
                    TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts), 0)) / 1e6, 1)  AS gap_s,
       ROUND(100 * SUM(IF(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000,
                          TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts), 0)) / 1e6 / 86400, 3) AS missing_pct
FROM s
GROUP BY camera_id, quality, DATE(start_ts)
ORDER BY camera_id, quality, day_utc;

-- The individual gaps (largest first), to correlate with recorder restarts in the logs:
-- WITH s AS (SELECT camera_id, quality, start_ts,
--                   LAG(end_ts) OVER (PARTITION BY camera_id, quality ORDER BY start_ts) AS prev_end
--            FROM segments WHERE missing = 0 AND start_ts >= UTC_DATE() - INTERVAL 7 DAY)
-- SELECT camera_id, quality, prev_end AS gap_from_utc, start_ts AS gap_to_utc,
--        ROUND(TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) / 1e6, 1) AS gap_s
-- FROM s WHERE TIMESTAMPDIFF(MICROSECOND, prev_end, start_ts) > 1000000
-- ORDER BY gap_s DESC LIMIT 50;

-- Rows whose file is gone (should be 0 until retention exists):
-- SELECT camera_id, quality, COUNT(*) FROM segments WHERE missing = 1 GROUP BY camera_id, quality;
