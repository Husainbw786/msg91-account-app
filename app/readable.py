"""The readable "reconciliations" table in DB Dash: one plain row per reconciliation for people to browse.

It holds text and links only (Google Sheet links, the report download link, the app page), never the report
or the ledgers themselves. Set DBDASH_TABLE_RECONCILIATIONS to the table id; without it nothing is written.

Columns (all long text): name, counterparty, started_on, started_by, status, walkover_ledger,
counterparty_ledger, walkover_closing, counterparty_closing, unexplained, report_link, open_in_app.
Rows are found again by open_in_app, which holds the run's unique link.

Writes happen on one background thread, in order, so a slow or failing DB Dash call never holds up or breaks
a reconciliation; failures are only logged.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

log = logging.getLogger("recon.readable")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATUS = {"parsing": "Reading the ledgers", "parsed": "Ledgers read, ready to reconcile",
          "reconciling": "Reconciling", "done": "Done", "failed": "Failed"}

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readable")
_rowids: dict[tuple, str] = {}
_lock = threading.Lock()


def table_id() -> Optional[str]:
    return os.environ.get("DBDASH_TABLE_RECONCILIATIONS") or None


def base_url() -> str:
    url = os.environ.get("APP_URL")
    if not url and os.environ.get("APP_NAME"):
        url = f"https://{os.environ['APP_NAME']}.embarko.app"
    return (url or "https://ledger-recon.embarko.app").rstrip("/")


def money(rupees) -> str:
    """Indian grouping: -1234567.5 -> '-₹12,34,567.50'."""
    if rupees is None:
        return ""
    neg = rupees < 0
    whole, frac = f"{abs(rupees):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    t = dt.datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def _source(run: dict, side: str) -> str:
    if run.get(f"{side}_url"):
        return run[f"{side}_url"]
    name = run.get(f"{side}_filename")
    return f"Uploaded file: {name}" if name else ""


def fields(run: dict) -> dict:
    base = base_url()
    status = STATUS.get(run.get("status"), run.get("status") or "")
    if run.get("status") == "failed" and run.get("error"):
        status = f"Failed: {run['error']}"[:500]
    done = run.get("status") == "done"
    return {
        "name": f"{run.get('counterparty', '')} · {_when(run.get('created_at'))}",
        "counterparty": run.get("counterparty") or "",
        "started_on": _when(run.get("created_at")),
        "started_by": run.get("created_by") or "",
        "status": status,
        "walkover_ledger": _source(run, "w"),
        "counterparty_ledger": _source(run, "c"),
        "walkover_closing": money(run.get("w_closing")) if done else "",
        "counterparty_closing": money(run.get("c_closing")) if done else "",
        "unexplained": money(run.get("residual")) if done else "",
        "report_link": f"{base}/api/runs/{run['id']}/report.xlsx" if done else "",
        "open_in_app": f"{base}/#/run/{run['id']}",
    }


def _write(run: dict) -> None:
    _write_row(table_id(), run["id"], fields(run))


def _write_row(table: Optional[str], key: str, f: dict) -> None:
    """Add or update the row whose open_in_app link is f["open_in_app"]."""
    from .store import DbDashStore, get_store
    store = get_store()
    if not table or not isinstance(store, DbDashStore):
        return
    try:
        with _lock:
            rowid = _rowids.get((table, key))
        if rowid is None:
            rows = store._rows(table, "open_in_app='" + f["open_in_app"].replace("'", "\\'") + "'")
            rows = [r for r in rows if r.get("open_in_app") == f["open_in_app"]]
            rowid = rows[0]["rowid"] if rows else None
        if rowid:
            store._req("PATCH", table, None, {"records": [{"where": f"rowid='{rowid}'", "fields": f}]})
        else:
            created = store._req("POST", table, None, {"records": [f]})
            if isinstance(created, list):
                created = created[0] if created else {}
            rowid = (created or {}).get("rowid")
        if rowid:
            with _lock:
                _rowids[(table, key)] = rowid
    except Exception as exc:          # the readable table is a convenience; never break a run over it
        log.warning("could not update readable table %s for %s: %s", table, key, exc)


def submit(table: Optional[str], key: str, f: dict) -> None:
    """Queue a write of one readable row (used by other features)."""
    if table:
        _pool.submit(_write_row, table, key, dict(f))


def sync(run: dict) -> None:
    """Queue an update of this run's readable row."""
    if table_id():
        _pool.submit(_write, dict(run))


def flush(timeout: float = 60) -> None:
    """Wait for queued writes (tests and scripts)."""
    _pool.submit(lambda: None).result(timeout)
