"""HTTP API and static hosting for the command center."""
import asyncio
import datetime as dt
import gzip
import hmac
import json

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from . import alerts, analytics, auth, collector, config, db, mirror, reports

router = APIRouter()


# ------------------------------------------------------------------- auth --

def current_user(session: str = Cookie(default=None)):
    u = auth.user_for_token(session)
    if not u:
        raise HTTPException(401, "not authenticated")
    return u


def require(permission):
    def dep(user=Depends(current_user)):
        if not auth.can(user, permission):
            raise HTTPException(403, f"role '{user['role']}' lacks '{permission}'")
        return user
    return dep


@router.post("/auth/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    ip = request.client.host if request.client else None
    token = auth.login(body.get("username", ""), body.get("password", ""), ip)
    if not token:
        raise HTTPException(401, "invalid credentials")
    # secure only on the mirror: it is the one deployment reachable over the public
    # internet, and without the flag a browser sent to http:// leaks the session before
    # the redirect to https. On-prem the UI is plain HTTP on the LAN, where Secure would
    # drop the cookie entirely. (Browsers exempt localhost, so mirror smoke tests still work.)
    response.set_cookie("session", token, httponly=True, samesite="lax",
                        secure=config.MIRROR, max_age=auth.SESSION_HOURS * 3600)
    u = auth.user_for_token(token)
    return {"ok": True, "user": {"username": u["username"], "role": u["role"],
                                 "full_name": u["full_name"],
                                 "permissions": sorted(auth.PERMISSIONS.get(u["role"], []))}}


@router.post("/auth/logout")
def logout(response: Response, session: str = Cookie(default=None)):
    auth.logout(session)
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/auth/me")
def me(user=Depends(current_user)):
    return {"username": user["username"], "role": user["role"], "full_name": user["full_name"],
            "email": user["email"], "permissions": sorted(auth.PERMISSIONS.get(user["role"], []))}


@router.post("/auth/password")
def change_password(body: dict = Body(...), user=Depends(current_user)):
    if not auth.verify(user["username"], body.get("current", "")):
        raise HTTPException(400, "current password is wrong")
    new = body.get("new", "")
    if len(new) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    h, salt = auth.hash_password(new)
    db.ex("UPDATE users SET password_hash=?, salt=? WHERE id=?", (h, salt, user["id"]))
    db.audit(user["username"], "password_change", "user", user["id"])
    return {"ok": True}


# --------------------------------------------------------------- overview --

@router.get("/overview")
def overview(date: str = None, user=Depends(current_user)):
    return analytics.department_overview(date)


@router.get("/attention")
def attention(user=Depends(current_user)):
    return analytics.attention_list()


@router.get("/intelligence")
def intelligence(days: int = 7, user=Depends(current_user)):
    """The questions management actually asks, answered in one call."""
    today = dt.date.today().isoformat()
    ov = analytics.department_overview(today)
    rank = analytics.ranking(days=days)["rows"]
    bn = analytics.bottleneck(days=days)
    loss = analytics.loss_analysis(today)
    idle = [m for m in ov["machines"] if m["status"] in ("IDLE", "STOPPED", "OFFLINE")]
    fc = db.q1("SELECT value FROM forecast WHERE machine_id IS NULL AND metric='units' "
               "AND target_date=? ORDER BY id DESC LIMIT 1", (today,))
    return {
        "top_producer": rank[0] if rank else None,
        "lowest_producer": rank[-1] if rank else None,
        "highest_downtime": max(rank, key=lambda r: r["down_minutes"] or 0) if rank else None,
        "idle_machines": [{"id": m["id"], "name": m["name"], "status": m["status"],
                           "minutes": round((m["idle_seconds"] or 0) / 60)} for m in idle],
        "capacity": analytics.capacity(days=days),
        "bottleneck": bn["rows"][0] if bn["rows"] else None,
        "lost_capacity_hours": bn["total_lost_hours"],
        "lost_units_estimate": bn["total_lost_units"],
        "todays_loss": loss,
        "attention": analytics.attention_list()[:8],
        "forecast_units": fc["value"] if fc else None,
        "target": ov["totals"]["target"], "units_today": ov["totals"]["units"],
        "on_track": (None if not ov["totals"]["target"] or not fc
                     else fc["value"] >= ov["totals"]["target"] * 0.95),
        "department_utilization": ov["totals"]["utilization"],
    }


# ---------------------------------------------------------------- machines --

@router.get("/machines")
def machines(user=Depends(current_user)):
    rows = db.q("""SELECT m.*, s.status, s.connectivity, s.online, s.last_heartbeat, s.last_heartbeat_ok,
                          s.latency_ms, s.last_sync, s.last_sync_ok, s.last_error, s.consecutive_fail,
                          s.current_design, s.idle_seconds, s.last_source_id, s.source_mtime,
                          i.model AS detected_model, i.serial_number AS detected_serial,
                          i.n_lasers, i.emark_version, i.duty_max
                   FROM machines m LEFT JOIN machine_state s ON s.machine_id=m.id
                   LEFT JOIN machine_identity i ON i.machine_id=m.id ORDER BY m.id""")
    show_secret = auth.can(user, "credentials")
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password_enc", None)
        d["password_set"] = bool(r["password_enc"])
        if show_secret:
            d["password"] = config.decrypt(r["password_enc"])
        out.append(d)
    return out


@router.get("/machines/{mid}")
def machine_detail(mid: int, user=Depends(current_user)):
    m = db.q1("SELECT * FROM machines WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404, "unknown machine")
    d = dict(m)
    d.pop("password_enc", None)
    today = dt.date.today().isoformat()
    return {
        "machine": d,
        "state": dict(db.q1("SELECT * FROM machine_state WHERE machine_id=?", (mid,)) or {}),
        "identity": dict(db.q1("SELECT * FROM machine_identity WHERE machine_id=?", (mid,)) or {}),
        "today": dict(db.q1("SELECT * FROM machine_daily WHERE machine_id=? AND production_date=?",
                            (mid, today)) or {}),
        "health": analytics.health_breakdown(mid, today),
        "forecast": analytics.forecast_machine(mid),
        "maintenance_risk": analytics.maintenance_risk(mid),
        "downtime_forecast": analytics.forecast_downtime(mid),
        "recent_jobs": [dict(r) for r in db.q(
            "SELECT init_date,end_date,design,copies,units,laser_ms,elapsed_seconds,laser_duty,operator,"
            "alarm1,alarm2,temp_galvo_x,temp_galvo_y,temp_servo_x,temp_servo_y,shift "
            "FROM production WHERE machine_id=? AND suspect=0 ORDER BY init_date DESC LIMIT 40", (mid,))],
        "recent_alarms": [dict(r) for r in db.q(
            "SELECT ts,category,severity,error_code,description FROM machine_alarms "
            "WHERE machine_id=? ORDER BY ts DESC LIMIT 40", (mid,))],
        "open_alerts": [dict(r) for r in db.q(
            "SELECT * FROM alerts WHERE machine_id=? AND status IN ('open','acknowledged') "
            "ORDER BY started_at DESC", (mid,))],
        "state_history": [dict(r) for r in db.q(
            "SELECT status,connectivity,start_time,end_time,duration_seconds FROM state_history "
            "WHERE machine_id=? ORDER BY start_time DESC LIMIT 60", (mid,))],
    }


@router.get("/machines/{mid}/history")
def machine_hist(mid: int, start: str = None, end: str = None, bucket: str = "day",
                 user=Depends(current_user)):
    end = end or dt.date.today().isoformat()
    start = start or (dt.date.fromisoformat(end) - dt.timedelta(days=30)).isoformat()
    return {"machine_id": mid, "start": start, "end": end, "bucket": bucket,
            "rows": analytics.machine_history(mid, start, end, bucket)}


EDITABLE = ("code", "name", "host", "port", "share", "db_file", "username", "connection_method",
            "model", "machine_type", "serial_number", "location", "production_area", "status",
            "heartbeat_interval", "sync_interval", "timeout_seconds", "retry_max", "retry_backoff",
            "target_units_day", "ideal_seconds_unit", "enabled", "push_token", "notes")

# Changing where we connect is as sensitive as the password itself: point a machine at a
# hostile host and the server will offer the stored credential to it. Same gate as `password`.
CREDENTIAL_FIELDS = ("host", "share", "username", "db_file", "port", "push_token")


@router.post("/machines")
def create_machine(body: dict = Body(...), user=Depends(require("config"))):
    fields = {k: v for k, v in body.items() if k in EDITABLE}
    if not fields.get("name"):
        raise HTTPException(400, "name is required")
    mid = body.get("id")
    if mid is None:
        mx = db.q1("SELECT COALESCE(MAX(id),0) x FROM machines")["x"]
        mid = mx + 1
    cols = ["id"] + list(fields)
    vals = [mid] + [fields[k] for k in fields]
    if body.get("password"):
        cols.append("password_enc")
        vals.append(config.encrypt(body["password"]))
    ph = ",".join("?" * len(cols))
    db.ex(f"INSERT INTO machines({','.join(cols)},created_at,updated_at) VALUES({ph},?,?)",
          vals + [db.now(), db.now()])
    db.ex("INSERT OR IGNORE INTO machine_state(machine_id,status,connectivity,updated_at) "
          "VALUES(?,'UNKNOWN','DISCONNECTED',?)", (mid, db.now()))
    db.audit(user["username"], "machine_create", "machine", mid, after=fields)
    return {"ok": True, "id": mid}


@router.put("/machines/{mid}")
def update_machine(mid: int, body: dict = Body(...), user=Depends(require("config"))):
    before = db.q1("SELECT * FROM machines WHERE id=?", (mid,))
    if not before:
        raise HTTPException(404, "unknown machine")
    fields = {k: v for k, v in body.items() if k in EDITABLE}
    touched = [k for k in fields if k in CREDENTIAL_FIELDS and str(fields[k]) != str(before[k])]
    if touched and not auth.can(user, "credentials"):
        raise HTTPException(403, f"changing {', '.join(touched)} requires the credentials permission")
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v)
    if body.get("password"):
        if not auth.can(user, "credentials"):
            raise HTTPException(403, "changing credentials requires admin")
        sets.append("password_enc=?")
        vals.append(config.encrypt(body["password"]))
    if not sets:
        return {"ok": True, "changed": 0}
    sets.append("updated_at=?")
    vals.append(db.now())
    vals.append(mid)
    db.ex(f"UPDATE machines SET {','.join(sets)} WHERE id=?", vals)
    b = {k: before[k] for k in fields}
    db.audit(user["username"], "machine_update", "machine", mid, before=b, after=fields)
    collector._mounts.pop((before["host"] or "").lower(), None)
    return {"ok": True, "changed": len(fields)}


@router.delete("/machines/{mid}")
def disable_machine(mid: int, user=Depends(require("config"))):
    db.ex("UPDATE machines SET enabled=0, status='disabled', updated_at=? WHERE id=?", (db.now(), mid))
    db.audit(user["username"], "machine_disable", "machine", mid)
    return {"ok": True}


@router.get("/machines/targets/suggest")
def suggest_targets(days: int = 30, percentile: int = 50, user=Depends(current_user)):
    """Propose a daily target per machine from its own recent output.

    Nothing is invented: the suggestion is a percentile of the machine's own
    producing days, so a slow machine is not handed a fast machine's target.
    """
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for m in db.q("SELECT id,name,target_units_day FROM machines WHERE enabled=1 ORDER BY id"):
        vals = [r["units"] for r in db.q(
            "SELECT units FROM machine_daily WHERE machine_id=? AND production_date >= ? "
            "AND units > 0 ORDER BY units", (m["id"], since))]
        suggested = None
        if len(vals) >= 5:
            idx = min(len(vals) - 1, max(0, int(len(vals) * percentile / 100.0)))
            suggested = int(vals[idx])
        out.append({"machine_id": m["id"], "name": m["name"],
                    "current_target": m["target_units_day"] or 0,
                    "suggested": suggested, "producing_days": len(vals),
                    "best_day": int(vals[-1]) if vals else 0,
                    "median": int(vals[len(vals) // 2]) if vals else 0})
    return {"days": days, "percentile": percentile, "rows": out}


@router.post("/machines/targets")
def apply_targets(body: dict = Body(...), user=Depends(require("config"))):
    """Bulk-set daily targets: {"targets": {"<machine_id>": units, ...}}."""
    targets = body.get("targets") or {}
    if not isinstance(targets, dict):
        raise HTTPException(400, "targets must be an object of machine_id -> units")
    applied = 0
    for mid, units in targets.items():
        try:
            mid_i, units_i = int(mid), max(0, int(units))
        except (TypeError, ValueError):
            continue
        db.ex("UPDATE machines SET target_units_day=?, updated_at=? WHERE id=?",
              (units_i, db.now(), mid_i))
        applied += 1
    db.audit(user["username"], "targets_update", "machine", None, after=targets)
    return {"ok": True, "applied": applied}


@router.post("/machines/{mid}/test")
async def test_machine(mid: int, user=Depends(require("config"))):
    res = await asyncio.to_thread(collector.test_connection, mid)
    db.audit(user["username"], "machine_test", "machine", mid, after={"ok": res.get("ok")})
    return res


@router.post("/machines/{mid}/sync")
async def sync_machine(mid: int, full: bool = False, user=Depends(require("config"))):
    res = await asyncio.to_thread(collector.sync, mid, full)
    await asyncio.to_thread(analytics.rebuild_hourly, 400 if full else 3, mid)
    await asyncio.to_thread(analytics.rebuild_daily, 400 if full else 3, mid)
    db.audit(user["username"], "machine_sync", "machine", mid, after={"full": full})
    return res


@router.post("/machines/{mid}/identity")
async def refresh_identity(mid: int, user=Depends(require("config"))):
    m = db.q1("SELECT * FROM machines WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404, "unknown machine")
    return await asyncio.to_thread(collector.sync_identity, m)


@router.get("/machines/{mid}/logs")
def machine_logs(mid: int, limit: int = 200, user=Depends(current_user)):
    return {"connection": [dict(r) for r in db.q(
                "SELECT * FROM connection_log WHERE machine_id=? ORDER BY id DESC LIMIT ?", (mid, limit))],
            "sync": [dict(r) for r in db.q(
                "SELECT * FROM sync_log WHERE machine_id=? ORDER BY id DESC LIMIT ?", (mid, limit))]}


@router.post("/ingest/{mid}")
async def ingest(mid: int, body: dict = Body(...)):
    """Push endpoint for machines we cannot poll. Auth is the per-machine push token."""
    m = db.q1("SELECT * FROM machines WHERE id=? AND enabled=1", (mid,))
    if not m:
        raise HTTPException(404, "unknown machine")
    # Fail closed: a machine with no push_token configured accepts nothing. Anything
    # else lets an unauthenticated caller on the LAN write production rows.
    import hmac as _hmac
    if not m["push_token"] or not _hmac.compare_digest(str(body.get("token") or ""), m["push_token"]):
        raise HTTPException(403, "bad or missing push token")
    rows = body.get("rows") or []
    if not isinstance(rows, list):
        raise HTTPException(400, "rows must be a list")
    return await asyncio.to_thread(collector.ingest_push, mid, rows)


# ---------------------------------------------------------------- analytics --

@router.get("/analytics/ranking")
def r_ranking(days: int = 7, metric: str = "units", user=Depends(current_user)):
    return analytics.ranking(days, metric)


@router.get("/analytics/bottleneck")
def r_bottleneck(days: int = 7, user=Depends(current_user)):
    return analytics.bottleneck(days)


@router.get("/analytics/downtime")
def r_downtime(days: int = 30, machine_id: int = None, user=Depends(current_user)):
    return analytics.downtime_analysis(days, machine_id)


@router.get("/analytics/alarms")
def r_alarms(days: int = 30, machine_id: int = None, user=Depends(current_user)):
    return analytics.alarm_analysis(days, machine_id)


@router.get("/analytics/shifts")
def r_shifts(days: int = 7, machine_id: int = None, user=Depends(current_user)):
    return analytics.shift_performance(days, machine_id)


@router.get("/analytics/operators")
def r_operators(days: int = 30, machine_id: int = None, user=Depends(current_user)):
    return analytics.operator_performance(days, machine_id)


@router.get("/analytics/designs")
def r_designs(days: int = 30, limit: int = 40, user=Depends(current_user)):
    return analytics.design_performance(days, limit)


@router.get("/analytics/capacity")
def r_capacity(days: int = 7, user=Depends(current_user)):
    return analytics.capacity(days)


@router.get("/analytics/loss")
def r_loss(date: str = None, user=Depends(current_user)):
    return analytics.loss_analysis(date)


@router.get("/analytics/trend")
def r_trend(days: int = 30, user=Depends(current_user)):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return [dict(r) for r in db.q(
        """SELECT production_date d, SUM(units) units, SUM(jobs) jobs,
                  ROUND(AVG(utilization),1) utilization, ROUND(AVG(availability),1) availability,
                  ROUND(AVG(performance),1) performance, ROUND(AVG(oee),1) oee,
                  ROUND(AVG(health_score),1) health,
                  ROUND(SUM(down_seconds+offline_seconds)/3600.0,1) down_hours,
                  ROUND(SUM(idle_seconds)/3600.0,1) idle_hours, SUM(alarm_count) alarms
           FROM machine_daily WHERE production_date >= ? GROUP BY d ORDER BY d""", (since,))]


@router.get("/analytics/optics-drift")
def r_optics_drift(min_peers: int = 3, user=Depends(current_user)):
    return analytics.optics_drift(min_peers)


@router.post("/analytics/optics-refresh")
async def r_optics_refresh(user=Depends(require("config"))):
    """Re-read config.db from every reachable machine."""
    ms = db.q("SELECT * FROM machines WHERE enabled=1 AND connection_method='smb' AND host IS NOT NULL")
    done, failed = 0, []
    for m in ms:
        try:
            await asyncio.to_thread(collector.sync_optics, m)
            done += 1
        except Exception as e:                                # noqa: BLE001
            failed.append({"machine": m["name"], "error": str(e)[:120]})
    db.audit(user["username"], "optics_refresh", after={"machines": done})
    return {"refreshed": done, "failed": failed}


@router.get("/analytics/heatmap")
def r_heatmap(days: int = 21, machine_id: int = None, user=Depends(current_user)):
    return analytics.production_heatmap(days, machine_id)


@router.get("/analytics/hourly")
def r_hourly(date: str = None, machine_id: int = None, user=Depends(current_user)):
    date = date or dt.date.today().isoformat()
    where = "AND machine_id=?" if machine_id else ""
    args = [date] + ([machine_id] if machine_id else [])
    return [dict(r) for r in db.q(
        f"""SELECT substr(hour,12,5) h, SUM(units) units, SUM(jobs) jobs,
                   ROUND(SUM(busy_seconds)/3600.0,2) busy_hours,
                   ROUND(SUM(idle_seconds)/3600.0,2) idle_hours,
                   ROUND(SUM(down_seconds)/3600.0,2) down_hours, SUM(alarm_count) alarms
            FROM machine_hourly WHERE production_date=? {where} GROUP BY h ORDER BY h""", args)]


@router.get("/analytics/forecast")
def r_forecast(machine_id: int = None, user=Depends(current_user)):
    if machine_id:
        return {"machine": analytics.forecast_machine(machine_id),
                "downtime": analytics.forecast_downtime(machine_id),
                "health": analytics.forecast_health(machine_id),
                "maintenance": analytics.maintenance_risk(machine_id)}
    today = dt.date.today().isoformat()
    return {"department": [dict(r) for r in db.q(
                "SELECT f.*, m.name FROM forecast f LEFT JOIN machines m ON m.id=f.machine_id "
                "WHERE f.target_date=? AND f.metric='units' ORDER BY f.value DESC", (today,))],
            "capacity": analytics.capacity(7)}


@router.get("/analytics/compare")
def r_compare(machines: str = Query(...), days: int = 30, user=Depends(current_user)):
    ids = [int(x) for x in machines.split(",") if x.strip().isdigit()]
    start = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    end = dt.date.today().isoformat()
    return {mid: analytics.machine_history(mid, start, end, "day") for mid in ids}


@router.post("/analytics/rebuild")
async def r_rebuild(deep: bool = False, days: int = 7, user=Depends(require("settings"))):
    if deep:
        await asyncio.to_thread(analytics.refresh_deep)
    else:
        await asyncio.to_thread(analytics.refresh_all, days)
    db.audit(user["username"], "analytics_rebuild", after={"deep": deep, "days": days})
    return {"ok": True}


# ------------------------------------------------------------------ alerts --

@router.get("/alerts")
def get_alerts(status: str = None, days: int = 30, machine_id: int = None, user=Depends(current_user)):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    where = ["a.started_at >= ?"]
    args = [since]
    if status:
        where.append("a.status=?")
        args.append(status)
    if machine_id:
        where.append("a.machine_id=?")
        args.append(machine_id)
    return [dict(r) for r in db.q(
        f"""SELECT a.*, m.name machine_name FROM alerts a LEFT JOIN machines m ON m.id=a.machine_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE a.status WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END,
                     CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                     a.started_at DESC LIMIT 500""", args)]


@router.get("/alerts/summary")
def alerts_summary(days: int = 30, user=Depends(current_user)):
    return alerts.summary(days)


@router.post("/alerts/{aid}/ack")
def ack(aid: int, body: dict = Body(default={}), user=Depends(require("ack"))):
    alerts.acknowledge(aid, user["username"], body.get("note"))
    return {"ok": True}


@router.post("/alerts/{aid}/resolve")
def resolve_alert(aid: int, body: dict = Body(default={}), user=Depends(require("ack"))):
    alerts.resolve(aid, user["username"], body.get("resolution", "resolved"))
    return {"ok": True}


@router.post("/alerts/{aid}/assign")
def assign_alert(aid: int, body: dict = Body(...), user=Depends(require("ack"))):
    alerts.assign(aid, user["username"], body.get("assignee"))
    return {"ok": True}


@router.get("/alerts/rules")
def get_rules(user=Depends(current_user)):
    return [dict(r) for r in db.q("SELECT r.*, m.name machine_name FROM alert_rules r "
                                  "LEFT JOIN machines m ON m.id=r.machine_id ORDER BY r.type, r.machine_id")]


@router.put("/alerts/rules/{rid}")
def update_rule(rid: int, body: dict = Body(...), user=Depends(require("alerts"))):
    before = db.q1("SELECT * FROM alert_rules WHERE id=?", (rid,))
    if not before:
        raise HTTPException(404, "unknown rule")
    allowed = ("enabled", "severity", "threshold", "window_min", "cooldown_min", "channels",
               "recipients", "escalate_after_min", "escalate_to", "description")
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return {"ok": True}
    db.ex(f"UPDATE alert_rules SET {','.join(f'{k}=?' for k in fields)}, updated_at=? WHERE id=?",
          list(fields.values()) + [db.now(), rid])
    db.audit(user["username"], "rule_update", "alert_rule", rid,
             before={k: before[k] for k in fields}, after=fields)
    return {"ok": True}


@router.post("/alerts/rules")
def create_rule(body: dict = Body(...), user=Depends(require("alerts"))):
    db.ex("""INSERT INTO alert_rules(machine_id,type,enabled,severity,threshold,window_min,cooldown_min,
             channels,recipients,escalate_after_min,escalate_to,description,updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (body.get("machine_id"), body["type"], 1 if body.get("enabled", True) else 0,
           body.get("severity", "warning"), body.get("threshold"), body.get("window_min"),
           body.get("cooldown_min", 30), body.get("channels", "app"), body.get("recipients"),
           body.get("escalate_after_min"), body.get("escalate_to"), body.get("description"), db.now()))
    db.audit(user["username"], "rule_create", "alert_rule", None, after=body)
    return {"ok": True}


@router.delete("/alerts/rules/{rid}")
def delete_rule(rid: int, user=Depends(require("alerts"))):
    db.ex("DELETE FROM alert_rules WHERE id=?", (rid,))
    db.audit(user["username"], "rule_delete", "alert_rule", rid)
    return {"ok": True}


@router.post("/alerts/evaluate")
async def eval_alerts(user=Depends(require("alerts"))):
    n = await asyncio.to_thread(alerts.evaluate)
    return {"raised": n}


@router.post("/alerts/test-channel")
async def test_channel(body: dict = Body(...), user=Depends(require("settings"))):
    ok, detail = await asyncio.to_thread(alerts.test_channel, body.get("channel", "email"),
                                         body.get("recipient", user["email"] or ""))
    return {"ok": ok, "detail": detail}


# ----------------------------------------------------------------- reports --

@router.get("/reports")
def list_reports(user=Depends(current_user)):
    return [{"key": k, "title": v[0]} for k, v in reports.REPORTS.items()]


@router.get("/reports/{name}")
def get_report(name: str, fmt: str = "json", days: int = None, date: str = None,
               machine_id: int = None, user=Depends(current_user)):
    try:
        rep = reports.build(name, days=days, date=date, machine_id=machine_id)
    except KeyError:
        raise HTTPException(404, "unknown report")
    if fmt == "json":
        return rep
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        data = reports.to_xlsx(rep)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        fn = f"{name}-{stamp}.xlsx"
    elif fmt == "pdf":
        data = reports.to_pdf(rep)
        media, fn = "application/pdf", f"{name}-{stamp}.pdf"
    else:
        raise HTTPException(400, "fmt must be json, xlsx or pdf")
    db.audit(user["username"], "report_export", "report", name, after={"fmt": fmt})
    import io
    return StreamingResponse(io.BytesIO(data), media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# ------------------------------------------------------- settings & system --

@router.get("/settings")
def get_settings(user=Depends(current_user)):
    admin = auth.can(user, "settings")
    out = {}
    for k, default in config.DEFAULTS.items():
        v = db.get_setting(k, default)
        if not admin and any(s in k for s in ("key", "token", "webhook", "endpoint",
                                              "secret", "password")):
            v = "***" if v else ""
        elif "key" in k or "token" in k:
            v = v
        out[k] = v
    out["shifts"] = [dict(r) for r in db.q("SELECT * FROM shifts ORDER BY start_time")]
    return out


@router.put("/settings")
def put_settings(body: dict = Body(...), user=Depends(require("settings"))):
    changed = {}
    for k, v in body.items():
        if k in config.DEFAULTS:
            db.set_setting(k, v)
            changed[k] = "***" if ("key" in k or "token" in k) else v
    db.audit(user["username"], "settings_update", after=changed)
    return {"ok": True, "changed": list(changed)}


@router.put("/shifts")
def put_shifts(body: list = Body(...), user=Depends(require("settings"))):
    db.ex("DELETE FROM shifts")
    db.exmany("INSERT INTO shifts(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)",
              [(s["name"], s["start_time"], s["end_time"], s.get("break_minutes", 0)) for s in body])
    db._shift_cache["rows"] = None
    db.audit(user["username"], "shifts_update", after=body)
    return {"ok": True}


@router.get("/users")
def list_users(user=Depends(require("users"))):
    return [dict(r) for r in db.q("SELECT id,username,full_name,email,role,active,last_login,created_at "
                                  "FROM users ORDER BY username")]


@router.post("/users")
def add_user(body: dict = Body(...), user=Depends(require("users"))):
    if body.get("role") not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {auth.ROLES}")
    if len(body.get("password", "")) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    try:
        auth.create_user(body["username"], body["password"], body["role"],
                         body.get("full_name"), body.get("email"))
    except Exception as e:                                    # noqa: BLE001
        raise HTTPException(400, str(e))
    db.audit(user["username"], "user_create", "user", body["username"],
             after={"role": body["role"], "email": body.get("email")})
    return {"ok": True}


@router.put("/users/{uid}")
def edit_user(uid: int, body: dict = Body(...), user=Depends(require("users"))):
    fields = {k: v for k, v in body.items() if k in ("full_name", "email", "role", "active")}
    if fields:
        db.ex(f"UPDATE users SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
              list(fields.values()) + [uid])
    if body.get("password"):
        h, salt = auth.hash_password(body["password"])
        db.ex("UPDATE users SET password_hash=?, salt=? WHERE id=?", (h, salt, uid))
        fields["password"] = "***"
    db.audit(user["username"], "user_update", "user", uid, after=fields)
    return {"ok": True}


@router.get("/audit")
def get_audit(limit: int = 300, user=Depends(require("config"))):
    return [dict(r) for r in db.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]


@router.get("/system")
def system_status(user=Depends(current_user)):
    import os
    size = os.path.getsize(config.DB_PATH) if config.DB_PATH.exists() else 0
    counts = {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in
              ("production", "machine_alarms", "machine_daily", "machine_hourly", "alerts",
               "downtime", "connection_log", "head_health")}
    last = db.q1("SELECT MAX(ts) t FROM sync_log")
    fails = [dict(r) for r in db.q(
        "SELECT c.machine_id, m.name, COUNT(*) n, MAX(c.ts) last FROM connection_log c "
        "JOIN machines m ON m.id=c.machine_id WHERE c.ok=0 AND c.ts >= datetime('now','-1 day') "
        "GROUP BY c.machine_id ORDER BY n DESC")]
    ingest = [dict(r) for r in db.q(
        "SELECT substr(ts,1,13)||':00' h, SUM(rows_new) rows, COUNT(*) runs, SUM(ok=0) failures "
        "FROM sync_log WHERE ts >= datetime('now','-1 day') GROUP BY h ORDER BY h")]
    return {"database": {"path": str(config.DB_PATH), "size_mb": round(size / 1e6, 1), "counts": counts},
            "last_sync": last["t"] if last else None,
            "failing_machines": fails, "ingestion_last_24h": ingest,
            "backup": [dict(r) for r in db.q(
                "SELECT value FROM settings WHERE key='last_backup'")] or None,
            "server_time": db.now()}


@router.get("/system/public-url")
def public_url(user=Depends(current_user)):
    """The live public URL, maintained by the tunnel keeper.

    Quick-tunnel hostnames are disposable, so the LAN address is the stable place
    to look up whatever the current one is."""
    return {"url": db.get_setting("public_url", "") or None,
            "checked": db.get_setting("public_url_checked", "") or None}


@router.post("/system/backup")
async def backup(user=Depends(require("settings"))):
    """Consistent online backup via SQLite's own backup API."""
    import sqlite3
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.DATA / "backup" / f"laser_platform-{stamp}.db"
    dest.parent.mkdir(exist_ok=True)

    def run():
        src = db.connect()
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        out.close()
        keep = sorted(dest.parent.glob("laser_platform-*.db"))[:-14]
        for old in keep:
            old.unlink(missing_ok=True)
        return dest.stat().st_size

    size = await asyncio.to_thread(run)
    db.set_setting("last_backup", f"{db.now()} -> {dest.name}")
    db.audit(user["username"], "backup", "database", dest.name)
    return {"ok": True, "file": str(dest), "size_mb": round(size / 1e6, 1)}


@router.post("/system/import-legacy")
async def import_legacy(user=Depends(require("settings"))):
    res = await asyncio.to_thread(db.import_central_db)
    await asyncio.to_thread(analytics.refresh_deep)
    db.audit(user["username"], "import_legacy", after=res)
    return res


@router.get("/data-dictionary")
def data_dictionary(user=Depends(current_user)):
    p = config.ROOT / "DATA_DICTIONARY.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


# ------------------------------------------------------------------ mirror --

def _mirror_auth(request: Request):
    """Only the mirror serves these, and only to the collector holding the shared secret.
    Refusing on the collector matters: both ends carry the same token, so without this a
    push aimed at the factory would overwrite the real machines table with NULL credentials.
    Fail closed like /ingest — an unset LASER_MIRROR_TOKEN accepts nothing."""
    if not config.MIRROR:
        raise HTTPException(404, "not a mirror")
    # Compare as bytes: compare_digest on str raises TypeError for any non-ASCII
    # character, and a header byte >0x7f is one curl away — that turned a 403 into a
    # 500 with a traceback on an unauthenticated, internet-facing route.
    tok = (request.headers.get("x-mirror-token") or "").encode("utf-8", "replace")
    if not config.MIRROR_TOKEN or not hmac.compare_digest(tok, config.MIRROR_TOKEN.encode()):
        raise HTTPException(403, "bad or missing mirror token")


@router.get("/mirror/state")
def mirror_state(request: Request):
    """What the mirror holds. The pusher uses it to decide what to send; an operator uses
    it to confirm data is flowing."""
    _mirror_auth(request)
    return {"ok": True, "role": config.ROLE, "have": mirror.have(),
            "machines": db.q1("SELECT COUNT(*) n FROM machines")["n"],
            "last_push": (db.q1("SELECT MAX(updated_at) t FROM settings "
                                "WHERE key LIKE 'mirror_hash:%'") or {})["t"]}


@router.post("/mirror/push")
async def mirror_push(request: Request):
    _mirror_auth(request)
    raw = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    applied = await asyncio.to_thread(mirror.apply, json.loads(raw))
    return {"ok": True, "applied": applied, "have": mirror.have()}
