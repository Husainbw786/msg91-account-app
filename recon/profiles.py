"""Counterparty profiles and format fingerprints.

A format fingerprint is a hash of a table's header labels and layout. When an upload matches a
saved fingerprint, the saved column roles and reading are reused (and still proven by Stage 2).
A counterparty profile keeps what was learned about that company: orientation, net/gross TDS
booking, TDS rate, document prefixes and party account.

Storage is a JSON file for now; the web app will move this to PostgreSQL behind the same API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from .detect import Table


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fingerprint(t: Table) -> str:
    labels = [_norm(t.labels[c]) for c in sorted(t.labels) if _norm(t.labels[c])]
    raw = t.layout + "|" + "|".join(labels)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class ProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = {"formats": {}, "counterparties": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        self.data.setdefault("formats", {})
        self.data.setdefault("counterparties", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, default=str))

    # ------------------------------------------------------------- formats
    def get_format(self, fp: str) -> Optional[dict]:
        return self.data["formats"].get(fp)

    def put_format(self, fp: str, t: Table, reading: dict, source: str) -> None:
        roles = {role: t.labels.get(col, "") for role, col in t.roles.items()}
        f = self.data["formats"].setdefault(fp, {"first_seen": str(dt.date.today()), "uses": 0})
        f.update({"layout": t.layout, "roles": roles, "reading": reading, "last_source": source})
        f["uses"] = f.get("uses", 0) + 1

    # ------------------------------------------------------------- counterparties
    def get_counterparty(self, name: str) -> dict:
        return self.data["counterparties"].get(_norm(name), {})

    def put_counterparty(self, name: str, **facts) -> None:
        cp = self.data["counterparties"].setdefault(_norm(name), {"name": name, "runs": []})
        for k, v in facts.items():
            if v is not None:
                cp[k] = v
        cp["runs"] = (cp.get("runs") or [])[-19:] + [str(dt.datetime.now().replace(microsecond=0))]


def apply_saved_roles(t: Table, fmt: dict) -> bool:
    """Map saved role -> header label back onto this table's columns. True if every role was found."""
    by_label: dict[str, list[int]] = {}
    for c, lab in t.labels.items():
        by_label.setdefault(_norm(lab), []).append(c)
    roles: dict[str, int] = {}
    used: set[int] = set()
    for role, lab in fmt.get("roles", {}).items():
        cols = [c for c in by_label.get(_norm(lab), []) if c not in used]
        if not cols:
            return False
        roles[role] = cols[0]
        used.add(cols[0])
    t.roles = roles
    t.layout = fmt.get("layout", t.layout)
    return True
