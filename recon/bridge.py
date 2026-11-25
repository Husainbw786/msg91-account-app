"""Stage 5 (phase 1): sort every difference into a named bucket and bridge the two closing balances.

Walkover closing + sum(bucket adjustments) = counterparty closing (in Walkover's direction).
This holds by construction when both ledgers were parsed completely, so a residual against the
*printed* closing balances points at a parsing gap, never at a silent reconciliation error.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field

from .classify import CN, DN, INVOICE, JOURNAL, OPENING, PAYMENT, REFUND, TDS
from .match import Result, vw
from .rows import Entry


@dataclass
class BridgeLine:
    label: str
    amount: int                       # adjustment added to Walkover's closing (paise)
    count: int
    category: str                     # timing | not accounted | tds | posting | amount | rounding | opening
    items: list = field(default_factory=list)


@dataclass
class Bridge:
    w_closing: int
    c_closing_w: int                  # counterparty closing, in Walkover's direction
    lines: list[BridgeLine]
    w_closing_printed: int | None
    c_closing_printed_w: int | None

    @property
    def derived(self) -> int:
        return self.w_closing + sum(l.amount for l in self.lines)

    @property
    def residual(self) -> int:
        return self.c_closing_w - self.derived


def _w_bucket(e: Entry, c_last) -> tuple[str, str]:
    late = c_last is not None and e.date is not None and e.date > c_last
    if e.kind == OPENING:                            # "To Opening Balance" printed as a dated row
        return "Opening balance difference", "opening"
    if e.kind == TDS:
        return "TDS as per 26AS booked by Walkover", "tds"
    if e.kind == INVOICE:
        return ("Timing: Walkover invoice after the counterparty's last entry", "timing") if late else \
            ("Invoice booked by Walkover, not yet accounted by counterparty", "not accounted")
    if e.kind == CN:
        return "Credit note issued by Walkover, not yet accounted by counterparty", "not accounted"
    if e.kind in (PAYMENT,):
        return ("Timing: receipt after the counterparty's last entry", "timing") if late else \
            ("Receipt booked by Walkover, not shown in counterparty's ledger", "not accounted")
    if e.kind == REFUND:
        return "Refund paid by Walkover, not shown in counterparty's ledger", "not accounted"
    return "Other entry in Walkover's books, not shown in counterparty's ledger", "not accounted"


def _key_date(e: Entry):
    """Invoice date coded in a YYMMDD-N key, else the document date, else the posting date."""
    import datetime as dt
    for k in [e.inv_key] + list(e.keys or []):      # any reference that is a YYMMDD-N invoice number
        if k and re.fullmatch(r"\d{6}-\d+", k):
            try:
                return dt.date(2000 + int(k[:2]), int(k[2:4]), int(k[4:6]))
            except ValueError:
                continue
    return e.doc_date or e.date


def _c_bucket(e: Entry, w_last, w_first=None) -> tuple[str, str]:
    late = w_last is not None and e.date is not None and e.date > w_last
    if e.kind == OPENING:                            # "To Opening Balance" printed as a dated row
        return "Opening balance difference", "opening"
    kd = _key_date(e)
    if w_first is not None and kd is not None and kd < w_first and e.kind == INVOICE:
        return "Counterparty invoice dated before Walkover's ledger period (sits in Walkover's opening balance)", \
            "opening"
    if e.kind in (TDS, JOURNAL) and e.inv_key:
        return "Counterparty deduction / adjustment journal against an invoice", "tds"
    if e.kind == TDS:
        return "TDS entry in counterparty ledger only", "tds"
    if e.kind == INVOICE:
        return ("Timing: counterparty invoice after Walkover's last entry", "timing") if late else \
            ("Entry booked by counterparty, not yet accounted in Walkover's books", "not accounted")
    if e.kind in (CN, DN):
        return "Credit/debit note raised by counterparty, not yet accounted in Walkover's books", "not accounted"
    if e.kind in (PAYMENT, REFUND):
        return ("Timing: payment after Walkover's last entry", "timing") if late else \
            ("Payment made by counterparty, not yet accounted in Walkover's books", "not accounted")
    return "Other entry in counterparty's ledger, not yet accounted in Walkover's books", "not accounted"


def build_bridge(res: Result) -> Bridge:
    W, C, o = res.W, res.C, res.orient
    buckets: "OrderedDict[str, BridgeLine]" = OrderedDict()

    def add(label: str, amount: int, cat: str, item) -> None:
        bl = buckets.setdefault(label, BridgeLine(label, 0, 0, cat))
        bl.amount += amount
        bl.count += 1
        bl.items.append(item)

    open_diff = W.opening - o * C.opening
    if open_diff:
        add("Opening balance difference", -open_diff, "opening", None)
    for m in res.matches:
        for lab, amt in m.parts:
            cat = {"Rounding": "rounding", "Amount differs": "amount"}.get(lab, "posting" if "posted" in lab else "tds")
            add(lab, -amt, cat, m)
    for e in res.unmatched_w:
        lab, cat = _w_bucket(e, C.last_date)
        add(lab, -vw(e), cat, e)
    for e in res.unmatched_c:
        lab, cat = _c_bucket(e, W.last_date, W.first_date)
        add(lab, o * (e.dr - e.cr), cat, e)
    order = {"opening": 0, "posting": 1, "timing": 2, "not accounted": 3, "tds": 4, "amount": 5, "rounding": 9}
    lines = sorted(buckets.values(), key=lambda b: (order.get(b.category, 6), -abs(b.amount)))
    cpw = o * C.closing_printed if C.closing_printed is not None else None
    return Bridge(W.closing, o * C.closing, lines, W.closing_printed, cpw)
