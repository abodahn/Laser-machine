"""Alert engine and notification delivery (in-app, Brevo email, Teams, WhatsApp gateway)."""
import datetime as dt
import json

import httpx

from . import analytics, db

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
SEV_ORDER = {"info": 0, "warning": 1, "critical": 2}

# A child is listed here only when it is a mechanical CONSEQUENCE of the parent,
# never an independent fault. A dark PC cannot answer a heartbeat, a sync or an SMB
# probe (slow_response on an offline machine is literally the 10 s probe timeout),
# and it reports no jobs, so every output-derived rule reads zero against a baseline
# built while it was running. A machine that is STOPPED or alarming out likewise
# produces nothing, so its output rules are already explained by that root.
# Anything ABSENT from this table is never masked: thermal_risk, head_drift,
# laser_credit_low, data_quality and repeated_alarm are measured from data already
# collected -- losing the PC does not cool a servo down or re-calibrate a galvo.
SUPPRESSED_BY = {
    "heartbeat_lost":        ["machine_offline"],
    "communication_failure": ["machine_offline"],
    "sync_failure":          ["machine_offline"],
    "slow_response":         ["machine_offline"],
    "machine_stopped":       ["machine_offline"],
    "excessive_idle":        ["machine_offline", "machine_stopped", "repeated_alarm"],
    "production_drop":       ["machine_offline", "machine_stopped", "repeated_alarm"],
    "abnormal_behaviour":    ["machine_offline", "machine_stopped", "repeated_alarm"],
    "performance_low":       ["machine_offline", "machine_stopped", "repeated_alarm"],
    "excessive_downtime":    ["machine_offline", "machine_stopped", "repeated_alarm"],
    "target_risk":           ["machine_offline", "machine_stopped", "repeated_alarm"],
    "maintenance_risk":      ["machine_offline"],
}
MASK_NOTE = " Also masking: "
ROOT_TYPES = {p for ps in SUPPRESSED_BY.values() for p in ps}


# ----------------------------------------------------------------- raising --

def raise_alert(rule, machine_id, title, message, value=None, dedup=None, severity=None):
    """Open a new alert, or refresh the one already open for the same condition."""
    sev = severity or (rule["severity"] if rule else "warning")
    typ = rule["type"] if rule else "manual"
    key = dedup or f"{typ}:{machine_id}"
    stamp = db.now()
    open_row = db.q1("SELECT * FROM alerts WHERE dedup_key=? AND status IN ('open','acknowledged')", (key,))
    if open_row:
        db.ex("UPDATE alerts SET last_seen_at=?, value=COALESCE(?,value), message=? WHERE id=?",
              (stamp, value, message, open_row["id"]))
        return None
    if _cooling(key, rule["cooldown_min"] if rule and rule["cooldown_min"] else 30):
        return None
    cur = db.ex("""INSERT INTO alerts(rule_id,machine_id,type,severity,title,message,value,threshold,
                   dedup_key,started_at,last_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'open')""",
                (rule["id"] if rule else None, machine_id, typ, sev, title, message, value,
                 rule["threshold"] if rule else None, key, stamp, stamp))
    alert_id = cur.lastrowid
    notify(alert_id, rule)
    return alert_id


def _cooling(key, cooldown_min):
    """True while raise_alert would refuse to re-open this dedup key.

    Rows _supersede() closed are skipped: nobody was ever told about that child, so
    counting it as recently-raised lets one connectivity blip hide a genuine fault for
    the rest of its cooldown -- 12 h for maintenance_risk, whose key rolls at midnight.
    """
    recent = db.q1("SELECT started_at FROM alerts WHERE dedup_key=? AND "
                   "COALESCE(resolution,'') NOT LIKE 'superseded by %' ORDER BY id DESC LIMIT 1", (key,))
    age = _age(recent["started_at"], dt.datetime.now()) if recent else None
    return age is not None and age < cooldown_min * 60


def resolve_alerts(dedup_key, resolution="condition cleared"):
    stamp = db.now()
    rows = db.q("SELECT id,started_at FROM alerts WHERE dedup_key=? AND status IN ('open','acknowledged')",
                (dedup_key,))
    for r in rows:
        db.ex("UPDATE alerts SET status='resolved', resolved_at=?, ended_at=?, resolution=?, "
              "duration_seconds=CAST((julianday(?)-julianday(started_at))*86400 AS INT) WHERE id=?",
              (stamp, stamp, resolution, stamp, r["id"]))
    return len(rows)


