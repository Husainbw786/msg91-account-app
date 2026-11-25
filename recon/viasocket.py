"""viaSocket over plain HTTP: the apps module of https://viasocket.com/docs/apps-api.md and the AI call of
https://viasocket.com/docs/ai.md, ported to Python. Server only.

Environment (.env): VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID, VIASOCKET_EMBED_SECRET, and VIASOCKET_APP_USER (the
stable id this app signs as; default 'recon-app').
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt

API = "https://flow-api.viasocket.com"
RUN = "https://flow.sokt.io"
SERVICES = "/embed/services"
UA = {"User-Agent": "ledger-recon/1.0"}


def load_env(path: Optional[Path] = None) -> None:
    """Read KEY=VALUE lines from .env into the environment without overriding values already set."""
    p = path or Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())



class ViaSocketError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status, self.body = status, body


def configured() -> bool:
    return all(os.environ.get(k) for k in ("VIASOCKET_ORG_ID", "VIASOCKET_PROJECT_ID", "VIASOCKET_EMBED_SECRET"))


def app_user() -> str:
    return os.environ.get("VIASOCKET_APP_USER", "recon-app")


def embed_token(user_id: str) -> str:
    return jwt.encode({"org_id": os.environ["VIASOCKET_ORG_ID"], "project_id": os.environ["VIASOCKET_PROJECT_ID"],
                       "unique_identifier": str(user_id)}, os.environ["VIASOCKET_EMBED_SECRET"], algorithm="HS256")


def _hide(path: str) -> str:
    return re.sub(r"/(enable|revoke|updatestatus|updateflowembed|update-subscribed-event)/[^/?]+", r"/\1/<id>", path)


def api_call(user_id: str, method: str, path: str, body: Any = None, timeout: float = 120.0) -> Any:
    headers = {"Content-Type": "application/json", "authorization": embed_token(user_id), **UA}
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, API + path, headers=headers, json=body)
    try:
        payload = r.json()
    except ValueError:
        payload = r.text
    if r.status_code >= 400 or (isinstance(payload, dict) and payload.get("success") is False):
        msg = payload.get("message") if isinstance(payload, dict) else None
        raise ViaSocketError(msg or f"viaSocket {_hide(path)}: {r.status_code}", r.status_code, payload)
    return payload.get("data") if isinstance(payload, dict) else payload


def _post_json(url: str, body: Any, timeout: float = 120.0) -> Any:
    shown = re.sub(r"/func/.*", "/func/<id>", url.split("?")[0])
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json=body, headers={"Content-Type": "application/json", **UA})
    try:
        parsed = r.json()
    except ValueError:
        raise ViaSocketError(f"{shown}: {r.status_code} {r.text[:200]}", r.status_code)
    if r.status_code >= 400 or (isinstance(parsed, dict) and parsed.get("success") is False):
        em = parsed.get("errorMsg") if isinstance(parsed, dict) else None
        msg = (em or {}).get("message") if isinstance(em, dict) else None
        raise ViaSocketError(msg or (parsed.get("message") if isinstance(parsed, dict) else None)
                             or f"{shown}: {r.status_code}", r.status_code, parsed)
    return parsed


# ------------------------------------------------------------------ one user
_listings: dict[str, tuple[float, Any]] = {}


def list_actions(user_id: str, slug: str) -> dict:
    cached = _listings.get(slug)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    body = api_call(user_id, "GET", f"{SERVICES}/{slug}/actions")
    _listings[slug] = (time.time(), body)
    return body


def service_id_for(user_id: str, slug: str) -> str:
    return list_actions(user_id, slug)["service_id"]


def list_connections(user_id: str) -> list:
    return api_call(user_id, "GET", "/embed/authentications") or []


def list_flows(user_id: str) -> list:
    return (api_call(user_id, "GET", f"/projects/{os.environ['VIASOCKET_PROJECT_ID']}/integrations") or {}).get("flows") or []


def find_enabled_app(user_id: str, slug: str, auth_id: Optional[str] = None) -> Optional[str]:
    sid = service_id_for(user_id, slug)
    for f in list_flows(user_id):
        if f.get("service_id") == sid and (not auth_id or f.get("auth_id") == auth_id) \
                and f.get("status") in ("active", "paused"):
            return f.get("id")
    return None


def enable_app(user_id: str, slug: str, auth_id: str) -> str:
    return api_call(user_id, "POST", f"{SERVICES}/{slug}/enable/{auth_id}")["script_id"]


def list_options(user_id: str, slug: str, action_key: str, field_key: str, auth_id: str,
                 existing_fields: Optional[dict] = None, version: Optional[str] = None) -> dict:
    data = api_call(user_id, "POST", f"{SERVICES}/{slug}/actions/{action_key}/options",
                    {"auth_id": auth_id, "fieldKey": field_key, "existingFields": existing_fields or {},
                     **({"version": version} if version else {})})
    return {"options": (data or {}).get("options") or [], "has_more": bool((data or {}).get("has_more")),
            "message": (data or {}).get("message")}


def run_action(script_id: str, action_key: str, input_data: dict, version: Optional[str] = None,
               timeout: float = 120.0) -> Any:
    payload = _post_json(f"{RUN}/func/{script_id}",
                         {"action": action_key, **({"version": version} if version else {}), "inputData": input_data},
                         timeout)
    if isinstance(payload, dict) and payload.get("message") == "Flow Queued" and "success" not in payload:
        raise ViaSocketError("runAction: no flow has this script_id (unknown or deleted)", 200, payload)
    if isinstance(payload, dict) and isinstance(payload.get("success"), bool) and "data" in payload:
        return payload["data"]
    return payload


# ------------------------------------------------------------------ AI
def ask_ai(user_id: str, body: dict, timeout: float = 300.0) -> dict:
    """POST /embed/ai/message (sync). Returns data: content (object for JSON answers), thread_id, finish_reason."""
    return api_call(user_id, "POST", "/embed/ai/message", body, timeout=timeout)
