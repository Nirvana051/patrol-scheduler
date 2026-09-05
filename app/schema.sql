-- 巡检调度系统 SQLite 模式 v6（见 docs/task.md §4；增量迁移见 app/db.py）
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maps (
  name            TEXT PRIMARY KEY,
  synced_at       TEXT,
  waypoint_count  INTEGER NOT NULL DEFAULT 0,
  pointcloud_path TEXT
);

-- 导航航点：地图自带，来自 GET /maps/{name}/waypoints 的只读缓存
CREATE TABLE IF NOT EXISTS nav_waypoints (
  map_name  TEXT NOT NULL,
  node_id   TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL DEFAULT 0,
  qx REAL NOT NULL DEFAULT 0, qy REAL NOT NULL DEFAULT 0, qz REAL NOT NULL DEFAULT 0, qw REAL NOT NULL DEFAULT 1,
  yaw       REAL NOT NULL DEFAULT 0,
  neighbors TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (map_name, node_id)
);

-- 任务航点：巡检点（单表）
CREATE TABLE IF NOT EXISTS task_waypoints (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  map_name        TEXT NOT NULL,
  nav_node_id     TEXT,                         -- 原本是几号导航航点；手工坐标时为 NULL
  x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL DEFAULT 0,
  yaw             REAL NOT NULL DEFAULT 0,
  prompt          TEXT NOT NULL DEFAULT '',
  angle_from      REAL NOT NULL DEFAULT 150,    -- 全景角度范围，0–360，见 task.md §4.1
  angle_to        REAL NOT NULL DEFAULT 210,
  answer_template TEXT NOT NULL DEFAULT '{}',   -- JSON {expected, on_pass, on_fail, on_unknown}
  reference_image TEXT,
  enabled         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  map_name    TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  options     TEXT NOT NULL DEFAULT '{}',       -- JSON：speed/gait/obs_mode/nav_mode/settle_seconds/leg_timeout/max_retries/return_to_start
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_items (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,
  task_waypoint_id INTEGER NOT NULL REFERENCES task_waypoints(id) ON DELETE CASCADE,
  UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER,
  task_name   TEXT,
  map_name    TEXT,
  status      TEXT NOT NULL,                    -- pending|preflight|running|paused|completed|failed|aborted
  mode        TEXT NOT NULL DEFAULT 'mock',     -- mock|real
  current_leg INTEGER NOT NULL DEFAULT 0,
  total_legs  INTEGER NOT NULL DEFAULT 0,
  started_at  TEXT,
  ended_at    TEXT,
  error       TEXT,
  summary     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS run_legs (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq              INTEGER NOT NULL,
  task_waypoint_id INTEGER,
  item_seq         INTEGER,                     -- 对应任务里第几个航点（重跑剩余航点用）
  waypoint_name    TEXT,
  from_node        TEXT,
  to_node          TEXT,
  path             TEXT NOT NULL DEFAULT '[]',
  idempotency_key  TEXT,
  status           TEXT NOT NULL,               -- pending|planning|dispatched|navigating|arrived|inspecting|done|failed|skipped|aborted
  attempt          INTEGER NOT NULL DEFAULT 0,
  dispatched_at    TEXT,
  arrived_at       TEXT,
  ended_at         TEXT,
  cloud_task       TEXT,
  error            TEXT
);

CREATE TABLE IF NOT EXISTS inspections (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  leg_id           INTEGER,
  task_waypoint_id INTEGER,
  waypoint_name    TEXT,
  prompt           TEXT,
  angle_from       REAL, angle_to REAL,
  image_path       TEXT, crop_path TEXT,
  vlm_provider     TEXT, vlm_raw TEXT,
  answer           TEXT,                        -- yes|no|unknown|error
  expected         TEXT,
  passed           INTEGER,
  tts_text         TEXT, tts_audio_path TEXT, tts_status TEXT,
  latency_ms       INTEGER,
  human_passed     INTEGER,                     -- 人工改判：1 通过 / 0 不通过 / NULL 未改判
  human_note       TEXT,
  human_at         TEXT,
  capture_pose     TEXT,                        -- 抓图时的位姿 JSON {x,y,z,yaw}（v5）
  created_at       TEXT NOT NULL
);

-- 云端事件 + 系统事件的统一时间线（云端只留 500 条内存，这里才是持久记录）
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,
  source    TEXT NOT NULL,                      -- cloud|system
  type      TEXT NOT NULL,
  cloud_seq INTEGER,
  run_id    INTEGER,
  leg_id    INTEGER,
  level     TEXT NOT NULL DEFAULT 'info',       -- info|warn|error
  message   TEXT NOT NULL DEFAULT '',
  data      TEXT NOT NULL DEFAULT '{}',
  gateway   TEXT                                -- 云端事件来自哪个网关/机器人（host|alias）；seq 只在同一网关内唯一（v6）
);
CREATE UNIQUE INDEX IF NOT EXISTS events_gw_seq ON events(gateway, cloud_seq) WHERE cloud_seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS inspections_run ON inspections(run_id);
CREATE INDEX IF NOT EXISTS run_legs_run ON run_legs(run_id);

-- 定时计划：daily = 每天固定时刻（"07:00,19:30"），interval = 每 N 分钟
CREATE TABLE IF NOT EXISTS schedules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,                     -- daily | interval
  spec        TEXT NOT NULL,                     -- "07:00,19:30" 或 "120"
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  last_result TEXT,
  next_run_at TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