def acknowledge(alert_id, username, note=None):
    db.ex("UPDATE alerts SET status='acknowledged', ack_by=?, ack_at=?, resolution=COALESCE(?,resolution) "
          "WHERE id=? AND status='open'", (username, db.now(), note, alert_id))
    db.audit(username, "alert_ack", "alert", alert_id)


def resolve(alert_id, username, resolution):
    stamp = db.now()
    db.ex("UPDATE alerts SET status='resolved', resolved_at=?, ended_at=?, resolution=?, "
          "duration_seconds=CAST((julianday(?)-julianday(started_at))*86400 AS INT) WHERE id=?",
          (stamp, stamp, resolution, stamp, alert_id))
    db.audit(username, "alert_resolve", "alert", alert_id, after={"resolution": resolution})


def assign(alert_id, username, assignee):
    db.ex("UPDATE alerts SET assigned_to=? WHERE id=?", (assignee, alert_id))
    db.audit(username, "alert_assign", "alert", alert_id, after={"assigned_to": assignee})


# ------------------------------------------------------------- rule engine --

def rules_for(machine_id):
    rows = db.q("SELECT * FROM alert_rules WHERE enabled=1 AND (machine_id IS NULL OR machine_id=?)",
                (machine_id,))
    best = {}
    for r in rows:                    # a machine-specific rule overrides the global one
        if r["type"] not in best or r["machine_id"] is not None:
            best[r["type"]] = r
    return best


def _active_roots(mid, st, R, now):
    """Root conditions active for this machine, decided with the SAME predicates the
    rule bodies use so a root and its own alert can never disagree. A disabled rule is
    never a root: with no parent alert there would be nothing to carry the mask note."""
    roots = set()
    r = R.get("machine_offline")
    if r and (st["consecutive_fail"] or 0) >= (r["threshold"] or 3):
        roots.add("machine_offline")
    r = R.get("machine_stopped")
    if r and st["status"] == "STOPPED" and st["online"] and (st["idle_seconds"] or 0) > (r["threshold"] or 1800):
        roots.add("machine_stopped")
    r = R.get("repeated_alarm")
    if r:
        win = (now - dt.timedelta(minutes=r["window_min"] or 60)).strftime("%Y-%m-%d %H:%M:%S")
        # 'localtime': machines write Cairo timestamps and `win` is built from a local
        # datetime.now(), so a bare datetime('now') (UTC) capped the window 3 h in the
        # past -- with the default 60 min window the range was empty and this root could
        # never activate. The cap itself is wanted: max(ts) is 2057, clock-drift junk.
        n = db.q1("SELECT COUNT(*) n FROM machine_alarms WHERE machine_id=? AND ts>=? "
                  "AND ts<=datetime('now','localtime') AND suspect=0 "
                  "AND severity IN ('warning','critical')",
                  (mid, win))["n"]
        if n >= (r["threshold"] or 5):
            roots.add("repeated_alarm")
    return {t for t in roots if _carries(mid, t, R[t])}


def _carries(mid, typ, r):
    """A root may only mask when it can actually carry the disclosure: its own alert is
    already open, or raise_alert is free to open one. A flapping machine hits this --
    offline, reachable, offline again inside the cooldown -- and without the check every
    child would be superseded under a parent that was never raised, leaving the dark
    machine with no open alert at all."""
    key = f"{typ}:{mid}"       # the three roots never pass a custom dedup to raise_alert
    return bool(db.q1("SELECT 1 FROM alerts WHERE dedup_key=? AND status IN ('open','acknowledged')",
                      (key,))) or not _cooling(key, r["cooldown_min"] or 30)


def _gate(name, masked, fired):
    """Wrapper that shadows raise_alert inside evaluate(), so every rule body is gated
    without touching a single call site. The body still RUNS: the mask note must name
    conditions that were really detected, not guessed."""
    def gated(rule, machine_id, title, *a, **kw):
        typ = rule["type"] if rule else None
        if typ in masked:
            fired[typ] = title[len(name):].strip() if title.startswith(name) else title
            return None
        return raise_alert(rule, machine_id, title, *a, **kw)
    return gated


