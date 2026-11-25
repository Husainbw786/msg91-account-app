"""Where the app keeps its data.

A small document store with two backends, chosen by STORAGE:
  dbdash   viaSocket Table (DB Dash, table.viasocket.com) over its REST API: tables users, runs, files and
           profiles, each with a `name` key column and a `data` long-text column.
  sqlite   a local SQLite file under DATA_DIR (development and tests).

Documents are JSON objects; files (uploads, extractions, reports) are blobs, stored base64 and split into
chunks so no single cell has to hold a large ledger.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

COLLECTIONS = ("users", "runs", "profiles", "checks")
CHUNK = int(os.environ.get("STORE_CHUNK_CHARS", "500000"))     # DB Dash long text held 1,000,000 chars in a test


def data_dir() -> Path:
    d = Path(os.environ.get("RECON_DATA_DIR") or os.environ.get("DATA_DIR")
             or Path(__file__).resolve().parent.parent / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


class StoreError(RuntimeError):
    pass


# ============================================================================ SQLite
class SqliteStore:
    def __init__(self, path: Optional[Path] = None):
        import sqlite3
        self.path = str(path or data_dir() / "store.db")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS docs (coll TEXT, key TEXT, data TEXT, PRIMARY KEY (coll, key))")
        self._db.execute("CREATE TABLE IF NOT EXISTS blobs (key TEXT PRIMARY KEY, data BLOB)")
        self._db.commit()

    def get(self, coll: str, key: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT data FROM docs WHERE coll=? AND key=?", (coll, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, coll: str, key: str, doc: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO docs (coll, key, data) VALUES (?, ?, ?)",
                             (coll, key, json.dumps(doc, default=str)))
            self._db.commit()

    def list(self, coll: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM docs WHERE coll=?", (coll,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def put_blob(self, key: str, data: bytes) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO blobs (key, data) VALUES (?, ?)", (key, data))
            self._db.commit()

    def get_blob(self, key: str) -> Optional[bytes]:
        with self._lock:
            row = self._db.execute("SELECT data FROM blobs WHERE key=?", (key,)).fetchone()
        return bytes(row[0]) if row else None


# ============================================================================ DB Dash (viaSocket Table)
class DbDashStore:
    """REST API of https://table-api.viasocket.com/<db>/<table>, header `auth-key` (formats checked live):
      list   GET  ?filter=name='x'&limit&offset   (identical GETs are cached by the API: a `_` param busts it)
      add    POST {"records": [{...}]}
      update PATCH {"records": [{"where": "rowid='...'", "fields": {...}}]}

    Settings: DBDASH_DB_ID, DBDASH_AUTH_KEY, DBDASH_TABLE_USERS, DBDASH_TABLE_RUNS, DBDASH_TABLE_FILES,
    DBDASH_TABLE_PROFILES, DBDASH_TABLE_CHECKS (table ids such as tbl15z5cg). Each table has the fields `name` and `data`."""

    BASE = os.environ.get("DBDASH_API", "https://table-api.viasocket.com")

    def __init__(self):
        self.db = os.environ["DBDASH_DB_ID"]
        self.key = os.environ["DBDASH_AUTH_KEY"]
        self.tables = {c: os.environ[f"DBDASH_TABLE_{c.upper()}"] for c in ("users", "runs", "files", "profiles")}
        # payment checks have their own table; until it is set they live in the runs table (kind "paycheck")
        self.tables["checks"] = os.environ.get("DBDASH_TABLE_CHECKS") or self.tables["runs"]
        self._rowids: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- HTTP
    def _req(self, method: str, table: str, params: Optional[dict] = None, body=None):
        url = f"{self.BASE}/{self.db}/{table}"
        headers = {"auth-key": self.key, "Content-Type": "application/json", "User-Agent": "ledger-recon/1.0"}
        with httpx.Client(timeout=60) as c:
            r = c.request(method, url, params=params, json=body, headers=headers)
        try:
            payload = r.json()
        except ValueError:
            raise StoreError(f"DB Dash {method} {table}: {r.status_code} {r.text[:200]}")
        if r.status_code >= 400 or payload.get("success") is False:
            raise StoreError(f"DB Dash {method} {table}: {r.status_code} {str(payload.get('message'))[:200]}")
        return payload.get("data")

    @staticmethod
    def _filter(name: str) -> str:
        return "name='" + str(name).replace("'", "\\'") + "'"

    def _rows(self, table: str, flt: Optional[str] = None) -> list[dict]:
        rows, offset = [], None
        while True:
            params = {"limit": "500", "_": f"{time.time():.6f}"}
            if flt:
                params["filter"] = flt
            if offset:
                params["offset"] = offset
            data = self._req("GET", table, params) or {}
            rows += data.get("rows") or []
            offset = data.get("offset")
            if not offset:
                return rows

    def _find(self, table: str, name: str) -> Optional[dict]:
        rows = [r for r in self._rows(table, self._filter(name)) if r.get("name") == name]
        return rows[0] if rows else None

    def _upsert(self, table: str, name: str, data: str) -> None:
        with self._lock:
            rowid = self._rowids.get((table, name))
        if rowid is None:
            row = self._find(table, name)
            rowid = row.get("rowid") if row else None
        if rowid:
            self._req("PATCH", table, None, {"records": [{"where": f"rowid='{rowid}'", "fields": {"data": data}}]})
        else:
            created = self._req("POST", table, None, {"records": [{"name": name, "data": data}]})
            if isinstance(created, list):
                created = created[0] if created else {}
            rowid = (created or {}).get("rowid")
        if rowid:
            with self._lock:
                self._rowids[(table, name)] = rowid

    # ---------------------------------------------------------------- documents
    def get(self, coll: str, key: str) -> Optional[dict]:
        row = self._find(self.tables[coll], key)
        if not row or not row.get("data"):
            return None
        with self._lock:
            self._rowids[(self.tables[coll], key)] = row.get("rowid")
        return json.loads(row["data"])

    def put(self, coll: str, key: str, doc: dict) -> None:
        self._upsert(self.tables[coll], key, json.dumps(doc, default=str))

    def list(self, coll: str) -> list[dict]:
        out = []
        for r in self._rows(self.tables[coll]):
            if r.get("name") and r.get("data"):
                try:
                    out.append(json.loads(r["data"]))
                except json.JSONDecodeError:
                    continue
        return out

    # ---------------------------------------------------------------- blobs, chunked
    def put_blob(self, key: str, data: bytes) -> None:
        text = base64.b64encode(data).decode()
        parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or [""]
        for i, part in enumerate(parts):
            self._upsert(self.tables["files"], f"{key}#{i}", part)
        self._upsert(self.tables["files"], key, json.dumps({"chunks": len(parts), "bytes": len(data)}))

    def get_blob(self, key: str) -> Optional[bytes]:
        head = self._find(self.tables["files"], key)
        if not head or not head.get("data"):
            return None
        n = json.loads(head["data"])["chunks"]
        text = ""
        for i in range(n):
            row = self._find(self.tables["files"], f"{key}#{i}")
            if row is None:
                raise StoreError(f"file {key} is missing part {i}")
            text += row.get("data") or ""
        return base64.b64decode(text)


# ============================================================================ choice
_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            kind = os.environ.get("STORAGE", "dbdash" if os.environ.get("DBDASH_AUTH_KEY") else "sqlite").lower()
            _store = DbDashStore() if kind == "dbdash" else SqliteStore()
        return _store


def reset_store() -> None:
    """For tests: forget the current backend."""
    global _store
    with _store_lock:
        _store = None


def put_json(key: str, obj) -> None:
    get_store().put_blob(key, json.dumps(obj, default=str).encode())


def get_json(key: str):
    raw = get_store().get_blob(key)
    return json.loads(raw) if raw else None
