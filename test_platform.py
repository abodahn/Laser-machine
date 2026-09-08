"""Self-check for the laser platform. Run: python test_platform.py

Uses a throwaway database so it never touches production data.
"""
import datetime as dt
import os
import sys
import tempfile

os.environ["LASER_DATA"] = tempfile.mkdtemp(prefix="laser-test-")
os.environ.setdefault("LEGACY_CENTRAL_DB", os.path.join(os.environ["LASER_DATA"], "none.db"))

from app import alerts, analytics, auth, collector, config, db, reports  # noqa: E402

db.init_db()
STAMP = db.now()


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"FAIL  {name}: {e}")
        return 1
    except Exception as e:                                    # noqa: BLE001
        print(f"ERROR {name}: {type(e).__name__}: {e}")
        return 1
    print(f"ok    {name}")
    return 0


# ------------------------------------------------------------------- shifts

def t_shifts():
    assert db.shift_of("2026-09-03 06:00:00") == ("A", "2026-09-03")
    assert db.shift_of("2026-09-03 13:59:59") == ("A", "2026-09-03")
    assert db.shift_of("2026-09-03 14:00:00") == ("B", "2026-09-03")
    assert db.shift_of("2026-09-03 22:00:00") == ("C", "2026-09-03")
    # a night shift that runs past midnight belongs to the day it started
    assert db.shift_of("2026-09-03 02:30:00") == ("C", "2026-09-02")
    assert db.shift_of("2026-09-03 00:00:00") == ("C", "2026-09-02")
    assert db.shift_of("") == (None, "")
    assert db.shift_of(None) == (None, "")


def t_design_parse():
    style, size, color = db.parse_design("8307-244-800-BLACK-36.jean5")
    assert (style, size, color) == ("8307-244-800", "36", "BLACK"), (style, size, color)
    assert db.parse_design("Dragon dark-46.jean5")[1] == "46"
    assert db.parse_design("MARQUEE WASHED BLACK-40-OK.jean5")[1] == "40"
    assert db.parse_design(None) == (None, None, None)
    assert db.parse_design("Production 1")[0] == "Production 1"


def t_elapsed():
    assert db._elapsed("2026-09-03 10:00:00", "2026-09-03 10:01:30") == 90
    assert db._elapsed("2026-09-03 10:00:00", "") is None        # job still running
    assert db._elapsed("2026-09-03 10:00:00", None) is None
    assert db._elapsed("2026-09-03 10:00:00", "2026-09-01 10:00:00") is None   # negative
    assert db._elapsed("bad", "worse") is None


def t_norm_ts():
    assert collector.norm_ts("20210224::135700::0618") == "2021-02-24 13:57:00"
    assert collector.norm_ts("2026-09-03 12:38:42") == "2026-09-03 12:38:42"
    assert collector.norm_ts("") is None
    assert collector.norm_ts(None) is None


def t_alarm_classification():
    assert collector.classify("Water alarm.", -3, "marking") == "critical"
    assert collector.classify("Water flow below limit.", 14, "laser") == "critical"
    assert collector.classify("Shutter not opened.", -10, "marking") == "warning"
    assert collector.classify("Cloud disconnected.", 1, "system") == "info"
    assert collector.classify("No shapes to mark or incorrect size.", -25, "marking") == "info"


# --------------------------------------------------------------- ingestion

M = 901          # scratch machine id, well clear of the real fleet


