"""Background work for a run: extraction (AI unless EXTRACTION=rules), then the maths-only reconciliation.

Runs in a worker thread (FastAPI background task).
"""
from __future__ import annotations

import threading
import traceback
import warnings

from recon.ai import AIStats, default_backend
from recon.ledger_io import ledger_from_dict, ledger_to_dict
from recon.parser import sheet_names
from recon.pipeline import ExtractionUnavailable, extract, extraction_mode, reconcile_ledgers
from recon.profiles import ProfileStore
from recon.report import write_report

from . import runs
from .serialize import ledger_preview, result_json
from .store import get_store

warnings.filterwarnings("ignore")
_profile_lock = threading.Lock()


def _load_profiles() -> ProfileStore:
    ps = ProfileStore.__new__(ProfileStore)
    ps.path = None
    ps.data = get_store().get("profiles", "all") or {"formats": {}, "counterparties": {}}
    ps.data.setdefault("formats", {})
    ps.data.setdefault("counterparties", {})
    return ps


def _save_profiles(ps: ProfileStore) -> None:
    get_store().put("profiles", "all", ps.data)


def parse_run(run_id: str, sides: tuple[str, ...] = ("W", "C")) -> None:
    """Extraction. Saves the extracted ledgers for review and for reconcile."""
    try:
        run = runs.get(run_id)
        preview = runs.get_part(run_id, "preview") or {}
        stats = AIStats()
        ai = default_backend(stats) if extraction_mode() == "ai" else None
        with _profile_lock:
            store = _load_profiles()
            for side in sides:
                key = "w" if side == "W" else "c"
                path = runs.upload_path(run, key)
                led = extract(path, side, run["counterparty"], store, ai, stats, run.get(f"{key}_sheet"),
                              run.get(f"{key}_overrides"))
                preview[side] = ledger_preview(led, sheet_names(path))
                preview[side]["extraction"] = (led.meta or {}).get("extraction", "ai")
                runs.put_part(run_id, f"{key}_ledger", ledger_to_dict(led))
                run[f"{key}_sheet"] = led.sheet
            _save_profiles(store)
        preview["ai"] = {"calls": stats.calls, "accepted": stats.accepted, "rejected": stats.rejected,
                         "failures": stats.failures, "log": stats.log[:60]}
        runs.put_part(run_id, "preview", preview)
        runs.update(run_id, status="parsed", error=None, w_sheet=run.get("w_sheet"), c_sheet=run.get("c_sheet"),
                    has_preview=True, has_result=False, has_report=False, residual=None, c_closing=None)
    except ExtractionUnavailable as exc:
        runs.update(run_id, status="failed", error=str(exc))
    except Exception as exc:  # surfaced to the user, with the cause
        runs.update(run_id, status="failed", error=f"Could not read the files: {exc}")
        traceback.print_exc()


def reconcile_run(run_id: str) -> None:
    """Pure maths on the saved extractions. No AI call happens here."""
    try:
        run = runs.get(run_id)
        wd, cd = runs.get_part(run_id, "w_ledger"), runs.get_part(run_id, "c_ledger")
        if not wd or not cd:
            raise ValueError("the ledgers have not been extracted yet")
        W, C = ledger_from_dict(wd), ledger_from_dict(cd)
        with _profile_lock:
            store = _load_profiles()
            out = reconcile_ledgers(W, C, run["counterparty"], store)
            _save_profiles(store)
        result = result_json(out.res, out.bridge, out.tds, run["counterparty"])
        runs.put_part(run_id, "result", result)
        runs.update(run_id, status="done", error=None, has_result=True, has_report=True,
                    residual=result["bridge"]["residual"], c_closing=result["bridge"]["c_closing"],
                    w_closing=result["bridge"]["w_closing"])
    except Exception as exc:
        runs.update(run_id, status="failed", error=f"Reconciliation failed: {exc}")
        traceback.print_exc()


def build_report(run: dict) -> bytes:
    """The Excel report, rebuilt from the saved extractions. reconcile() depends only on the two ledgers, so this
    gives the same report every time; nothing is stored."""
    wd, cd = runs.get_part(run["id"], "w_ledger"), runs.get_part(run["id"], "c_ledger")
    if not wd or not cd:
        raise FileNotFoundError("the extracted ledgers are no longer stored")
    out = reconcile_ledgers(ledger_from_dict(wd), ledger_from_dict(cd), run["counterparty"])
    path = runs.scratch_path(run["id"], "report.xlsx")
    write_report(out.res, out.bridge, str(path), "Walkover", run["counterparty"], out.tds)
    return path.read_bytes()
