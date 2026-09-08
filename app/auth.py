"""Users, sessions and role-based access control (stdlib crypto only)."""
import datetime as dt
import hashlib
import hmac
import os
import secrets

from . import db

SESSION_HOURS = 12
ROLES = ("admin", "manager", "production", "maintenance", "it", "viewer")

# What each role may do beyond reading dashboards.
PERMISSIONS = {
    "admin":       {"config", "users", "alerts", "reports", "credentials", "settings", "ack", "maintenance"},
    "manager":     {"reports", "alerts", "ack"},
    "production":  {"alerts", "ack", "reports"},
    "maintenance": {"alerts", "ack", "reports", "maintenance"},
    "it":          {"config", "alerts", "ack", "settings", "reports"},
    "viewer":      set(),
}


def hash_password(password: str, salt: str = None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return h.hex(), salt


def create_user(username, password, role="viewer", full_name=None, email=None):
    h, salt = hash_password(password)
    db.ex("INSERT INTO users(username,full_name,email,role,password_hash,salt,active,created_at) "
          "VALUES(?,?,?,?,?,?,1,?)",
          (username, full_name or username, email, role, h, salt, db.now()))
    return db.q1("SELECT id,username,role FROM users WHERE username=?", (username,))


def verify(username, password):
    u = db.q1("SELECT * FROM users WHERE username=? AND active=1", (username,))
    if not u:
        return None
    h, _ = hash_password(password, u["salt"])
    if not hmac.compare_digest(h, u["password_hash"]):
        return None
    return u


def login(username, password, ip=None):
    u = verify(username, password)
    if not u:
        db.audit(username, "login_failed", "user", username, ip=ip)
        return None
    token = secrets.token_urlsafe(32)
    exp = (dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    db.ex("INSERT INTO sessions(token,user_id,created_at,expires_at,ip) VALUES(?,?,?,?,?)",
          (token, u["id"], db.now(), exp, ip))
    db.ex("UPDATE users SET last_login=? WHERE id=?", (db.now(), u["id"]))
    db.audit(username, "login", "user", u["id"], ip=ip)
    return token


def logout(token):
    db.ex("DELETE FROM sessions WHERE token=?", (token,))


def user_for_token(token):
    if not token:
        return None
    r = db.q1("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
              "WHERE s.token=? AND s.expires_at > ? AND u.active=1", (token, db.now()))
    return r


def can(user, permission) -> bool:
    if not user:
        return False
    return permission in PERMISSIONS.get(user["role"], set())


def purge_expired():
    db.ex("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))


def bootstrap_admin_password():
    """Print a one-time random admin password if the default is still in place."""
    u = db.q1("SELECT * FROM users WHERE username='admin'")
    if not u:
        return None
    h, _ = hash_password("admin", u["salt"])
    if hmac.compare_digest(h, u["password_hash"]):
        new = os.environ.get("LASER_ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        nh, salt = hash_password(new)
        db.ex("UPDATE users SET password_hash=?, salt=? WHERE id=?", (nh, salt, u["id"]))
        return new
    return None
