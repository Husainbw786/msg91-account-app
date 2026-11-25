"""Stage 2: prove the read is correct using the file's own arithmetic.

A reading (sign convention, whether the TDS column reduces the balance, which column is the
balance) is accepted only when the file's balances / totals / internal relations agree with it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from .detect import Table
from .grid import AMOUNT, Sheet
from .rows import Controls, Entry

GREEN, AMBER, RED = "green", "amber", "red"


@dataclass
class Check:
    name: str
    passed: int
    total: int
    detail: str = ""
    strong: bool = False             # a full pass proves the reading
    informational: bool = False      # a finding about the data, not a test of the read

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total


@dataclass
class Reading:
    sign: int = 1                    # +1: Dr increases the balance as printed
    bal_sign: int = 1                # -1: balances printed credit-positive (payable shown as a plus)
    tds_reduces: bool = False        # TDS column also reduces the balance (manual payable sheets)
    balance_col: Optional[int] = None
    checks: list[Check] = field(default_factory=list)
    score: float = 0.0


def amount_of(e: Entry, rd: Reading) -> int:
    a = e.dr - e.cr
    if rd.tds_reduces:
        a += e.tds                    # TDS reduces a payable (credit) balance, i.e. acts like a debit
    return a * rd.sign


def running_check(entries: list[Entry], ctl: Controls, rd: Reading, balances: list[Optional[int]]) -> Check:
    """balance[i] = balance[prev] + sum(amounts between). Works for dense and sparse balance columns."""
    balances = [b * rd.bal_sign if b is not None else None for b in balances]
    idx = [i for i, b in enumerate(balances) if b is not None]
    if not idx:
        return Check("running balance", 0, 0, "no balance column")
    passed = total = 0
    bad = []
    # first anchor: opening (if any) -> first balance
    start = ctl.opening if ctl.opening is not None else 0
    acc = start + sum(amount_of(entries[k], rd) for k in range(0, idx[0] + 1))
    if abs(acc - balances[idx[0]]) <= 100:
        passed += 1
        total += 1
    elif ctl.opening is not None:             # an unseen opening balance is not a failure; a printed one is
        total += 1
        bad.append(entries[idx[0]].rows[0])
    for a, b in zip(idx, idx[1:]):
        acc = balances[a] + sum(amount_of(entries[k], rd) for k in range(a + 1, b + 1))
        total += 1
        if abs(acc - balances[b]) <= 100:
            passed += 1
        else:
            bad.append(entries[b].rows[0])
    dense = len(idx) >= 0.9 * len(entries)
    name = "running balance" if dense else "running balance (typed on some rows only)"
    detail = f"{passed}/{total} stretches agree"
    if bad:
        detail += "; first disagreements at rows " + ", ".join(map(str, bad[:8]))
    return Check(name, passed, total, detail, strong=dense)


def totals_check(entries: list[Entry], ctl: Controls, rd: Reading) -> list[Check]:
    out = []
    before = [e for e in entries if ctl.total_row is None or e.rows[0] < ctl.total_row]
    if ctl.total_dr is not None and ctl.total_cr is not None:
        sdr = sum(e.dr for e in before)
        scr = sum(e.cr for e in before)
        ok = abs(sdr - ctl.total_dr) <= 100 and abs(scr - ctl.total_cr) <= 100
        detail = (f"debits {sdr / 100:,.2f} vs printed {ctl.total_dr / 100:,.2f}; "
                  f"credits {scr / 100:,.2f} vs printed {ctl.total_cr / 100:,.2f}")
        # printed totals may be algebraic (negative values inside the debit column); same net = same data
        if not ok and abs((sdr - ctl.total_dr) - (scr - ctl.total_cr)) <= 100:
            ok = True
            detail += f" (printed totals net {(sdr - ctl.total_dr) / 100:,.2f} of negative entries; net agrees)"
        out.append(Check("column totals", int(ok), 1, detail, strong=True))
    else:
        field_of = {"gross": lambda e: e.dr - e.cr, "base": lambda e: e.base or 0, "gst": lambda e: e.gst or 0,
                    "net": lambda e: e.net or 0, "tds": lambda e: e.tds,
                    "debit": lambda e: e.dr, "credit": lambda e: e.cr}
        res = []
        for role, printed in ctl.role_totals.items():
            if role not in field_of:
                continue
            calc = sum(field_of[role](e) for e in before)
            ok = abs(abs(calc) - abs(printed)) <= 100
            if not ok and ctl.opening and abs(abs(printed) - abs(calc) - abs(ctl.opening)) <= 100:
                ok = True                     # printed total includes the balance brought forward
                calc += abs(ctl.opening)
            res.append((role, ok, calc, printed))
        if res:
            passed = sum(1 for x in res if x[1])
            det = "; ".join(f"{ro} {c / 100:,.2f} vs printed {p / 100:,.2f}" for ro, ok, c, p in res)
            out.append(Check("column totals", passed, len(res), det, strong=True))
    if ctl.closing is not None:
        opening = ctl.opening or 0
        calc = opening + sum(amount_of(e, rd) for e in entries)
        exact = abs(calc - ctl.closing) <= 100
        out.append(Check("opening + movements = closing", int(exact), 1,
                         f"computed {calc / 100:,.2f} vs printed {ctl.closing / 100:,.2f}", strong=True))
    return out


def row_net_column(entries: list[Entry]) -> bool:
    """A 'balance' column that is just dr - cr of each row is not a running balance."""
    rows = [e for e in entries if e.balance is not None]
    if len(rows) < 5:
        return False
    same = sum(1 for e in rows if abs(e.balance - (e.dr - e.cr)) <= 100)
    return same / len(rows) > 0.9


def register_check(entries: list[Entry]) -> Optional[Check]:
    rows = [e for e in entries if e.base is not None and e.gst is not None]
    if len(rows) < 3:
        return None
    ok = 0
    for e in rows:
        gross = e.dr - e.cr
        g_ok = abs(e.base + e.gst - gross) <= 100
        n_ok = True
        if e.net is not None:
            n_ok = abs(gross - e.tds - e.net) <= 100 or abs(gross + e.tds - e.net) <= 100
        ok += g_ok and n_ok
    return Check("base + GST = gross, gross - TDS = net", ok, len(rows), f"{ok}/{len(rows)} rows agree", strong=True)


def voucher_check(entries: list[Entry]) -> Optional[Check]:
    rows = [e for e in entries if e.voucher_total]
    if len(rows) < 3:
        return None
    ok = sum(1 for e in rows if abs(abs(e.dr - e.cr) - e.voucher_total) <= 100)
    partial = [e for e in rows if abs(abs(e.dr - e.cr) - e.voucher_total) > 100]
    for e in partial:
        e.flags.append(f"voucher amount {e.voucher_total / 100:,.2f} but only {abs(e.dr - e.cr) / 100:,.2f} "
                       f"posted to this ledger")
    return Check("voucher lines add up to voucher amount", ok, len(rows),
                 f"{len(partial)} voucher(s) only partly posted to this ledger" if partial else "all agree",
                 informational=True)


def repair_dates(entries: list[Entry]) -> list[str]:
    """Fix dd/mm vs mm/dd swaps: a date out of order whose swap fits between its neighbours."""
    notes = []
    for i in range(1, len(entries) - 1):
        p, e, n = entries[i - 1].date, entries[i].date, entries[i + 1].date
        if not (p and e and n) or p <= e <= n or not (p <= n):
            continue
        if e.day <= 12 and e.day != e.month:
            try:
                sw = dt.date(e.year, e.day, e.month)
            except ValueError:
                continue
            if p <= sw <= n + dt.timedelta(days=31):
                entries[i].flags.append(f"date {e} read as {sw} (day/month swapped in source)")
                notes.append(f"row {entries[i].rows[0]}: {e} -> {sw}")
                entries[i].date = sw
    return notes


def candidate_balances(sheet: Sheet, t: Table, entries: list[Entry]) -> dict[Optional[int], list[Optional[int]]]:
    """Balance per entry for the labelled balance column and for unlabelled numeric columns."""
    cands: dict[Optional[int], list[Optional[int]]] = {}
    if "balance" in t.roles and not row_net_column(entries):
        cands[t.roles["balance"]] = [e.balance for e in entries]
    elif "balance" in t.roles:
        t.notes.append("The 'balance' column is each row's net (debit - credit), not a running balance.")
    for c in t.meta.get("free_numeric_columns", []) or []:
        vals: list[Optional[int]] = []
        for e in entries:
            v = None
            for r in e.rows:
                cell = sheet.cell(r - 1, c)
                if cell.kind == AMOUNT:
                    v = cell.paise
            vals.append(v)
        if sum(v is not None for v in vals) >= 3 and len({v for v in vals if v is not None}) > 3:
            cands[c] = vals
    return cands


def choose_reading(sheet: Sheet, t: Table, entries: list[Entry], ctl: Controls) -> Reading:
    """Try every reading; keep the one the file's own numbers support best."""
    cands = candidate_balances(sheet, t, entries)
    labelled = t.roles.get("balance")
    if labelled not in cands:                       # no usable labelled balance: reading without one is an option
        cands = {None: [None] * len(entries), **cands}
    has_tds = any(e.tds for e in entries)
    best: Optional[Reading] = None
    for col, bals in cands.items():
        # a balance column that already carries signs (negatives) is signed Dr+/Cr-: no credit-positive reading
        signed = any(b is not None and b < 0 for b in bals)
        for bal_sign in ((1, -1) if col is not None and not signed else (1,)):
            for tds_red in ((False, True) if has_tds else (False,)):
                rd = Reading(bal_sign=bal_sign, tds_reduces=tds_red, balance_col=col)
                chk = []
                rc = running_check(entries, ctl, rd, bals)
                if col is not None and col != labelled and rc.ratio < 0.9:
                    continue                        # an unlabelled column is a balance only if it behaves as one
                if rc.total:
                    chk.append(rc)
                chk += totals_check(entries, ctl, rd)
                score = 0.0
                for c in chk:
                    score += c.ratio * (3 if c.strong else 2) * min(1.0, c.total / 3 + 0.34)
                rd.checks, rd.score = chk, score
                if best is None or score > best.score + 1e-9:
                    best = rd
    assert best is not None
    if best.balance_col is not None and best.balance_col != t.roles.get("balance"):
        t.notes.append(f"Column {best.balance_col + 1} has no header but behaves as a running balance; used as one.")
        for e, b in zip(entries, cands[best.balance_col]):
            e.balance = b
    return best


def confidence(checks: list[Check]) -> str:
    """green: a strong check passes and every other check holds on at least 90% of its items; a check that fails on
    every one of 3+ items (e.g. running balance 0/3) means the read is wrong somewhere: red."""
    tests = [c for c in checks if c.total and not c.informational]
    if any(c.total >= 3 and c.passed == 0 for c in tests):
        return RED
    if any(c.strong and c.ok for c in checks) and not any(c.ratio < 0.9 for c in tests):
        return GREEN
    if any(c.ratio >= 0.6 for c in checks if c.total):
        return AMBER
    return RED
