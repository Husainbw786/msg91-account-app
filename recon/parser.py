"""Stage 1 + 2 orchestration: file in, proven ledger out."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .detect import Table, _decide_layout, detect
from .grid import EMPTY
from .profiles import ProfileStore, apply_saved_roles, fingerprint
from .grid import Sheet, load
from .rows import Controls, Entry, build_entries, party_filter
from .validate import (RED, Check, Reading, choose_reading, confidence, register_check, repair_dates,
                       totals_check, voucher_check)


@dataclass
class Ledger:
    path: str
    side: str
    sheet: str
    tables: list[Table]
    entries: list[Entry]
    opening: int                          # Dr positive, file's own books
    closing_printed: Optional[int]
    checks: list[Check]
    confidence: str
    reading: Reading
    party_account: Optional[str] = None
    other_account_rows: int = 0
    notes: list[str] = field(default_factory=list)
    other_sheets: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def closing(self) -> int:
        return self.opening + sum(e.dr - e.cr for e in self.entries)

    @property
    def first_date(self):
        ds = [e.date for e in self.entries if e.date]
        return min(ds) if ds else None

    @property
    def last_date(self):
        ds = [e.date for e in self.entries if e.date]
        return max(ds) if ds else None


def _samples(sheet: Sheet, t: Table, n: int = 4) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for c in sorted(t.labels):
        vals = []
        for r in range(t.start, t.end + 1):
            cell = sheet.cell(r, c)
            if cell.kind != EMPTY and cell.text:
                vals.append(cell.text[:40])
                if len(vals) >= n:
                    break
        out[c] = vals
    return out


def _parse_sheet(sheet: Sheet, side: str, path: str, store: Optional[ProfileStore] = None,
                 role_overrides: Optional[dict] = None, forced_tables: Optional[list] = None) -> Optional[Ledger]:
    tables = forced_tables if forced_tables is not None else detect(sheet)
    if role_overrides:
        for ti, roles in role_overrides.items():
            ti = int(ti)
            if ti < len(tables):
                t = tables[ti]
                t.roles = {r: int(c) for r, c in roles.items() if c is not None and int(c) in t.labels}
                t.role_scores = {r: 1.0 for r in t.roles}
                _decide_layout(t)
                t.meta["user_mapping"] = True
                t.notes.append("Column roles set by the user.")
    for t in tables:
        t.meta["samples"] = _samples(sheet, t)
    known = 0
    for t in tables:
        fp = fingerprint(t)
        t.meta["fingerprint"] = fp
        fmt = store.get_format(fp) if store and not (t.meta.get("user_mapping") or t.meta.get("ai_layout")) else None
        if fmt:
            detected = dict(t.roles), t.layout
            if apply_saved_roles(t, fmt):
                known += 1
                t.meta["known_format"] = True
                t.notes.append(f"Known format {fp} (seen {fmt.get('uses', 1)} time(s)); saved column roles reused.")
            else:
                t.roles, t.layout = detected
    tables = [t for t in tables if t.layout != "unknown"]
    if not tables:
        return None
    all_entries: list[Entry] = []
    all_checks: list[Check] = []
    notes: list[str] = []
    opening_total = 0
    closing_printed: Optional[int] = None
    reading: Optional[Reading] = None
    party = None
    other_rows = 0
    prev_closing: Optional[int] = None
    table_conf: list[str] = []
    for ti, t in enumerate(tables):
        entries, ctl = build_entries(sheet, t, side)
        if not entries:
            continue
        if _newest_first(entries):
            # exports that list the latest entry first (Dynamics, many portals): put them in time order, so the
            # running balance is followed the way it was built. Same-day entries keep their reversed file order.
            entries.reverse()
            for i, e in enumerate(entries):
                e.idx = i
            notes.append("This file lists the newest entry first; entries were put in date order before the checks.")
        if ti > 0 and ctl.opening is None and prev_closing is not None:
            ctl.opening = prev_closing
            notes.append(f"Table {ti + 1} continues table {ti}: balance brought forward "
                         f"{prev_closing / 100:,.2f} used as its opening.")
        # totals are printed for the whole report, so check them before the party filter
        pre_checks = totals_check(entries, ctl, Reading())
        kept, party_acct, rest = party_filter(entries, t)
        if party_acct and not t.meta.get("party_account") and _filter_unsupported(sheet, t, entries, kept, ctl):
            notes.append(f"The account column looked like a list of GL accounts, but keeping only '{party_acct}' "
                         f"broke the running balance, so every row was kept (the column holds the offset account).")
            for i, e in enumerate(entries):
                e.idx = i
            kept, party_acct, rest = entries, None, []
        if not party_acct:
            pre_checks = []
        if party_acct:
            party = party_acct
            other_rows += len(rest)
            notes.append(f"Report lists {len(set(e.account for e in entries))} GL accounts; kept the party account "
                         f"'{party_acct}' ({len(kept)} of {len(entries)} lines).")
            entries = kept
            ctl.total_dr = ctl.total_cr = None
            ctl.role_totals = {}
            ctl.closing = None
            party_net = sum(e.dr - e.cr for e in entries)
            pre_checks.append(Check("party account nets to its own movements", 1, 1,
                                    f"party account movement {party_net / 100:,.2f}"))
        notes += repair_dates(entries)
        rd = choose_reading(sheet, t, entries, ctl)
        implied = _implied_opening(entries, ctl, rd)
        if implied is not None:
            ctl.opening = implied
            notes.append(f"No opening balance row was read; the first printed balance implies an opening of "
                         f"{implied / 100:,.2f} ({'Dr' if implied > 0 else 'Cr'}), which is used.")
            rd = choose_reading(sheet, t, entries, ctl)
        if rd.bal_sign == -1:
            notes.append("Balances are printed credit-positive in this file (outstanding payable shown as a plus).")
        if rd.tds_reduces:
            for e in entries:
                if e.tds:
                    e.dr += e.tds
                    e.flags.append("TDS column reduces the balance (treated as a deduction entry)")
            notes.append("TDS column reduces the balance in this file (manual payable sheet).")
        checks = pre_checks + rd.checks
        rc = register_check(entries)
        if rc:
            checks.append(rc)
        vc = voucher_check(entries)
        if vc:
            checks.append(vc)
        if len(tables) > 1:
            for c in checks:
                c.name = f"table {ti + 1}: {c.name}"
        table_conf.append(confidence(checks) if (checks or ti == 0) else "amber")   # a later table with nothing to check
        all_checks += checks
        notes += t.notes + ctl.notes[:5]
        if ti == 0:
            notes += [n for n in sheet.notes if n not in notes]
        if ti == 0:
            opening_total = ctl.opening or 0
            reading = rd
        if ctl.closing is not None:
            closing_printed = ctl.closing
        prev_closing = ctl.closing if ctl.closing is not None else \
            (ctl.opening or 0) + sum(e.dr - e.cr for e in entries)
        for e in entries:
            e.idx = len(all_entries)
            all_entries.append(e)
    if not all_entries or reading is None:
        return None
    order = {"green": 2, "amber": 1, "red": 0}
    conf = min(table_conf, key=lambda c: order[c]) if table_conf else confidence(all_checks)
    return Ledger(path, side, sheet.name, tables, all_entries, opening_total, closing_printed, all_checks, conf,
                  reading, party, other_rows, notes)


def _newest_first(entries) -> bool:
    ds = [e.date for e in entries if e.date]
    down = sum(1 for a, b in zip(ds, ds[1:]) if b < a)
    up = sum(1 for a, b in zip(ds, ds[1:]) if b > a)
    return down >= 2 and down > 2 * up


def _implied_opening(entries, ctl, rd) -> Optional[int]:
    """With no opening row read, the balance column still proves the opening: first printed balance minus the
    movements up to it. Used only when the rest of the balance column agrees (a real running balance)."""
    from .validate import amount_of
    if ctl.opening is not None or rd.balance_col is None and not any(e.balance is not None for e in entries):
        return None
    if not any(c.name.startswith("running balance") and c.total >= 3 and c.ratio >= 0.9 for c in rd.checks):
        return None
    first = next((i for i, e in enumerate(entries) if e.balance is not None), None)
    if first is None:
        return None
    implied = entries[first].balance * rd.bal_sign - sum(amount_of(entries[k], rd) for k in range(first + 1))
    return implied if abs(implied) > 100 else None


def _filter_unsupported(sheet, t, entries, kept, ctl) -> bool:
    """An automatic party-account pick is only trusted if the file's balances back it at least as well as
    keeping every row. (Tally prints the offset account - bank, sales, TDS - in its account column.)"""
    import copy
    rank = {"green": 2, "amber": 1, "red": 0}
    notes_before = list(t.notes)
    bal_before = [e.balance for e in entries]
    every = choose_reading(sheet, t, entries, ctl)
    c_kept = copy.copy(ctl)
    c_kept.total_dr = c_kept.total_cr = None
    c_kept.role_totals = {}
    c_kept.closing = None
    only = choose_reading(sheet, t, kept, c_kept)
    whole_report = totals_check(entries, ctl, Reading())      # what the filtered path is credited with too
    t.notes[:] = notes_before                    # choose_reading runs again for real below
    for e, b in zip(entries, bal_before):
        e.balance = b
    return rank[confidence(every.checks)] > rank[confidence(whole_report + only.checks)]


def sheet_names(path: str | Path) -> list[str]:
    return [s.name for s in load(str(path).split("#", 1)[0])]


def parse_file(path: str | Path, side: str = "C", sheet: Optional[str] = None,
               store: Optional[ProfileStore] = None, role_overrides: Optional[dict] = None) -> Ledger:
    """Parse a ledger file. `path` may carry a sheet as 'file.xlsx#Sheet1'.
    With a ProfileStore, known formats reuse their saved column roles and new proven formats are saved."""
    p = str(path)
    if "#" in p and sheet is None:
        p, sheet = p.split("#", 1)
    sheets = load(p)
    if sheet:
        pick = [s for s in sheets if s.name.lower() == sheet.lower()]
        if not pick:
            raise ValueError(f"Sheet '{sheet}' not found. Sheets: {[s.name for s in sheets]}")
        sheets_to_try = pick
    else:
        sheets_to_try = sheets
    results = []
    for s in sheets_to_try:
        led = _parse_sheet(s, side, p, store, role_overrides if sheet else None)
        if led is not None and led.confidence == RED and any(t.meta.get("known_format") for t in led.tables):
            fresh = _parse_sheet(s, side, p, None)          # saved roles did not prove out: detect afresh
            if fresh is not None:
                fresh.notes.append("Saved format did not pass the checks this time; columns re-detected.")
                led = fresh
        if led is not None:
            rank = {"green": 2, "amber": 1, "red": 0}[led.confidence]
            results.append((rank, len(led.entries), led))
    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not results:
        raise ValueError("No ledger table found: no sheet has rows with both a date and an amount.")
    best = results[0][2]
    best.other_sheets = [r[2].sheet for r in results[1:]]
    if best.other_sheets:
        best.notes.append("Other sheets with ledger-like data (not used): " + ", ".join(best.other_sheets))
    if best.confidence == RED:
        best.notes.append("Read could not be proven from the file's own numbers (no balance/total check passed).")
    elif store is not None:                      # proven read (detected or set by the user): remember it
        rd = {"bal_sign": best.reading.bal_sign, "tds_reduces": best.reading.tds_reduces}
        for t in best.tables:
            store.put_format(t.meta["fingerprint"], t, rd, Path(p).name)
    return best
