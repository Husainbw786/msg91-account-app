"""Payment checks: one Walkover ledger, each invoice checked against the payment on its row (recon/paycheck.py).

A check is a small document in the "checks" collection; the upload and the result are blobs. The output Excel is
not stored: it is rebuilt from the upload and the result when someone downloads it. Each check also gets one
plain row in the readable DB Dash table set by DBDASH_TABLE_PAYMENT_CHECKS (text and links only).
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import traceback
import uuid
from typing import Optional

from recon import paycheck
from recon.ai import AIStats, default_backend
from recon.pipeline import extraction_mode

from . import readable
from .store import data_dir, get_json, get_store, put_json

_lock = threading.Lock()
COLL = "checks"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()


def new(name: str, created_by: str) -> dict:
    chk = {"id": str(uuid.uuid4()), "kind": "paycheck", "created_at": _now(), "updated_at": _now(),
           "created_by": created_by, "name": name, "status": "checking", "error": None, "filename": None,
           "ext": None, "url": None, "sheet": None, "override": None, "has_result": False, "summary": None}
    save(chk)
    return chk


def get(check_id: str) -> Optional[dict]:
    doc = get_store().get(COLL, check_id)
    return doc if doc and doc.get("kind") == "paycheck" else None


def save(chk: dict) -> None:
    chk["updated_at"] = _now()
    get_store().put(COLL, chk["id"], chk)
    readable.submit(os.environ.get("DBDASH_TABLE_PAYMENT_CHECKS"), chk["id"], readable_fields(chk))


def update(check_id: str, **fields) -> dict:
    with _lock:
        chk = get(check_id)
        if chk is None:
            raise KeyError(check_id)
        chk.update(fields)
        save(chk)
        return chk


def public(chk: dict) -> dict:
    return {k: chk.get(k) for k in ("id", "name", "status", "created_at", "created_by", "filename", "url", "sheet",
                                    "error", "summary")}


def list_recent(limit: int = 200) -> list[dict]:
    docs = [d for d in get_store().list(COLL) if d.get("kind") == "paycheck"]
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return [public(d) for d in docs[:limit]]


def put_upload(check_id: str, data: bytes, ext: str) -> None:
    get_store().put_blob(f"checks/{check_id}/upload{ext}", data)


def upload_path(chk: dict) -> str:
    ext = chk.get("ext") or ".xlsx"
    p = data_dir() / "cache" / chk["id"] / f"upload{ext}"
    if not p.exists():
        data = get_store().get_blob(f"checks/{chk['id']}/upload{ext}")
        if data is None:
            raise FileNotFoundError("the uploaded file is no longer stored")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return str(p)


def get_result(check_id: str):
    return get_json(f"checks/{check_id}/result.json")


# ------------------------------------------------------------------ the work
def run_check(check_id: str) -> None:
    """Read the columns (rules; AI once only if they do not prove), then the maths. Background thread."""
    try:
        chk = get(check_id)
        stats = AIStats()
        ai = default_backend(stats) if extraction_mode() == "ai" else None
        res = paycheck.analyse(upload_path(chk), chk.get("sheet"), chk.get("override"), ai, stats)
        d = paycheck.as_dict(res)
        d["ai"] = {"calls": stats.calls, "failures": stats.failures, "log": stats.log[:20]}
        put_json(f"checks/{check_id}/result.json", d)
        update(check_id, status="done", error=None, has_result=True, sheet=res.sheet, summary=d["summary"],
               confidence=res.confidence)
    except Exception as exc:
        update(check_id, status="failed", error=f"Could not check the file: {exc}")
        traceback.print_exc()


def build_output(chk: dict) -> bytes:
    d = get_result(chk["id"])
    if not d:
        raise FileNotFoundError("the result is no longer stored")
    out = data_dir() / "cache" / chk["id"] / "payment_check.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    paycheck.write_output(upload_path(chk), chk.get("ext") or ".xlsx", d, str(out))
    return out.read_bytes()


# ------------------------------------------------------------------ readable row
def readable_fields(chk: dict) -> dict:
    base = readable.base_url()
    s = chk.get("summary") or {}
    done = chk.get("status") == "done"
    status = {"checking": "Checking", "done": "Done", "failed": "Failed"}.get(chk.get("status"), chk.get("status"))
    if chk.get("status") == "failed" and chk.get("error"):
        status = f"Failed: {chk['error']}"[:500]
    if done:
        status = "All payments reconciled" if s.get("all_clear") else f"{s.get('open', 0)} invoice(s) with an amount remaining"
    return {
        "name": f"{chk.get('name', '')} · {readable._when(chk.get('created_at'))}",
        "ledger": chk.get("url") or (f"Uploaded file: {chk['filename']}" if chk.get("filename") else ""),
        "started_on": readable._when(chk.get("created_at")),
        "started_by": chk.get("created_by") or "",
        "status": status or "",
        "invoices": str(s.get("invoices", "")) if done else "",
        "matched": str(s.get("invoices", 0) - s.get("open", 0)) if done else "",
        "remaining_total": readable.money((s.get("remaining_total") or 0) / 100) if done else "",
        "report_link": f"{base}/api/checks/{chk['id']}/output.xlsx" if done else "",
        "open_in_app": f"{base}/#/check/{chk['id']}",
    }
