"""Stage 1.6 / 1.7: turn table rows into ledger entries.

Handles multi-line vouchers, narration continuation rows, control rows (totals, opening,
closing), subtotal rows, and GL dumps that contain every account (party filter).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .detect import CONTROL_RE, Table, header_score
from .grid import AMOUNT, DATE, EMPTY, REF, Sheet

ROUND_OFF_RE = re.compile(r"round\s*off|rounding", re.I)
PARTY_ACCOUNT_RE = re.compile(r"creditor|debtor|vendor|supplier|customer|payable|receivable|sundry|control", re.I)


@dataclass
class Entry:
    side: str                          # 'W' (Walkover) or 'C' (counterparty)
    idx: int
    rows: list[int]                    # 1-based source row numbers
    date: Optional[dt.date]
    doc_date: Optional[dt.date] = None
    vtype: str = ""
    ref: str = ""
    ref2: str = ""
    narration: str = ""
    account: str = ""
    dr: int = 0                        # paise, in the file's own books
    cr: int = 0
    tds: int = 0                       # deduction shown in a TDS column (positive)
    voucher_total: Optional[int] = None
    base: Optional[int] = None
    gst: Optional[int] = None
    net: Optional[int] = None
    balance: Optional[int] = None      # running balance printed on the entry's last line (Dr +)
    status: str = ""
    flags: list[str] = field(default_factory=list)
    # filled by classify
    kind: str = ""
    kind_why: str = ""
    inv_key: str = ""
    keys: list[str] = field(default_factory=list)
    utrs: list[str] = field(default_factory=list)
    refs_to: list[str] = field(default_factory=list)   # invoice keys this entry refers to (revisions, CNs)

    @property
    def amount(self) -> int:
        """Signed amount in the file's own books: debit positive."""
        return self.dr - self.cr

    def label(self) -> str:
        return self.ref or self.ref2 or self.narration[:40]


@dataclass
class Controls:
    opening: Optional[int] = None      # Dr positive
    closing: Optional[int] = None
    total_dr: Optional[int] = None
    total_cr: Optional[int] = None
    role_totals: dict = field(default_factory=dict)   # role -> printed total on a 'Total' row
    total_row: Optional[int] = None                    # 1-based row of the first 'Total' row
    notes: list[str] = field(default_factory=list)


def _val(sheet: Sheet, r: int, t: Table, role: str):
    c = t.roles.get(role)
    return sheet.cell(r, c) if c is not None else None


def _paise(sheet: Sheet, r: int, t: Table, role: str) -> Optional[int]:
    cell = _val(sheet, r, t, role)
    if cell is None or cell.kind != AMOUNT:
        return None
    return cell.paise


def _txt(sheet: Sheet, r: int, t: Table, role: str) -> str:
    cell = _val(sheet, r, t, role)
    if cell is None or cell.kind == EMPTY:
        return ""
    if cell.kind == AMOUNT and isinstance(cell.value, float) and cell.value.is_integer():
        return str(int(cell.value))
    return cell.text


def _row_amounts(sheet: Sheet, r: int, t: Table) -> tuple[int, int, bool]:
    """(dr, cr, any_amount_present) for a row, by layout. Negative debits become credits."""
    dr = cr = 0
    present = False
    if t.layout == "drcr":
        d = _paise(sheet, r, t, "debit")
        c = _paise(sheet, r, t, "credit")
        for v, is_dr in ((d, True), (c, False)):
            if v is None:
                continue
            present = True
            if v < 0:                       # negative value in a Dr column is a Cr (and vice versa)
                is_dr = not is_dr
                v = -v
            if is_dr:
                dr += v
            else:
                cr += v
    elif t.layout == "flag":
        a = _paise(sheet, r, t, "amount")
        f = _txt(sheet, r, t, "drcr").lower()
        if a is not None:
            present = True
            if f.startswith("c"):
                cr += abs(a)
            else:
                dr += abs(a)
    elif t.layout == "register":
        g = _paise(sheet, r, t, "gross")
        if g is None and "net" in t.roles:
            g = _paise(sheet, r, t, "net")
        if g is not None:
            present = True
            # register = list of invoices; sign is decided later by orientation. Store as debit.
            if g >= 0:
                dr += g
            else:
                cr += -g
    return dr, cr, present


