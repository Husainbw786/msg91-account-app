"""Where to find every bridge line in the ledgers: the entries behind each line, described so an accountant can go
straight to them (which ledger, sheet and row, date, voucher / invoice number, amount, narration).

Used by the web app (on screen) and the Excel report, so both describe a difference the same way.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .bridge import Bridge, BridgeLine
from .match import Match, Result
from .rows import Entry


def _date(d: Optional[dt.date]) -> str:
    return d.strftime("%d-%b-%y") if d else ""


def _ref(e: Entry) -> str:
    """The entry's number(s). On the counterparty's side, our invoice number and their own voucher number when the
    row has both (their team searches by theirs). On Walkover's side only our number (the second one there is an
    internal sequence number)."""
    refs = [x for x in ((e.ref, e.ref2) if e.side == "C" else (e.ref or e.ref2,)) if x]
    return " / ".join(dict.fromkeys(refs))


def _rows(e: Entry, word: str = "row") -> str:
    if not e.rows:
        return ""
    return f"{word} {e.rows[0]}" if len(e.rows) == 1 else f"{word}s {e.rows[0]}-{e.rows[-1]}"


def _short(e: Entry, file: str, sheet: str) -> str:
    """'True Balance ledger.xlsx › tab "Ledger" › rows 126-127 · 04-Dec-23 · CR-231204-1': the uploaded file's own
    name and tab, so the accounts team opens exactly that file. A PDF has no tabs; its rows are lines of the
    statement as read."""
    is_pdf = file.lower().endswith(".pdf")
    loc = [f"{file}"]
    if sheet and not is_pdf:
        loc.append(f'tab "{sheet}"')
    loc.append(_rows(e, "line" if is_pdf else "row"))
    where = " › ".join(x for x in loc if x)
    return " · ".join(x for x in (where, _date(e.date), _ref(e)) if x)


def _file(led, given: Optional[str]) -> str:
    return given or (led.path or "").split("/")[-1] or "ledger"


def line_details(bl: BridgeLine, res: Result, w_name: str = "Walkover", c_name: str = "Counterparty",
                 w_file: Optional[str] = None, c_file: Optional[str] = None) -> list[dict]:
    """One dict per entry (or matched pair) behind a bridge line; amount is its share of the line (paise).
    w_file / c_file: the names of the files as uploaded (shown so the right file is opened)."""
    out = []
    W, C, o = res.W, res.C, res.orient
    wf, cf = _file(W, w_file), _file(C, c_file)
    for it in bl.items:
        if it is None:                                   # computed from the two opening balances
            out.append({"ledger": "Both", "where": f"Opening balances: {w_name} {W.opening / 100:,.2f}, "
                                                    f"{c_name} {o * C.opening / 100:,.2f} (Walkover's direction)",
                        "date": "", "ref": "", "kind": "opening", "amount": None, "narration": "", "with": ""})
        elif isinstance(it, Match):
            share = -sum(a for lab, a in it.parts if lab == bl.label)
            w = it.w[0] if it.w else None
            c = it.c[0] if it.c else None
            out.append({
                "ledger": "Both",
                "where": " ↔ ".join(x for x in (
                    _short(w, wf, W.sheet) if w else "", _short(c, cf, C.sheet) if c else "") if x),
                "date": _date(w.date if w else c.date if c else None),
                "ref": ", ".join(dict.fromkeys(_ref(e) for e in it.w if _ref(e))),
                "kind": (w or c).kind if (w or c) else "",
                "amount": share,
                "narration": it.note,
                "with": ", ".join(dict.fromkeys(_ref(e) for e in it.c if _ref(e))),
            })
        elif isinstance(it, Entry):
            is_w = it.side == "W"
            name, led = (w_name, W) if is_w else (c_name, C)
            amount = (it.dr - it.cr) if is_w else o * (it.dr - it.cr)
            out.append({"ledger": name, "where": _short(it, wf if is_w else cf, led.sheet), "date": _date(it.date),
                        "ref": _ref(it), "kind": it.kind, "amount": -amount if is_w else amount,
                        "narration": (it.narration or "")[:120], "with": ""})
    out.sort(key=lambda d: (d["date"] == "", dt.datetime.strptime(d["date"], "%d-%b-%y") if d["date"] else 0))
    return out


def line_summary(details: list[dict], limit: int = 3) -> str:
    """A one-line 'where to find it' for a bridge line: the entries themselves when few, else a pointer."""
    if not details:
        return ""
    if len(details) <= limit:
        parts = []
        for d in details:
            s = d["where"]
            if d["amount"] is not None and len(details) > 1:
                s += f" ({d['amount'] / 100:,.2f})"
            parts.append(s)
        return "; ".join(parts)
    return f"{len(details)} entries, listed on the 'Bridge details' sheet"


def bridge_details(br: Bridge, res: Result, w_name: str = "Walkover", c_name: str = "Counterparty",
                   w_file: Optional[str] = None, c_file: Optional[str] = None) -> list[list[dict]]:
    return [line_details(bl, res, w_name, c_name, w_file, c_file) for bl in br.lines]
