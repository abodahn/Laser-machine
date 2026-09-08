"""Rollups, OEE, machine health, baselines, forecasting and production intelligence.

Everything here is derived from stored data; nothing talks to a machine.
Methods are deliberately explainable (moving averages, percentiles, z-scores) so
a production manager can argue with the number instead of trusting a black box.
"""
import datetime as dt
import math
import statistics

from . import db

DAY = 86400.0


def _d(x, default=None):
    return x if x is not None else default


def parse_weights():
    out = {}
    for part in (db.get_setting("health_weights") or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out or {"connectivity": 20, "alarms": 20, "downtime": 20,
                   "utilization": 15, "performance": 15, "thermal": 10}


# ------------------------------------------------------------- design stats

def rebuild_design_stats(days=120, min_jobs=5):
    """Learn the achievable cycle time per machine+design (p10 seconds per unit)."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = db.q("""SELECT machine_id, design, elapsed_seconds, units, init_date
                   FROM production
                   WHERE production_date >= ? AND suspect=0 AND units>0
                     AND elapsed_seconds IS NOT NULL AND elapsed_seconds>0 AND design IS NOT NULL""",
                (since,))
    buckets = {}
    for r in rows:
        key = (r["machine_id"], r["design"])
        b = buckets.setdefault(key, {"v": [], "u": 0, "last": ""})
        b["v"].append(r["elapsed_seconds"] / r["units"])
        b["u"] += r["units"]
        b["last"] = max(b["last"], r["init_date"] or "")
    stamp = db.now()
    out = []
    for (mid, design), b in buckets.items():
        v = sorted(b["v"])
        if len(v) < min_jobs:
            continue
        p10 = v[max(0, int(len(v) * 0.10) - 1)]
        out.append((mid, design, len(v), b["u"], round(p10, 3),
                    round(statistics.median(v), 3), round(sum(v) / len(v), 3), b["last"], stamp))
    db.ex("DELETE FROM design_stats")
    db.exmany("INSERT INTO design_stats(machine_id,design,jobs,units,p10_sec_unit,median_sec_unit,"
              "mean_sec_unit,last_seen,calculated_at) VALUES(?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def ideal_seconds_map():
    """machine_id -> {design: ideal seconds per unit}, plus a per-design cross-machine fallback."""
    per_machine, cross = {}, {}
    for r in db.q("SELECT machine_id,design,p10_sec_unit,jobs FROM design_stats"):
        per_machine.setdefault(r["machine_id"], {})[r["design"]] = r["p10_sec_unit"]
        cross.setdefault(r["design"], []).append(r["p10_sec_unit"])
    cross = {k: min(v) for k, v in cross.items()}
    return per_machine, cross


def handling_floor(days=120, min_jobs=20):
    """Best achievable non-laser time per unit, per machine.

    A job's wall time is laser-on time plus garment handling (load, position,
    unload). Laser time is dictated by the design and is not recoverable, so the
    honest performance reference is the machine's own best handling time:

        ideal_job_seconds = laser_seconds + handling_p10 * units

    Anything above that is handling loss, which is what a supervisor can act on.
    """
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = db.q("""SELECT machine_id, (elapsed_seconds - laser_ms/1000.0)/units AS h
                   FROM production
                   WHERE production_date >= ? AND suspect=0 AND units>0
                     AND elapsed_seconds IS NOT NULL AND elapsed_seconds > laser_ms/1000.0
                     AND elapsed_seconds < 7200""", (since,))
    buckets = {}
    for r in rows:
        if r["h"] is not None and 0 < r["h"] < 3600:
            buckets.setdefault(r["machine_id"], []).append(r["h"])
    out = {}
    for mid, v in buckets.items():
        if len(v) < min_jobs:
            continue
        v.sort()
        out[mid] = v[max(0, int(len(v) * 0.10) - 1)]
    return out


# ------------------------------------------------------------------ rollups

def busy_by_hour(since, machine_id=None):
    """Seconds each machine was actually running, per clock hour.

    Three things the naive SUM(elapsed) grouped by start-hour gets wrong:
      * a job spanning several hours belongs to all of them, not just the first
        (32% of jobs cross an hour boundary here);
      * two overlapping job records must not be counted twice;
      * a job left open overnight reports a huge elapsed with almost no laser
        time -- the machine was idle with a job open, not producing. A job is
        therefore credited with at most the run time its own laser time implies
        at `min_plausible_duty`, so normal jobs (duty ~0.57) are untouched while
        a 16 h job at 5% duty stops claiming 16 h of utilization.

    Jobs still RUNNING are included, counted from their start up to now. The
    machine writes end_date only when a job finishes, so excluding them made
    today's utilization read low all day and then jump -- and it was inconsistent,
    because an open job's laser time and units already counted while its run time
    did not. An open job is credited only up to `open_job_max_hours`; past that it
    is a stale record, not production (same rule the live state engine uses).
    """
    min_duty = db.get_float("min_plausible_duty", 0.20)
    stale_h = db.get_float("open_job_max_hours", 12)
    now = dt.datetime.now()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    rows = db.q(f"""SELECT machine_id, init_date, end_date, COALESCE(laser_ms,0) laser_ms
                    FROM production
                    WHERE production_date >= ? AND suspect=0 {where}""", args)
    per = {}
    for r in rows:
        try:
            a = dt.datetime.strptime(r["init_date"][:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        closed = bool(r["end_date"])
        if closed:
            try:
                b = dt.datetime.strptime(r["end_date"][:19], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            # a finished job's run time is bounded by what its laser time implies
            if min_duty > 0 and r["laser_ms"]:
                cap = (r["laser_ms"] / 1000.0) / min_duty
                if (b - a).total_seconds() > cap:
                    b = a + dt.timedelta(seconds=cap)
        else:
            # No end_date means one of two very different things. If the job began
            # recently it is running right now, and its time counts up to now. If it
            # began long ago it is an abandoned record whose real duration is
            # unknowable -- crediting it would invent run time in the history, so it
            # contributes nothing. No duty cap while running: laser_ms is partial.
            if (now - a).total_seconds() > stale_h * 3600:
                continue
            b = now
        if b <= a:
            continue
        per.setdefault(r["machine_id"], []).append((a, b, (r["laser_ms"] or 0) / 1000.0))

    busy, laser = {}, {}
    for mid, ivs in per.items():
        # Laser time is spread pro-rata over the hours the job actually spanned,
        # so laser and run time always land in the same buckets. Filing a job's
        # laser under its start hour while its run time spread across later hours
        # is what made a cross-midnight job report >100% duty on the first day.
        for a, b, ls in ivs:
            span = (b - a).total_seconds()
            if span <= 0:
                continue
            cur = a
            while cur < b:
                hs = cur.replace(minute=0, second=0, microsecond=0)
                seg_end = min(b, hs + dt.timedelta(hours=1))
                seg = (seg_end - cur).total_seconds()
                key = (mid, hs.strftime("%Y-%m-%d %H:00"))
                laser[key] = laser.get(key, 0.0) + ls * (seg / span)
                cur = seg_end
        # Run time uses the union of intervals, so two overlapping job records
        # can never bill the same second twice.
        ivs.sort()
        merged = []
        for a, b, _ls in ivs:
            if merged and a <= merged[-1][1]:
                if b > merged[-1][1]:
                    merged[-1][1] = b
            else:
                merged.append([a, b])
        for a, b in merged:
            cur = a
            while cur < b:
                hs = cur.replace(minute=0, second=0, microsecond=0)
                seg_end = min(b, hs + dt.timedelta(hours=1))
                key = (mid, hs.strftime("%Y-%m-%d %H:00"))
                busy[key] = busy.get(key, 0.0) + (seg_end - cur).total_seconds()
                cur = seg_end
    return busy, laser


def rebuild_hourly(days=7, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    rows = db.q(f"""
        SELECT machine_id, substr(init_date,1,13)||':00' AS hour,
               MIN(production_date) production_date, MIN(shift) shift,
               COUNT(*) jobs, SUM(units) units, SUM(copies) copies,
               SUM(COALESCE(laser_ms,0))/1000.0 laser_seconds,
               SUM(COALESCE(elapsed_seconds,0)) busy_seconds,
               SUM(CASE WHEN COALESCE(alarm1,0)+COALESCE(alarm2,0)>0 THEN 1 ELSE 0 END) job_alarms,
               MAX(MAX(COALESCE(temp_galvo_x,0),COALESCE(temp_galvo_y,0))) tg,
               MAX(MAX(COALESCE(temp_servo_x,0),COALESCE(temp_servo_y,0))) ts,
               COUNT(DISTINCT design) designs,
               GROUP_CONCAT(DISTINCT operator) operators
        FROM production
        WHERE production_date >= ? AND suspect=0 {where}
        GROUP BY machine_id, hour""", args)
    alarms = {}
    for r in db.q(f"SELECT machine_id, substr(ts,1,13)||':00' h, COUNT(*) n FROM machine_alarms "
                  f"WHERE production_date >= ? AND suspect=0 AND severity IN ('warning','critical') {where} "
                  f"GROUP BY machine_id,h", args):
        alarms[(r["machine_id"], r["h"])] = r["n"]
    downs = {}
    for r in db.q(f"SELECT machine_id, substr(start_time,1,13)||':00' h, "
                  f"SUM(COALESCE(duration_seconds,0)) s FROM downtime "
                  f"WHERE production_date >= ? AND kind IN ('stopped','offline','alarm') {where} "
                  f"GROUP BY machine_id,h", args):
        downs[(r["machine_id"], r["h"])] = r["s"]
    stamp = db.now()
    busy_map, laser_map = busy_by_hour(since, machine_id)
    # a job spanning hours contributes to hours it started in AND later ones, so
    # emit a row for every hour that has either production or running time
    keys = {(r["machine_id"], r["hour"]) for r in rows} | set(busy_map)
    by_key = {(r["machine_id"], r["hour"]): r for r in rows}
    out = []
    for key in sorted(keys):
        mid, hour = key
        r = by_key.get(key)
        busy = min(busy_map.get(key, 0.0), 3600.0)
        # laser-on can never exceed the run time it happened inside
        lsec = min(laser_map.get(key, 0.0), busy)
        down = min(downs.get(key, 0) or 0, 3600.0)
        idle = max(0.0, 3600.0 - busy - down)
        if r is None:                       # running time only, no job started this hour
            shift, pdate = db.shift_of(hour[:13] + ":00:00")
            out.append((mid, hour, pdate, shift, 0, 0, 0, round(lsec, 1), round(busy, 1),
                        round(idle, 1), round(down, 1), alarms.get(key, 0), 0, None, None,
                        0, None, stamp))
            continue
        out.append((r["machine_id"], r["hour"], r["production_date"], r["shift"], r["jobs"],
                    r["units"] or 0, r["copies"] or 0, round(lsec, 1),
                    round(busy, 1), round(idle, 1), round(down, 1),
                    alarms.get(key, 0), r["job_alarms"], r["tg"], r["ts"], r["designs"],
                    r["operators"], stamp))
    db.exmany("""INSERT INTO machine_hourly(machine_id,hour,production_date,shift,jobs,units,copies,
                 laser_seconds,busy_seconds,idle_seconds,down_seconds,alarm_count,job_alarms,
                 temp_galvo_max,temp_servo_max,designs,operators,calculated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(machine_id,hour) DO UPDATE SET jobs=excluded.jobs,units=excluded.units,
                 copies=excluded.copies,laser_seconds=excluded.laser_seconds,
                 busy_seconds=excluded.busy_seconds,idle_seconds=excluded.idle_seconds,
                 down_seconds=excluded.down_seconds,alarm_count=excluded.alarm_count,
                 job_alarms=excluded.job_alarms,temp_galvo_max=excluded.temp_galvo_max,
                 temp_servo_max=excluded.temp_servo_max,designs=excluded.designs,
                 operators=excluded.operators,calculated_at=excluded.calculated_at""", out)
    return len(out)


def rebuild_daily(days=7, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    where = "AND p.machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    planned_h = db.get_float("planned_hours_per_day", 24)
    breaks = db.get_int("planned_break_minutes", 15) * 3 * 60      # 3 shifts
    quality_on = db.get_setting("quality_proxy_enabled") == "1"
    targets = {r["id"]: (r["target_units_day"] or 0, r["ideal_seconds_unit"])
               for r in db.q("SELECT id,target_units_day,ideal_seconds_unit FROM machines")}

    agg = db.q(f"""SELECT p.machine_id, p.production_date,
                     COUNT(*) jobs, SUM(p.units) units, SUM(p.copies) copies,
                     SUM(COALESCE(p.laser_ms,0))/1000.0 laser_seconds,
                     SUM(CASE WHEN COALESCE(p.alarm1,0)+COALESCE(p.alarm2,0)>0 THEN 1 ELSE 0 END) jobs_alarm,
                     MAX(MAX(COALESCE(p.temp_galvo_x,0),COALESCE(p.temp_galvo_y,0))) tg,
                     MAX(MAX(COALESCE(p.temp_servo_x,0),COALESCE(p.temp_servo_y,0))) ts
                   FROM production p
                   WHERE p.production_date >= ? AND p.suspect=0 {where}
                   GROUP BY p.machine_id,p.production_date""", args)

    # Running time comes from the hour-clipped, overlap-merged rollup, never from a
    # raw SUM(elapsed) — see busy_by_hour() for why that number is not trustworthy.
    busy_day, laser_day = {}, {}
    for r in db.q(f"""SELECT machine_id, production_date, SUM(busy_seconds) b,
                             SUM(laser_seconds) l
                      FROM machine_hourly WHERE production_date >= ?
                      {where.replace('p.', '')} GROUP BY machine_id, production_date""", args):
        busy_day[(r["machine_id"], r["production_date"])] = r["b"] or 0.0
        laser_day[(r["machine_id"], r["production_date"])] = r["l"] or 0.0

    # Expected (ideal) run time per day = laser-on time + best handling time for the units made.
    # A machine-level `ideal_seconds_unit` override replaces the whole model when set.
    floors = handling_floor()
    expected = {}
    for r in db.q(f"""SELECT p.machine_id,p.production_date,SUM(p.units) u
                      FROM production p WHERE p.production_date >= ? AND p.suspect=0 AND p.units>0 {where}
                      GROUP BY p.machine_id,p.production_date""", args):
        k = (r["machine_id"], r["production_date"])
        override = targets.get(r["machine_id"], (0, None))[1]
        if override:
            expected[k] = override * r["u"]
        elif r["machine_id"] in floors:
            # laser term uses the day-attributed figure so it pairs with the same
            # run time the ratio divides by
            expected[k] = laser_day.get(k, 0.0) + floors[r["machine_id"]] * r["u"]

    alarm_cnt, down_sec, off_sec = {}, {}, {}
    for r in db.q(f"SELECT machine_id,production_date d,COUNT(*) n FROM machine_alarms "
                  f"WHERE production_date >= ? AND suspect=0 AND severity IN ('warning','critical') "
                  f"{where.replace('p.', '')} GROUP BY machine_id,d", args):
        alarm_cnt[(r["machine_id"], r["d"])] = r["n"]
    for r in db.q(f"SELECT machine_id,production_date d,kind,SUM(COALESCE(duration_seconds,0)) s "
                  f"FROM downtime WHERE production_date >= ? {where.replace('p.', '')} "
                  f"GROUP BY machine_id,d,kind", args):
        k = (r["machine_id"], r["d"])
        if r["kind"] in ("offline",):
            off_sec[k] = off_sec.get(k, 0) + r["s"]
        elif r["kind"] in ("stopped", "alarm", "maintenance"):
            down_sec[k] = down_sec.get(k, 0) + r["s"]

    planned = planned_h * 3600 - breaks
    stamp = db.now()
    out = []
    today = dt.date.today().isoformat()
    for r in agg:
        mid, d = r["machine_id"], r["production_date"]
        k = (mid, d)
        busy = min(busy_day.get(k, 0.0), planned)
        down = min(down_sec.get(k, 0) or 0, planned)
        off = min(off_sec.get(k, 0) or 0, planned)
        # today is partial: scale planned time to elapsed part of the day
        p = planned
        if d == today:
            p = max(3600.0, min(planned, (dt.datetime.now() -
                    dt.datetime.combine(dt.date.today(), dt.time(0, 0))).total_seconds()))
        # A machine cannot have been unreachable during time it was demonstrably
        # producing. Overlapping offline spans (a heartbeat gap while jobs were
        # still being written) used to eat the whole day, collapsing avail_base to
        # one second and reporting availability of 8,370,000%.
        off = min(off, max(0.0, p - busy))
        down = min(down, max(0.0, p - busy - off))
        idle = max(0.0, p - busy - down - off)
        exp = expected.get(k)
        # availability excludes time the machine was unreachable or under maintenance
        avail_base = max(1.0, p - off)
        availability = min(1.0, busy / avail_base)
        # unfinished jobs contribute laser time but no elapsed time; clamp the residual noise
        performance = min(exp / busy, 2.0) if (exp and busy > 60) else None
        jobs = r["jobs"] or 0
        quality = (1 - (r["jobs_alarm"] or 0) / jobs) if (quality_on and jobs) else None
        oee = None
        if availability is not None and performance is not None:
            # Performance above 100% means the ideal-time model was pessimistic for
            # that day's job mix, not that the machine beat physics. Keep the raw
            # figure visible as a diagnostic, but OEE is bounded by definition.
            oee = availability * min(performance, 1.0) * (quality if quality is not None else 1.0)
        target = targets.get(mid, (0, None))[0] or 0
        units = r["units"] or 0
        uph = units / (busy / 3600.0) if busy > 0 else 0
        lost = None
        if target:
            lost = max(0, target - units) if d != today else None
        # laser time comes from the same hour-clipped attribution as run time, so
        # duty is coherent and a cross-midnight job cannot exceed 100% on day one
        lsec = min(laser_day.get(k, 0.0), busy)
        out.append((mid, d, jobs, units, r["copies"] or 0, round(lsec, 1),
                    round(busy, 1), round(idle, 1), round(down, 1), round(off, 1), round(p, 1),
                    alarm_cnt.get(k, 0), r["jobs_alarm"] or 0,
                    _pct(availability), _pct(performance), _pct(quality), _pct(oee),
                    _pct(busy / p if p else None),
                    round(lsec / busy, 4) if busy > 0 else None,
                    round(uph, 2), target, round(units * 100.0 / target, 1) if target else None,
                    lost, None, r["tg"], r["ts"], stamp))
    db.exmany("""INSERT INTO machine_daily(machine_id,production_date,jobs,units,copies,laser_seconds,
                 busy_seconds,idle_seconds,down_seconds,offline_seconds,planned_seconds,alarm_count,
                 jobs_with_alarm,availability,performance,quality_proxy,oee,utilization,laser_duty,
                 units_per_hour,target_units,target_pct,lost_units,health_score,temp_galvo_max,
                 temp_servo_max,calculated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(machine_id,production_date) DO UPDATE SET jobs=excluded.jobs,
                 units=excluded.units,copies=excluded.copies,laser_seconds=excluded.laser_seconds,
                 busy_seconds=excluded.busy_seconds,idle_seconds=excluded.idle_seconds,
                 down_seconds=excluded.down_seconds,offline_seconds=excluded.offline_seconds,
                 planned_seconds=excluded.planned_seconds,alarm_count=excluded.alarm_count,
                 jobs_with_alarm=excluded.jobs_with_alarm,availability=excluded.availability,
                 performance=excluded.performance,quality_proxy=excluded.quality_proxy,oee=excluded.oee,
                 utilization=excluded.utilization,laser_duty=excluded.laser_duty,
                 units_per_hour=excluded.units_per_hour,target_units=excluded.target_units,
                 target_pct=excluded.target_pct,lost_units=excluded.lost_units,
                 temp_galvo_max=excluded.temp_galvo_max,temp_servo_max=excluded.temp_servo_max,
                 calculated_at=excluded.calculated_at""", out)
    compute_health(days=days, machine_id=machine_id)
    return len(out)


def _pct(x):
    return round(x * 100.0, 2) if x is not None else None


# ------------------------------------------------------------- health score

def compute_health(days=7, machine_id=None):
    """0-100 per machine per day. Each component is a 0-100 sub-score, then weighted."""
    w = parse_weights()
    total_w = sum(w.values()) or 1
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    conn_ok = {}
    for r in db.q(f"SELECT machine_id, substr(ts,1,10) d, AVG(ok)*100 pct FROM connection_log "
                  f"WHERE ts >= ? AND kind='heartbeat' {where} GROUP BY machine_id,d", args):
        conn_ok[(r["machine_id"], r["d"])] = r["pct"]
    rows = db.q(f"SELECT * FROM machine_daily WHERE production_date >= ? {where}", args)
    updates = []
    for r in rows:
        k = (r["machine_id"], r["production_date"])
        planned = r["planned_seconds"] or 1
        s_conn = conn_ok.get(k, 100.0 if (r["jobs"] or 0) > 0 else 50.0)
        alarms_per_h = (r["alarm_count"] or 0) / max(1.0, planned / 3600.0)
        s_alarm = max(0.0, 100.0 - alarms_per_h * 20.0)            # 5 alarms/h -> 0
        s_down = max(0.0, 100.0 - ((r["down_seconds"] or 0) + (r["offline_seconds"] or 0)) * 100.0 / planned * 2)
        s_util = min(100.0, (r["utilization"] or 0))
        s_perf = min(100.0, (r["performance"] or 0))
        tmax = max(r["temp_galvo_max"] or 0, r["temp_servo_max"] or 0)
        s_therm = 100.0 if tmax <= 0 else max(0.0, min(100.0, (60.0 - tmax) / 20.0 * 100.0))
        score = (w.get("connectivity", 0) * s_conn + w.get("alarms", 0) * s_alarm +
                 w.get("downtime", 0) * s_down + w.get("utilization", 0) * s_util +
                 w.get("performance", 0) * s_perf + w.get("thermal", 0) * s_therm) / total_w
        updates.append((round(score, 1), r["machine_id"], r["production_date"]))
    db.exmany("UPDATE machine_daily SET health_score=? WHERE machine_id=? AND production_date=?", updates)
    return len(updates)


def health_breakdown(machine_id, date=None):
    date = date or dt.date.today().isoformat()
    r = db.q1("SELECT * FROM machine_daily WHERE machine_id=? AND production_date=?", (machine_id, date))
    if not r:
        return None
    w = parse_weights()
    planned = r["planned_seconds"] or 1
    conn = db.q1("SELECT AVG(ok)*100 p FROM connection_log WHERE machine_id=? AND kind='heartbeat' "
                 "AND substr(ts,1,10)=?", (machine_id, date))
    alarms_per_h = (r["alarm_count"] or 0) / max(1.0, planned / 3600.0)
    tmax = max(r["temp_galvo_max"] or 0, r["temp_servo_max"] or 0)
    comps = {
        "connectivity": round(_d(conn["p"] if conn else None, 50.0), 1),
        "alarms": round(max(0.0, 100 - alarms_per_h * 20), 1),
        "downtime": round(max(0.0, 100 - ((r["down_seconds"] or 0) + (r["offline_seconds"] or 0)) * 200.0 / planned), 1),
        "utilization": round(min(100.0, r["utilization"] or 0), 1),
        "performance": round(min(100.0, r["performance"] or 0), 1),
        "thermal": round(100.0 if tmax <= 0 else max(0.0, min(100.0, (60 - tmax) / 20 * 100)), 1),
    }
    return {"score": r["health_score"], "components": comps, "weights": w, "date": date}


# ---------------------------------------------------------------- baselines

def rebuild_baselines(weeks=8, machine_id=None):
    """Normal output per machine per hour-of-week — the reference for anomaly detection."""
    since = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    rows = db.q(f"""SELECT machine_id, hour, units, busy_seconds FROM machine_hourly
                    WHERE production_date >= ? {where}""", args)
    buckets = {}
    for r in rows:
        try:
            h = dt.datetime.strptime(r["hour"][:13], "%Y-%m-%d %H")
        except ValueError:
            continue
        how = ((h.weekday() + 1) % 7) * 24 + h.hour           # Sunday = 0
        b = buckets.setdefault((r["machine_id"], how), {"u": [], "b": []})
        b["u"].append(r["units"] or 0)
        b["b"].append(r["busy_seconds"] or 0)
    stamp = db.now()
    out = []
    for (mid, how), b in buckets.items():
        if len(b["u"]) < 3:
            continue
        out.append((mid, how, round(statistics.fmean(b["u"]), 2),
                    round(statistics.pstdev(b["u"]), 2) if len(b["u"]) > 1 else 0.0,
                    round(statistics.fmean(b["b"]), 1),
                    round(statistics.pstdev(b["b"]), 1) if len(b["b"]) > 1 else 0.0,
                    len(b["u"]), stamp))
    db.exmany("""INSERT INTO machine_baseline(machine_id,hour_of_week,units_mean,units_std,busy_mean,
                 busy_std,samples,calculated_at) VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(machine_id,hour_of_week) DO UPDATE SET units_mean=excluded.units_mean,
                 units_std=excluded.units_std,busy_mean=excluded.busy_mean,busy_std=excluded.busy_std,
                 samples=excluded.samples,calculated_at=excluded.calculated_at""", out)
    return len(out)


def hour_of_week(when=None):
    when = when or dt.datetime.now()
    return ((when.weekday() + 1) % 7) * 24 + when.hour


def deviation(machine_id, when=None):
    """How far the last complete hour deviates from this machine's own normal."""
    when = when or dt.datetime.now()
    prev = (when - dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    hour = prev.strftime("%Y-%m-%d %H:00")
    base = db.q1("SELECT * FROM machine_baseline WHERE machine_id=? AND hour_of_week=?",
                 (machine_id, hour_of_week(prev)))
    act = db.q1("SELECT units FROM machine_hourly WHERE machine_id=? AND hour=?", (machine_id, hour))
    if not base or base["samples"] < 3:
        return None
    units = act["units"] if act else 0
    std = base["units_std"] or 0
    z = (units - base["units_mean"]) / std if std > 0.5 else 0.0
    drop = (1 - units / base["units_mean"]) * 100 if base["units_mean"] > 0 else 0
    return {"hour": hour, "units": units, "expected": round(base["units_mean"], 1),
            "z": round(z, 2), "drop_pct": round(drop, 1), "samples": base["samples"]}


# --------------------------------------------------------------- forecasting

def forecast_machine(machine_id, date=None):
    """End-of-day production forecast from today's rate blended with this machine's baseline."""
    date = date or dt.date.today().isoformat()
    now = dt.datetime.now()
    today = db.q1("SELECT COALESCE(SUM(units),0) u, COALESCE(SUM(busy_seconds),0) b "
                  "FROM machine_hourly WHERE machine_id=? AND production_date=?", (machine_id, date))
    units_so_far = today["u"] if today else 0
    hours_elapsed = max(0.5, (now - dt.datetime.combine(dt.date.fromisoformat(date), dt.time(0, 0))
                              ).total_seconds() / 3600.0)
    hours_left = max(0.0, 24 - hours_elapsed)
    recent = db.q1("""SELECT AVG(units) r FROM (SELECT units FROM machine_hourly
                      WHERE machine_id=? ORDER BY hour DESC LIMIT 3)""", (machine_id,))
    recent_rate = recent["r"] if recent and recent["r"] is not None else 0.0
    base_left = 0.0
    have_base = False
    for h in range(int(hours_elapsed), 24):
        b = db.q1("SELECT units_mean FROM machine_baseline WHERE machine_id=? AND hour_of_week=?",
                  (machine_id, ((dt.date.fromisoformat(date).weekday() + 1) % 7) * 24 + h))
        if b and b["units_mean"] is not None:
            base_left += b["units_mean"]
            have_base = True
    projected = units_so_far + (0.5 * recent_rate * hours_left + 0.5 * base_left
                                if have_base else recent_rate * hours_left)
    spread = 0.18 * max(projected, 1)
    target = (db.q1("SELECT target_units_day t FROM machines WHERE id=?", (machine_id,)) or {})["t"] or 0
    return {"machine_id": machine_id, "date": date, "units_so_far": units_so_far,
            "projected_units": round(projected), "low": round(max(units_so_far, projected - spread)),
            "high": round(projected + spread), "hours_left": round(hours_left, 1),
            "recent_rate": round(recent_rate, 1), "target": target,
            "target_pct": round(projected * 100.0 / target, 1) if target else None,
            "method": "recent-rate + hour-of-week baseline" if have_base else "recent-rate"}


def forecast_downtime(machine_id, days=14):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    r = db.q1("SELECT AVG(down_seconds+offline_seconds) s, AVG(alarm_count) a "
              "FROM machine_daily WHERE machine_id=? AND production_date >= ?", (machine_id, since))
    return {"expected_downtime_minutes": round((r["s"] or 0) / 60.0, 1) if r else 0,
            "expected_alarms": round(r["a"] or 0, 1) if r else 0, "window_days": days}


def trend(values):
    """Least-squares slope per step; None when there is not enough signal."""
    v = [x for x in values if x is not None]
    if len(v) < 4:
        return None
    n = len(v)
    mx = (n - 1) / 2.0
    my = sum(v) / n
    den = sum((i - mx) ** 2 for i in range(n))
    if den == 0:
        return None
    return sum((i - mx) * (v[i] - my) for i in range(n)) / den


def forecast_health(machine_id, days=14):
    rows = db.q("SELECT health_score FROM machine_daily WHERE machine_id=? AND production_date >= ? "
                "ORDER BY production_date",
                (machine_id, (dt.date.today() - dt.timedelta(days=days)).isoformat()))
    vals = [r["health_score"] for r in rows if r["health_score"] is not None]
    sl = trend(vals)
    cur = vals[-1] if vals else None
    return {"current": cur, "slope_per_day": round(sl, 2) if sl is not None else None,
            "in_7_days": round(max(0, min(100, cur + sl * 7)), 1) if (cur is not None and sl is not None) else None,
            "samples": len(vals)}


def maintenance_risk(machine_id):
    """Blend alarm pressure, thermal load and scan-head calibration drift into one risk view."""
    since14 = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    since60 = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    a14 = db.q1("SELECT COUNT(*) n FROM machine_alarms WHERE machine_id=? AND production_date>=? "
                "AND severity='critical'", (machine_id, since14))["n"]
    aprev = db.q1("SELECT COUNT(*) n FROM machine_alarms WHERE machine_id=? AND production_date>=? "
                  "AND production_date<? AND severity='critical'", (machine_id, since60, since14))["n"]
    rate_now = a14 / 14.0
    rate_prev = aprev / 46.0 if aprev else 0.0
    temps = db.q("SELECT temp_galvo_max, temp_servo_max FROM machine_daily WHERE machine_id=? "
                 "AND production_date>=? ORDER BY production_date", (machine_id, since60))
    t_slope = trend([max(t["temp_galvo_max"] or 0, t["temp_servo_max"] or 0) for t in temps])
    hcm = db.q("SELECT slope_x,slope_y,hyst_pos0_x,hyst_pos0_y FROM head_health WHERE machine_id=? "
               "ORDER BY id DESC LIMIT 40", (machine_id,))
    drift = None
    if len(hcm) >= 10:
        recent = [h["slope_x"] for h in hcm[:5] if h["slope_x"] is not None]
        older = [h["slope_x"] for h in hcm[-10:] if h["slope_x"] is not None]
        if recent and older and statistics.fmean(older):
            drift = round((statistics.fmean(recent) / statistics.fmean(older) - 1) * 100, 1)
    score = 0
    reasons = []
    if rate_prev and rate_now > rate_prev * 1.5:
        score += 35
        reasons.append(f"critical alarms up {round((rate_now / rate_prev - 1) * 100)}% vs the last 6 weeks")
    elif rate_now > 1:
        score += 20
        reasons.append(f"{round(rate_now, 1)} critical alarms per day")
    if t_slope and t_slope > 0.15:
        score += 25
        reasons.append(f"peak temperature trending up {round(t_slope, 2)} degC/day")
    if drift is not None and abs(drift) > 20:
        score += 25
        reasons.append(f"scan-head gain drifted {drift}%")
    hf = forecast_health(machine_id)
    if hf["slope_per_day"] is not None and hf["slope_per_day"] < -1:
        score += 15
        reasons.append(f"health score falling {abs(hf['slope_per_day'])} points/day")
    return {"machine_id": machine_id, "risk": min(100, score),
            "level": "high" if score >= 60 else "medium" if score >= 30 else "low",
            "reasons": reasons, "critical_alarms_14d": a14, "head_drift_pct": drift,
            "temp_slope": round(t_slope, 3) if t_slope else None, "health": hf}


def store_forecasts():
    stamp = db.now()
    today = dt.date.today().isoformat()
    db.ex("DELETE FROM forecast WHERE target_date=? AND horizon='eod'", (today,))
    rows = []
    dept = 0
    for m in db.q("SELECT id FROM machines WHERE enabled=1 AND status='active'"):
        f = forecast_machine(m["id"])
        dept += f["projected_units"]
        rows.append((m["id"], "units", "eod", today, f["projected_units"], f["low"], f["high"],
                     0.7, f["method"], stamp))
        d = forecast_downtime(m["id"])
        rows.append((m["id"], "downtime", "day+1", today, d["expected_downtime_minutes"],
                     None, None, 0.6, "14-day mean", stamp))
    rows.append((None, "units", "eod", today, dept, None, None, 0.7, "sum of machine forecasts", stamp))
    db.exmany("INSERT INTO forecast(machine_id,metric,horizon,target_date,value,low,high,confidence,"
              "method,calculated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ------------------------------------------------- production intelligence --

def department_overview(date=None):
    date = date or dt.date.today().isoformat()
    states = db.q("""SELECT m.id,m.name,m.model,m.production_area,m.location,m.status cfg_status,
                            m.target_units_day, s.*
                     FROM machines m LEFT JOIN machine_state s ON s.machine_id=m.id
                     WHERE m.enabled=1 ORDER BY m.id""")
    daily = {r["machine_id"]: dict(r) for r in
             db.q("SELECT * FROM machine_daily WHERE production_date=?", (date,))}
    since = (dt.date.fromisoformat(date) - dt.timedelta(days=13)).isoformat()
    spark = {}
    for r in db.q("SELECT machine_id, production_date, units FROM machine_daily "
                  "WHERE production_date BETWEEN ? AND ? ORDER BY production_date", (since, date)):
        spark.setdefault(r["machine_id"], {})[r["production_date"]] = r["units"] or 0
    dates14 = [(dt.date.fromisoformat(since) + dt.timedelta(days=i)).isoformat() for i in range(14)]
    out, counts = [], {}
    tot = {"units": 0, "jobs": 0, "busy": 0.0, "planned": 0.0, "down": 0.0, "idle": 0.0,
           "alarms": 0, "target": 0, "laser": 0.0}
    for s in states:
        d = daily.get(s["id"], {})
        st = s["status"] or "UNKNOWN"
        counts[st] = counts.get(st, 0) + 1
        tot["units"] += d.get("units") or 0
        tot["jobs"] += d.get("jobs") or 0
        tot["busy"] += d.get("busy_seconds") or 0
        tot["laser"] += d.get("laser_seconds") or 0
        tot["planned"] += d.get("planned_seconds") or 0
        tot["down"] += (d.get("down_seconds") or 0) + (d.get("offline_seconds") or 0)
        tot["idle"] += d.get("idle_seconds") or 0
        tot["alarms"] += d.get("alarm_count") or 0
        tot["target"] += s["target_units_day"] or 0
        out.append({
            "id": s["id"], "name": s["name"], "model": s["model"], "area": s["production_area"],
            "location": s["location"], "status": st, "connectivity": s["connectivity"],
            "online": s["online"], "last_heartbeat": s["last_heartbeat"], "latency_ms": s["latency_ms"],
            "last_sync": s["last_sync"], "last_error": s["last_error"],
            "current_design": s["current_design"], "current_job_start": s["current_job_start"],
            "idle_seconds": s["idle_seconds"], "status_since": s["status_since"],
            "units": d.get("units") or 0, "jobs": d.get("jobs") or 0,
            "target": s["target_units_day"] or 0, "target_pct": d.get("target_pct"),
            "utilization": d.get("utilization"), "availability": d.get("availability"),
            "performance": d.get("performance"), "oee": d.get("oee"),
            "health": d.get("health_score"), "alarms": d.get("alarm_count") or 0,
            "down_minutes": round(((d.get("down_seconds") or 0) + (d.get("offline_seconds") or 0)) / 60, 1),
            "idle_minutes": round((d.get("idle_seconds") or 0) / 60, 1),
            "laser_duty": d.get("laser_duty"), "units_per_hour": d.get("units_per_hour"),
            "spark": [spark.get(s["id"], {}).get(dd, 0) for dd in dates14],
        })
    util = tot["busy"] / tot["planned"] * 100 if tot["planned"] else 0
    open_alerts = db.q1("SELECT COUNT(*) n, SUM(severity='critical') c FROM alerts WHERE status='open'")
    fc = db.q1("SELECT value FROM forecast WHERE machine_id IS NULL AND metric='units' AND horizon='eod' "
               "AND target_date=? ORDER BY id DESC LIMIT 1", (date,))
    return {
        "date": date, "machines": out, "status_counts": counts,
        "totals": {"units": tot["units"], "jobs": tot["jobs"],
                   "utilization": round(util, 1),
                   "laser_duty": round(tot["laser"] / tot["busy"] * 100, 1) if tot["busy"] else 0,
                   "down_hours": round(tot["down"] / 3600, 1),
                   "idle_hours": round(tot["idle"] / 3600, 1),
                   "alarms": tot["alarms"], "target": tot["target"],
                   "target_pct": round(tot["units"] * 100.0 / tot["target"], 1) if tot["target"] else None,
                   "forecast_units": fc["value"] if fc else None,
                   "machines_total": len(out),
                   "machines_online": sum(1 for m in out if m["online"]),
                   "machines_producing": counts.get("PRODUCING", 0),
                   "open_alerts": open_alerts["n"] if open_alerts else 0,
                   "critical_alerts": (open_alerts["c"] or 0) if open_alerts else 0},
    }


def ranking(days=7, metric="units"):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = db.q("""SELECT d.machine_id, m.name, SUM(d.units) units, SUM(d.jobs) jobs,
                          AVG(d.utilization) utilization, AVG(d.performance) performance,
                          AVG(d.oee) oee, AVG(d.health_score) health,
                          SUM(d.down_seconds+d.offline_seconds)/60.0 down_minutes,
                          SUM(d.idle_seconds)/60.0 idle_minutes, SUM(d.alarm_count) alarms,
                          AVG(d.units_per_hour) uph, AVG(d.laser_duty)*100 laser_duty
                   FROM machine_daily d JOIN machines m ON m.id=d.machine_id
                   WHERE d.production_date >= ? GROUP BY d.machine_id ORDER BY units DESC""", (since,))
    data = [dict(r) for r in rows]
    key = {"units": "units", "utilization": "utilization", "oee": "oee",
           "downtime": "down_minutes", "health": "health", "alarms": "alarms"}.get(metric, "units")
    data.sort(key=lambda r: (r.get(key) is None, r.get(key) or 0),
              reverse=key not in ("down_minutes", "alarms"))
    return {"days": days, "metric": metric, "rows": data}


def bottleneck(days=7):
    """Where the department loses the most capacity, ranked by lost machine-hours."""
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = db.q("""SELECT d.machine_id, m.name,
                          SUM(d.planned_seconds - d.busy_seconds)/3600.0 lost_hours,
                          SUM(d.down_seconds+d.offline_seconds)/3600.0 down_hours,
                          SUM(d.idle_seconds)/3600.0 idle_hours,
                          AVG(d.utilization) utilization, SUM(d.units) units,
                          AVG(d.units_per_hour) uph, SUM(d.alarm_count) alarms
                   FROM machine_daily d JOIN machines m ON m.id=d.machine_id
                   WHERE d.production_date >= ? GROUP BY d.machine_id
                   ORDER BY lost_hours DESC""", (since,))
    out = []
    for r in rows:
        lost_units = (r["lost_hours"] or 0) * (r["uph"] or 0)
        out.append({**dict(r), "lost_units_estimate": round(lost_units)})
    return {"days": days, "rows": out,
            "total_lost_hours": round(sum(r["lost_hours"] or 0 for r in rows), 1),
            "total_lost_units": round(sum(o["lost_units_estimate"] for o in out))}


def shift_performance(days=7, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    return [dict(r) for r in db.q(
        f"""SELECT shift, production_date, SUM(units) units, SUM(jobs) jobs,
                   SUM(busy_seconds)/3600.0 busy_hours, SUM(idle_seconds)/3600.0 idle_hours,
                   SUM(down_seconds)/3600.0 down_hours, SUM(alarm_count) alarms
            FROM machine_hourly WHERE production_date >= ? {where}
            GROUP BY shift, production_date ORDER BY production_date, shift""", args)]


def downtime_analysis(days=30, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    by_kind = [dict(r) for r in db.q(
        f"""SELECT kind, COUNT(*) events, SUM(COALESCE(duration_seconds,0))/3600.0 hours,
                   AVG(COALESCE(duration_seconds,0))/60.0 avg_minutes,
                   MAX(COALESCE(duration_seconds,0))/60.0 max_minutes
            FROM downtime WHERE production_date >= ? {where} GROUP BY kind ORDER BY hours DESC""", args)]
    by_machine = [dict(r) for r in db.q(
        f"""SELECT d.machine_id, m.name, COUNT(*) events,
                   SUM(COALESCE(d.duration_seconds,0))/3600.0 hours,
                   AVG(COALESCE(d.duration_seconds,0))/60.0 avg_minutes
            FROM downtime d JOIN machines m ON m.id=d.machine_id
            WHERE d.production_date >= ? {where.replace('machine_id', 'd.machine_id')}
            GROUP BY d.machine_id ORDER BY hours DESC""", args)]
    longest = [dict(r) for r in db.q(
        f"""SELECT d.machine_id, m.name, d.start_time, d.end_time, d.kind,
                   COALESCE(d.duration_seconds,0)/60.0 minutes, d.reason
            FROM downtime d JOIN machines m ON m.id=d.machine_id
            WHERE d.production_date >= ? {where.replace('machine_id', 'd.machine_id')}
            ORDER BY COALESCE(d.duration_seconds,0) DESC LIMIT 20""", args)]
    return {"days": days, "by_kind": by_kind, "by_machine": by_machine, "longest": longest}


def alarm_analysis(days=30, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    top = [dict(r) for r in db.q(
        f"""SELECT description, category, severity, error_code, COUNT(*) n,
                   COUNT(DISTINCT machine_id) machines, MAX(ts) last_seen
            FROM machine_alarms WHERE production_date >= ? AND suspect=0 {where}
            GROUP BY description, category, severity, error_code ORDER BY n DESC LIMIT 25""", args)]
    by_machine = [dict(r) for r in db.q(
        f"""SELECT a.machine_id, m.name, COUNT(*) n,
                   SUM(a.severity='critical') critical, SUM(a.severity='warning') warning
            FROM machine_alarms a JOIN machines m ON m.id=a.machine_id
            WHERE a.production_date >= ? AND a.suspect=0 {where.replace('machine_id', 'a.machine_id')}
            GROUP BY a.machine_id ORDER BY n DESC""", args)]
    daily = [dict(r) for r in db.q(
        f"""SELECT production_date d, COUNT(*) n, SUM(severity='critical') critical
            FROM machine_alarms WHERE production_date >= ? AND suspect=0 {where}
            GROUP BY d ORDER BY d""", args)]
    return {"days": days, "top": top, "by_machine": by_machine, "daily": daily}


def machine_history(machine_id, start, end, bucket="day"):
    if bucket == "hour":
        rows = db.q("""SELECT hour AS bucket, units, jobs, busy_seconds, idle_seconds, down_seconds,
                              alarm_count, laser_seconds, temp_galvo_max, temp_servo_max
                       FROM machine_hourly WHERE machine_id=? AND hour >= ? AND hour <= ?
                       ORDER BY hour""", (machine_id, start + " 00:00", end + " 23:59"))
    elif bucket in ("week", "month"):
        fmt = "%Y-W%W" if bucket == "week" else "%Y-%m"
        rows = db.q(f"""SELECT strftime('{fmt}', production_date) bucket, SUM(units) units,
                               SUM(jobs) jobs, SUM(busy_seconds) busy_seconds,
                               SUM(idle_seconds) idle_seconds, SUM(down_seconds+offline_seconds) down_seconds,
                               SUM(alarm_count) alarm_count, SUM(laser_seconds) laser_seconds,
                               AVG(utilization) utilization, AVG(oee) oee, AVG(health_score) health_score
                        FROM machine_daily WHERE machine_id=? AND production_date BETWEEN ? AND ?
                        GROUP BY bucket ORDER BY bucket""", (machine_id, start, end))
    elif bucket == "shift":
        rows = db.q("""SELECT production_date||' '||COALESCE(shift,'-') bucket, SUM(units) units,
                              SUM(jobs) jobs, SUM(busy_seconds) busy_seconds, SUM(idle_seconds) idle_seconds,
                              SUM(down_seconds) down_seconds, SUM(alarm_count) alarm_count,
                              SUM(laser_seconds) laser_seconds
                       FROM machine_hourly WHERE machine_id=? AND production_date BETWEEN ? AND ?
                       GROUP BY bucket ORDER BY bucket""", (machine_id, start, end))
    else:
        rows = db.q("""SELECT production_date bucket, units, jobs, busy_seconds, idle_seconds,
                              down_seconds+offline_seconds down_seconds, alarm_count, laser_seconds,
                              utilization, availability, performance, oee, health_score,
                              units_per_hour, target_units, target_pct, temp_galvo_max, temp_servo_max
                       FROM machine_daily WHERE machine_id=? AND production_date BETWEEN ? AND ?
                       ORDER BY production_date""", (machine_id, start, end))
    return [dict(r) for r in rows]


def operator_performance(days=30, machine_id=None):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    return [dict(r) for r in db.q(
        f"""SELECT operator, COUNT(*) jobs, SUM(units) units,
                   SUM(elapsed_seconds)/3600.0 hours,
                   SUM(units)/NULLIF(SUM(elapsed_seconds)/3600.0,0) units_per_hour,
                   AVG(laser_duty)*100 laser_duty,
                   SUM(CASE WHEN alarm1+alarm2>0 THEN 1 ELSE 0 END) jobs_with_alarm,
                   COUNT(DISTINCT machine_id) machines
            FROM production WHERE production_date >= ? AND suspect=0 AND operator IS NOT NULL {where}
            GROUP BY operator ORDER BY units DESC""", args)]


def design_performance(days=30, limit=40):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return [dict(r) for r in db.q(
        """SELECT design, style, size, color, COUNT(*) jobs, SUM(units) units,
                  AVG(elapsed_seconds/NULLIF(units,0)) sec_per_unit,
                  MIN(elapsed_seconds/NULLIF(units,0)) best_sec_per_unit,
                  AVG(laser_duty)*100 laser_duty, COUNT(DISTINCT machine_id) machines,
                  SUM(elapsed_seconds)/3600.0 hours
           FROM production WHERE production_date >= ? AND suspect=0 AND units>0
           GROUP BY design ORDER BY units DESC LIMIT ?""", (since, limit))]


def capacity(days=7):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    r = db.q1("""SELECT SUM(planned_seconds)/3600.0 planned_h, SUM(busy_seconds)/3600.0 busy_h,
                        SUM(idle_seconds)/3600.0 idle_h,
                        SUM(down_seconds+offline_seconds)/3600.0 down_h,
                        SUM(units) units, COUNT(DISTINCT machine_id) machines
                 FROM machine_daily WHERE production_date >= ?""", (since,))
    if not r or not r["planned_h"]:
        return {"days": days, "planned_hours": 0}
    uph = (r["units"] or 0) / r["busy_h"] if r["busy_h"] else 0
    return {"days": days, "machines": r["machines"], "planned_hours": round(r["planned_h"], 1),
            "busy_hours": round(r["busy_h"], 1), "idle_hours": round(r["idle_h"], 1),
            "down_hours": round(r["down_h"], 1),
            "utilization_pct": round(r["busy_h"] / r["planned_h"] * 100, 1),
            "units": r["units"] or 0, "units_per_machine_hour": round(uph, 1),
            "unused_capacity_units": round((r["idle_h"] + r["down_h"]) * uph),
            "capacity_units_at_100pct": round(r["planned_h"] * uph)}


def attention_list():
    """Which machines need attention right now, and why."""
    today = dt.date.today().isoformat()
    out = []
    for m in db.q("SELECT id,name FROM machines WHERE enabled=1 AND status='active'"):
        reasons, score = [], 0
        s = db.q1("SELECT * FROM machine_state WHERE machine_id=?", (m["id"],))
        d = db.q1("SELECT * FROM machine_daily WHERE machine_id=? AND production_date=?", (m["id"], today))
        if s:
            if s["status"] == "OFFLINE":
                reasons.append("offline")
                score += 45
            elif s["status"] == "STOPPED":
                reasons.append(f"stopped for {round((s['idle_seconds'] or 0) / 60)} min")
                score += 35
            elif s["status"] == "ALARM":
                reasons.append("alarm condition")
                score += 30
            elif s["status"] == "IDLE":
                reasons.append(f"idle {round((s['idle_seconds'] or 0) / 60)} min")
                score += 12
            if s["connectivity"] == "SLOW":
                reasons.append("slow link")
                score += 5
        if d:
            if d["health_score"] is not None and d["health_score"] < 60:
                reasons.append(f"health {round(d['health_score'])}")
                score += 20
            if (d["alarm_count"] or 0) >= 10:
                reasons.append(f"{d['alarm_count']} alarms today")
                score += 15
            if d["target_units"] and (d["target_pct"] or 0) < 70:
                reasons.append(f"{round(d['target_pct'] or 0)}% of target")
                score += 15
        mr = maintenance_risk(m["id"])
        if mr["level"] == "high":
            reasons.append("maintenance risk high")
            score += 25
        elif mr["level"] == "medium":
            score += 10
        if reasons:
            out.append({"machine_id": m["id"], "name": m["name"], "score": min(100, score),
                        "reasons": reasons, "status": s["status"] if s else None})
    out.sort(key=lambda r: -r["score"])
    return out


def loss_analysis(date=None):
    """What did today's production loss consist of?"""
    date = date or dt.date.today().isoformat()
    rows = db.q("""SELECT d.machine_id, m.name, d.planned_seconds, d.busy_seconds, d.idle_seconds,
                          d.down_seconds, d.offline_seconds, d.units, d.units_per_hour,
                          d.alarm_count, d.target_units
                   FROM machine_daily d JOIN machines m ON m.id=d.machine_id
                   WHERE d.production_date=?""", (date,))
    buckets = {"idle": 0.0, "stopped": 0.0, "offline": 0.0, "performance": 0.0}
    detail = []
    for r in rows:
        uph = r["units_per_hour"] or 0
        idle_u = (r["idle_seconds"] or 0) / 3600 * uph
        down_u = (r["down_seconds"] or 0) / 3600 * uph
        off_u = (r["offline_seconds"] or 0) / 3600 * uph
        buckets["idle"] += idle_u
        buckets["stopped"] += down_u
        buckets["offline"] += off_u
        detail.append({"machine_id": r["machine_id"], "name": r["name"], "units": r["units"],
                       "lost_idle": round(idle_u), "lost_stopped": round(down_u),
                       "lost_offline": round(off_u), "alarms": r["alarm_count"],
                       "target": r["target_units"]})
    detail.sort(key=lambda x: -(x["lost_idle"] + x["lost_stopped"] + x["lost_offline"]))
    return {"date": date, "buckets": {k: round(v) for k, v in buckets.items()},
            "total_lost_units": round(sum(buckets.values())), "by_machine": detail}


def production_heatmap(days=21, machine_id=None):
    """Units by weekday x hour, for a calendar-style heatmap. Sunday=0."""
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [since] + ([machine_id] if machine_id else [])
    grid = [[0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    for r in db.q(f"""SELECT hour, SUM(units) u FROM machine_hourly
                      WHERE production_date >= ? {where} GROUP BY hour""", args):
        try:
            h = dt.datetime.strptime(r["hour"][:13], "%Y-%m-%d %H")
        except (ValueError, TypeError):
            continue
        wd = (h.weekday() + 1) % 7
        grid[wd][h.hour] += r["u"] or 0
        counts[wd][h.hour] += 1
    # average units per occurrence of that weekday-hour, so a short window is not penalised
    avg = [[round(grid[d][h] / counts[d][h], 1) if counts[d][h] else 0 for h in range(24)] for d in range(7)]
    peak = max((max(row) for row in avg), default=0)
    return {"days": days, "grid": avg, "peak": peak,
            "weekdays": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]}


def refresh_all(days=3):
    """Cheap incremental refresh — safe to run every few minutes."""
    rebuild_hourly(days=days)
    rebuild_daily(days=days)
    store_forecasts()


def refresh_deep(days=400):
    """Full recompute, used after a backfill or a settings change."""
    rebuild_design_stats()
    rebuild_hourly(days=days)
    rebuild_daily(days=days)
    rebuild_baselines()
    store_forecasts()


# ------------------------------------------------------------ optics drift --

# markSpeed / jumpSpeed set the physical marking ceiling: a machine below the
# fleet value is permanently slower and no production metric explains why.
OPTICS_SPEED = ("markSpeed", "jumpSpeed")
OPTICS_POWER = ("frequency", "dutyCycle", "powerFactor", "minPulseWidthL1", "minPulseWidthL2")
OPTICS_TIMING = ("timeLag", "markDelay", "jumpDelay", "polygonDelay",
                 "laserOnDelay", "laserOffDelay", "endDelay", "scanHeadTimeLag")
OPTICS_CALIB = ("laserGainL1X", "laserGainL1Y", "laserGainL2X", "laserGainL2Y",
                "correctionFile", "offsetL1X", "offsetL1Y", "offsetL2X", "offsetL2Y",
                "diodeGainCorrectionL1", "diodeGainCorrectionL2", "fieldRotationX",
                "fieldRotationY", "fieldRotationZ")
OPTICS_IDENTITY = ("scanHead", "markingAreaMM", "markingArea")

# Parameters that legitimately differ per machine — never flag these.
OPTICS_IGNORE = {"qualityFactorL1", "qualityFactorL2", "tableManequinPosition",
                 "manequinHeight", "rectangleSide", "speed"}


def _optics_impact(param):
    if param in OPTICS_SPEED:
        return "speed", "critical"
    if param in OPTICS_POWER:
        return "power", "critical"
    if param in OPTICS_IDENTITY:
        return "hardware", "warning"
    if param in OPTICS_TIMING:
        return "timing", "warning"
    if param in OPTICS_CALIB:
        return "calibration", "info"
    return "other", "info"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def optics_drift(min_peers=3):
    """Compare every machine's laser recipe against the standard for its model.

    The reference is the fleet itself: for each (model, preset, parameter) the
    most common value among machines of that model is taken as standard, and any
    machine differing from it is reported. A parameter is only judged when at
    least `min_peers` machines of the model carry it, so a one-off machine is
    never measured against nothing.
    """
    model_of, name_of = {}, {}
    for r in db.q("""SELECT m.id, m.name, COALESCE(i.model, m.model, m.machine_type, 'unknown') model
                     FROM machines m LEFT JOIN machine_identity i ON i.machine_id=m.id
                     WHERE m.enabled=1"""):
        model_of[r["id"]] = r["model"]
        name_of[r["id"]] = r["name"]

    groups = {}          # (model, preset, param) -> {machine_id: value}
    for r in db.q("SELECT machine_id, preset, param, value FROM machine_optics"):
        if r["param"] in OPTICS_IGNORE:
            continue
        mdl = model_of.get(r["machine_id"])
        if not mdl:
            continue
        groups.setdefault((mdl, r["preset"], r["param"]), {})[r["machine_id"]] = r["value"]

    findings = []
    for (mdl, preset, param), vals in groups.items():
        if len(vals) < min_peers:
            continue
        counts = {}
        for v in vals.values():
            counts[v] = counts.get(v, 0) + 1
        standard, n_std = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
        if n_std == len(vals):
            continue                                  # whole model agrees
        area, severity = _optics_impact(param)
        for mid, v in vals.items():
            if v == standard:
                continue
            cur, ref = _num(v), _num(standard)
            delta_pct = None
            if cur is not None and ref not in (None, 0):
                delta_pct = round((cur - ref) / abs(ref) * 100, 1)
            # a slower mark/jump speed is a direct, quantifiable throughput loss
            slower = (area == "speed" and delta_pct is not None and delta_pct < 0)
            findings.append({
                "machine_id": mid, "machine": name_of.get(mid), "model": mdl,
                "preset": preset, "param": param, "value": v, "standard": standard,
                "peers": len(vals), "agree": n_std, "delta_pct": delta_pct,
                "area": area,
                "severity": "critical" if slower else severity,
                "note": (f"{abs(delta_pct)}% slower than the fleet standard"
                         if slower else None),
            })

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 3),
                                 -abs(f["delta_pct"] or 0), f["machine_id"]))
    by_machine = {}
    for f in findings:
        b = by_machine.setdefault(f["machine_id"], {
            "machine_id": f["machine_id"], "machine": f["machine"], "model": f["model"],
            "total": 0, "critical": 0, "speed_loss_pct": None})
        b["total"] += 1
        if f["severity"] == "critical":
            b["critical"] += 1
        if f["area"] == "speed" and f["delta_pct"] is not None and f["delta_pct"] < 0:
            worst = b["speed_loss_pct"]
            b["speed_loss_pct"] = min(worst, f["delta_pct"]) if worst is not None else f["delta_pct"]
    # Pair each deviation with how that machine actually performs against its own
    # model. A config difference matters far more on a machine that is also slow;
    # this is context for a human decision, never a claim of cause.
    perf = {}
    for r in db.q("""SELECT d.machine_id, AVG(d.units_per_hour) uph
                     FROM machine_daily d WHERE d.production_date >= date('now','-30 day')
                       AND d.units_per_hour > 0 GROUP BY 1"""):
        perf[r["machine_id"]] = r["uph"]
    model_uph = {}
    for mid, u in perf.items():
        model_uph.setdefault(model_of.get(mid), []).append(u)
    medians = {}
    for mdl, vals in model_uph.items():
        vals.sort()
        medians[mdl] = vals[len(vals) // 2] if vals else None
    for b in by_machine.values():
        u = perf.get(b["machine_id"])
        med = medians.get(b["model"])
        b["units_per_hour"] = round(u, 1) if u else None
        b["model_median_uph"] = round(med, 1) if med else None
        b["vs_model_pct"] = round((u - med) / med * 100, 1) if (u and med) else None

    covered = db.q1("SELECT COUNT(DISTINCT machine_id) n FROM machine_optics")["n"]
    return {
        "machines_with_optics": covered,
        "machines_with_drift": len(by_machine),
        "findings": findings,
        "by_machine": sorted(by_machine.values(), key=lambda b: (-b["critical"], -b["total"])),
        "presets": db.q1("SELECT COUNT(DISTINCT preset) n FROM machine_optics")["n"],
        "parameters": db.q1("SELECT COUNT(DISTINCT param) n FROM machine_optics")["n"],
    }
