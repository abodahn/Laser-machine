"""Machine connectivity: heartbeat, incremental ingestion and live state.

Transport is Windows SMB (the machines expose \\host\\JeanologiaDB read-only).
SQLite files are copied to a local cache before being read: locking over SMB is
unreliable and we must never block the machine's own eMark application.
"""
import datetime as dt
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time

from . import config, db

SEP = chr(92)
_mounts = {}
_mount_lock = threading.Lock()


# --------------------------------------------------------------- SMB access

def unc(host, share="JeanologiaDB"):
    return f"{SEP}{SEP}{host}{SEP}{share}"


_host_locks = {}


def _host_lock(key):
    with _mount_lock:
        return _host_locks.setdefault(key, threading.Lock())


def ensure_mount(host, share, user, password, timeout=15, force=False):
    """Make the machine's share usable by this process.

    Windows keeps one SMB session per server, so two threads issuing `net use`
    for the same host will fight each other (errors 1219/1385). Everything for a
    host is therefore serialised, and we only re-authenticate when the share is
    actually unreachable — an already-working session is left alone.
    """
    key = (host or "").lower()
    target = unc(host, share)
    with _host_lock(key):
        if not force:
            with _mount_lock:
                last = _mounts.get(key)
            if last and time.time() - last < 600:
                return True, ""
            try:                                              # already authenticated?
                os.listdir(target)
                with _mount_lock:
                    _mounts[key] = time.time()
                return True, ""
            except OSError:
                pass
        net_timeout = max(25, (timeout or 10) * 2)
        try:
            subprocess.run(["net", "use", target, "/delete", "/y"],
                           capture_output=True, timeout=net_timeout)
            r = subprocess.run(["net", "use", target, password, f"/user:{user}"],
                               capture_output=True, timeout=net_timeout, text=True)
            ok = r.returncode == 0
            if ok:
                with _mount_lock:
                    _mounts[key] = time.time()
            return ok, (r.stderr or r.stdout or "").strip()
        except subprocess.TimeoutExpired:
            return False, "mount timeout"
        except Exception as e:                                # noqa: BLE001
            return False, str(e)


