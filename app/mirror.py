"""Collector -> mirror replication.

Render is public cloud and cannot reach 10.100.x.x, so the collector stays on the
factory PC and pushes a compact projection of the database to a read-only mirror.
Both halves live here: the on-prem pusher and the mirror-side apply().

The projection is a list of SLICES. A slice is a table plus a WHERE clause; it is
hashed, and only slices whose hash differs from what the mirror reports are sent.
Hot/cold splits exist purely so a row that churns every minute (today's rollups,
open alerts) does not drag 90 days of history along with it.
"""
import datetime as dt
import gzip
import hashlib
import json
import logging
import re

import httpx

from . import config, db

log = logging.getLogger("laser.mirror")

# Everything that could point the server at a host or let it authenticate there.
# Mirrored as explicit NULL, never omitted: leaving a column out of the INSERT
# makes SQLite fill the schema DEFAULT, which republishes username='Admin',
# share='JeanologiaDB', db_file='stats.db', port=445 on the public internet.
BLANK = ("host", "port", "share", "db_file", "username", "password_enc", "push_token")

# (name, table, where, columns to NULL out).  ":d" is the collector's local date,
# sent with the payload — the mirror runs UTC and must not recompute it.
SLICES = [
    ("machines",         "machines",         "", BLANK),
    ("machine_identity", "machine_identity", "", ("raw_json",)),   # a fat blob nothing reads
    ("machine_state",    "machine_state",    "", ()),
    ("shifts",           "shifts",           "", ()),
    # recipients/escalate_to are operator emails and Teams/WhatsApp webhook ids —
    # bearer secrets. The mirror has no alerts loop, so it never needs them.
    ("alert_rules",      "alert_rules",      "", ("recipients", "escalate_to")),
    ("alerts_open",      "alerts", "WHERE status IN ('open','acknowledged')", ()),
    ("alerts_closed",    "alerts", "WHERE status NOT IN ('open','acknowledged') "
                                   "AND started_at >= date(:d,'-30 day')", ()),
    ("daily_hot",        "machine_daily",  "WHERE production_date >= date(:d,'-2 day')", ()),
    ("daily_cold",       "machine_daily",  "WHERE production_date >= date(:d,'-90 day') "
                                           "AND production_date < date(:d,'-2 day')", ()),
    ("hourly_hot",       "machine_hourly", "WHERE production_date >= date(:d,'-2 day')", ()),
    ("hourly_cold",      "machine_hourly", "WHERE production_date >= date(:d,'-14 day') "
                                           "AND production_date < date(:d,'-2 day')", ()),
    # store_forecasts only prunes horizon='eod', so the table accumulates ~1.8k stale
    # day+1 rows a day. Every reader filters target_date=today anyway.
    ("forecast",         "forecast", "WHERE target_date >= date(:d)", ()),
    ("machine_baseline", "machine_baseline", "", ()),
    ("design_stats",     "design_stats",     "", ()),
    ("machine_optics",   "machine_optics",   "", ()),
    ("downtime",         "downtime", "WHERE production_date >= date(:d,'-90 day')", ()),
    ("state_history",    "state_history", "WHERE start_time >= datetime(:d,'-14 day')", ()),
]
_BY_NAME = {s[0]: s for s in SLICES}

# NOT mirrored, deliberately: production (871k) and machine_alarms (1.5M) are too big
# and only feed per-job drill-downs; settings holds the Brevo/WhatsApp/Teams keys;
# users/sessions/audit_log/connection_log/sync_log are on-prem operational data.


def _hash(rows) -> str:
    return hashlib.sha256(_dumps(rows).encode()).hexdigest()[:16]


def _dumps(obj) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"), sort_keys=False)


def _redactor():
    r"""Literal host/share/db_file values, and a regex that finds them inside free text.

    Blanking the columns is not enough: os.stat and `net use` quote the UNC path they
    failed on, heartbeat writes that into machine_state.last_error, and the alert engine
    copies it into alerts.message — so '\\10.100.3.32\JeanologiaDB\stats.db' was reaching
    the public cloud through columns BLANK never touches. A string that IS exactly one of
    the values is left alone: only machine 25's serial_number (= its own NetBIOS name)
    hits that, and redacting every serial to protect one is the worse trade. Usernames
    stay out of the pattern — `net use` never echoes them and 'Admin' collides with
    ordinary text."""
    vals = {str(v) for r in db.q("SELECT host,share,db_file FROM machines")
            for v in tuple(r) if v}
    # A whole UNC run goes first so it wins the alternation and takes host+share+file in
    # one bite — that also covers a machine since deleted from the table, whose host the
    # literals no longer know but whose old alerts still quote it.
    pat = [r"\\{2,}[^\s'\"]*"] + [re.escape(v) for v in sorted(vals, key=len, reverse=True)]
    return vals, re.compile("|".join(pat), re.I)


