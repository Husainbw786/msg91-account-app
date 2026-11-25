"""Save / load an extracted ledger as JSON, so the reconciliation runs on exactly what was reviewed
(no second extraction, no second AI call at reconcile time)."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, fields

from .detect import Table
from .parser import Ledger
from .rows import Entry
from .validate import Check, Reading

_TABLE_META_KEYS = ("samples", "helper_columns", "ai_layout", "user_mapping", "known_format", "party_account",
                    "fingerprint")


def _d(x):
    return x.isoformat() if isinstance(x, (dt.date, dt.datetime)) else x


def _pd(x):
    return dt.date.fromisoformat(x) if x else None


def ledger_to_dict(led: Ledger) -> dict:
    return {
        "path": led.path, "side": led.side, "sheet": led.sheet, "opening": led.opening,
        "closing_printed": led.closing_printed, "confidence": led.confidence, "party_account": led.party_account,
        "other_account_rows": led.other_account_rows, "notes": led.notes, "other_sheets": led.other_sheets,
        "meta": getattr(led, "meta", {}) or {},
        "reading": {"sign": led.reading.sign, "bal_sign": led.reading.bal_sign,
                    "tds_reduces": led.reading.tds_reduces, "balance_col": led.reading.balance_col},
        "checks": [asdict(c) for c in led.checks],
        "tables": [{"sheet": t.sheet, "header_row": t.header_row, "start": t.start, "end": t.end,
                    "labels": {str(k): v for k, v in t.labels.items()}, "roles": t.roles, "layout": t.layout,
                    "notes": t.notes,
                    "meta": {k: ({str(c): v for c, v in t.meta[k].items()} if k == "samples" else t.meta[k])
                             for k in _TABLE_META_KEYS if k in t.meta}} for t in led.tables],
        "entries": [{f.name: _d(getattr(e, f.name)) for f in fields(Entry)} for e in led.entries],
    }


def ledger_from_dict(d: dict) -> Ledger:
    tables = []
    for td in d["tables"]:
        t = Table(td["sheet"], td["header_row"], td["start"], td["end"], {int(k): v for k, v in td["labels"].items()})
        t.roles, t.layout, t.notes = dict(td["roles"]), td["layout"], list(td["notes"])
        meta = dict(td.get("meta", {}))
        if "samples" in meta:
            meta["samples"] = {int(k): v for k, v in meta["samples"].items()}
        t.meta = meta
        tables.append(t)
    entries = []
    for ed in d["entries"]:
        ed = dict(ed)
        ed["date"], ed["doc_date"] = _pd(ed.get("date")), _pd(ed.get("doc_date"))
        entries.append(Entry(**ed))
    rd = d["reading"]
    led = Ledger(d["path"], d["side"], d["sheet"], tables, entries, d["opening"], d["closing_printed"],
                 [Check(**c) for c in d["checks"]], d["confidence"],
                 Reading(sign=rd["sign"], bal_sign=rd["bal_sign"], tds_reduces=rd["tds_reduces"],
                         balance_col=rd["balance_col"]),
                 d.get("party_account"), d.get("other_account_rows", 0), list(d.get("notes", [])),
                 list(d.get("other_sheets", [])))
    led.meta = d.get("meta", {})
    return led
