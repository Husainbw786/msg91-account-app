"""Google Sheets links -> the whole workbook as .xlsx (every tab), so sheet selection works as for uploads.

Only docs.google.com spreadsheet links are accepted (no other hosts are fetched). The sheet must be shared
as 'Anyone with the link can view'; a private sheet returns Google's sign-in page and is reported as such.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

MAX_BYTES = 25 * 1024 * 1024
ALLOWED_REDIRECT_HOSTS = ("docs.google.com", "googleusercontent.com")
_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")


def sheet_id(url: str) -> str:
    u = urlparse((url or "").strip())
    if u.scheme != "https" or u.hostname != "docs.google.com":
        raise HTTPException(400, "Paste a Google Sheets link that starts with https://docs.google.com/spreadsheets/")
    m = _ID_RE.search(u.path)
    if not m:
        raise HTTPException(400, "That link does not point to a Google Sheet.")
    return m.group(1)


def fetch_xlsx(url: str, timeout: float = 30.0) -> bytes:
    sid = sheet_id(url)
    export = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        target = export
        for _ in range(5):
            resp = client.get(target)
            if resp.status_code in (301, 302, 303, 307, 308):
                nxt = resp.headers.get("location", "")
                host = urlparse(nxt).hostname or ""
                if "accounts.google.com" in host or "ServiceLogin" in nxt:
                    raise HTTPException(400, _PRIVATE)
                if not any(host == h or host.endswith("." + h) for h in ALLOWED_REDIRECT_HOSTS):
                    raise HTTPException(400, "Google redirected to an unexpected address; download the sheet as "
                                             "Excel and upload it instead.")
                target = nxt
                continue
            break
        else:
            raise HTTPException(400, "Too many redirects while fetching the Google Sheet.")
    if resp.status_code in (401, 403, 404):
        raise HTTPException(400, _PRIVATE)
    if resp.status_code != 200:
        raise HTTPException(400, f"Google returned an error ({resp.status_code}) for that sheet.")
    data = resp.content
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "The Google Sheet is larger than 25 MB.")
    if not data.startswith(b"PK"):                    # sign-in page or other HTML instead of a workbook
        raise HTTPException(400, _PRIVATE)
    return data


_PRIVATE = ("This Google Sheet is not shared. In Google Sheets choose Share → General access → 'Anyone with the "
            "link' (Viewer), or download it as Excel (.xlsx) and upload the file.")
