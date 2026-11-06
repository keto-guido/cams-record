"""cams_config schema, applied by cams_record.db.Database(cfg, migrations=MIGRATIONS)."""

from __future__ import annotations

_T = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        # id: App Hub registry id rules, fixed for life (MQTT/Home Assistant entities are keyed by it).
        f"""CREATE TABLE IF NOT EXISTS cameras (
            id VARCHAR(64) NOT NULL PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            connector_type VARCHAR(32) NOT NULL DEFAULT 'go2rtc',
            connector_json TEXT NULL,
            visibility ENUM('everyone','restricted') NOT NULL DEFAULT 'restricted',
            sort_order INT NOT NULL DEFAULT 0,
            created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            updated_by VARCHAR(128) NULL
        ) {_T}""",
        # One row per stream the camera offers. go2rtc_name is the go2rtc stream (credentials stay in go2rtc.yaml).
        f"""CREATE TABLE IF NOT EXISTS camera_streams (
            camera_id VARCHAR(64) NOT NULL,
            stream_key VARCHAR(32) NOT NULL,
            go2rtc_name VARCHAR(128) NOT NULL,
            width INT NULL,
            height INT NULL,
            codec VARCHAR(32) NULL,
            fps DECIMAL(6,2) NULL,
            probed_at DATETIME(3) NULL,
            PRIMARY KEY (camera_id, stream_key),
            KEY idx_streams_go2rtc (go2rtc_name),
            CONSTRAINT fk_streams_camera FOREIGN KEY (camera_id) REFERENCES cameras (id) ON DELETE CASCADE
        ) {_T}""",
        # Which stream does which job. record may name several streams; every other purpose one.
        f"""CREATE TABLE IF NOT EXISTS camera_stream_use (
            camera_id VARCHAR(64) NOT NULL,
            purpose ENUM('live','live_hd','detect','verify','snapshot','record') NOT NULL,
            stream_key VARCHAR(32) NOT NULL,
            PRIMARY KEY (camera_id, purpose, stream_key),
            CONSTRAINT fk_use_stream FOREIGN KEY (camera_id, stream_key)
                REFERENCES camera_streams (camera_id, stream_key) ON DELETE CASCADE
        ) {_T}""",
        # Grants for visibility='restricted'. Admins always see every camera; no rows = admins only.
        f"""CREATE TABLE IF NOT EXISTS camera_access (
            camera_id VARCHAR(64) NOT NULL,
            kind ENUM('user','role') NOT NULL,
            principal VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,  -- exact, case-sensitive
            PRIMARY KEY (camera_id, kind, principal),
            CONSTRAINT fk_access_camera FOREIGN KEY (camera_id) REFERENCES cameras (id) ON DELETE CASCADE
        ) {_T}""",
        # cams-detect settings (masks, zones, classes, motion, ...), the cameras.yaml entry minus streams.
        f"""CREATE TABLE IF NOT EXISTS camera_detection (
            camera_id VARCHAR(64) NOT NULL PRIMARY KEY,
            settings_json MEDIUMTEXT NOT NULL,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            updated_by VARCHAR(128) NULL,
            CONSTRAINT fk_detection_camera FOREIGN KEY (camera_id) REFERENCES cameras (id) ON DELETE CASCADE
        ) {_T}""",
        f"""CREATE TABLE IF NOT EXISTS camera_recording (
            camera_id VARCHAR(64) NOT NULL PRIMARY KEY,
            main_days DECIMAL(8,2) NULL,
            sub_days DECIMAL(8,2) NULL,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            CONSTRAINT fk_recording_camera FOREIGN KEY (camera_id) REFERENCES cameras (id) ON DELETE CASCADE
        ) {_T}""",
        # recipients_json NULL = everyone allowed to view the camera; a list narrows it further.
        f"""CREATE TABLE IF NOT EXISTS camera_notify_rules (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            camera_id VARCHAR(64) NOT NULL,
            channel ENUM('push','mqtt') NOT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            labels_json TEXT NULL,
            zones_json TEXT NULL,
            verified_only TINYINT(1) NOT NULL DEFAULT 0,
            cooldown_s INT NOT NULL DEFAULT 0,
            quiet_json TEXT NULL,
            recipients_json TEXT NULL,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
            updated_by VARCHAR(128) NULL,
            KEY idx_notify_camera (camera_id),
            CONSTRAINT fk_notify_camera FOREIGN KEY (camera_id) REFERENCES cameras (id) ON DELETE CASCADE
        ) {_T}""",
        # Bumped in the same transaction as every change; readers poll it to know when to reload.
        f"""CREATE TABLE IF NOT EXISTS config_revision (
            id TINYINT NOT NULL PRIMARY KEY,
            revision BIGINT NOT NULL,
            updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
        ) {_T}""",
        "INSERT IGNORE INTO config_revision (id, revision) VALUES (1, 0)",
        f"""CREATE TABLE IF NOT EXISTS config_audit (
            id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
            actor VARCHAR(128) NOT NULL,
            camera_id VARCHAR(64) NULL,
            action VARCHAR(32) NOT NULL,
            revision BIGINT NOT NULL,
            before_json MEDIUMTEXT NULL,
            after_json MEDIUMTEXT NULL,
            KEY idx_audit_camera (camera_id, at)
        ) {_T}""",
    ]),
]

# Drop order for tests (children first).
TABLES = ("camera_notify_rules", "camera_recording", "camera_detection", "camera_access",
          "camera_stream_use", "camera_streams", "cameras", "config_revision", "config_audit",
          "schema_version")