def snapshot(have=None):
    """The compact payload. With `have` ({slice: hash}) only differing slices are built."""
    today = dt.date.today().isoformat()
    vals, rx = _redactor()
    out = {}
    for name, table, where, blank in SLICES:
        rows = [dict(r) for r in db.q(f"SELECT * FROM {table} {where}", {"d": today})]
        for r in rows:
            for k in blank:
                r[k] = None
            if rx:
                for k, v in r.items():
                    if type(v) is str and v not in vals:
                        r[k] = rx.sub("<redacted>", v)
        h = _hash(rows)
        if not have or have.get(name) != h:
            out[name] = {"hash": h, "rows": rows}
    return {"d": today, "slices": out}


def table_hashes():
    """{slice: hash} of everything, for change detection and for eyeballing drift."""
    return {n: s["hash"] for n, s in snapshot()["slices"].items()}


# ------------------------------------------------------------- mirror side --

def have():
    """What the mirror already holds. It echoes the hash the pusher supplied rather
    than recomputing: the collector's date(:d) windows are Cairo, the mirror is UTC,
    so a recomputed hash would never match and every cycle would resend everything."""
    return {r["key"].split(":", 1)[1]: r["value"] for r in
            db.q("SELECT key,value FROM settings WHERE key LIKE 'mirror_hash:%'")}


def apply(payload):
    """Replace each named slice transactionally. Returns {slice: rows written}."""
    d = payload.get("d") or dt.date.today().isoformat()
    applied = {}
    for name, sl in (payload.get("slices") or {}).items():
        spec = _BY_NAME.get(name)
        if not spec:
            log.warning("ignoring unknown slice %r", name)     # SQL comes from SLICES, never the wire
            continue
        _, table, where, _ = spec
        rows = sl.get("rows") or []
        c = db.connect()
        known = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        cols = [k for k in (rows[0] if rows else {}) if k in known]
        with db._write_lock:
            c.commit()                        # PRAGMA foreign_keys is a no-op inside a transaction
            # ponytail: FK off for the apply. The mirror is a projection, not a source of
            # truth, and REPLACE on machines would cascade-delete machine_state. The live
            # on-prem DB also already fails PRAGMA foreign_key_check (orphan
            # machine_identity for machine_id=999). Turn this back on once that is cleaned up.
            c.execute("PRAGMA foreign_keys=OFF")
            try:
                c.execute(f"DELETE FROM {table} {where}", {"d": d})
                if cols:
                    ph = ",".join("?" * len(cols))
                    c.executemany(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({ph})",
                                  [[r.get(k) for k in cols] for r in rows])
                # same transaction as the data: a crash must never leave a hash claiming
                # rows that were rolled back
                c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                          (f"mirror_hash:{name}", str(sl.get("hash") or ""), db.now()))
                # what actually landed, not what we were handed: push_once compares this
                # with what it sent, so a REPLACE collapse or a row that no longer matches
                # the slice WHERE is reported instead of passing a check on itself
                applied[name] = c.execute(f"SELECT COUNT(*) FROM {table} {where}",
                                          {"d": d}).fetchone()[0]
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.execute("PRAGMA foreign_keys=ON")
    return applied


# --------------------------------------------------------------- collector --