def probe(host, port, timeout):
    """TCP reachability + latency in ms."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, int(port or 445)), timeout=timeout):
            return True, (time.perf_counter() - t0) * 1000.0, ""
    except Exception as e:                                    # noqa: BLE001
        return False, (time.perf_counter() - t0) * 1000.0, str(e)


def fetch_file(m, filename, force=False):
    """Copy one machine sqlite file into the local cache if it changed. Returns (path, mtime, changed)."""
    src = os.path.join(unc(m["host"], m["share"] or "JeanologiaDB"), filename)
    dst_dir = config.CACHE_DIR / str(m["id"])
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / filename
    st = os.stat(src)
    mtime = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    if not force and dst.exists() and dst.stat().st_mtime >= st.st_mtime and dst.stat().st_size == st.st_size:
        return dst, mtime, False
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    shutil.copy2(src, tmp)
    for side in ("-wal", "-shm"):                              # copy journal siblings when present
        s2 = src + side
        if os.path.exists(s2):
            try:
                shutil.copy2(s2, str(dst) + side)
            except OSError:
                pass
    os.replace(tmp, dst)
    return dst, mtime, True


def open_ro(path):
    return sqlite3.connect(f"file:{str(path).replace(SEP, '/')}?mode=ro", uri=True)


# ------------------------------------------------------------- alarm mapping

ALARM_SOURCES = {
    "markingAlarms":        ("marking",   "date"),
    "laserErrorsHistory":   ("laser",     "date"),
    "headErrors":           ("head",      "date"),
    "operationErrors":      ("operation", "date"),
    "genericErrorsWarnings": ("system",   "date"),
    "errorrecord":          ("system",    "date"),
}

CRITICAL_WORDS = ("water alarm", "interlock", "nitrogen", "vswr", "laser head fault",
                  "temperature too high", "emergency", "power fail", "head fault",
                  "water flow below")
WARNING_WORDS = ("shutter", "water flow near", "disk space", "temperature", "rearm",
                 "scanhead stop", "position error", "dc voltage")
INFO_WORDS = ("cloud disconnected", "no shapes to mark", "emission indicator", "shutter closed")


def classify(description, error_code, category):
    d = (description or "").lower()
    for w in INFO_WORDS:
        if w in d:
            return "info"
    for w in CRITICAL_WORDS:
        if w in d:
            return "critical"
    for w in WARNING_WORDS:
        if w in d:
            return "warning"
    if category in ("laser", "head"):
        return "warning"
    return "info"


_ERRREC_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})::(\d{2})(\d{2})(\d{2})")


def norm_ts(value):
    """Machine timestamps are usually 'YYYY-MM-DD HH:MM:SS'; errorrecord uses its own format."""
    if not value:
        return None
    s = str(value).strip()
    m = _ERRREC_DATE.match(s)
    if m:
        return "%s-%s-%s %s:%s:%s" % m.groups()
    s = s.replace("T", " ").replace("/", "-")
    return s[:19] if len(s) >= 19 else s


# ------------------------------------------------------------------ ingestion

TRAIL = 30          # re-read this many trailing source rows so late end_date updates land


def sync_production(m, full=False):
    t0 = time.perf_counter()
    path, mtime, _ = fetch_file(m, m["db_file"] or "stats.db")
    src = open_ro(path)
    src.row_factory = sqlite3.Row
    cols = {r[1] for r in src.execute("PRAGMA table_info(production)")}
    last = db.q1("SELECT COALESCE(MAX(source_id),0) x FROM production WHERE machine_id=?", (m["id"],))["x"]
    since = 0 if full else max(0, last - TRAIL)

    def col(name, default="NULL"):
        return name if name in cols else default

    sql = (f"SELECT id,init_date,end_date,design,copies,laser_time,units,"
           f"{col('user')} AS op,{col('alarm1','0')} AS a1,{col('alarm2','0')} AS a2,"
           f"{col('optimizer')} AS opt,{col('temp_maxima_galvo_X')} AS gx,{col('temp_maxima_galvo_Y')} AS gy,"
           f"{col('temp_maxima_servo_X')} AS sx,{col('temp_maxima_servo_Y')} AS sy,"
           f"{col('average_design_time')} AS adt,{col('eMarkVersion')} AS ver "
           f"FROM production WHERE id > ? ORDER BY id")
    drift = (dt.datetime.now() + dt.timedelta(days=db.get_int("clock_drift_days", 2))
             ).strftime("%Y-%m-%d %H:%M:%S")
    stamp = db.now()
    batch, rejected, maxid = [], 0, last
    for r in src.execute(sql, (since,)):
        init = norm_ts(r["init_date"])
        end = norm_ts(r["end_date"]) or None
        if not init:
            rejected += 1
            continue
        suspect = 1 if (init > drift or init < "2015-01-01") else 0
        rejected += suspect
        elapsed = db._elapsed(init, end)
        laser_ms = r["laser_time"] or 0
        duty = round((laser_ms / 1000.0) / elapsed, 4) if elapsed and elapsed > 0 else None
        shift, pdate = db.shift_of(init)
        style, size, color = db.parse_design(r["design"])
        batch.append((m["id"], r["id"], init, end, r["design"], r["copies"], r["units"], laser_ms,
                      elapsed, duty, r["op"], r["a1"] or 0, r["a2"] or 0, r["opt"], r["gx"], r["gy"],
                      r["sx"], r["sy"], r["adt"], str(r["ver"]) if r["ver"] is not None else None,
                      style, size, color, shift, pdate, suspect, stamp))
        maxid = max(maxid, r["id"])
        if len(batch) >= 4000:
            db._insert_production(batch)
            batch.clear()
    db._insert_production(batch)
    src.close()
    new = max(0, maxid - last)
    db.ex("INSERT INTO sync_log(ts,machine_id,source,rows_new,duration_ms,ok) VALUES(?,?,?,?,?,1)",
          (stamp, m["id"], "stats", new, (time.perf_counter() - t0) * 1000))
    return {"new": new, "max_source_id": maxid, "mtime": mtime, "rejected": rejected}


def sync_alarms(m, full=False):
    drift = (dt.datetime.now() + dt.timedelta(days=db.get_int("clock_drift_days", 2)
                                              )).strftime("%Y-%m-%d %H:%M:%S")
    try:
        path, _, _ = fetch_file(m, "error.db")
    except FileNotFoundError:
        return {"new": 0}
    src = open_ro(path)
    src.row_factory = sqlite3.Row
    present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    stamp = db.now()
    total = 0
    for table, (category, _datecol) in ALARM_SOURCES.items():
        if table not in present:
            continue
        last = db.q1("SELECT COALESCE(MAX(source_id),0) x FROM machine_alarms "
                     "WHERE machine_id=? AND source_table=?", (m["id"], table))["x"]
        cols = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
        sel = ["id", "date", "description"]
        sel.append("type" if "type" in cols else "NULL AS type")
        sel.append("errorcode" if "errorcode" in cols else "NULL AS errorcode")
        sel.append("serialnumber" if "serialnumber" in cols else "NULL AS serialnumber")
        sel.append("eMarkVersion" if "eMarkVersion" in cols else "NULL AS eMarkVersion")
        rows = []
        for r in src.execute(f"SELECT {','.join(sel)} FROM {table} WHERE id > ? ORDER BY id",
                             (0 if full else last,)):
            ts = norm_ts(r["date"])
            if not ts:
                continue
            typ = r["type"]
            desc = (r["description"] or "").strip()
            shift, pdate = db.shift_of(ts)
            # A machine with a wrong clock stamps alarms in the future. Those made
            # every "alarms in the last hour" query true forever and pinned the
            # machine in ALARM. Flag them the same way suspect production rows are.
            suspect = 1 if (ts > drift or ts < "2015-01-01") else 0
            rows.append((m["id"], table, r["id"], ts,
                         typ if isinstance(typ, int) else None, r["errorcode"], desc,
                         r["serialnumber"], str(r["eMarkVersion"]) if r["eMarkVersion"] else None,
                         category, classify(desc, r["errorcode"], category), pdate, shift,
                         suspect, stamp))
        total += db.exmany(
            "INSERT OR IGNORE INTO machine_alarms(machine_id,source_table,source_id,ts,type_code,"
            "error_code,description,serial_number,emark_version,category,severity,production_date,"
            "shift,suspect,collected_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # scan-head calibration history
    if "headHCM" in present:
        last = db.q1("SELECT COALESCE(MAX(source_id),0) x FROM head_health WHERE machine_id=?",
                     (m["id"],))["x"]
        rows = [(m["id"], r["id"], norm_ts(r["date"]), r["minCurrentX"], r["minCurrentY"],
                 r["maxCurrentX"], r["maxCurrentY"], r["meanCurrentX"], r["meanCurrentY"],
                 r["hystLeftX"], r["hystLeftY"], r["hystPos0X"], r["hystPos0Y"],
                 r["hystRightX"], r["hystRightY"], r["slopeX"], r["slopeY"], stamp)
                for r in src.execute("SELECT * FROM headHCM WHERE id > ? ORDER BY id",
                                     (0 if full else last,))]
        db.exmany("INSERT OR IGNORE INTO head_health(machine_id,source_id,ts,min_current_x,min_current_y,"
                  "max_current_x,max_current_y,mean_current_x,mean_current_y,hyst_left_x,hyst_left_y,"
                  "hyst_pos0_x,hyst_pos0_y,hyst_right_x,hyst_right_y,slope_x,slope_y,collected_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    src.close()
    db.ex("INSERT INTO sync_log(ts,machine_id,source,rows_new,ok) VALUES(?,?,?,?,1)",
          (stamp, m["id"], "error", total))
    return {"new": total}


def sync_identity(m):
    """Model, serial, optics and licence credit — slow-moving, refreshed daily."""
    stamp = db.now()
    info = {}
    try:
        path, _, _ = fetch_file(m, "machines.db")
        src = open_ro(path)
        src.row_factory = sqlite3.Row
        sel = src.execute("SELECT machine FROM selectedMachine").fetchone()
        if sel:
            r = src.execute(
                "SELECT id,name,machineType,nlasers,nmannequins,hasCamera,hasColumns,hasLengthSensor,"
                "has40ixLaser,dutyMax FROM machines WHERE id=?", (sel["machine"],)).fetchone()
            if r:
                info = dict(r)
        try:
            info["serial"] = src.execute("SELECT serialNumber FROM proteo").fetchone()[0]
        except Exception:                                     # noqa: BLE001
            pass
        try:
            w = src.execute("SELECT beam_diameter,laser_power_avg FROM whiteLevelParams "
                            "ORDER BY id DESC LIMIT 1").fetchone()
            if w:
                info["beam_diameter"], info["laser_power_avg"] = w[0], w[1]
        except Exception:                                     # noqa: BLE001
            pass
        src.close()
    except Exception as e:                                    # noqa: BLE001
        info["error"] = str(e)
    ver = db.q1("SELECT emark_version v FROM production WHERE machine_id=? AND emark_version IS NOT NULL "
                "ORDER BY init_date DESC LIMIT 1", (m["id"],))
    import json
    db.ex("""INSERT INTO machine_identity(machine_id,model,model_id,serial_number,n_lasers,n_mannequins,
             has_camera,has_columns,has_length_sensor,has_40ix_laser,duty_max,emark_version,
             laser_power_avg,beam_diameter,raw_json,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(machine_id) DO UPDATE SET
             model=COALESCE(excluded.model,machine_identity.model),
             model_id=COALESCE(excluded.model_id,machine_identity.model_id),
             serial_number=COALESCE(excluded.serial_number,machine_identity.serial_number),
             n_lasers=COALESCE(excluded.n_lasers,machine_identity.n_lasers),
             n_mannequins=COALESCE(excluded.n_mannequins,machine_identity.n_mannequins),
             has_camera=COALESCE(excluded.has_camera,machine_identity.has_camera),
             has_columns=COALESCE(excluded.has_columns,machine_identity.has_columns),
             has_length_sensor=COALESCE(excluded.has_length_sensor,machine_identity.has_length_sensor),
             has_40ix_laser=COALESCE(excluded.has_40ix_laser,machine_identity.has_40ix_laser),
             duty_max=COALESCE(excluded.duty_max,machine_identity.duty_max),
             emark_version=COALESCE(excluded.emark_version,machine_identity.emark_version),
             laser_power_avg=COALESCE(excluded.laser_power_avg,machine_identity.laser_power_avg),
             beam_diameter=COALESCE(excluded.beam_diameter,machine_identity.beam_diameter),
             raw_json=excluded.raw_json,
             updated_at=excluded.updated_at""",
          (m["id"], info.get("name"), info.get("machineType"), info.get("serial"),
           info.get("nlasers"), info.get("nmannequins"), _b(info.get("hasCamera")),
           _b(info.get("hasColumns")), _b(info.get("hasLengthSensor")), _b(info.get("has40ixLaser")),
           info.get("dutyMax"), ver["v"] if ver else None, info.get("laser_power_avg"),
           info.get("beam_diameter"), json.dumps(info, default=str), stamp))
    if info.get("name") and not m["model"]:
        db.ex("UPDATE machines SET model=?, serial_number=COALESCE(serial_number,?) WHERE id=?",
              (info["name"], info.get("serial"), m["id"]))
    try:
        sync_optics(m)
    except Exception:                                         # noqa: BLE001
        pass
    # licence / rental credit
    try:
        path, _, _ = fetch_file(m, "codes.db")
        src = open_ro(path)
        rows = [(m["id"], r[0], r[1],
                 dt.datetime.fromtimestamp((r[1] or 0) / 1000).strftime("%Y-%m-%d %H:%M:%S") if r[1] else None,
                 r[2], r[3], r[4], stamp)
                for r in src.execute("SELECT id,time,previous,added,rented FROM codes ORDER BY id")]
        src.close()
        db.exmany("INSERT OR IGNORE INTO laser_credit(machine_id,source_id,applied_ms,applied_at,"
                  "balance_before,credit_added,rented,collected_at) VALUES(?,?,?,?,?,?,?,?)", rows)
    except Exception:                                         # noqa: BLE001
        pass
    return info


def sync_optics(m):
    """Capture the machine's laser recipe from config.db.

    34 presets x 38 parameters. markSpeed and jumpSpeed set the physical ceiling
    on how fast the machine can mark, so a unit configured below the fleet
    standard is permanently slow in a way no production metric explains.
    """
    stamp = db.now()
    rows = []
    try:
        path, _, _ = fetch_file(m, "config.db")
    except Exception:                                         # noqa: BLE001
        return {"presets": 0}
    src = open_ro(path)
    src.row_factory = sqlite3.Row
    try:
        cols = [c[1] for c in src.execute("PRAGMA table_info(diodeBoardParameters)")]
        for r in src.execute("SELECT * FROM diodeBoardParameters"):
            preset = r["presetName"]
            if not preset:
                continue
            for c in cols:
                if c == "presetName":
                    continue
                v = r[c]
                rows.append((m["id"], preset, c, None if v is None else str(v), stamp))
    except Exception:                                         # noqa: BLE001
        pass
    try:                                        # machine-wide laser settings live in one row
        mr = src.execute("SELECT * FROM machine").fetchone()
        if mr:
            for c in mr.keys():
                v = mr[c]
                rows.append((m["id"], "_machine", c, None if v is None else str(v), stamp))
    except Exception:                                         # noqa: BLE001
        pass
    src.close()
    db.exmany("INSERT INTO machine_optics(machine_id,preset,param,value,collected_at) "
              "VALUES(?,?,?,?,?) ON CONFLICT(machine_id,preset,param) DO UPDATE SET "
              "value=excluded.value, collected_at=excluded.collected_at", rows)
    presets = len({r[1] for r in rows})
    db.ex("INSERT INTO sync_log(ts,machine_id,source,rows_new,ok) VALUES(?,?,?,?,1)",
          (stamp, m["id"], "optics", len(rows)))
    return {"presets": presets, "params": len(rows)}


def _b(v):
    if v in (None, ""):
        return None
    if isinstance(v, str):
        return 1 if v.lower() in ("true", "1", "yes") else 0
    return 1 if v else 0


# -------------------------------------------------------------- state engine

def evaluate_state(machine_id):
    """Derive the operational status of one machine from what we have stored."""
    m = db.q1("SELECT * FROM machines WHERE id=?", (machine_id,))
    st = db.q1("SELECT * FROM machine_state WHERE machine_id=?", (machine_id,)) or {}
    idle_th = db.get_int("idle_threshold_seconds", 300)
    down_th = db.get_int("down_threshold_seconds", 900)
    fail_th = db.get_int("offline_after_failures", 3)
    slow_ms = db.get_int("slow_latency_ms", 1500)
    alarm_win = db.get_int("alarm_recent_minutes", 15)

    last_job = db.q1("SELECT init_date,end_date,design,units FROM production "
                     "WHERE machine_id=? AND suspect=0 ORDER BY init_date DESC LIMIT 1", (machine_id,))
    now_dt = dt.datetime.now()
    last_activity, current_design, job_start, job_units = None, None, None, None
    open_job = False
    if last_job:
        current_design = last_job["design"]
        job_start = last_job["init_date"]
        job_units = last_job["units"]
        if last_job["end_date"]:
            last_activity = last_job["end_date"]
        else:
            last_activity = last_job["init_date"]
            open_job = True
    idle_seconds = None
    if last_activity:
        try:
            idle_seconds = (now_dt - dt.datetime.strptime(last_activity[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except ValueError:
            idle_seconds = None

    fails = st["consecutive_fail"] if st else 0
    latency = st["latency_ms"] if st else None
    if fails >= fail_th:
        conn = "NOT_RESPONDING"
    elif fails > 0:
        conn = "DISCONNECTED"
    elif latency and latency > slow_ms:
        conn = "SLOW"
    else:
        conn = "CONNECTED"

    recent_alarm = db.q1(
        "SELECT COUNT(*) n FROM machine_alarms WHERE machine_id=? AND severity IN ('critical','warning') "
        "AND suspect=0 AND ts <= ? AND ts >= ?",
        (machine_id, now_dt.strftime("%Y-%m-%d %H:%M:%S"),
         (now_dt - dt.timedelta(minutes=alarm_win)).strftime("%Y-%m-%d %H:%M:%S")))

    # An OPEN job (no end_date yet) means the machine is working on it right now.
    # Its age is run time, not idle time -- a two-hour batch is still production.
    # Only a job open beyond any plausible duration is treated as a stale record.
    stale_h = db.get_float("open_job_max_hours", 12)
    job_stale = open_job and idle_seconds is not None and idle_seconds > stale_h * 3600

    if not m["enabled"] or m["status"] == "disabled":
        status = "DISABLED"
    elif m["status"] == "maintenance":
        status = "MAINTENANCE"
    elif fails >= fail_th:
        status = "OFFLINE"
    elif open_job and not job_stale:
        status = "PRODUCING"
    elif idle_seconds is None:
        status = "STOPPED"
    elif idle_seconds < idle_th:
        status = "PRODUCING"
    elif recent_alarm and recent_alarm["n"] > 0 and idle_seconds >= idle_th:
        status = "ALARM"
    elif idle_seconds < down_th:
        status = "IDLE"
    else:
        status = "STOPPED"

    prev = st["status"] if st else None
    stamp = db.now()
    if prev != status:
        db.ex("UPDATE state_history SET end_time=?, duration_seconds=CAST((julianday(?)-julianday(start_time))*86400 AS INT) "
              "WHERE machine_id=? AND end_time IS NULL", (stamp, stamp, machine_id))
        db.ex("INSERT INTO state_history(machine_id,status,connectivity,start_time,created_at) VALUES(?,?,?,?,?)",
              (machine_id, status, conn, stamp, stamp))
        _record_downtime(machine_id, prev, status, stamp)
    db.ex("""UPDATE machine_state SET status=?, connectivity=?, online=?, current_design=?,
             current_job_start=?, current_job_units=?, last_activity=?, idle_seconds=?,
             status_since=COALESCE(CASE WHEN ?=status THEN status_since END, ?), updated_at=?
             WHERE machine_id=?""",
          (status, conn, 1 if conn in ("CONNECTED", "SLOW") else 0, current_design, job_start,
           job_units, last_activity, idle_seconds, status, stamp, stamp, machine_id))
    return status


DOWN_KIND = {"IDLE": "idle", "STOPPED": "stopped", "OFFLINE": "offline",
             "ALARM": "alarm", "MAINTENANCE": "maintenance"}


def _record_downtime(machine_id, prev, status, stamp):
    if prev in DOWN_KIND:
        db.ex("UPDATE downtime SET end_time=?, duration_seconds=CAST((julianday(?)-julianday(start_time))*86400 AS INT) "
              "WHERE machine_id=? AND end_time IS NULL", (stamp, stamp, machine_id))
    if status in DOWN_KIND:
        shift, pdate = db.shift_of(stamp)
        db.ex("INSERT OR IGNORE INTO downtime(machine_id,start_time,kind,detected_by,production_date,"
              "shift,created_at) VALUES(?,?,?,?,?,?,?)",
              (machine_id, stamp, DOWN_KIND[status], "state_engine", pdate, shift, stamp))


# --------------------------------------------------------------- entry points

def heartbeat(machine_id):
    m = db.q1("SELECT * FROM machines WHERE id=?", (machine_id,))
    if not m:
        return {"ok": False, "error": "unknown machine"}
    stamp = db.now()
    if m["connection_method"] == "http_push":
        # A push machine is alive if it delivered recently.
        last = db.q1("SELECT MAX(collected_at) c FROM production WHERE machine_id=?", (machine_id,))["c"]
        ok = bool(last) and (dt.datetime.now() - dt.datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
                             ).total_seconds() < max(300, (m["heartbeat_interval"] or 60) * 5)
        _store_heartbeat(m, ok, None, "" if ok else "no push received", stamp)
        return {"ok": ok, "mode": "push"}
    if not m["host"] or not m["enabled"]:
        _store_heartbeat(m, False, None, "no host configured", stamp)
        return {"ok": False, "error": "no host"}
    ok, latency, err = probe(m["host"], m["port"], m["timeout_seconds"] or 10)
    if ok:
        mok, merr = ensure_mount(m["host"], m["share"], m["username"], config.decrypt(m["password_enc"]),
                                 m["timeout_seconds"] or 10)
        if not mok:
            ok, err = False, f"share auth failed: {merr[:180]}"
        else:
            try:
                os.stat(os.path.join(unc(m["host"], m["share"]), m["db_file"] or "stats.db"))
            except Exception as e:                            # noqa: BLE001
                ok, err = False, str(e)[:180]
    _store_heartbeat(m, ok, latency, err, stamp)
    evaluate_state(machine_id)
    return {"ok": ok, "latency_ms": round(latency, 1), "error": err}


def _store_heartbeat(m, ok, latency, err, stamp):
    if ok:
        db.ex("UPDATE machine_state SET last_heartbeat=?, last_heartbeat_ok=?, latency_ms=?, "
              "consecutive_fail=0, last_error=NULL, updated_at=? WHERE machine_id=?",
              (stamp, stamp, latency, stamp, m["id"]))
    else:
        db.ex("UPDATE machine_state SET last_heartbeat=?, latency_ms=?, "
              "consecutive_fail=COALESCE(consecutive_fail,0)+1, last_error=?, updated_at=? "
              "WHERE machine_id=?", (stamp, latency, (err or "")[:400], stamp, m["id"]))
        with _mount_lock:
            _mounts.pop((m["host"] or "").lower(), None)
    db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,latency_ms,detail) VALUES(?,?,?,?,?,?)",
          (m["id"], stamp, "heartbeat", 1 if ok else 0, latency, (err or "")[:400]))


def sync(machine_id, full=False):
    m = db.q1("SELECT * FROM machines WHERE id=?", (machine_id,))
    if not m or not m["enabled"] or m["connection_method"] in ("http_push", "disabled"):
        return {"skipped": True}
    stamp = db.now()
    try:
        ensure_mount(m["host"], m["share"], m["username"], config.decrypt(m["password_enc"]),
                     m["timeout_seconds"] or 10)
        res = sync_production(m, full=full)
        try:
            res["alarms"] = sync_alarms(m, full=full)["new"]
        except Exception as e:                                # noqa: BLE001
            res["alarms_error"] = str(e)[:200]
        db.ex("UPDATE machine_state SET last_sync=?, last_sync_ok=?, last_source_id=?, source_mtime=?, "
              "updated_at=? WHERE machine_id=?",
              (stamp, stamp, res["max_source_id"], res["mtime"], stamp, machine_id))
        db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,rows_new,detail) VALUES(?,?,?,1,?,?)",
              (machine_id, stamp, "sync", res["new"], f"alarms={res.get('alarms', 0)}"))
        evaluate_state(machine_id)
        return res
    except Exception as e:                                    # noqa: BLE001
        msg = str(e)[:400]
        db.ex("UPDATE machine_state SET last_sync=?, last_error=?, updated_at=? WHERE machine_id=?",
              (stamp, msg, stamp, machine_id))
        db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,detail) VALUES(?,?,?,0,?)",
              (machine_id, stamp, "sync", msg))
        db.ex("INSERT INTO sync_log(ts,machine_id,source,ok,error) VALUES(?,?,?,0,?)",
              (stamp, machine_id, "stats", msg))
        return {"error": msg}


def test_connection(machine_id):
    """Full diagnostic used by the configuration page."""
    m = db.q1("SELECT * FROM machines WHERE id=?", (machine_id,))
    if not m:
        return {"ok": False, "error": "unknown machine"}
    out = {"machine": m["name"], "host": m["host"], "method": m["connection_method"], "steps": []}
    if m["connection_method"] == "http_push":
        last = db.q1("SELECT MAX(collected_at) c FROM production WHERE machine_id=?", (machine_id,))["c"]
        out["steps"].append({"step": "last push", "ok": bool(last), "detail": last or "never"})
        out["ok"] = bool(last)
        return out
    ok, latency, err = probe(m["host"], m["port"], m["timeout_seconds"] or 10)
    out["steps"].append({"step": f"tcp {m['host']}:{m['port']}", "ok": ok,
                         "detail": f"{latency:.0f} ms" if ok else err[:200]})
    if ok:
        mok, merr = ensure_mount(m["host"], m["share"], m["username"],
                                 config.decrypt(m["password_enc"]), m["timeout_seconds"] or 10,
                                 force=True)
        out["steps"].append({"step": f"authenticate {m['share']}", "ok": mok, "detail": merr[:200]})
        if mok:
            try:
                files = [f for f in os.listdir(unc(m["host"], m["share"])) if f.endswith(".db")]
                out["steps"].append({"step": "list share", "ok": True, "detail": ", ".join(files)})
                p, mtime, _ = fetch_file(m, m["db_file"] or "stats.db", force=True)
                src = open_ro(p)
                n, mx, last_ts = src.execute("SELECT COUNT(*),MAX(id),MAX(init_date) FROM production").fetchone()
                src.close()
                out["steps"].append({"step": "read stats.db", "ok": True,
                                     "detail": f"{n} rows, last id {mx}, last job {last_ts}, file mtime {mtime}"})
            except Exception as e:                            # noqa: BLE001
                out["steps"].append({"step": "read stats.db", "ok": False, "detail": str(e)[:250]})
    out["ok"] = all(s["ok"] for s in out["steps"])
    db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,latency_ms,detail) VALUES(?,?,?,?,?,?)",
          (machine_id, db.now(), "test", 1 if out["ok"] else 0, latency,
           "; ".join(f"{s['step']}={'ok' if s['ok'] else 'FAIL'}" for s in out["steps"])))
    return out


def ingest_push(machine_id, rows):
    """Accept production rows pushed by a machine that we cannot poll (HTTP transport)."""
    drift = (dt.datetime.now() + dt.timedelta(days=db.get_int("clock_drift_days", 2))
             ).strftime("%Y-%m-%d %H:%M:%S")
    stamp = db.now()
    batch = []
    for r in rows:
        init = norm_ts(r.get("init_date"))
        if not init:
            continue
        end = norm_ts(r.get("end_date")) or None
        elapsed = db._elapsed(init, end)
        laser_ms = r.get("laser_time") or r.get("laser_ms") or 0
        duty = round((laser_ms / 1000.0) / elapsed, 4) if elapsed and elapsed > 0 else None
        shift, pdate = db.shift_of(init)
        style, size, color = db.parse_design(r.get("design"))
        batch.append((machine_id, int(r["id"]), init, end, r.get("design"), r.get("copies"),
                      r.get("units"), laser_ms, elapsed, duty, r.get("user"), r.get("alarm1") or 0,
                      r.get("alarm2") or 0, r.get("optimizer"), r.get("temp_maxima_galvo_X"),
                      r.get("temp_maxima_galvo_Y"), r.get("temp_maxima_servo_X"),
                      r.get("temp_maxima_servo_Y"), r.get("average_design_time"),
                      str(r.get("eMarkVersion")) if r.get("eMarkVersion") else None,
                      style, size, color, shift, pdate, 1 if init > drift else 0, stamp))
    n = db._insert_production(batch)
    db.ex("UPDATE machine_state SET last_sync=?, last_sync_ok=?, updated_at=? WHERE machine_id=?",
          (stamp, stamp, stamp, machine_id))
    db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,rows_new,detail) VALUES(?,?,?,1,?,'http_push')",
          (machine_id, stamp, "sync", len(batch)))
    evaluate_state(machine_id)
    return {"received": len(batch), "stored": n}


def prune_logs():
    days = db.get_int("connection_log_retention_days", 30)
    cut = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    db.ex("DELETE FROM connection_log WHERE ts < ?", (cut,))
    db.ex("DELETE FROM sync_log WHERE ts < ?", (cut,))