def seed():
    db.ex("INSERT OR REPLACE INTO machines(id,name,host,connection_method,enabled,status,"
          "target_units_day,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (M, "Test Machine", "127.0.0.1", "smb", 1, "active", 100, STAMP, STAMP))
    db.ex("INSERT OR REPLACE INTO machine_state(machine_id,status,connectivity,updated_at) "
          "VALUES(?,?,?,?)", (M, "UNKNOWN", "DISCONNECTED", STAMP))
    day = dt.date.today().isoformat()
    rows = []
    for i in range(20):                                       # 20 jobs, 10 units each, 08:00 onward
        start = f"{day} {8 + i // 4:02d}:{(i % 4) * 12:02d}:00"
        end = f"{day} {8 + i // 4:02d}:{(i % 4) * 12 + 10:02d}:00"      # 600 s each
        rows.append({"id": i + 1, "init_date": start, "end_date": end, "design": "TEST-32.jean5",
                     "copies": 10, "units": 10, "laser_time": 400_000, "user": "tester",
                     "alarm1": 1 if i == 3 else 0, "alarm2": 0})
    collector.ingest_push(M, rows)


def t_ingest():
    seed()
    n = db.q1("SELECT COUNT(*) n FROM production WHERE machine_id=?", (M,))["n"]
    assert n == 20, n
    r = db.q1("SELECT * FROM production WHERE machine_id=? AND source_id=1", (M,))
    assert r["units"] == 10 and r["laser_ms"] == 400_000
    assert r["elapsed_seconds"] == 600, r["elapsed_seconds"]
    assert abs(r["laser_duty"] - 400 / 600) < 0.001, r["laser_duty"]
    assert r["operator"] == "tester" and r["size"] == "32"
    assert r["shift"] == "A"


def t_late_end_date():
    """A job is first seen with no end_date; the next sync must fill it in."""
    day = dt.date.today().isoformat()
    open_job = [{"id": 99, "init_date": f"{day} 20:00:00", "end_date": "", "design": "TEST-32.jean5",
                 "copies": 5, "units": 5, "laser_time": 100_000}]
    collector.ingest_push(M, open_job)
    r = db.q1("SELECT end_date, elapsed_seconds FROM production WHERE machine_id=? AND source_id=99", (M,))
    assert not r["end_date"] and r["elapsed_seconds"] is None, dict(r)
    open_job[0]["end_date"] = f"{day} 20:05:00"
    collector.ingest_push(M, open_job)
    r = db.q1("SELECT end_date, elapsed_seconds FROM production WHERE machine_id=? AND source_id=99", (M,))
    assert r["end_date"] == f"{day} 20:05:00", dict(r)
    assert r["elapsed_seconds"] == 300, dict(r)


def t_clock_drift_rejected():
    future = (dt.datetime.now() + dt.timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S")
    collector.ingest_push(M, [{"id": 500, "init_date": future, "end_date": "", "design": "X.jean5",
                               "copies": 1, "units": 1, "laser_time": 1000}])
    r = db.q1("SELECT suspect FROM production WHERE machine_id=? AND source_id=500", (M,))
    assert r["suspect"] == 1, "a job dated 400 days in the future must be flagged suspect"
    day = dt.date.today().isoformat()
    analytics.rebuild_hourly(2, M)
    analytics.rebuild_daily(2, M)
    d = db.q1("SELECT units FROM machine_daily WHERE machine_id=? AND production_date=?", (M, day))
    assert d["units"] == 205, f"suspect rows must be excluded from KPIs, got {d['units']}"


# ---------------------------------------------------------------- analytics

def t_rollups():
    day = dt.date.today().isoformat()
    analytics.rebuild_hourly(2, M)
    analytics.rebuild_daily(2, M)
    d = db.q1("SELECT * FROM machine_daily WHERE machine_id=? AND production_date=?", (M, day))
    assert d is not None, "no daily rollup produced"
    assert d["jobs"] == 21 and d["units"] == 205, dict(d)      # 20 x 10 + the 5-unit late job
    assert abs(d["busy_seconds"] - 12300) < 1, d["busy_seconds"]          # 20*600 + 300
    assert abs(d["laser_seconds"] - 8100) < 1, d["laser_seconds"]         # 20*400 + 100
    assert d["jobs_with_alarm"] == 1, d["jobs_with_alarm"]
    assert 0 < d["utilization"] <= 100, d["utilization"]
    assert d["target_units"] == 100 and d["target_pct"] == 205.0, dict(d)
    assert abs(d["laser_duty"] - 8100 / 12300) < 0.001, d["laser_duty"]
    assert d["quality_proxy"] is not None and d["quality_proxy"] < 100    # one job had an alarm


def t_health_bounded():
    day = dt.date.today().isoformat()
    d = db.q1("SELECT health_score FROM machine_daily WHERE machine_id=? AND production_date=?", (M, day))
    assert d["health_score"] is not None, "health score not computed"
    assert 0 <= d["health_score"] <= 100, d["health_score"]
    hb = analytics.health_breakdown(M, day)
    assert set(hb["components"]) == {"connectivity", "alarms", "downtime", "utilization",
                                     "performance", "thermal"}, hb["components"]
    for k, v in hb["components"].items():
        assert 0 <= v <= 100, f"{k}={v} out of range"


def t_forecast():
    f = analytics.forecast_machine(M)
    assert f["projected_units"] >= f["units_so_far"], f
    assert f["low"] <= f["projected_units"] <= f["high"], f
    assert f["target"] == 100 and f["target_pct"] is not None


def t_hour_of_week():
    # Sunday must map to 0..23 and the mapping must agree with rebuild_baselines
    sunday = dt.datetime(2026, 9, 6, 5, 0)                    # 2026-09-06 is a Sunday
    assert sunday.weekday() == 6
    assert analytics.hour_of_week(sunday) == 5, analytics.hour_of_week(sunday)
    monday = dt.datetime(2026, 9, 7, 0, 0)
    assert analytics.hour_of_week(monday) == 24, analytics.hour_of_week(monday)


def t_intelligence_queries():
    for fn in (analytics.department_overview, analytics.attention_list):
        fn()
    for fn, a in ((analytics.ranking, (7,)), (analytics.bottleneck, (7,)), (analytics.capacity, (7,)),
                  (analytics.downtime_analysis, (7,)), (analytics.alarm_analysis, (7,)),
                  (analytics.shift_performance, (7,)), (analytics.operator_performance, (7,)),
                  (analytics.design_performance, (7,)), (analytics.loss_analysis, ())):
        fn(*a)
    mr = analytics.maintenance_risk(M)
    assert mr["level"] in ("low", "medium", "high") and 0 <= mr["risk"] <= 100


# ------------------------------------------------------------------ alerts

def t_alert_lifecycle():
    rule = db.q1("SELECT * FROM alert_rules WHERE type='machine_stopped'")
    aid = alerts.raise_alert(rule, M, "Test stopped", "unit test", 1800, dedup="test:stopped")
    assert aid, "alert was not raised"
    again = alerts.raise_alert(rule, M, "Test stopped", "unit test", 1800, dedup="test:stopped")
    assert again is None, "duplicate alert must be suppressed while the first is open"
    alerts.acknowledge(aid, "tester", "looking at it")
    assert db.q1("SELECT status FROM alerts WHERE id=?", (aid,))["status"] == "acknowledged"
    alerts.resolve(aid, "tester", "fixed")
    a = db.q1("SELECT * FROM alerts WHERE id=?", (aid,))
    assert a["status"] == "resolved" and a["resolved_at"] and a["duration_seconds"] is not None


def t_alert_engine_runs():
    n = alerts.evaluate(M)
    assert isinstance(n, int)


def t_notification_fails_safe():
    """With no Brevo key configured, sending must fail cleanly and never raise."""
    ok, detail = alerts.send_email("nobody@example.com", "s", "b")
    assert ok is False and "api key" in detail.lower(), detail
    alerts.send("a@b.com", "subject", "body", "app,email,teams,whatsapp", None)   # must not raise


# ------------------------------------------------------------------- other

def t_credentials_encrypted():
    sample = "not-a-real-password"        # never use a live credential as test data
    token = config.encrypt(sample)
    assert token != sample and config.decrypt(token) == sample
    assert config.decrypt("not-a-token") == ""
    assert config.decrypt(None) == ""


def t_rbac():
    assert auth.can({"role": "admin"}, "credentials")
    assert not auth.can({"role": "manager"}, "credentials")
    assert not auth.can({"role": "viewer"}, "config")
    assert not auth.can(None, "reports")
    assert auth.can({"role": "maintenance"}, "ack")


def t_password_hashing():
    h, salt = auth.hash_password("correct horse")
    assert auth.hash_password("correct horse", salt)[0] == h
    assert auth.hash_password("wrong horse", salt)[0] != h
    assert h != "correct horse" and len(h) == 64


def t_reports_render():
    for name in reports.REPORTS:
        rep = reports.build(name, days=7, date=dt.date.today().isoformat())
        assert "title" in rep and "sections" in rep, name
        x = reports.to_xlsx(rep)
        assert x[:2] == b"PK", f"{name}: not a valid xlsx"
        p = reports.to_pdf(rep)
        assert p[:5] == b"%PDF-", f"{name}: not a valid pdf"


def t_settings_roundtrip():
    db.set_setting("idle_threshold_seconds", 456)
    assert db.get_int("idle_threshold_seconds") == 456
    db.set_setting("idle_threshold_seconds", 300)
    assert db.get_int("nonexistent_key", 7) == 7


def t_collector_thread_leak_bounded():
    """A machine whose call never returns must burn one thread, not one per cycle.

    This is what took the fleet offline: unbounded re-dispatch filled the executor
    and every machine, healthy or not, then timed out.
    """
    import asyncio
    import threading

    from app import main as appmain

    started = threading.Semaphore(0)
    release = threading.Event()
    calls = []

    def hangs(mid):
        calls.append(mid)
        started.release()
        release.wait(30)          # simulates a wedged SMB call

    hangs.__name__ = "heartbeat"

    async def drive():
        # ten scheduling cycles against the same unreachable machine
        for _ in range(10):
            await appmain._run(hangs, 99, deadline=0.2, key=("hb", 99))
        # and a healthy machine must still get through while that one is stuck
        ok = []
        await appmain._run(lambda mid: ok.append(mid), 1, deadline=5, key=("hb", 1))
        return ok

    try:
        ok = asyncio.run(drive())
        assert started.acquire(timeout=5), "the stuck call never started"
        assert len(calls) == 1, f"re-dispatched a stuck machine {len(calls)} times — thread leak is unbounded"
        assert ok == [1], "a healthy machine was starved while another was stuck"
    finally:
        release.set()


if __name__ == "__main__":
    tests = [(n[2:].replace("_", " "), f) for n, f in sorted(globals().items())
             if n.startswith("t_") and callable(f)]
    # ingestion order matters: seed first, then the rollups that read it
    order = ["ingest", "late end date", "clock drift rejected", "rollups"]
    tests.sort(key=lambda t: order.index(t[0]) if t[0] in order else 99)
    failures = sum(check(name, fn) for name, fn in tests)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