def _root_title(mid, typ):
    a = db.q1("SELECT title FROM alerts WHERE machine_id=? AND type=? AND status IN ('open','acknowledged') "
              "ORDER BY id DESC LIMIT 1", (mid, typ))
    return a["title"] if a else typ.replace("_", " ")


def _supersede(mid, typ, parent_title):
    """Close a child's open alerts. Matched on machine_id+type, never on dedup_key: those
    carry a time bucket, 'production_drop:1:' is a prefix of 'production_drop:11:', and
    abnormal_behaviour writes 'abnormal:'. Already-resolved rows are left alone."""
    stamp = db.now()
    rows = db.q("SELECT id FROM alerts WHERE machine_id=? AND type=? AND status IN ('open','acknowledged')",
                (mid, typ))
    for a in rows:
        db.ex("UPDATE alerts SET status='resolved', resolved_at=?, ended_at=?, resolution=?, "
              "duration_seconds=CAST((julianday(?)-julianday(started_at))*86400 AS INT) WHERE id=?",
              (stamp, stamp, f"superseded by {parent_title}", stamp, a["id"]))
    return len(rows)


def _mask_note(mid, typ, names):
    """Nothing is hidden silently: the surviving root alert says how many conditions it is
    covering and which. Rebuilt from the base text every pass so the sentence can never
    accumulate -- and dropped when names is empty, otherwise a root whose condition stops
    masking (machine_offline only self-resolves at zero failures, machine_stopped only on
    PRODUCING) keeps claiming to hide children that are open again."""
    a = db.q1("SELECT id,message FROM alerts WHERE machine_id=? AND type=? AND status IN ('open','acknowledged') "
              "ORDER BY id DESC LIMIT 1", (mid, typ))
    if not a:
        return
    msg = (a["message"] or "").split(MASK_NOTE)[0]
    if names:
        msg += f"{MASK_NOTE}{len(names)} related conditions ({', '.join(sorted(names))})."
    if msg != a["message"]:
        db.ex("UPDATE alerts SET message=? WHERE id=?", (msg, a["id"]))


