"""Paths, secrets and runtime settings."""
import os
import pathlib

from cryptography.fernet import Fernet

ROOT = pathlib.Path(__file__).resolve().parent.parent          # .../platform
DATA = pathlib.Path(os.environ.get("LASER_DATA", ROOT / "data"))
WEB = ROOT / "web"
DATA.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA / "laser_platform.db"
CACHE_DIR = DATA / "cache"                                     # local copies of machine sqlite files
CACHE_DIR.mkdir(exist_ok=True)

# Two roles, one codebase. "collector" runs on the factory PC and owns the SMB polling;
# "mirror" is the public read-only copy (Render) that cannot reach 10.100.x.x and is fed
# entirely by the collector's push. See app/mirror.py and DEPLOY.md.
ROLE = os.environ.get("LASER_ROLE", "collector").strip().lower()
MIRROR = ROLE == "mirror"
MIRROR_URL = os.environ.get("LASER_MIRROR_URL", "").rstrip("/")   # collector -> where to push
MIRROR_TOKEN = os.environ.get("LASER_MIRROR_TOKEN", "")           # shared secret, both sides
MIRROR_INTERVAL = int(os.environ.get("LASER_MIRROR_INTERVAL", "60"))

# Legacy sources shipped with the folder — used once, for history seeding. The mirror must
# never seed 26 factory IPs from machines.json, so point it at nothing; both call sites are
# .exists()-guarded.
_LEGACY = DATA / "no-legacy-on-mirror" if MIRROR else ROOT.parent
LEGACY_CENTRAL_DB = pathlib.Path(os.environ.get("LEGACY_CENTRAL_DB", _LEGACY / "central.db"))
LEGACY_MACHINES_JSON = pathlib.Path(os.environ.get("LEGACY_MACHINES_JSON", _LEGACY / "machines.json"))

KEY_FILE = DATA / "secret.key"


def _load_key() -> bytes:
    """Fernet key for machine credentials. Env wins so it can live in a vault.

    The mirror never receives password_enc, so it has nothing to decrypt: letting it
    generate a throwaway key is correct. Do NOT copy the factory key to the cloud."""
    env = os.environ.get("LASER_SECRET_KEY")
    if env:
        return env.encode()
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
    return KEY_FILE.read_bytes()


_fernet = Fernet(_load_key())


def encrypt(plain: str) -> str:
    return _fernet.encrypt((plain or "").encode()).decode()


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        return ""


# Defaults; every one is overridable at runtime from the `settings` table (see db.get_setting).
DEFAULTS = {
    "idle_threshold_seconds": "300",       # gap above this = idle
    "down_threshold_seconds": "900",       # gap above this = stopped/downtime
    "offline_after_failures": "3",         # consecutive heartbeat failures -> OFFLINE
    "slow_latency_ms": "1500",             # heartbeat slower than this -> SLOW
    "alarm_recent_minutes": "15",
    "planned_hours_per_day": "24",
    "planned_break_minutes": "15",         # per shift
    "quality_proxy_enabled": "1",
    "min_plausible_duty": "0.20",           # a job left open cannot claim run time below this laser duty
    "open_job_max_hours": "12",             # a job open longer than this is a stale record, not production
    "clock_drift_days": "2",               # reject source rows this far in the future
    "history_days_ui": "90",
    "connection_log_retention_days": "30",
    "brevo_api_key": "",
    "brevo_sender_email": "noreply@tcgarments.com",
    "brevo_sender_name": "T&C Laser Command Center",
    "teams_webhook": "",
    "whatsapp_endpoint": "",
    "whatsapp_token": "",
    "notifications_enabled": "1",
    "health_weights": "connectivity:20,alarms:20,downtime:20,utilization:15,performance:15,thermal:10",
    "department_target_units": "0",
}
