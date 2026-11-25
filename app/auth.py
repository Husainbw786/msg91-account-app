"""Login ID + password sign-in.

- Passwords are stored as PBKDF2-SHA256 hashes (600,000 iterations, per-user salt; standard library only).
- 5 wrong passwords lock the account for 15 minutes.
- The first admin is created from ADMIN_USERNAME / ADMIN_PASSWORD when no user exists yet.
- Admins add users, reset passwords and deactivate accounts from the Users page.

Environment:
  SECRET_KEY                 signs the session cookie (required in production)
  ADMIN_USERNAME, ADMIN_PASSWORD   bootstrap admin (used only while the users table is empty)
  AUTH_DISABLED=1            local development only: everyone is signed in as an admin 'dev'
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re
import secrets

from fastapi import HTTPException, Request

from .store import get_store

AUTH_DISABLED = os.environ.get("AUTH_DISABLED") == "1"
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
ITERATIONS = int(os.environ.get("PBKDF2_ITERATIONS", "600000"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


_DUMMY = hash_password(secrets.token_hex(8))       # equal work for unknown users (no username probing)


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters.")
    if password.lower() == password or password.upper() == password or not re.search(r"\d", password):
        raise HTTPException(400, "Password must mix upper- and lower-case letters and include a digit.")


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(d):
    return d.isoformat() if d else None


def _parse(s):
    return dt.datetime.fromisoformat(s) if s else None


def new_user(username: str, name: str, password: str, is_admin: bool, must_change: bool = True) -> dict:
    return {"id": username, "username": username, "name": name, "password_hash": hash_password(password),
            "is_admin": is_admin, "active": True, "must_change": must_change, "failed_attempts": 0,
            "locked_until": None, "session_version": 1, "created_at": _iso(_now()), "last_login": None}


def public(u: dict) -> dict:
    return {k: u.get(k) for k in ("id", "username", "name", "is_admin", "active", "must_change", "last_login")}


def get_user(username: str):
    return get_store().get("users", username)


def save_user(u: dict) -> None:
    get_store().put("users", u["username"], u)


def bootstrap_admin() -> None:
    u, p = os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD")
    if not u or not p:
        return
    if not get_store().list("users"):
        save_user(new_user(u.strip().lower(), "Administrator", p, True, True))


def current_user(request: Request) -> dict:
    if AUTH_DISABLED:
        return {"username": "dev", "name": "Local developer", "is_admin": True, "id": "dev", "must_change": False}
    uid = request.session.get("uid")
    if not uid or not isinstance(uid, str):      # a cookie from the old SQLite version held a numeric id
        request.session.clear()
        raise HTTPException(status_code=401, detail="Sign in required")
    u = get_user(uid)
    if not u or not u.get("active") or u.get("session_version") != request.session.get("sv"):
        request.session.clear()
        raise HTTPException(status_code=401, detail="Sign in required")
    return {"username": u["username"], "name": u.get("name"), "is_admin": u.get("is_admin"), "id": u["username"],
            "must_change": u.get("must_change")}


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do this.")
    return user


def login(request: Request, username: str, password: str) -> dict:
    username = (username or "").strip().lower()
    u = get_user(username) if username else None
    if not u or not u.get("active"):
        verify_password(password, _DUMMY)
        raise HTTPException(401, "Wrong login ID or password.")
    locked = _parse(u.get("locked_until"))
    if locked and locked > _now():
        raise HTTPException(429, "Too many wrong attempts. Try again in a few minutes.")
    if not verify_password(password, u["password_hash"]):
        u["failed_attempts"] = (u.get("failed_attempts") or 0) + 1
        if u["failed_attempts"] >= MAX_FAILED:
            u["locked_until"] = _iso(_now() + dt.timedelta(minutes=LOCK_MINUTES))
            u["failed_attempts"] = 0
        save_user(u)
        raise HTTPException(401, "Wrong login ID or password.")
    u.update(failed_attempts=0, locked_until=None, last_login=_iso(_now()))
    save_user(u)
    request.session.clear()
    request.session["uid"] = u["username"]
    request.session["sv"] = u.get("session_version")
    return {"username": u["username"], "name": u.get("name"), "is_admin": u.get("is_admin"),
            "must_change": u.get("must_change")}


def change_password(user: dict, old: str, new: str) -> None:
    check_password_rules(new)
    u = get_user(user["username"])
    if not u or not verify_password(old, u["password_hash"]):
        raise HTTPException(400, "Current password is wrong.")
    u["password_hash"] = hash_password(new)
    u["must_change"] = False
    save_user(u)


def logout(request: Request) -> None:
    request.session.clear()
