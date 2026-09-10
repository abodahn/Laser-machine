"""Application entry point: FastAPI app, background schedulers, static UI."""
import asyncio
import contextlib
import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, analytics, api, auth, collector, config, db, mirror

log = logging.getLogger("laser")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Separate pools: a stuck sync must never starve the heartbeats, and vice versa.
# They shared one semaphore before, so eight hung SMB calls froze the whole
# collector while the service still reported "Running" and the alert engine kept
# firing on stale data.
_sem_hb = asyncio.Semaphore(6)
_sem_sync = asyncio.Semaphore(4)
_next = {"hb": {}, "sync": {}}
_stuck = {}          # machine_id -> consecutive timeouts, used to back off

# Windows SMB calls (os.stat, net use) can block far past any socket timeout when
# a host answers TCP but refuses the share. A thread running one cannot be
# cancelled: wait_for gives up on the await and frees the semaphore, but the
# thread itself stays blocked and keeps its executor slot forever.
#
# That is what took the collector down. asyncio.to_thread uses the interpreter's
# shared default executor (min(32, cpu+4) workers). Two unreachable hosts leaked a
# thread on every cycle, unbounded, because the scheduler kept dispatching fresh
# calls for a machine whose previous call was still stuck. Once the pool filled,
# every machine — healthy ones included — sat in the queue and timed out in
# batches the exact size of the semaphores. Symptom: a whole fleet "offline" while
# ping and port 445 answered fine.
#
# Two rules keep it bounded:
#   * our own executor, so nothing else in the process shares the damage
#   * one in-flight call per machine, so the leak ceiling is one thread per
#     machine (26) and never grows, whatever the network does
_EXEC = ThreadPoolExecutor(max_workers=64, thread_name_prefix="collector")
_inflight = set()

HB_DEADLINE = 45
SYNC_DEADLINE = 180


async def _run(fn, *a, sem=None, deadline=60, key=None):
    sem = sem or _sem_hb
    if key in _inflight:
        log.debug("%s%s skipped: previous call still running", fn.__name__, a)
        return
    _inflight.add(key)

    def call():
        try:
            return fn(*a)
        finally:
            # clears whenever the thread truly finishes, which may be long after
            # wait_for gave up on it — that is exactly the point
            _inflight.discard(key)

    try:
        async with sem:
            fut = asyncio.get_running_loop().run_in_executor(_EXEC, call)
            return await asyncio.wait_for(asyncio.shield(fut), timeout=deadline)
    except asyncio.TimeoutError:
        n = _stuck.get(key, 0) + 1
        _stuck[key] = n
        log.warning("%s%s exceeded %ss (%s consecutive) — machine backed off, "
                    "no new call until this thread returns", fn.__name__, a, deadline, n)
    except Exception as e:                                    # noqa: BLE001
        _inflight.discard(key)
        log.warning("%s%s failed: %s", fn.__name__, a, e)
    else:
        _stuck.pop(key, None)


def _backoff(key):
    """A machine that keeps timing out is polled less often instead of every cycle."""
    n = _stuck.get(key, 0)
    return 0 if n < 2 else min(900, 60 * (2 ** min(n - 1, 4)))


async def heartbeat_loop():
    while True:
        try:
            now = time.time()
            due = []
            for m in db.q("SELECT id,heartbeat_interval FROM machines WHERE enabled=1 "
                          "AND status IN ('active','maintenance')"):
                iv = max(15, m["heartbeat_interval"] or 60) + _backoff(("hb", m["id"]))
                if now >= _next["hb"].get(m["id"], 0):
                    _next["hb"][m["id"]] = now + iv
                    due.append(m["id"])
            if due:
                await asyncio.gather(*(_run(collector.heartbeat, mid, sem=_sem_hb,
                                            deadline=HB_DEADLINE, key=("hb", mid)) for mid in due))
        except Exception as e:                                # noqa: BLE001
            log.exception("heartbeat loop: %s", e)
        await asyncio.sleep(10)


