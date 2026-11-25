"""Reconciliation runs in the document store.

A run is a small document (status, names, choices, totals). Its large parts are files: the two uploads,
the review preview, the two saved extractions and the result. The Excel report is not stored: it is
rebuilt from the saved extractions when someone downloads it.
"""
from __future__ import annotations

import datetime as dt
import threading
import uuid
from pathlib import Path
from typing import Optional

from . import readable
from .store import data_dir, get_json, get_store, put_json

_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()


def new(counterparty: str, created_by: str) -> dict:
    run = {"id": str(uuid.uuid4()), "created_at": _now(), "updated_at": _now(), "created_by": created_by,
           "counterparty": counterparty, "status": "parsing", "error": None, "w_filename": None, "c_filename": None,
           "w_url": None, "c_url": None, "w_closing": None,
           "w_sheet": None, "c_sheet": None, "w_overrides": None, "c_overrides": None, "residual": None,
           "c_closing": None, "has_preview": False, "has_result": False, "has_report": False}
    save(run)
    return run


def get(run_id: str) -> Optional[dict]:
    return get_store().get("runs", run_id)


def save(run: dict) -> None:
    run["updated_at"] = _now()
    get_store().put("runs", run["id"], run)
    readable.sync(run)


def update(run_id: str, **fields) -> dict:
    with _lock:
        run = get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.update(fields)
        save(run)
        return run


def summary(run: dict) -> dict:
    return {k: run.get(k) for k in ("id", "counterparty", "status", "created_at", "created_by", "w_filename",
                                    "c_filename", "residual", "c_closing", "error")}


def list_recent(limit: int = 200) -> list[dict]:
    runs = [r for r in get_store().list("runs") if r.get("kind") != "paycheck"]
    runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [summary(r) for r in runs[:limit]]


# ------------------------------------------------------------------ large parts
def put_upload(run_id: str, side: str, data: bytes, ext: str) -> None:
    get_store().put_blob(f"runs/{run_id}/{side}_upload{ext}", data)


def upload_path(run: dict, side: str) -> str:
    """The upload as a local file (the readers need a path). Cached under DATA_DIR/cache; refetched if missing."""
    ext = run.get(f"{side}_ext") or ".xlsx"
    p = data_dir() / "cache" / run["id"] / f"{side}_upload{ext}"
    if not p.exists():
        data = get_store().get_blob(f"runs/{run['id']}/{side}_upload{ext}")
        if data is None:
            raise FileNotFoundError("the uploaded file is no longer stored")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return str(p)


def put_part(run_id: str, name: str, obj) -> None:
    put_json(f"runs/{run_id}/{name}.json", obj)


def get_part(run_id: str, name: str):
    return get_json(f"runs/{run_id}/{name}.json")


def scratch_path(run_id: str, name: str) -> Path:
    p = data_dir() / "cache" / run_id / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
