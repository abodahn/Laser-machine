-- T&C Laser Intelligence & Command Center — central platform schema (SQLite)
-- Every table here is written by the platform. Machine-side databases are read-only sources.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- registry --

CREATE TABLE IF NOT EXISTS machines (
    id                  INTEGER PRIMARY KEY,          -- platform machine id (matches machines.json key)
    code                TEXT UNIQUE,                  -- human machine id / asset tag
    name                TEXT NOT NULL,
    host                TEXT,                         -- IP or hostname
    port                INTEGER DEFAULT 445,
    share               TEXT DEFAULT 'JeanologiaDB',
    db_file             TEXT DEFAULT 'stats.db',
    username            TEXT DEFAULT 'Admin',
    password_enc        TEXT,                         -- Fernet ciphertext, never returned to non-admins
    connection_method   TEXT DEFAULT 'smb',           -- smb | http_push | disabled
    model               TEXT,                         -- TWIN HS-55 / TWIN SUPER / COMPACT SUPER J6 / ...
    machine_type        TEXT,                         -- laser / compact / flexi
    serial_number       TEXT,
    location            TEXT,
    production_area     TEXT DEFAULT 'Laser',
    status              TEXT DEFAULT 'active',        -- active | disabled | maintenance
    heartbeat_interval  INTEGER DEFAULT 60,           -- seconds
    sync_interval       INTEGER DEFAULT 120,          -- seconds
    timeout_seconds     INTEGER DEFAULT 10,
    retry_max           INTEGER DEFAULT 3,
    retry_backoff       INTEGER DEFAULT 30,           -- seconds, multiplied by attempt
    target_units_day    INTEGER DEFAULT 0,            -- production target, 0 = unset
    ideal_seconds_unit  REAL,                         -- override for performance calc; NULL = learn from data
    enabled             INTEGER DEFAULT 1,
    push_token          TEXT,                         -- for connection_method = http_push
    notes               TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS machine_identity (      -- snapshot read from machines.db / config.db
    machine_id      INTEGER PRIMARY KEY REFERENCES machines(id) ON DELETE CASCADE,
    model           TEXT,
    model_id        INTEGER,
    serial_number   TEXT,
    n_lasers        INTEGER,
    n_mannequins    INTEGER,
    has_camera      INTEGER,
    has_columns     INTEGER,
    has_length_sensor INTEGER,
    has_40ix_laser  INTEGER,
    duty_max        INTEGER,
    emark_version   TEXT,
    interface_ver   TEXT,
    laser_power_avg INTEGER,
    beam_diameter   INTEGER,
    raw_json        TEXT,
    updated_at      TEXT
);

-- --------------------------------------------------------------- ingestion --

CREATE TABLE IF NOT EXISTS production (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id          INTEGER NOT NULL,
    source_id           INTEGER NOT NULL,             -- stats.db production.id
    init_date           TEXT NOT NULL,
    end_date            TEXT,
    design              TEXT,
    copies              INTEGER,
    units               INTEGER,
    laser_ms            INTEGER,                      -- stats.db laser_time (milliseconds)
    elapsed_seconds     REAL,                         -- end_date - init_date
    laser_duty          REAL,                         -- laser_ms/1000 / elapsed_seconds
    operator            TEXT,                         -- stats.db user
    alarm1              INTEGER DEFAULT 0,
    alarm2              INTEGER DEFAULT 0,
    optimizer           INTEGER,
    temp_galvo_x        REAL,
    temp_galvo_y        REAL,
    temp_servo_x        REAL,
    temp_servo_y        REAL,
    avg_design_ms       REAL,
    emark_version       TEXT,
    style               TEXT,                         -- parsed from design
    size                TEXT,
    color               TEXT,
    shift               TEXT,
    production_date     TEXT,                         -- shift-adjusted business date
    suspect             INTEGER DEFAULT 0,            -- 1 = clock drift / impossible timestamps
    collected_at        TEXT NOT NULL,
    UNIQUE(machine_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_prod_machine_date ON production(machine_id, init_date);
CREATE INDEX IF NOT EXISTS ix_prod_date        ON production(production_date);
CREATE INDEX IF NOT EXISTS ix_prod_design      ON production(design);
CREATE INDEX IF NOT EXISTS ix_prod_operator    ON production(operator);
CREATE INDEX IF NOT EXISTS ix_prod_init        ON production(init_date);

CREATE TABLE IF NOT EXISTS machine_alarms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id      INTEGER NOT NULL,
    source_table    TEXT NOT NULL,                    -- markingAlarms | laserErrorsHistory | ...
    source_id       INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    type_code       INTEGER,
    error_code      INTEGER,
    description     TEXT,
    serial_number   TEXT,
    emark_version   TEXT,
    category        TEXT,                             -- laser | marking | head | operation | system | generic
    severity        TEXT,                             -- info | warning | critical
    production_date TEXT,
    shift           TEXT,
    suspect         INTEGER DEFAULT 0,   -- 1 = machine clock drift, excluded from state and KPIs
    collected_at    TEXT NOT NULL,
    UNIQUE(machine_id, source_table, source_id)
);
CREATE INDEX IF NOT EXISTS ix_alarm_machine_ts ON machine_alarms(machine_id, ts);
CREATE INDEX IF NOT EXISTS ix_alarm_date       ON machine_alarms(production_date);
CREATE INDEX IF NOT EXISTS ix_alarm_code       ON machine_alarms(error_code, category);

CREATE TABLE IF NOT EXISTS head_health (           -- error.db headHCM — galvo scan-head calibration drift
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id    INTEGER NOT NULL,
    source_id     INTEGER NOT NULL,
    ts            TEXT,
    min_current_x REAL, min_current_y REAL,
    max_current_x REAL, max_current_y REAL,
    mean_current_x REAL, mean_current_y REAL,
    hyst_left_x REAL, hyst_left_y REAL,
    hyst_pos0_x REAL, hyst_pos0_y REAL,
    hyst_right_x REAL, hyst_right_y REAL,
    slope_x REAL, slope_y REAL,
    collected_at  TEXT NOT NULL,
    UNIQUE(machine_id, source_id)
);

CREATE TABLE IF NOT EXISTS laser_credit (          -- codes.db — laser licence / rental credit events
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   INTEGER NOT NULL,
    source_id    INTEGER NOT NULL,
    applied_ms   INTEGER,
    applied_at   TEXT,
    balance_before INTEGER,
    credit_added INTEGER,
    rented       INTEGER,
    collected_at TEXT NOT NULL,
    UNIQUE(machine_id, source_id)
);

-- ------------------------------------------------------ state & heartbeat --

CREATE TABLE IF NOT EXISTS machine_state (
    machine_id        INTEGER PRIMARY KEY REFERENCES machines(id) ON DELETE CASCADE,
    status            TEXT,        -- PRODUCING|IDLE|STOPPED|ALARM|MAINTENANCE|OFFLINE|DISABLED
    connectivity      TEXT,        -- CONNECTED|SLOW|DISCONNECTED|NOT_RESPONDING
    online            INTEGER DEFAULT 0,
    last_heartbeat    TEXT,
    last_heartbeat_ok TEXT,
    latency_ms        REAL,
    consecutive_fail  INTEGER DEFAULT 0,
    last_error        TEXT,
    last_sync         TEXT,
    last_sync_ok      TEXT,
    last_source_id    INTEGER,
    source_mtime      TEXT,
    current_design    TEXT,
    current_job_start TEXT,
    current_job_units INTEGER,
    last_activity     TEXT,        -- end_date (or init_date) of most recent job
    idle_seconds      REAL,
    status_since      TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS state_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   INTEGER NOT NULL,
    status       TEXT,
    connectivity TEXT,
    start_time   TEXT NOT NULL,
    end_time     TEXT,
    duration_seconds INTEGER,
    reason       TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_state_hist ON state_history(machine_id, start_time);

CREATE TABLE IF NOT EXISTS connection_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT,            -- heartbeat | sync | test
    ok           INTEGER,
    latency_ms   REAL,
    rows_new     INTEGER,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS ix_conn_log ON connection_log(machine_id, ts);

CREATE TABLE IF NOT EXISTS downtime (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   INTEGER NOT NULL,
    start_time   TEXT NOT NULL,
    end_time     TEXT,
    duration_seconds INTEGER,
    kind         TEXT,            -- idle | stopped | offline | alarm | maintenance
    reason       TEXT,
    detected_by  TEXT,
    production_date TEXT,
    shift        TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(machine_id, start_time, kind)
);
CREATE INDEX IF NOT EXISTS ix_down ON downtime(machine_id, start_time);

-- ---------------------------------------------------------------- rollups --

CREATE TABLE IF NOT EXISTS machine_hourly (
    machine_id      INTEGER NOT NULL,
    hour            TEXT NOT NULL,     -- 'YYYY-MM-DD HH:00'
    production_date TEXT,
    shift           TEXT,
    jobs            INTEGER DEFAULT 0,
    units           INTEGER DEFAULT 0,
    copies          INTEGER DEFAULT 0,
    laser_seconds   REAL DEFAULT 0,
    busy_seconds    REAL DEFAULT 0,
    idle_seconds    REAL DEFAULT 0,
    down_seconds    REAL DEFAULT 0,
    alarm_count     INTEGER DEFAULT 0,
    job_alarms      INTEGER DEFAULT 0,
    temp_galvo_max  REAL,
    temp_servo_max  REAL,
    designs         INTEGER DEFAULT 0,
    operators       TEXT,
    calculated_at   TEXT,
    PRIMARY KEY (machine_id, hour)
);
CREATE INDEX IF NOT EXISTS ix_hourly_date ON machine_hourly(production_date);

CREATE TABLE IF NOT EXISTS machine_daily (
    machine_id       INTEGER NOT NULL,
    production_date  TEXT NOT NULL,
    jobs             INTEGER DEFAULT 0,
    units            INTEGER DEFAULT 0,
    copies           INTEGER DEFAULT 0,
    laser_seconds    REAL DEFAULT 0,
    busy_seconds     REAL DEFAULT 0,
    idle_seconds     REAL DEFAULT 0,
    down_seconds     REAL DEFAULT 0,
    offline_seconds  REAL DEFAULT 0,
    planned_seconds  REAL DEFAULT 0,
    alarm_count      INTEGER DEFAULT 0,
    jobs_with_alarm  INTEGER DEFAULT 0,
    availability     REAL,
    performance      REAL,
    quality_proxy    REAL,
    oee              REAL,
    utilization      REAL,
    laser_duty       REAL,
    units_per_hour   REAL,
    target_units     INTEGER,
    target_pct       REAL,
    lost_units       REAL,
    health_score     REAL,
    temp_galvo_max   REAL,
    temp_servo_max   REAL,
    calculated_at    TEXT,
    PRIMARY KEY (machine_id, production_date)
);
CREATE INDEX IF NOT EXISTS ix_daily_date ON machine_daily(production_date);

CREATE TABLE IF NOT EXISTS design_stats (     -- learned ideal cycle time per design/machine
    machine_id     INTEGER NOT NULL,
    design         TEXT NOT NULL,
    jobs           INTEGER,
    units          INTEGER,
    p10_sec_unit   REAL,        -- best realistic cycle time per unit  -> ideal
    median_sec_unit REAL,
    mean_sec_unit  REAL,
    last_seen      TEXT,
    calculated_at  TEXT,
    PRIMARY KEY (machine_id, design)
);

CREATE TABLE IF NOT EXISTS machine_baseline (   -- normal operating envelope per machine / hour-of-week
    machine_id   INTEGER NOT NULL,
    hour_of_week INTEGER NOT NULL,   -- 0..167 (Sun 00:00 = 0)
    units_mean   REAL,
    units_std    REAL,
    busy_mean    REAL,
    busy_std     REAL,
    samples      INTEGER,
    calculated_at TEXT,
    PRIMARY KEY (machine_id, hour_of_week)
);

CREATE TABLE IF NOT EXISTS forecast (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id     INTEGER,          -- NULL = whole department
    metric         TEXT NOT NULL,    -- units | utilization | downtime | alarms | health
    horizon        TEXT NOT NULL,    -- eod | day+1 | week
    target_date    TEXT,
    value          REAL,
    low            REAL,
    high           REAL,
    confidence     REAL,
    method         TEXT,
    calculated_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_forecast ON forecast(metric, target_date, machine_id);

-- ----------------------------------------------------------------- alerts --

CREATE TABLE IF NOT EXISTS alert_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id   INTEGER,           -- NULL = applies to all machines
    type         TEXT NOT NULL,     -- machine_offline, heartbeat_lost, ...
    enabled      INTEGER DEFAULT 1,
    severity     TEXT DEFAULT 'warning',
    threshold    REAL,
    window_min   INTEGER,
    cooldown_min INTEGER DEFAULT 30,
    channels     TEXT DEFAULT 'app',    -- csv: app,email,teams,whatsapp
    recipients   TEXT,                  -- csv emails / webhook ids
    escalate_after_min INTEGER,
    escalate_to  TEXT,
    description  TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER,
    machine_id    INTEGER,
    type          TEXT NOT NULL,
    severity      TEXT NOT NULL,      -- info | warning | critical
    title         TEXT NOT NULL,
    message       TEXT,
    value         REAL,
    threshold     REAL,
    dedup_key     TEXT,
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    ended_at      TEXT,
    duration_seconds INTEGER,
    status        TEXT DEFAULT 'open',   -- open | acknowledged | resolved
    ack_by        TEXT,
    ack_at        TEXT,
    assigned_to   TEXT,
    resolved_at   TEXT,
    resolution    TEXT,
    escalated_at  TEXT,
    notified      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(status, severity, started_at);
CREATE INDEX IF NOT EXISTS ix_alerts_dedup ON alerts(dedup_key, status);
CREATE INDEX IF NOT EXISTS ix_alerts_machine ON alerts(machine_id, started_at);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id   INTEGER,
    channel    TEXT,
    recipient  TEXT,
    ts         TEXT,
    ok         INTEGER,
    detail     TEXT
);

-- ------------------------------------------------------- security & audit --

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT,
    email         TEXT,
    role          TEXT NOT NULL DEFAULT 'viewer',  -- admin|manager|production|maintenance|it|viewer
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    active        INTEGER DEFAULT 1,
    last_login    TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    ip         TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    username  TEXT,
    action    TEXT,
    entity    TEXT,
    entity_id TEXT,
    before    TEXT,
    after     TEXT,
    ip        TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit ON audit_log(ts);

-- -------------------------------------------------------------- reference --

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS shifts (
    name       TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,   -- 'HH:MM'
    end_time   TEXT NOT NULL,
    break_minutes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    machine_id  INTEGER,
    source      TEXT,          -- stats | error | codes | identity
    rows_new    INTEGER,
    rows_updated INTEGER,
    duration_ms REAL,
    ok          INTEGER,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_synclog ON sync_log(ts);

-- --------------------------------------------------------- optics / config --
-- The laser recipe per machine: 34 presets x 38 parameters from config.db.
-- markSpeed and jumpSpeed literally set how fast the machine can run, so a
-- machine quietly configured below the fleet standard is permanently slower
-- and nothing else in the system would ever reveal it.
CREATE TABLE IF NOT EXISTS machine_optics (
    machine_id   INTEGER NOT NULL,
    preset       TEXT NOT NULL,
    param        TEXT NOT NULL,
    value        TEXT,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (machine_id, preset, param)
);
CREATE INDEX IF NOT EXISTS ix_optics_param ON machine_optics(param, preset);
