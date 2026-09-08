"""Root-cause alert suppression check. Run: python test_alert_suppression.py

Uses a throwaway database so it never touches production data.
"""
import datetime as dt
import os
import sys
import tempfile

os.environ["LASER_DATA"] = tempfile.mkdtemp(prefix="laser-suppress-")
os.environ.setdefault("LEGACY_CENTRAL_DB", os.path.join(os.environ["LASER_DATA"], "none.db"))

from app import alerts, db  # noqa: E402

db.init_db()
STAMP = db.now()
M = 902          # scratch machine id, well clear of the real fleet


def seed_dark_machine():
    """A machine whose PC is switched off, with every connectivity symptom really true."""
    now = dt.datetime.now()
    old = (now - dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    db.ex("INSERT OR REPLACE INTO machines(id,name,host,connection_method,enabled,status,"
          "target_units_day,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (M, "Dark Machine", "10.100.0.99", "smb", 1, "active", 100, STAMP, STAMP))
    db.ex("INSERT OR REPLACE INTO machine_state(machine_id,status,connectivity,online,last_heartbeat,"
          "last_heartbeat_ok,latency_ms,consecutive_fail,last_error,idle_seconds,updated_at) "
          "VALUES(?,'OFFLINE','DISCONNECTED',0,?,?,?,?,?,?,?)",
          (M, STAMP, old, 10000.5, 64, "connection timed out", 12000.0, STAMP))
    for i in range(10):                       # failed probes -> communication_failure + sync_failure
        ts = (now - dt.timedelta(minutes=5 * i + 1)).strftime("%Y-%m-%d %H:%M:%S")
        db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,latency_ms,detail) VALUES(?,?,?,0,?,?)",
              (M, ts, "heartbeat", 10000.5, "timeout"))
        db.ex("INSERT INTO sync_log(ts,machine_id,source,ok,error) VALUES(?,?,?,0,?)",
              (ts, M, "stats", "timeout"))
    day = dt.date.today().isoformat()         # a hot servo measured while it was still running
    db.ex("INSERT OR REPLACE INTO machine_daily(machine_id,production_date,jobs,units,down_seconds,"
          "offline_seconds,performance,health_score,temp_servo_max,target_units,calculated_at) "
          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (M, day, 20, 40, 3600, 18000, 12.0, 28.0, 61.5, 100, STAMP))


def t_offline_masks_its_consequences():
    seed_dark_machine()
    # an already-open child from a previous cycle must be superseded, not left dangling
    rule = db.q1("SELECT * FROM alert_rules WHERE type='slow_response'")
    stale = alerts.raise_alert(rule, M, "Dark Machine responding slowly", "old latency alert",
                               10000.5, dedup=f"slow_response:{M}")
    assert stale, "could not seed the pre-existing child alert"

    alerts.evaluate(M)

    open_types = {r["type"] for r in db.q(
        "SELECT type FROM alerts WHERE machine_id=? AND status IN ('open','acknowledged')", (M,))}
    conn = open_types & {"machine_offline", "heartbeat_lost", "communication_failure",
                         "sync_failure", "slow_response"}
    assert conn == {"machine_offline"}, f"expected one connectivity alert, got {sorted(conn)}"
    assert "thermal_risk" in open_types, f"independent fault was suppressed: {sorted(open_types)}"
    assert not (open_types & set(alerts.SUPPRESSED_BY)), f"a masked child stayed open: {sorted(open_types)}"

    s = db.q1("SELECT status,resolution FROM alerts WHERE id=?", (stale,))
    assert s["status"] == "resolved", s["status"]
    assert s["resolution"].startswith("superseded by "), s["resolution"]

    msg = db.q1("SELECT message FROM alerts WHERE machine_id=? AND type='machine_offline' "
                "AND status='open'", (M,))["message"]
    assert alerts.MASK_NOTE.strip() in msg and msg.endswith("."), msg
    for expected in ("heartbeat lost", "data synchronisation failing"):
        assert expected in msg, f"{expected!r} missing from mask note: {msg}"

    alerts.evaluate(M)                        # second pass must not double the sentence
    msg2 = db.q1("SELECT message FROM alerts WHERE machine_id=? AND type='machine_offline' "
                 "AND status='open'", (M,))["message"]
    assert msg2.count(alerts.MASK_NOTE) == 1, msg2


if __name__ == "__main__":
    try:
        t_offline_masks_its_consequences()
    except AssertionError as e:
        print(f"FAIL  offline masks its consequences: {e}")
        sys.exit(1)
    print("ok    offline masks its consequences")