def evaluate(machine_id=None):
    """Run every rule against current state. Returns the number of alerts raised."""
    machines = (db.q("SELECT * FROM machines WHERE id=?", (machine_id,)) if machine_id
                else db.q("SELECT * FROM machines WHERE enabled=1"))
    raised = 0
    now = dt.datetime.now()
    today = dt.date.today().isoformat()
    for m in machines:
        mid = m["id"]
        R = rules_for(mid)
        st = db.q1("SELECT * FROM machine_state WHERE machine_id=?", (mid,))
        d = db.q1("SELECT * FROM machine_daily WHERE machine_id=? AND production_date=?", (mid, today))
        if not st:
            continue
        name = m["name"]

        # Subtracting the roots is load-bearing: machine_stopped is both a root and a
        # child of machine_offline, and without it a genuinely stopped machine would
        # suppress its own root alert and go silent.
        roots = _active_roots(mid, st, R, now)
        masked = {c for c, ps in SUPPRESSED_BY.items() if roots.intersection(ps)} - roots
        fired = {}
        raise_alert = _gate(name, masked, fired)   # local shadow -- gates every call below

        # --- connectivity -------------------------------------------------
        r = R.get("machine_offline")
        if r and (st["consecutive_fail"] or 0) >= (r["threshold"] or 3):
            raised += bool(raise_alert(
                r, mid, f"{name} is offline",
                f"{st['consecutive_fail']} consecutive failed heartbeats. Last error: "
                f"{st['last_error'] or 'unknown'}. Last contact {st['last_heartbeat_ok'] or 'never'}.",
                st["consecutive_fail"]))
        elif (st["consecutive_fail"] or 0) == 0:
            resolve_alerts(f"machine_offline:{mid}", "machine reachable again")

        r = R.get("heartbeat_lost")
        age = _age(st["last_heartbeat_ok"], now)
        if r and age is not None and age > (r["threshold"] or 300):
            raised += bool(raise_alert(r, mid, f"{name} heartbeat lost",
                                       f"No successful heartbeat for {round(age / 60)} minutes.", age))
        elif age is not None and age <= (r["threshold"] if r else 300):
            resolve_alerts(f"heartbeat_lost:{mid}", "heartbeat restored")

        r = R.get("slow_response")
        if r and st["latency_ms"] and st["latency_ms"] > (r["threshold"] or 1500):
            raised += bool(raise_alert(r, mid, f"{name} responding slowly",
                                       f"Heartbeat latency {round(st['latency_ms'])} ms.", st["latency_ms"]))
        elif st["latency_ms"] is not None:
            resolve_alerts(f"slow_response:{mid}", "latency back to normal")

        r = R.get("communication_failure")
        if r:
            win = (now - dt.timedelta(minutes=r["window_min"] or 60)).strftime("%Y-%m-%d %H:%M:%S")
            n = db.q1("SELECT COUNT(*) n FROM connection_log WHERE machine_id=? AND ok=0 AND ts>=?",
                      (mid, win))["n"]
            if n >= (r["threshold"] or 3):
                raised += bool(raise_alert(r, mid, f"{name} communication failures",
                                           f"{n} failed connections in the last {r['window_min']} minutes.", n))
            else:
                resolve_alerts(f"communication_failure:{mid}", "connections succeeding again")

        r = R.get("sync_failure")
        if r:
            win = (now - dt.timedelta(minutes=r["window_min"] or 60)).strftime("%Y-%m-%d %H:%M:%S")
            n = db.q1("SELECT COUNT(*) n FROM sync_log WHERE machine_id=? AND ok=0 AND ts>=?",
                      (mid, win))["n"]
            if n >= (r["threshold"] or 2):
                raised += bool(raise_alert(r, mid, f"{name} data synchronisation failing",
                                           f"{n} failed syncs in the last {r['window_min']} minutes.", n))
            else:
                resolve_alerts(f"sync_failure:{mid}", "synchronisation succeeding again")

        # --- production ---------------------------------------------------
        idle = st["idle_seconds"]
        r = R.get("machine_stopped")
        if r and st["status"] == "STOPPED" and st["online"] and idle and idle > (r["threshold"] or 1800):
            raised += bool(raise_alert(
                r, mid, f"{name} stopped unexpectedly",
                f"Online but no production for {round(idle / 60)} minutes. "
                f"Last job: {st['current_design'] or 'n/a'}.", idle))
        elif st["status"] == "PRODUCING":
            resolve_alerts(f"machine_stopped:{mid}", "production resumed")
            resolve_alerts(f"excessive_idle:{mid}", "production resumed")

        r = R.get("excessive_idle")
        if r and st["status"] == "IDLE" and idle and idle > (r["threshold"] or 900):
            raised += bool(raise_alert(r, mid, f"{name} idle too long",
                                       f"Idle for {round(idle / 60)} minutes.", idle))

        r = R.get("repeated_alarm")
        if r:
            win = (now - dt.timedelta(minutes=r["window_min"] or 60)).strftime("%Y-%m-%d %H:%M:%S")
            rows = db.q("SELECT description,COUNT(*) n FROM machine_alarms WHERE machine_id=? AND ts>=? "
                        "AND ts<=datetime('now','localtime') AND suspect=0 "   # UTC cap, see _active_roots
                        "AND severity IN ('warning','critical') GROUP BY description ORDER BY n DESC", (mid, win))
            tot = sum(x["n"] for x in rows)
            if tot >= (r["threshold"] or 5):
                top = ", ".join(f"{x['description']} x{x['n']}" for x in rows[:3])
                raised += bool(raise_alert(r, mid, f"{name} repeated alarms",
                                           f"{tot} alarms in {r['window_min']} minutes: {top}", tot))

        r = R.get("production_drop")
        dev = analytics.deviation(mid)
        if r and dev and dev["drop_pct"] >= (r["threshold"] or 40) and dev["expected"] > 5:
            raised += bool(raise_alert(
                r, mid, f"{name} production dropped",
                f"{dev['units']} units in hour {dev['hour'][11:]} vs {dev['expected']} expected "
                f"({dev['drop_pct']}% below its own baseline).", dev["drop_pct"],
                dedup=f"production_drop:{mid}:{dev['hour']}"))

        r = R.get("abnormal_behaviour")
        if r and dev and abs(dev["z"]) >= (r["threshold"] or 3) and dev["samples"] >= 5:
            raised += bool(raise_alert(
                r, mid, f"{name} behaving abnormally",
                f"Output {dev['units']} units is {dev['z']} sigma from its normal "
                f"{dev['expected']} for this hour of the week.", dev["z"],
                dedup=f"abnormal:{mid}:{dev['hour']}"))

        if d:
            r = R.get("performance_low")
            if r and d["performance"] is not None and d["performance"] < (r["threshold"] or 70) and d["jobs"] > 5:
                raised += bool(raise_alert(r, mid, f"{name} performance below threshold",
                                           f"Performance {round(d['performance'])}% "
                                           f"(threshold {round(r['threshold'])}%).", d["performance"],
                                           dedup=f"performance_low:{mid}:{today}"))
            r = R.get("excessive_downtime")
            dmin = ((d["down_seconds"] or 0) + (d["offline_seconds"] or 0)) / 60.0
            if r and dmin > (r["threshold"] or 120):
                raised += bool(raise_alert(r, mid, f"{name} excessive downtime",
                                           f"{round(dmin)} minutes of downtime today.", dmin,
                                           dedup=f"excessive_downtime:{mid}:{today}"))
            r = R.get("maintenance_risk")
            if r and d["health_score"] is not None and d["health_score"] < (r["threshold"] or 60):
                mr = analytics.maintenance_risk(mid)
                raised += bool(raise_alert(
                    r, mid, f"{name} health score low",
                    f"Health {round(d['health_score'])}/100. " + ("; ".join(mr["reasons"]) or "See maintenance view."),
                    d["health_score"], dedup=f"maintenance_risk:{mid}:{today}"))
            r = R.get("thermal_risk")
            tmax = max(d["temp_galvo_max"] or 0, d["temp_servo_max"] or 0)
            if r and tmax > (r["threshold"] or 55):
                raised += bool(raise_alert(r, mid, f"{name} temperature high",
                                           f"Peak galvo/servo temperature {round(tmax, 1)} degC today.", tmax,
                                           dedup=f"thermal_risk:{mid}:{today}"))
            r = R.get("target_risk")
            if r and d["target_units"]:
                f = analytics.forecast_machine(mid)
                if f["target_pct"] is not None and f["target_pct"] < (r["threshold"] or 85):
                    raised += bool(raise_alert(
                        r, mid, f"{name} at risk of missing target",
                        f"Forecast {f['projected_units']} of {f['target']} units "
                        f"({round(f['target_pct'])}% of target) by end of day.", f["target_pct"],
                        dedup=f"target_risk:{mid}:{today}"))

        r = R.get("head_drift")
        if r:
            mr = analytics.maintenance_risk(mid)
            if mr["head_drift_pct"] is not None and abs(mr["head_drift_pct"]) >= (r["threshold"] or 25):
                raised += bool(raise_alert(
                    r, mid, f"{name} scan-head calibration drift",
                    f"Galvo gain drifted {mr['head_drift_pct']}% versus its earlier calibration history.",
                    mr["head_drift_pct"], dedup=f"head_drift:{mid}:{today}"))

        r = R.get("laser_credit_low")
        if r:
            c = db.q1("SELECT balance_before,applied_at FROM laser_credit WHERE machine_id=? "
                      "ORDER BY source_id DESC LIMIT 1", (mid,))
            if c and c["balance_before"] is not None and c["balance_before"] < (r["threshold"] or 86400):
                raised += bool(raise_alert(
                    r, mid, f"{name} laser licence credit low",
                    f"Credit balance {round((c['balance_before'] or 0) / 3600, 1)} h at the last "
                    f"activation ({c['applied_at']}). Verify remaining rental time.",
                    c["balance_before"], dedup=f"laser_credit_low:{mid}:{today[:7]}"))

        r = R.get("data_quality")
        if r:
            win = (now - dt.timedelta(minutes=r["window_min"] or 60)).strftime("%Y-%m-%d %H:%M:%S")
            n = db.q1("SELECT COUNT(*) n FROM production WHERE machine_id=? AND suspect=1 AND collected_at>=?",
                      (mid, win))["n"]
            if n >= (r["threshold"] or 1):
                raised += bool(raise_alert(
                    r, mid, f"{name} suspect timestamps",
                    f"{n} job records rejected as out-of-range (machine clock drift). "
                    f"Check the machine's date/time settings.", n,
                    dedup=f"data_quality:{mid}:{today}"))

        # --- suppression: close the children, disclose them on the root ----
        titles = {p: _root_title(mid, p) for p in roots}
        by_parent = {}
        for typ in masked:
            parent = next(p for p in SUPPRESSED_BY[typ] if p in roots)
            if _supersede(mid, typ, titles[parent]) or typ in fired:
                by_parent.setdefault(parent, []).append(fired.get(typ, typ.replace("_", " ")))
        for parent in ROOT_TYPES:          # every root, so a note that no longer holds is dropped
            _mask_note(mid, parent, by_parent.get(parent, ()))
    sweep_stale()
    escalate()
    return raised