async def sync_loop():
    while True:
        try:
            now = time.time()
            due = []
            for m in db.q("SELECT id,sync_interval FROM machines WHERE enabled=1 "
                          "AND status='active' AND connection_method='smb'"):
                iv = max(30, m["sync_interval"] or 120) + _backoff(("sync", m["id"]))
                if now >= _next["sync"].get(m["id"], 0):
                    _next["sync"][m["id"]] = now + iv
                    due.append(m["id"])
            if due:
                await asyncio.gather(*(_run(collector.sync, mid, sem=_sem_sync,
                                            deadline=SYNC_DEADLINE, key=("sync", mid)) for mid in due))
        except Exception as e:                                # noqa: BLE001
            log.exception("sync loop: %s", e)
        await asyncio.sleep(15)


async def analytics_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(analytics.refresh_all, 2)
        except Exception as e:                                # noqa: BLE001
            log.exception("analytics loop: %s", e)
        await asyncio.sleep(180)


async def alerts_loop():
    await asyncio.sleep(60)
    while True:
        try:
            n = await asyncio.to_thread(alerts.evaluate)
            if n:
                log.info("raised %s alert(s)", n)
        except Exception as e:                                # noqa: BLE001
            log.exception("alerts loop: %s", e)
        await asyncio.sleep(60)


