"""Keep a public URL alive, and make it findable.

Cloudflare quick tunnels are disposable: the hostname is random, it dies when the
process stops, and Cloudflare reaps it on its own — after which cloudflared sits
in a retry loop against a dead registration ("Unauthorized: Tunnel not found")
and never recovers by itself. Restarting the service does not help, because the
stale state comes back with it.

So this watchdog owns the tunnel instead:
  * starts cloudflared and captures the hostname it is given
  * probes that hostname end to end (not just the process)
  * on failure, kills cloudflared outright and starts a fresh tunnel
  * writes the live URL into the platform settings, so the dashboard can show it

The LAN address never changes, so the dashboard is the stable place to look up
whatever the current public URL happens to be.

Run as a service; it never exits.
"""
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CLOUDFLARED = r"C:\Program Files (x86)\cloudflared\cloudflared.exe"
LOCAL = "http://127.0.0.1:8800"
LOG = HERE / "data" / "tunnel" / "keeper.log"
URL_FILE = HERE / "data" / "tunnel" / "current_url.txt"
CHECK_EVERY = 60          # seconds between probes
GRACE = 90                # seconds to let a new tunnel come up
URL_RE = re.compile(rb"https://[a-z0-9-]+\.trycloudflare\.com")


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="", flush=True)


def publish(url):
    """Record the live URL where the dashboard and the operator can find it."""
    URL_FILE.write_text(url or "", encoding="ascii")
    try:
        from app import db
        db.init_db()
        db.set_setting("public_url", url or "")
        db.set_setting("public_url_checked", time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:                                    # noqa: BLE001
        log(f"could not write public_url setting: {e}")


def alive(url, timeout=15):
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:                                         # noqa: BLE001
        return False


# This machine also runs TC Platform's own cloudflared tunnels (ports 5000-5003
# for itsm / assets / monitoring / commandtrack). Killing by image name takes
# those down too, which is exactly what this used to do. Only ever touch the
# process serving OUR port.
OUR_PORT = "8800"
_child = {"proc": None}


def kill_cloudflared():
    """Stop only the tunnel serving our port; leave every other tunnel alone."""
    proc = _child.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:                                     # noqa: BLE001
            pass
        _child["proc"] = None
    # sweep orphans from a previous run of THIS keeper, matched on our port
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='cloudflared.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=25).stdout
    except Exception:                                         # noqa: BLE001
        out = ""
    for line in out.splitlines():
        if OUR_PORT in line and "cloudflared" in line.lower():
            pid = line.strip().rsplit(",", 1)[-1].strip()
            if pid.isdigit():
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                log(f"stopped orphaned tunnel pid {pid} (ours, port {OUR_PORT})")
    time.sleep(3)


def start_tunnel():
    """Start cloudflared fresh and return the hostname Cloudflare hands out."""
    kill_cloudflared()
    out = HERE / "data" / "tunnel" / "cf.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass
    fh = open(out, "wb")
    # DEFAULTS ONLY. Forcing --protocol http2 and --edge-ip-version 4 makes
    # cloudflared register just one edge connection, and Cloudflare then answers
    # error 1033 "unable to resolve it" for the hostname. Left alone it picks QUIC
    # and the tunnel routes correctly. Do not add tuning flags here.
    _child["proc"] = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", LOCAL],
        stdout=fh, stderr=fh,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + GRACE
    while time.time() < deadline:
        time.sleep(2)
        try:
            m = URL_RE.search(out.read_bytes())
        except OSError:
            m = None
        if m:
            url = m.group(0).decode()
            # the hostname appears before it is routable; confirm end to end
            for _ in range(20):
                if alive(url):
                    return url
                time.sleep(4)
            return url
    return None


def main():
    log("tunnel keeper started")
    url = URL_FILE.read_text(encoding="ascii").strip() if URL_FILE.exists() else ""
    fails = 0
    while True:
        try:
            if not alive(LOCAL, timeout=8):
                log("platform itself is down — waiting, not touching the tunnel")
                time.sleep(CHECK_EVERY)
                continue
            if alive(url):
                if fails:
                    log(f"tunnel healthy again: {url}")
                fails = 0
                publish(url)
            else:
                fails += 1
                log(f"tunnel unreachable ({fails}) — {url or 'no url'}")
                if fails >= 3:                # three strikes, then rebuild it
                    new = start_tunnel()
                    if new:
                        log(f"NEW PUBLIC URL: {new}")
                        url = new
                        publish(url)
                        fails = 0
                    else:
                        log("could not obtain a new tunnel; will retry")
        except Exception as e:                                # noqa: BLE001
            log(f"keeper error: {e}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
