"""Web app: upload two ledgers (Excel, CSV, PDF or a Google Sheets link), check how they were read, reconcile, review, download.

Run locally:  AUTH_DISABLED=1 .venv/bin/uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import os as _os

from recon.viasocket import load_env

if _os.environ.get("RECON_LOAD_DOTENV", "1") != "0":
    load_env()                       # server secrets from .env (entry points only, never on library import)

from fastapi.concurrency import run_in_threadpool

from recon import paycheck

from . import auth, checks, gsheets, runs
from .jobs import build_report, parse_run, reconcile_run
from .serialize import ROLE_CHOICES
from .store import get_store

MAX_UPLOAD = 25 * 1024 * 1024
ALLOWED_EXT = {".xlsx", ".xlsm", ".xls", ".csv", ".pdf"}
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Ledger Reconciliation", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
                   same_site="lax", https_only=not auth.AUTH_DISABLED and os.environ.get("HTTPS_ONLY") != "0",
                   max_age=12 * 3600)
auth.bootstrap_admin()


# ------------------------------------------------------------------------ page images for the vision reader
# viaSocket's AI takes images by URL. Each page image is served once-off at a random, unlisted link that expires in
# 15 minutes; no login (viaSocket's servers fetch it). Only when the app knows its public https address (APP_URL).
_PAGES: dict[str, tuple[float, bytes, str]] = {}
_PAGES_LOCK = threading.Lock()
PAGE_TTL = 15 * 60


def _publish_page(data: bytes, content_type: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _PAGES_LOCK:
        for k in [k for k, v in _PAGES.items() if v[0] < now]:
            del _PAGES[k]
        _PAGES[token] = (now + PAGE_TTL, data, content_type)
    return f"{os.environ['APP_URL'].rstrip('/')}/vision-page/{token}.png"


if (os.environ.get("APP_URL") or "").startswith("https://"):
    from recon import vision as _vision
    _vision.set_publisher(_publish_page)


@app.get("/vision-page/{name}")
def vision_page(name: str):
    token = name.rsplit(".", 1)[0]
    with _PAGES_LOCK:
        item = _PAGES.get(token)
    if not item or item[0] < time.time():
        raise HTTPException(404, "Not found")
    return Response(item[1], media_type=item[2], headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


# ------------------------------------------------------------------------ sign-in
class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserIn(BaseModel):
    username: str
    name: Optional[str] = None
    is_admin: bool = False


class UserPatch(BaseModel):
    active: Optional[bool] = None
    is_admin: Optional[bool] = None


@app.post("/auth/login")
def auth_login(body: LoginIn, request: Request):
    return {"user": auth.login(request, body.username, body.password)}


@app.post("/auth/logout")
def auth_logout(request: Request):
    auth.logout(request)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    try:
        return {"user": auth.current_user(request), "dev_mode": auth.AUTH_DISABLED}
    except HTTPException:
        return JSONResponse({"user": None}, status_code=401)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: dict = Depends(auth.current_user)):
    auth.change_password(user, body.old_password, body.new_password)
    return {"ok": True}


# ------------------------------------------------------------------------ users (admin)
def _temp_password() -> str:
    return secrets.token_urlsafe(9) + "A1"


@app.get("/api/users")
def list_users(admin: dict = Depends(auth.require_admin)):
    return sorted((auth.public(u) for u in get_store().list("users")), key=lambda u: u["username"])


@app.post("/api/users")
def add_user(body: UserIn, admin: dict = Depends(auth.require_admin)):
    username = body.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._@-]{3,120}", username):
        raise HTTPException(400, "Login ID: 3-120 characters, letters, digits and . _ @ - only.")
    if auth.get_user(username):
        raise HTTPException(400, "That login ID already exists.")
    temp = _temp_password()
    u = auth.new_user(username, (body.name or username)[:200], temp, body.is_admin, True)
    auth.save_user(u)
    return {"user": auth.public(u), "temporary_password": temp}


@app.post("/api/users/{uid}/reset")
def reset_password(uid: str, admin: dict = Depends(auth.require_admin)):
    u = auth.get_user(uid)
    if not u:
        raise HTTPException(404, "User not found")
    temp = _temp_password()
    u.update(password_hash=auth.hash_password(temp), must_change=True, failed_attempts=0, locked_until=None,
             session_version=(u.get("session_version") or 1) + 1)
    auth.save_user(u)
    return {"user": auth.public(u), "temporary_password": temp}


@app.patch("/api/users/{uid}")
def update_user(uid: str, body: UserPatch, admin: dict = Depends(auth.require_admin)):
    if uid == admin["id"] and (body.active is False or body.is_admin is False):
        raise HTTPException(400, "You cannot deactivate or demote your own account.")
    u = auth.get_user(uid)
    if not u:
        raise HTTPException(404, "User not found")
    if body.active is not None:
        u["active"] = body.active
        if not body.active:
            u["session_version"] = (u.get("session_version") or 1) + 1
    if body.is_admin is not None:
        u["is_admin"] = body.is_admin
    auth.save_user(u)
    return auth.public(u)


# ------------------------------------------------------------------------ runs
def _get(run_id: str) -> dict:
    run = runs.get(run_id)
    if not run:
        raise HTTPException(404, "Reconciliation not found")
    return run


def _safe_name(name: str) -> str:
    # shown to people as the file's name (never used as a path): keep it as on their computer, brackets included
    base = re.sub(r"[^A-Za-z0-9._ ()\[\]&,+'-]+", "_", Path(name or "file").name)[:160]
    return base or "file"


async def _read_upload(upload: UploadFile) -> tuple[bytes, str, str]:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"{upload.filename}: upload an Excel (.xlsx, .xls), CSV or PDF file.")
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(400, f"{upload.filename} is larger than 25 MB.")
    if not data:
        raise HTTPException(400, f"{upload.filename} is empty.")
    return data, _safe_name(upload.filename), ext


@app.get("/api/runs")
def list_runs(user: dict = Depends(auth.current_user)):
    return runs.list_recent()


@app.post("/api/runs")
async def create_run(background: BackgroundTasks, counterparty: str = Form(...),
                     walkover_file: Optional[UploadFile] = File(None), counterparty_file: Optional[UploadFile] = File(None),
                     walkover_url: Optional[str] = Form(None), counterparty_url: Optional[str] = Form(None),
                     user: dict = Depends(auth.current_user)):
    counterparty = counterparty.strip()[:200]
    if not counterparty:
        raise HTTPException(400, "Enter the counterparty's name.")
    got = {}
    for side, up, url in (("w", walkover_file, walkover_url), ("c", counterparty_file, counterparty_url)):
        label = "Walkover" if side == "w" else "counterparty"
        if up is not None and up.filename:
            got[side] = (*await _read_upload(up), None)
        elif url and url.strip():
            data = await run_in_threadpool(gsheets.fetch_xlsx, url.strip())
            got[side] = (data, "Google Sheet " + gsheets.sheet_id(url.strip())[:10] + "…", ".xlsx", url.strip())
        else:
            raise HTTPException(400, f"Add the {label} ledger: upload a file or paste a Google Sheets link.")
    run = runs.new(counterparty, user["username"])
    for side, (data, name, ext, url) in got.items():
        await run_in_threadpool(runs.put_upload, run["id"], side, data, ext)
        run[f"{side}_filename"], run[f"{side}_ext"], run[f"{side}_url"] = name, ext, url
    runs.save(run)
    background.add_task(parse_run, run["id"])
    return {"id": run["id"]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(auth.current_user)):
    run = _get(run_id)
    preview = runs.get_part(run_id, "preview") if run.get("has_preview") else None
    result = runs.get_part(run_id, "result") if run.get("has_result") else None
    return {**runs.summary(run), "preview": preview, "result": result, "w_sheet": run.get("w_sheet"),
            "c_sheet": run.get("c_sheet"), "role_choices": ROLE_CHOICES}


class MappingIn(BaseModel):
    side: str
    sheet: Optional[str] = None
    roles: Optional[dict] = None          # {table_index: {role: column_index}}


@app.post("/api/runs/{run_id}/mapping")
def set_mapping(run_id: str, body: MappingIn, background: BackgroundTasks, user: dict = Depends(auth.current_user)):
    if body.side not in ("W", "C"):
        raise HTTPException(400, "side must be W or C")
    run = _get(run_id)
    if run["status"] in ("parsing", "reconciling"):
        raise HTTPException(409, "Still working on this reconciliation; try again in a moment.")
    key = "w" if body.side == "W" else "c"
    fields = {"status": "parsing"}
    if body.sheet is not None:
        fields[f"{key}_sheet"] = body.sheet
        fields[f"{key}_overrides"] = None
    if body.roles is not None:
        fields[f"{key}_overrides"] = {str(int(t)): {str(k): int(v) for k, v in roles.items() if v is not None}
                                      for t, roles in body.roles.items()}
    runs.update(run_id, **fields)
    background.add_task(parse_run, run_id, (body.side,))
    return {"ok": True}


@app.post("/api/runs/{run_id}/reconcile")
def start_reconcile(run_id: str, background: BackgroundTasks, user: dict = Depends(auth.current_user)):
    run = _get(run_id)
    if run["status"] not in ("parsed", "done", "failed") or not run.get("has_preview"):
        raise HTTPException(409, "The files have not been read yet.")
    runs.update(run_id, status="reconciling")
    background.add_task(reconcile_run, run_id)
    return {"ok": True}


@app.get("/api/runs/{run_id}/report.xlsx")
def download(run_id: str, request: Request):
    try:
        auth.current_user(request)
    except HTTPException:            # a link opened from the reconciliations table: sign in, then the run page
        return RedirectResponse(f"/#/run/{run_id}", status_code=303)
    run = _get(run_id)
    if run.get("status") != "done":
        raise HTTPException(404, "Report not ready")
    try:
        data = build_report(run)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"Report not available: {exc}")
    fname = re.sub(r"[^A-Za-z0-9]+", "_", run["counterparty"]).strip("_") or "counterparty"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Reconciliation_{fname}.xlsx"'})


# ------------------------------------------------------------------------ payment checks (one Walkover ledger)
def _get_check(check_id: str) -> dict:
    chk = checks.get(check_id)
    if not chk:
        raise HTTPException(404, "Payment check not found")
    return chk


@app.get("/api/checks")
def list_checks(user: dict = Depends(auth.current_user)):
    return checks.list_recent()


@app.post("/api/checks")
async def create_check(background: BackgroundTasks, name: str = Form(""), file: Optional[UploadFile] = File(None),
                       url: Optional[str] = Form(None), user: dict = Depends(auth.current_user)):
    if file is not None and file.filename:
        data, fname, ext = await _read_upload(file)
        if ext == ".pdf":
            raise HTTPException(400, "For the payment check, upload the ledger as Excel, CSV or a Google Sheets link.")
        src = None
    elif url and url.strip():
        data = await run_in_threadpool(gsheets.fetch_xlsx, url.strip())
        fname, ext, src = "Google Sheet " + gsheets.sheet_id(url.strip())[:10] + "…", ".xlsx", url.strip()
    else:
        raise HTTPException(400, "Add the ledger: upload a file or paste a Google Sheets link.")
    chk = checks.new((name or "").strip()[:200] or Path(fname).stem, user["username"])
    await run_in_threadpool(checks.put_upload, chk["id"], data, ext)
    chk.update(filename=fname, ext=ext, url=src)
    checks.save(chk)
    background.add_task(checks.run_check, chk["id"])
    return {"id": chk["id"]}


@app.get("/api/checks/{check_id}")
def get_check(check_id: str, user: dict = Depends(auth.current_user)):
    chk = _get_check(check_id)
    result = checks.get_result(check_id) if chk.get("has_result") else None
    return {**checks.public(chk), "result": result,
            "role_choices": [{"value": k, "label": v} for k, v in paycheck.LABELS.items()]}


class CheckMappingIn(BaseModel):
    sheet: Optional[str] = None
    header_row: Optional[int] = None
    cols: Optional[dict] = None           # {role: column index}


@app.post("/api/checks/{check_id}/mapping")
def set_check_mapping(check_id: str, body: CheckMappingIn, background: BackgroundTasks,
                      user: dict = Depends(auth.current_user)):
    chk = _get_check(check_id)
    if chk["status"] == "checking":
        raise HTTPException(409, "Still checking this file; try again in a moment.")
    fields = {"status": "checking"}
    if body.sheet is not None:
        fields.update(sheet=body.sheet, override=None)
    if body.cols is not None:
        cols = {str(k): int(v) for k, v in body.cols.items() if k in paycheck.ROLES and v is not None}
        missing = [paycheck.LABELS[k] for k in paycheck.REQUIRED if k not in cols]
        if missing:
            raise HTTPException(400, "Choose a column for: " + ", ".join(missing))
        fields["override"] = {"header_row": int(body.header_row or 1), "cols": cols}
    checks.update(check_id, **fields)
    background.add_task(checks.run_check, check_id)
    return {"ok": True}


@app.get("/api/checks/{check_id}/output.xlsx")
def download_check(check_id: str, request: Request):
    try:
        auth.current_user(request)
    except HTTPException:
        return RedirectResponse(f"/#/check/{check_id}", status_code=303)
    chk = _get_check(check_id)
    if chk.get("status") != "done":
        raise HTTPException(404, "Result not ready")
    try:
        data = checks.build_output(chk)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"File not available: {exc}")
    fname = re.sub(r"[^A-Za-z0-9]+", "_", chk.get("name") or "ledger").strip("_") or "ledger"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Payment_check_{fname}.xlsx"'})


# ------------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _asset_version() -> str:
    """A short fingerprint of the script and stylesheet: changes with every deploy that changes them."""
    import hashlib
    h = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:12]


_INDEX = (STATIC / "index.html").read_text().replace(
    "/static/app.js", f"/static/app.js?v={_asset_version()}").replace(
    "/static/styles.css", f"/static/styles.css?v={_asset_version()}")


@app.get("/")
def index():
    # the page itself is never cached, and it names the script by version, so a deploy reaches every browser
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_INDEX, headers={"Cache-Control": "no-cache, must-revalidate"})