async def nightly_loop():
    """Deep recompute, identity refresh, log pruning and a database backup."""
    while True:
        now = dt.datetime.now()
        target = now.replace(hour=2, minute=15, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            for m in db.q("SELECT * FROM machines WHERE enabled=1 AND connection_method='smb'"):
                await _run(collector.sync_identity, m)
            await asyncio.to_thread(analytics.rebuild_design_stats)
            await asyncio.to_thread(analytics.rebuild_hourly, 45)
            await asyncio.to_thread(analytics.rebuild_daily, 45)
            await asyncio.to_thread(analytics.rebuild_baselines)
            await asyncio.to_thread(collector.prune_logs)
            await asyncio.to_thread(auth.purge_expired)
            await _backup()
            log.info("nightly maintenance complete")
        except Exception as e:                                # noqa: BLE001
            log.exception("nightly loop: %s", e)


async def _backup():
    import sqlite3
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.DATA / "backup" / f"laser_platform-{stamp}.db"
    dest.parent.mkdir(exist_ok=True)

    def run():
        out = sqlite3.connect(dest)
        with out:
            db.connect().backup(out)
        out.close()
        for old in sorted(dest.parent.glob("laser_platform-*.db"))[:-14]:
            old.unlink(missing_ok=True)

    await asyncio.to_thread(run)
    db.set_setting("last_backup", f"{db.now()} -> {dest.name}")


async def watchdog_loop():
    """Shout if the collector goes quiet. A service that is 'Running' but has not
    touched a machine in an hour is exactly the failure that hid for 22 hours."""
    await asyncio.sleep(300)
    while True:
        try:
            r = db.q1("SELECT MAX(ts) t FROM connection_log")
            last = r["t"] if r else None
            if last:
                age = (dt.datetime.now() - dt.datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
                if age > 900:
                    log.error("WATCHDOG: no machine contact for %.0f minutes — collector stalled. "
                              "Held heartbeat slots: %s, sync slots: %s",
                              age / 60, _sem_hb._value, _sem_sync._value)
                    db.ex("INSERT INTO connection_log(machine_id,ts,kind,ok,detail) VALUES(0,?,?,0,?)",
                          (db.now(), "watchdog", "collector stalled: no contact for %.0f min" % (age / 60)))
        except Exception as e:                                # noqa: BLE001
            log.warning("watchdog: %s", e)
        await asyncio.sleep(300)


async def mirror_loop():
    """Push the compact snapshot to the public mirror. push_once() never raises."""
    while True:
        await asyncio.to_thread(mirror.push_once)
        await asyncio.sleep(config.MIRROR_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    pw = auth.bootstrap_admin_password()
    if pw:
        log.warning("=" * 68)
        log.warning("ADMIN PASSWORD SET TO: %s   (change it after first login)", pw)
        log.warning("=" * 68)
    seeded = auth.bootstrap_users()
    if seeded:
        log.info("seeded account(s) from LASER_USERS: %s", ", ".join(seeded))
    if config.MIRROR:
        # No loops at all. Beyond being pointless without the LAN, analytics.refresh_all
        # and refresh_deep DELETE design_stats and today's forecasts and rebuild the
        # rollups from an empty production table — on a mirror that erases what was
        # just pushed. Same for the legacy import below.
        log.info("mirror role: serving %s machines from the pushed snapshot",
                 db.q1("SELECT COUNT(*) n FROM machines")["n"])
        yield
        return
    if not db.q1("SELECT 1 FROM production LIMIT 1"):
        log.info("empty database — importing legacy central.db history...")
        res = await asyncio.to_thread(db.import_central_db)
        log.info("legacy import: %s", res)
        await asyncio.to_thread(analytics.refresh_deep)
    loops = [heartbeat_loop, sync_loop, analytics_loop, alerts_loop, nightly_loop, watchdog_loop]
    if config.MIRROR_URL and config.MIRROR_TOKEN:
        loops.append(mirror_loop)
        log.info("mirroring to %s every %ss", config.MIRROR_URL, config.MIRROR_INTERVAL)
    tasks = [asyncio.create_task(t()) for t in loops]
    log.info("collector running for %s machines",
             db.q1("SELECT COUNT(*) n FROM machines WHERE enabled=1")["n"])
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="T&C Laser Intelligence & Command Center", version="1.0", lifespan=lifespan)
app.include_router(api.router, prefix="/api")
app.mount("/static", StaticFiles(directory=config.WEB), name="static")


# The mirror holds a projection, not the source of truth. One guard here beats 24 route
# decorators and covers routes other tracks add later.
_MIRROR_WRITE_OK = {"/api/auth/login", "/api/auth/logout", "/api/auth/password", "/api/mirror/push"}


@app.middleware("http")
async def readonly_mirror(request, call_next):
    if (config.MIRROR and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.url.path not in _MIRROR_WRITE_OK):
        return JSONResponse(status_code=409, content={
            "detail": "read-only mirror — make this change on the factory collector"})
    return await call_next(request)


# The UI now installs to personal phones, so authenticated JSON must not be written to
# the browser's on-disk HTTP cache, where it would outlive sign-out. The service worker
# already refuses to touch /api; this closes the same hole one layer down. FastAPI sets
# no Cache-Control of its own, and Chrome stores an uncacheable-looking body anyway.
@app.middleware("http")
async def no_store_api(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def healthz():
    return {"ok": True, "time": db.now()}


# The app shell must never be cached, or a browser keeps loading yesterday's
# script tags. The assets it references are versioned, so they cache normally.
SHELL_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(config.WEB / "index.html", headers=SHELL_HEADERS)


# "no-cache" WITHOUT "no-store": the browser must revalidate the worker on every
# update check, but it still has to be allowed to store the response. Chrome
# refuses to register a worker whose script came back no-store, failing with a
# bare "An unknown error occurred when fetching the script" that says nothing
# about the header — the script fetches fine on its own, so it looks like the
# worker is simply broken. SHELL_HEADERS here meant the PWA never installed.
SW_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}


# Both must sit at the root, above /{page}, and both must be declared here: the
# catch-all would otherwise hand back index.html as text/html, and registration
# dies with "unsupported MIME type". Root path is also what gives the worker
# scope "/". Explicit media types because .js/.webmanifest resolve through the
# Windows registry via mimetypes and cannot be trusted.
@app.get("/sw.js")
def service_worker():
    return FileResponse(config.WEB / "sw.js", media_type="text/javascript",
                        headers=SW_HEADERS)


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(config.WEB / "manifest.webmanifest",
                        media_type="application/manifest+json", headers=SW_HEADERS)


@app.get("/{page}")
def page(page: str):
    p = config.WEB / f"{page}.html"
    if p.exists():
        return FileResponse(p, headers=SHELL_HEADERS)
    return FileResponse(config.WEB / "index.html", headers=SHELL_HEADERS)