def _is_control(sheet: Sheet, r: int, t: Table, has_date: bool) -> Optional[str]:
    """A totals/opening/closing row. On a dated row only the label columns count, so a stray
    note like 'Total O/s' typed beside a real invoice does not swallow the invoice."""
    label_cols = {t.roles[k] for k in ("date", "narration", "ref", "ref2", "vtype", "party", "account") if k in t.roles}
    for cell in sheet.rows[r]:
        if has_date and cell.c not in label_cols:
            continue
        if cell.kind not in (EMPTY, AMOUNT, DATE) and CONTROL_RE.match(cell.text):
            return cell.text.strip().lower()
    if not has_date:                     # a label split across cells ("Opening" | "Balance" in a PDF)
        joined = " ".join(c.text.strip() for c in sheet.rows[r] if c.kind not in (EMPTY, AMOUNT, DATE) and c.text)
        if joined and CONTROL_RE.match(joined):
            return joined.lower()
    return None


def _first_amount(sheet: Sheet, r: int, t: Table) -> Optional[int]:
    """Signed amount on a control row (Dr +), looking at debit/credit/balance/gross columns."""
    for role, sign in (("debit", 1), ("credit", -1), ("balance", 1), ("gross", 1), ("amount", 1)):
        v = _paise(sheet, r, t, role)
        if v:
            cell = _val(sheet, r, t, role)
            if cell is not None and cell.drcr:   # '650,017.41 Dr.' already signed
                return v
            return v * sign
    return None