def push_once():
    """One replication cycle. Never raises — a dead mirror must not disturb collection.

    The mirror is asked what it holds every cycle rather than us remembering: Render's
    free disk is ephemeral, and a cached answer would go stale exactly when the mirror
    was wiped during a quiet minute — nothing dirty, so no push, so we would never learn
    it had emptied. The GET is ~600 B and makes the whole class of staleness impossible."""
    url, hdr = config.MIRROR_URL, {"X-Mirror-Token": config.MIRROR_TOKEN}
    # The token is a bearer secret and httpx does not follow redirects, so a typo'd
    # http:// URL hands it to the whole network path on the very first GET while the
    # push only ever logs "failed". Loopback stays allowed for the smoke test.
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1")
            or url.startswith("http://localhost")):
        log.error("refusing to push: LASER_MIRROR_URL must be https, got %r", url)
        return 0
    try:
        r = httpx.get(f"{url}/api/mirror/state", headers=hdr, timeout=30)
        r.raise_for_status()
        snap = snapshot(r.json().get("have") or {})
        if not snap["slices"]:
            return 0
        body = gzip.compress(_dumps(snap).encode(), 6)
        r = httpx.post(f"{url}/api/mirror/push", content=body, timeout=120,
                       headers={**hdr, "Content-Type": "application/json",
                                "Content-Encoding": "gzip"})
        r.raise_for_status()
        res = r.json()
        for n, sl in snap["slices"].items():
            got = (res.get("applied") or {}).get(n)
            if got != len(sl["rows"]):
                log.warning("mirror wrote %s rows for %s, we sent %s", got, n, len(sl["rows"]))
        log.info("mirror push: %s slice(s), %.1f KiB (%s)",
                 len(snap["slices"]), len(body) / 1024, ",".join(snap["slices"]))
        return len(snap["slices"])
    except Exception as e:                                    # noqa: BLE001
        log.warning("mirror push failed: %s", e)
        return 0


if __name__ == "__main__":                                    # python -m app.mirror
    import pathlib
    import sys

    if len(sys.argv) > 1:
        # Child half, run with LASER_DATA pointing at a scratch dir: apply the parent's
        # snapshot into an empty mirror and check the things that fail silently.
        db.init_db()
        payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
        sent = {k: len(v["rows"]) for k, v in payload["slices"].items()}
        n = apply(payload)
        assert db.q1("SELECT COUNT(*) c FROM machines WHERE COALESCE(host,port,share,db_file,"
                     "username,password_enc,push_token) IS NOT NULL")["c"] == 0,             "a credential reached the mirror"
        # apply() reports what the table HOLDS, so it has to be compared with what was SENT.
        # Re-COUNTing the same table here would only check a query against itself.
        assert n == sent, f"rows lost in apply: {[(k, sent[k], n.get(k)) for k in sent if n.get(k) != sent[k]]}"
        # What PRAGMA foreign_keys=OFF is actually there for: an INCREMENTAL push where only
        # `machines` is dirty. A full push hides the cascade because machine_state is
        # re-inserted one slice later; an incremental one leaves the mirror with zero
        # machine_state rows until that slice next changes.
        apply({"d": payload["d"], "slices": {"machines": payload["slices"]["machines"]}})
        for t in ("machine_state", "machine_identity"):
            assert db.q1(f"SELECT COUNT(*) c FROM {t}")["c"] == sent[t],                 f"{t} rows lost — a machines-only push cascade-deleted its children"
        assert have() == {k: v["hash"] for k, v in payload["slices"].items()},             "stored hashes do not match what was applied"
        print(f"apply round-trip OK: {len(n)} slices, {sum(n.values())} rows")
        sys.exit(0)

    import os
    import subprocess
    import tempfile

    db.init_db()
    snap = snapshot()
    body = gzip.compress(_dumps(snap).encode(), 6)
    for n, sl in snap["slices"].items():
        print(f"{n:18} {len(sl['rows']):>6} rows  {sl['hash']}")
    print(f"full snapshot: {len(body)/1024:.1f} KiB gzipped")

    for r in snap["slices"]["machines"]["rows"]:
        assert all(k in r for k in BLANK), "credential column omitted — SQLite would refill the DEFAULT"
        assert all(r[k] is None for k in BLANK), f"machine {r['id']} leaks {BLANK}"
    print("credential check: OK")

    # A UNC path anywhere in the payload means a host and share got out. The redactor's
    # first alternative eats the whole run, so a surviving '\\' is a leak, not a skeleton.
    assert r"\\\\" not in _dumps(snap), "a UNC path reached the payload"
    vals = _redactor()[0]
    leaks = [(n, k, v[:90]) for n, sl in snap["slices"].items() for row in sl["rows"]
             for k, v in row.items()
             if type(v) is str and len(v) > 24 and any(x.lower() in v.lower() for x in vals)]
    assert not leaks, f"host/share leaked in free text: {leaks[:3]}"
    print("host/share scrub: OK")

    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "snap.json"
        f.write_text(_dumps(snap), encoding="utf-8")
        subprocess.run([sys.executable, "-m", "app.mirror", str(f)], check=True, cwd=str(config.ROOT),
                       env={**os.environ, "LASER_DATA": tmp, "LASER_ROLE": "mirror"})