def _age(ts, now):
    if not ts:
        return None
    try:
        return (now - dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return None


STALE_HOURS = {                 # an alert of this type is meaningless once this old
    "production_drop": 4, "abnormal_behaviour": 4, "slow_response": 2,
    "communication_failure": 6, "sync_failure": 6, "repeated_alarm": 6,
    "data_quality": 24, "thermal_risk": 24, "performance_low": 24,
    "excessive_downtime": 24, "target_risk": 24, "maintenance_risk": 48,
    "head_drift": 72, "laser_credit_low": 168, "excessive_idle": 6,
}


def sweep_stale():
    """Close alerts whose condition can no longer be true.

    Most rules key on a time bucket (an hour of production, a given day), so the
    condition that opened them cannot recur or clear on its own -- without this
    they accumulate forever and the open-alert count stops meaning anything.
    """
    now = dt.datetime.now()
    closed = 0
    for a in db.q("SELECT id,type,started_at,machine_id FROM alerts WHERE status IN ('open','acknowledged')"):
        hours = STALE_HOURS.get(a["type"])
        if not hours:
            continue                       # connectivity/stopped types resolve from live state
        age = _age(a["started_at"], now)
        if age and age > hours * 3600:
            stamp = db.now()
            db.ex("UPDATE alerts SET status='resolved', resolved_at=?, ended_at=?, "
                  "resolution='auto-closed: condition window elapsed', "
                  "duration_seconds=CAST((julianday(?)-julianday(started_at))*86400 AS INT) WHERE id=?",
                  (stamp, stamp, stamp, a["id"]))
            closed += 1
    return closed


def escalate():
    """Bump un-acknowledged alerts to their escalation contacts."""
    now = dt.datetime.now()
    for a in db.q("SELECT a.*, r.escalate_after_min, r.escalate_to, r.channels FROM alerts a "
                  "LEFT JOIN alert_rules r ON r.id=a.rule_id "
                  "WHERE a.status='open' AND a.escalated_at IS NULL AND r.escalate_after_min IS NOT NULL"):
        age = _age(a["started_at"], now)
        if age and age > a["escalate_after_min"] * 60:
            db.ex("UPDATE alerts SET escalated_at=? WHERE id=?", (db.now(), a["id"]))
            send(a["escalate_to"], f"[ESCALATED] {a['title']}",
                 f"{a['message']}\n\nUnacknowledged for {round(age / 60)} minutes.",
                 a["channels"] or "email", a["id"])


# ---------------------------------------------------------- notifications --

def notify(alert_id, rule):
    if db.get_setting("notifications_enabled") != "1":
        return
    a = db.q1("SELECT * FROM alerts WHERE id=?", (alert_id,))
    if not a:
        return
    channels = (rule["channels"] if rule and rule["channels"] else "app")
    recipients = (rule["recipients"] if rule else None) or _default_recipients(a["severity"])
    body = (f"{a['message']}\n\n"
            f"Machine: {_mname(a['machine_id'])}\nSeverity: {a['severity'].upper()}\n"
            f"Started: {a['started_at']}\nType: {a['type']}")
    send(recipients, f"[{a['severity'].upper()}] {a['title']}", body, channels, alert_id)
    db.ex("UPDATE alerts SET notified=1 WHERE id=?", (alert_id,))


def _mname(mid):
    r = db.q1("SELECT name FROM machines WHERE id=?", (mid,)) if mid else None
    return r["name"] if r else "-"


def _default_recipients(severity):
    roles = "'admin','manager','maintenance','it','production'" if severity == "critical" else "'admin','it'"
    rows = db.q(f"SELECT email FROM users WHERE active=1 AND email IS NOT NULL AND role IN ({roles})")
    return ",".join(r["email"] for r in rows if r["email"])


def send(recipients, subject, body, channels="app", alert_id=None):
    chans = [c.strip() for c in (channels or "app").split(",") if c.strip()]
    for ch in chans:
        if ch == "app":
            _log(alert_id, "app", "-", True, "in-app")
        elif ch == "email":
            for to in [r.strip() for r in (recipients or "").split(",") if r.strip()]:
                ok, detail = send_email(to, subject, body)
                _log(alert_id, "email", to, ok, detail)
        elif ch == "teams":
            ok, detail = send_teams(subject, body)
            _log(alert_id, "teams", db.get_setting("teams_webhook", "")[:60], ok, detail)
        elif ch == "whatsapp":
            for to in [r.strip() for r in (recipients or "").split(",") if r.strip()]:
                ok, detail = send_whatsapp(to, f"{subject}\n{body}")
                _log(alert_id, "whatsapp", to, ok, detail)


def _log(alert_id, channel, recipient, ok, detail):
    db.ex("INSERT INTO notifications(alert_id,channel,recipient,ts,ok,detail) VALUES(?,?,?,?,?,?)",
          (alert_id, channel, recipient, db.now(), 1 if ok else 0, (detail or "")[:400]))


def send_email(to, subject, body, html=None):
    """Brevo transactional email."""
    key = db.get_setting("brevo_api_key")
    if not key:
        return False, "brevo api key not configured"
    payload = {
        "sender": {"email": db.get_setting("brevo_sender_email"),
                   "name": db.get_setting("brevo_sender_name")},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html or f"<pre style='font:14px system-ui'>{_esc(body)}</pre>",
        "textContent": body,
    }
    try:
        r = httpx.post(BREVO_URL, json=payload, timeout=20,
                       headers={"api-key": key, "accept": "application/json"})
        return r.status_code < 300, f"{r.status_code} {r.text[:200]}"
    except Exception as e:                                    # noqa: BLE001
        return False, str(e)[:200]


def send_teams(title, body):
    url = db.get_setting("teams_webhook")
    if not url:
        return False, "teams webhook not configured"
    try:
        r = httpx.post(url, json={"@type": "MessageCard", "@context": "http://schema.org/extensions",
                                  "summary": title, "title": title, "text": body.replace("\n", "\n\n")},
                       timeout=20)
        return r.status_code < 300, f"{r.status_code}"
    except Exception as e:                                    # noqa: BLE001
        return False, str(e)[:200]


def send_whatsapp(to, message):
    """Generic gateway POST — set whatsapp_endpoint/whatsapp_token to whatever is approved."""
    url = db.get_setting("whatsapp_endpoint")
    if not url:
        return False, "whatsapp endpoint not configured"
    try:
        r = httpx.post(url, json={"to": to, "message": message}, timeout=20,
                       headers={"Authorization": f"Bearer {db.get_setting('whatsapp_token')}"})
        return r.status_code < 300, f"{r.status_code}"
    except Exception as e:                                    # noqa: BLE001
        return False, str(e)[:200]


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_channel(channel, recipient):
    subject = "T&C Laser Command Center — test notification"
    body = f"This is a test notification sent at {db.now()}."
    if channel == "email":
        return send_email(recipient, subject, body)
    if channel == "teams":
        return send_teams(subject, body)
    if channel == "whatsapp":
        return send_whatsapp(recipient, f"{subject}\n{body}")
    return False, "unknown channel"


def summary(days=30):
    since = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return {
        "open": [dict(r) for r in db.q(
            "SELECT a.*, m.name machine_name FROM alerts a LEFT JOIN machines m ON m.id=a.machine_id "
            "WHERE a.status IN ('open','acknowledged') ORDER BY "
            "CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.started_at DESC")],
        "by_type": [dict(r) for r in db.q(
            "SELECT type, severity, COUNT(*) n, AVG(duration_seconds)/60.0 avg_minutes FROM alerts "
            "WHERE started_at >= ? GROUP BY type,severity ORDER BY n DESC", (since,))],
        "by_machine": [dict(r) for r in db.q(
            "SELECT a.machine_id, m.name, COUNT(*) n, SUM(a.severity='critical') critical "
            "FROM alerts a LEFT JOIN machines m ON m.id=a.machine_id WHERE a.started_at >= ? "
            "GROUP BY a.machine_id ORDER BY n DESC", (since,))],
        "daily": [dict(r) for r in db.q(
            "SELECT substr(started_at,1,10) d, COUNT(*) n, SUM(severity='critical') critical "
            "FROM alerts WHERE started_at >= ? GROUP BY d ORDER BY d", (since,))],
    }