def build_entries(sheet: Sheet, t: Table, side: str) -> tuple[list[Entry], Controls]:
    entries: list[Entry] = []
    ctl = Controls()
    cur: Optional[Entry] = None
    after_total = False
    sums_dr = sums_cr = 0

    def close():
        nonlocal cur
        if cur is not None:
            entries.append(cur)
        cur = None

    for r in range(t.start, t.end + 1):
        row = sheet.rows[r]
        if not any(c.kind != EMPTY for c in row):
            continue
        if r != t.header_row and header_score(sheet, r)[0] >= 4:
            continue                                        # repeated header (page break)
        date_cell = _val(sheet, r, t, "date")
        has_date = date_cell is not None and date_cell.kind == DATE
        dr, cr, present = _row_amounts(sheet, r, t)
        ctrl = _is_control(sheet, r, t, has_date)
        narr = _txt(sheet, r, t, "narration")
        ref = _txt(sheet, r, t, "ref")
        ref2 = _txt(sheet, r, t, "ref2")

        # ---- control rows
        if ctrl:
            close()
            amt = _first_amount(sheet, r, t)
            if ctrl.startswith("opening") and not has_date:
                ctl.opening = amt if amt is not None else 0
                continue
            if ctrl.startswith("opening") and has_date:
                pass                                        # dated opening row is an entry (some manual sheets)
            else:
                totals = {}
                for role in ("debit", "credit", "gross", "base", "gst", "tds", "net"):
                    v = _paise(sheet, r, t, role)
                    if v is not None:
                        totals[role] = v
                if ctrl.startswith(("closing", "balance to pay", "net balance")):
                    b = _paise(sheet, r, t, "balance")
                    if b is not None:
                        ctl.closing = b
                    elif not ("debit" in totals and "credit" in totals):
                        ctl.closing = amt
                        ctl.closing_in_column = amt is not None
                if "debit" in totals and "credit" in totals and t.layout == "drcr" and ctl.total_dr is None \
                        and not ctrl.startswith(("balance to pay",)):
                    ctl.total_dr, ctl.total_cr = abs(totals["debit"]), abs(totals["credit"])
                if ctrl.startswith(("total", "grand", "sub")) or totals:
                    if ctrl.startswith(("total", "grand", "sub")):
                        after_total = True
                    if ctl.total_row is None and totals and not ctrl.startswith(("balance to pay",)):
                        ctl.total_row = r + 1
                        ctl.role_totals = totals
                ctl.notes.append(f"row {r + 1}: control row '{ctrl[:30]}' skipped")
                continue

        if after_total and not has_date:
            continue                                        # scratch numbers under a totals row
        if has_date:
            after_total = False

        # ---- subtotal row without a label: no date, no text, several amount columns filled, and the
        # values are close to the running column sums or far larger than any single entry
        if not has_date and not narr and not ref and present and t.layout == "drcr":
            filled = sum(1 for ro in ("debit", "credit", "tds") if _paise(sheet, r, t, ro) is not None)
            biggest = max((max(e.dr, e.cr) for e in entries), default=0)
            near = lambda a, b: abs(a - b) <= max(100 + len(entries), abs(b) // 50)
            if filled >= 2 and ((near(dr, sums_dr) and near(cr, sums_cr)) or max(dr, cr) > 10 * max(biggest, 1)):
                close()
                if ctl.total_row is None:
                    ctl.total_row = r + 1
                    ctl.role_totals = {ro: v for ro in ("debit", "credit", "tds")
                                       if (v := _paise(sheet, r, t, ro)) is not None}
                ctl.notes.append(f"row {r + 1}: unlabelled totals row skipped")
                continue

        if has_date and (present or ref):
            # Walkover-style round-off journal: same voucher no as the entry above, tiny amount -> same voucher
            vt = _paise(sheet, r, t, "voucher_total")
            same_voucher = (cur is not None and ref and ref == cur.ref and date_cell.date == cur.date
                            and vt is not None and cur.voucher_total is not None and abs(vt) == cur.voucher_total)
            if cur is not None and ref and ref == cur.ref and (ROUND_OFF_RE.search(narr) or abs(dr - cr) < 100
                                                                or same_voucher):
                cur.dr += dr
                cur.cr += cr
                cur.rows.append(r + 1)
                cur.balance = _bal(sheet, r, t, cur.balance)
                sums_dr += dr
                sums_cr += cr
                if narr and not ROUND_OFF_RE.search(narr):
                    cur.account = (cur.account + "; " + narr).strip("; ")
                continue
            close()
            cur = _new_entry(sheet, r, t, side, len(entries), date_cell.date, dr, cr)
            sums_dr += dr
            sums_cr += cr
            if date_cell.repaired:
                cur.flags.append(f"date repaired from '{date_cell.text}'")
            if ctrl and ctrl.startswith("opening"):
                cur.kind, cur.kind_why = "opening", "row says opening balance"
            continue

        vtype_here = _val(sheet, r, t, "vtype")
        vtype_here = vtype_here.text.strip() if vtype_here is not None and vtype_here.kind != EMPTY else ""
        if not has_date and present and cur is not None and ((ref or ref2) and ref != cur.ref or
                                                             (vtype_here and not ref and not ref2)):
            # a separate entry whose date is missing: inherit the previous date and flag it
            close()
            prev = entries[-1].date if entries else None
            cur = _new_entry(sheet, r, t, side, len(entries), prev, dr, cr)
            cur.flags.append("date missing in source; previous date used")
            sums_dr += dr
            sums_cr += cr
            continue

        if cur is None:
            continue
        if present:                                          # another line of the same voucher
            cur.dr += dr
            cur.cr += cr
            sums_dr += dr
            sums_cr += cr
            cur.rows.append(r + 1)
            cur.balance = _bal(sheet, r, t, cur.balance)
            if narr and not ROUND_OFF_RE.search(narr):
                cur.account = cur.account or narr
            continue
        # narration continuation
        text = " ".join(c.text for c in row if c.kind not in (EMPTY,)).strip()
        if text:
            cur.narration = (cur.narration + " | " + text).strip(" |")
            cur.rows.append(r + 1)
    close()
    # T-format ledgers (Tally prints): "To Closing Balance" sits in the column that balances the account, so a
    # figure printed under Debit is a credit balance. Accept that reading only when the arithmetic proves it.
    if getattr(ctl, "closing_in_column", False) and ctl.closing and entries:
        calc = (ctl.opening or 0) + sum(e.dr - e.cr for e in entries)
        if abs(calc + ctl.closing) <= 100 and abs(calc - ctl.closing) > 100:
            ctl.notes.insert(0, f"Closing balance {abs(ctl.closing) / 100:,.2f} is printed as the balancing figure "
                                f"(T-format ledger), so it is a {'credit' if ctl.closing > 0 else 'debit'} balance.")
            ctl.closing = -ctl.closing
    return entries, ctl


def _bal(sheet: Sheet, r: int, t: Table, old: Optional[int]) -> Optional[int]:
    b = _paise(sheet, r, t, "balance")
    return b if b is not None else old


def _new_entry(sheet: Sheet, r: int, t: Table, side: str, idx: int, date, dr: int, cr: int) -> Entry:
    e = Entry(side=side, idx=idx, rows=[r + 1], date=date, dr=dr, cr=cr)
    dd = _val(sheet, r, t, "doc_date")
    if dd is not None and dd.kind == DATE:
        e.doc_date = dd.date
    e.vtype = _txt(sheet, r, t, "vtype")
    e.ref = _txt(sheet, r, t, "ref")
    e.ref2 = _txt(sheet, r, t, "ref2")
    e.narration = _txt(sheet, r, t, "narration")
    e.account = _txt(sheet, r, t, "account")
    e.status = _txt(sheet, r, t, "status")
    tds = _paise(sheet, r, t, "tds")
    if tds:
        e.tds = abs(tds)
    vt = _paise(sheet, r, t, "voucher_total")
    if vt is not None:
        e.voucher_total = abs(vt)
    for role in ("base", "gst", "net"):
        v = _paise(sheet, r, t, role)
        if v is not None:
            setattr(e, role, v)
    e.balance = _paise(sheet, r, t, "balance")
    # a status column text like 'O/s' sitting in a free column
    for c in sheet.rows[r]:
        if c.kind == "status" and c.text.lower() in ("o/s", "os", "paid", "unpaid", "pending"):
            e.status = c.text
    return e


# --------------------------------------------------------------------------- party filter
def party_filter(entries: list[Entry], t: Table) -> tuple[list[Entry], Optional[str], list[Entry]]:
    """For GL dumps that list every account: keep only the party (control) account rows."""
    if "account" not in t.roles:
        return entries, None, []
    forced = (t.meta.get("party_account") or "").strip().lower()
    if forced:                                   # named by the AI extraction spec
        keep = [e for e in entries if e.account.strip().lower() == forced]
        if keep:
            rest = [e for e in entries if e.account.strip().lower() != forced]
            for i, e in enumerate(keep):
                e.idx = i
            return keep, keep[0].account, rest
    accts: dict[str, list[Entry]] = {}
    for e in entries:
        accts.setdefault(e.account, []).append(e)
    if len(accts) < 3:
        return entries, None, []
    # the party account: has both debits and credits, nets near the ledger total, matches party words
    best, best_score = None, -1.0
    total = sum(e.amount for e in entries)
    for a, es in accts.items():
        has_dr = any(e.dr for e in es)
        has_cr = any(e.cr for e in es)
        refs = len({e.ref for e in es})
        score = refs / len(entries) + (1.0 if has_dr and has_cr else 0) + (2.0 if PARTY_ACCOUNT_RE.search(a) else 0)
        if abs(sum(e.amount for e in es) - total) <= 100:
            score += 0.5
        if score > best_score:
            best, best_score = a, score
    if best is None or not PARTY_ACCOUNT_RE.search(best):
        return entries, None, []
    keep = [e for e in entries if e.account == best]
    rest = [e for e in entries if e.account != best]
    for i, e in enumerate(keep):
        e.idx = i
    return keep, best, rest
