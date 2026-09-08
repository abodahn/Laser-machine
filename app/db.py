"""SQLite access, schema bootstrap, and one-time import of the legacy files."""
import datetime as dt
import json
import os
import re
import sqlite3
import threading

from . import config

_local = threading.local()
_write_lock = threading.Lock()   # ponytail: one global write lock; SQLite+WAL handles the rest.
                                 # Split per-table only if write contention ever shows up.


def connect() -> sqlite3.Connection:
    """One connection per thread."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def q(sql, args=()):
    return connect().execute(sql, args).fetchall()


def q1(sql, args=()):
    return connect().execute(sql, args).fetchone()


def ex(sql, args=()):
    with _write_lock:
        c = connect()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def exmany(sql, rows):
    if not rows:
        return 0
    with _write_lock:
        c = connect()
        cur = c.executemany(sql, rows)
        c.commit()
        return cur.rowcount


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ settings

def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if r is not None:
        return r["value"]
    return config.DEFAULTS.get(key, default)


def get_int(key, default=0):
    try:
        return int(float(get_setting(key, default)))
    except (TypeError, ValueError):
        return default


def get_float(key, default=0.0):
    try:
        return float(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
       (key, str(value), now()))


def audit(username, action, entity=None, entity_id=None, before=None, after=None, ip=None):
    ex("INSERT INTO audit_log(ts,username,action,entity,entity_id,before,after,ip) VALUES(?,?,?,?,?,?,?,?)",
       (now(), username, action, entity, str(entity_id) if entity_id is not None else None,
        json.dumps(before, default=str) if before is not None else None,
        json.dumps(after, default=str) if after is not None else None, ip))


# ------------------------------------------------------- shift / date helper

def shift_rows():
    rows = q("SELECT * FROM shifts ORDER BY start_time")
    return [(r["name"], r["start_time"], r["end_time"]) for r in rows]


_shift_cache = {"rows": None, "at": 0.0}


def shift_of(ts: str):
    """Return (shift_name, business_date) for a 'YYYY-MM-DD HH:MM:SS' timestamp.

    A shift crossing midnight belongs to the business date it started on.
    """
    if not ts or len(ts) < 16:
        return None, (ts or "")[:10]
    import time
    if _shift_cache["rows"] is None or time.time() - _shift_cache["at"] > 60:
        _shift_cache["rows"] = shift_rows()
        _shift_cache["at"] = time.time()
    rows = _shift_cache["rows"]
    date, hm = ts[:10], ts[11:16]
    if not rows:
        return None, date
    for name, start, end in rows:
        if start <= end:                       # normal shift within the day
            if start <= hm < end:
                return name, date
        else:                                  # night shift crossing midnight
            if hm >= start:
                return name, date
            if hm < end:                       # early hours belong to previous day's shift
                prev = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
                return name, prev
    return rows[0][0], date


# -------------------------------------------------------------- design parse

_SIZE_RE = re.compile(r"(?:^|[-_ ])(\d{2}(?:[/x]\d{2})?|XXS|XS|S|M|L|XL|XXL|XXXL|3XL|4XL)(?:$|[-_. ])", re.I)
_COLORS = ("BLACK", "BLUE", "WHITE", "GREY", "GRAY", "INDIGO", "DARK", "LIGHT", "MID",
           "WASHED", "ECRU", "GREEN", "BROWN", "RINSE", "BLEACH", "VINTAGE", "RAW")


def parse_design(design: str):
    """Split a Jeanologia design filename into (style, size, color) — best effort.

    Real examples: '8307-244-800-BLACK-36.jean5', 'MARQUEE WASHED BLACK-40-OK.jean5',
    'Dragon dark-46.jean5', 'Production 1'.
    """
    if not design:
        return None, None, None
    base = re.sub(r"\.(jean5?|jn5|xml)$", "", design.strip(), flags=re.I)
    parts = [p for p in re.split(r"[-_]", base) if p != ""]
    size = None
    for p in reversed(parts):
        t = p.strip().upper()
        if t in ("OK", "NEW", "COPY"):
            continue
        m = _SIZE_RE.match("-" + t + "-")
        if m and (t.isdigit() and 20 <= int(t) <= 60 or not t.isdigit()):
            size = t
            break
    color = None
    up = base.upper()
    for c in _COLORS:
        if c in up:
            color = c
            break
    style = parts[0].strip() if parts else base
    if len(parts) >= 3 and parts[0].isdigit():
        style = "-".join(parts[:3])
    return style or None, size, color


# ------------------------------------------------------------------ bootstrap

# Columns introduced after a table shipped. Keep entries forever: they are what
# upgrades an existing database in the field.
_ADDED_COLUMNS = [
    ("machine_alarms", "suspect", "INTEGER DEFAULT 0"),
]


def init_db():
    c = connect()
    with _write_lock:
        c.executescript((config.ROOT / "app" / "schema.sql").read_text(encoding="utf-8"))
        # schema.sql only CREATEs; columns added to an existing table need an ALTER
        for tbl, col, decl in _ADDED_COLUMNS:
            if col not in {r[1] for r in c.execute(f"PRAGMA table_info({tbl})")}:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {decl}")
        c.commit()
    _seed_defaults()
    import_machines_json()
    return c


def _seed_defaults():
    if not q1("SELECT 1 FROM shifts LIMIT 1"):
        exmany("INSERT INTO shifts(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)",
               [("A", "06:00", "14:00", 15), ("B", "14:00", "22:00", 15), ("C", "22:00", "06:00", 15)])
    if not q1("SELECT 1 FROM users LIMIT 1"):
        from .auth import create_user
        create_user("admin", "admin", "admin", full_name="System Administrator",
                    email="ahmed.elgohary@tcgarments.com")
    if not q1("SELECT 1 FROM alert_rules LIMIT 1"):
        seed_alert_rules()


DEFAULT_RULES = [
    # (type, severity, threshold, window_min, cooldown_min, description)
    ("machine_offline",       "critical", 3,     None, 30,  "Machine unreachable for N consecutive heartbeats"),
    ("heartbeat_lost",        "warning",  300,   None, 30,  "No successful heartbeat for N seconds"),
    ("slow_response",         "info",     1500,  None, 60,  "Heartbeat latency above N ms"),
    ("communication_failure", "warning",  3,     60,   60,  "N failed connection attempts within the window"),
    ("sync_failure",          "warning",  2,     60,   60,  "N failed data syncs within the window"),
    ("machine_stopped",       "critical", 1800,  None, 60,  "No production for N seconds while machine is online"),
    ("excessive_idle",        "warning",  900,   None, 45,  "Idle gap longer than N seconds"),
    ("repeated_alarm",        "critical", 5,     60,   30,  "N or more machine alarms within the window"),
    ("production_drop",       "warning",  40,    None, 120, "Hourly output N% below the machine baseline"),
    ("performance_low",       "warning",  70,    None, 180, "Daily performance below N%"),
    ("excessive_downtime",    "warning",  120,   None, 180, "Downtime today above N minutes"),
    ("abnormal_behaviour",    "warning",  3,     None, 120, "Output deviates more than N sigma from baseline"),
    ("maintenance_risk",      "warning",  60,    None, 720, "Machine health score below N"),
    ("target_risk",           "warning",  85,    None, 240, "Forecast end-of-day below N% of target"),
    ("thermal_risk",          "warning",  55,    None, 180, "Galvo/servo temperature above N degC"),
    ("head_drift",            "info",     25,    None, 1440, "Scan-head calibration drifted N% from its own baseline"),
    ("laser_credit_low",      "warning",  86400, None, 1440, "Laser licence credit below N seconds"),
    ("data_quality",          "info",     1,     60,   360, "Source rows rejected (clock drift / bad data)"),
]


def seed_alert_rules():
    exmany("INSERT INTO alert_rules(machine_id,type,enabled,severity,threshold,window_min,cooldown_min,"
           "channels,description,updated_at) VALUES(NULL,?,1,?,?,?,?,'app',?,?)",
           [(t, s, th, w, cd, d, now()) for t, s, th, w, cd, d in DEFAULT_RULES])


# --------------------------------------------------- import legacy artefacts

MODEL_TYPE = {"COMPACT": "compact", "FLEXI": "flexi", "TWIN": "laser"}


def import_machines_json(path=None):
    """Load machines.json into the registry. Existing rows are never overwritten."""
    path = path or config.LEGACY_MACHINES_JSON
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    added = 0
    for key, m in data.items():
        mid = int(key)
        if q1("SELECT 1 FROM machines WHERE id=?", (mid,)):
            continue
        name = m.get("name") or f"Machine {mid}"
        method = "http_push" if m.get("transport") == "http_push" or not m.get("ip") else "smb"
        mtype = "laser"
        for k, v in MODEL_TYPE.items():
            if k in name.upper():
                mtype = v
        ex("""INSERT INTO machines(id,code,name,host,port,share,db_file,username,password_enc,
                connection_method,machine_type,production_area,status,enabled,created_at,updated_at)
              VALUES(?,?,?,?,445,?,?,?,?,?,?,'Laser','active',?,?,?)""",
           (mid, f"LM{mid:02d}", name, m.get("ip"), m.get("share", "JeanologiaDB"),
            m.get("db", "stats.db"), _seed_user(), config.encrypt(_seed_password()), method, mtype,
            1 if m.get("enabled", True) else 0, now(), now()))
        ex("INSERT OR IGNORE INTO machine_state(machine_id,status,connectivity,updated_at) VALUES(?,?,?,?)",
           (mid, "UNKNOWN", "DISCONNECTED", now()))
        added += 1
    return added


def _seed_user():
    return os.environ.get("LASER_DEFAULT_MACHINE_USER", "Admin")


def _seed_password():
    """First-run SMB password for machines seeded from machines.json.

    A factory password must never live in the repository, so this is empty unless the
    operator supplies it. Empty simply means the machine sits DISCONNECTED until someone
    enters its credentials in Machine Configuration, which is the correct default for a
    fresh clone. Machines already in the database are never re-seeded, so setting this
    changes nothing on an existing install.
    """
    return os.environ.get("LASER_DEFAULT_MACHINE_PASSWORD", "")


def import_central_db(path=None, progress=None):
    """Seed production history from the legacy central.db (one-time).

    The machine-side stats.db carries richer columns, so the collector's deep
    backfill will upgrade these rows in place; this just guarantees history for
    machines that are currently unreachable.
    """
    path = path or config.LEGACY_CENTRAL_DB
    if not path.exists():
        return {"imported": 0, "reason": "central.db not found"}
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    drift = dt.datetime.now() + dt.timedelta(days=get_int("clock_drift_days", 2))
    drift_s = drift.strftime("%Y-%m-%d %H:%M:%S")
    total = src.execute("SELECT COUNT(*) FROM production").fetchone()[0]
    done = 0
    batch, ins = [], 0
    stamp = now()
    for r in src.execute("SELECT machine_id,source_id,init_date,end_date,design,copies,laser_time,"
                         "units,average_design_time,collected_at FROM production ORDER BY id"):
        init, end = r["init_date"], (r["end_date"] or None)
        suspect = 1 if (init > drift_s or init < "2015-01-01") else 0
        elapsed = _elapsed(init, end)
        laser_ms = r["laser_time"] or 0
        duty = round((laser_ms / 1000.0) / elapsed, 4) if elapsed and elapsed > 0 else None
        shift, pdate = shift_of(init)
        style, size, color = parse_design(r["design"])
        batch.append((r["machine_id"], r["source_id"], init, end, r["design"], r["copies"], r["units"],
                      laser_ms, elapsed, duty, None, 0, 0, None, None, None, None, None,
                      r["average_design_time"], None, style, size, color, shift, pdate, suspect,
                      r["collected_at"] or stamp))
        done += 1
        if len(batch) >= 5000:
            ins += _insert_production(batch)
            batch.clear()
            if progress:
                progress(done, total)
    ins += _insert_production(batch)
    src.close()
    if progress:
        progress(done, total)
    return {"imported": ins, "scanned": total}


PROD_COLS = ("machine_id,source_id,init_date,end_date,design,copies,units,laser_ms,elapsed_seconds,"
             "laser_duty,operator,alarm1,alarm2,optimizer,temp_galvo_x,temp_galvo_y,temp_servo_x,"
             "temp_servo_y,avg_design_ms,emark_version,style,size,color,shift,production_date,"
             "suspect,collected_at")
_PROD_PLACEHOLDERS = ",".join("?" * len(PROD_COLS.split(",")))


def _insert_production(batch):
    """Insert new jobs, and update the ones already stored (end_date arrives late)."""
    if not batch:
        return 0
    sql = (f"INSERT INTO production({PROD_COLS}) VALUES({_PROD_PLACEHOLDERS}) "
           "ON CONFLICT(machine_id,source_id) DO UPDATE SET "
           "end_date=excluded.end_date, copies=excluded.copies, units=excluded.units, "
           "laser_ms=excluded.laser_ms, elapsed_seconds=excluded.elapsed_seconds, "
           "laser_duty=excluded.laser_duty, alarm1=excluded.alarm1, alarm2=excluded.alarm2, "
           "temp_galvo_x=excluded.temp_galvo_x, temp_galvo_y=excluded.temp_galvo_y, "
           "temp_servo_x=excluded.temp_servo_x, temp_servo_y=excluded.temp_servo_y, "
           "avg_design_ms=excluded.avg_design_ms, collected_at=excluded.collected_at "
           "WHERE production.end_date IS NULL OR production.end_date='' "
           "   OR excluded.end_date IS NOT NULL AND excluded.end_date<>''")
    return exmany(sql, batch)


def _elapsed(init, end):
    if not init or not end:
        return None
    try:
        a = dt.datetime.strptime(init[:19], "%Y-%m-%d %H:%M:%S")
        b = dt.datetime.strptime(end[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    s = (b - a).total_seconds()
    return s if 0 <= s < 86400 * 2 else None
