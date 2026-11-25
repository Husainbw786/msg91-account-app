"""Stage 5: TDS engine.

Per invoice: expected TDS (rate x taxable, rate/base inferred from the data), actual deduction,
excess withholding. Per financial year: counterparty deductions vs Walkover's TDS entries, with a
timing test that moves a deduction to the period of its provision (usage month) only when that
makes the years agree better.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .classify import JOURNAL, TDS
from .match import GST_DIVISORS, TDS_RATES, TOL, Result
from .rows import Entry


def fy_of(d: dt.date) -> str:
    a = d.year if d.month >= 4 else d.year - 1
    return f"FY{a % 100:02d}-{(a + 1) % 100:02d}"


def quarter_of(d: dt.date) -> str:
    return "Q%d" % ({4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month])


def key_date(key: str) -> Optional[dt.date]:
    try:
        return dt.date(2000 + int(key[:2]), int(key[2:4]), int(key[4:6]))
    except (ValueError, TypeError, IndexError):
        return None


def month_before(d: dt.date) -> dt.date:
    return (d.replace(day=1) - dt.timedelta(days=1)).replace(day=1)


@dataclass
class Deduction:
    key: str
    gross: int
    expected: int
    deducted: int                     # TDS part only (excess separated)
    excess: int
    posted: dt.date                   # counterparty posting date
    candidates: list[dt.date]         # dates the deduction may belong to (posting, invoice, usage month)
    assigned: dt.date = None          # chosen by the timing test
    source: str = ""

    @property
    def moved(self) -> bool:
        return fy_of(self.assigned) != fy_of(self.posted)


@dataclass
class TdsReport:
    rate: float
    divisor: float
    deductions: list[Deduction]
    walkover_entries: list[Entry]
    other_deductions: list[tuple[str, int, str]]       # (key, amount, narration) not explained as TDS
    fy_rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rate_label(self) -> str:
        base = "gross incl. GST" if self.divisor == 1.0 else f"taxable value (gross/{self.divisor})"
        return f"{self.rate * 100:g}% on {base}"


def _expected(gross: int, rate: float, div: float) -> int:
    return round(abs(gross) / div * rate)


def _infer_rate(pairs: list[tuple[int, int]]) -> tuple[float, float]:
    """Most common (rate, GST divisor) that explains deduction = rate x gross / divisor."""
    c: Counter = Counter()
    for gross, ded in pairs:
        for r in TDS_RATES:
            for g in GST_DIVISORS:
                if abs(round(abs(gross) / g * r) - ded) <= TOL:
                    c[(r, g)] += 1
    if not c:
        return 0.02, 1.18
    # prefer the taxable-value base on ties (that is how TDS is meant to be computed)
    best = max(c.items(), key=lambda kv: (kv[1], kv[0][1] != 1.0))
    return best[0]


def analyse_tds(res: Result) -> TdsReport:
    raw: list[tuple[str, int, int, dt.date, str]] = []      # key, gross, deducted, posted, source
    col_tds: dict[str, int] = {}                             # what the counterparty's TDS column shows
    seen_c: set[int] = set()
    for m in res.matches:
        for e in m.c:
            seen_c.add(e.idx)
            if e.tds and m.key:
                col_tds[m.key] = col_tds.get(m.key, 0) + e.tds
        ded = sum(abs(a) for lab, a in m.parts if "TDS" in lab or lab.startswith("Deduction netted"))
        src = "netted in invoice"
        if not ded and m.c and any(e.tds for e in m.c) and res.c_amount_basis == "gross":
            ded = sum(e.tds for e in m.c)            # register shows TDS it will deduct on payment
            src = "TDS column (stated in counterparty register)"
        if not ded or not m.c:
            continue
        vt = max((e.voucher_total or 0) for e in m.w) if m.w else 0
        gross = vt or abs(m.w_amount)
        posted = min(e.date for e in m.c if e.date)
        raw.append((m.key, gross, ded, posted, src))
    gross_by_key = {k: g for k, g, _, _, _ in raw}
    w_gross = {}
    for e in res.W.entries:
        if e.inv_key and e.kind == "invoice":
            w_gross[e.inv_key] = max(w_gross.get(e.inv_key, 0), e.voucher_total or abs(e.dr - e.cr))
    rate, div = _infer_rate([(g, d) for _, g, d, _, _ in raw])

    # counterparty invoices outside the matches (e.g. before Walkover's period) that carry a TDS column
    for e in res.C.entries:
        if e.idx not in seen_c and e.kind == "invoice" and e.tds and e.date:
            gross = abs(e.dr - e.cr) + e.tds
            raw.append((e.inv_key or e.ref, gross, e.tds, e.date, "TDS column (invoice not matched)"))
    other: list[tuple[str, int, str]] = []
    for e in res.unmatched_c:
        if e.kind in (TDS, JOURNAL) and e.inv_key and e.date:
            amt = abs(e.dr - e.cr)
            gross = gross_by_key.get(e.inv_key) or w_gross.get(e.inv_key)
            if gross and abs(_expected(gross, rate, div) - amt) <= TOL:
                raw.append((e.inv_key, gross, amt, e.date, f"separate journal {e.ref2 or e.ref}"))
            elif e.kind == TDS and gross:
                raw.append((e.inv_key, gross, amt, e.date, f"TDS memo {e.ref}"))
            else:
                other.append((e.inv_key, amt, e.narration[:80]))

    # chains: a re-bill's deduction belongs to the invoices it replaced
    rebill_parts: dict[str, list[tuple[str, int]]] = {}
    for ch in res.chains:
        for rb in ch.rebill:
            rebill_parts[rb.inv_key] = [(x.inv_key, w_gross.get(x.inv_key, abs(x.dr - x.cr))) for x in ch.cancelled]

    deds: list[Deduction] = []
    for key, gross, ded, posted, src in raw:
        exp = _expected(gross, rate, div)
        excess = ded - exp if ded - exp > TOL else 0
        pieces = rebill_parts.get(key)
        if pieces and abs(sum(_expected(g, rate, div) for _, g in pieces) - exp) <= TOL * len(pieces):
            for pk, pg in pieces:
                pe = _expected(pg, rate, div)
                kd = key_date(pk)
                cands = [posted] + ([kd, month_before(kd)] if kd else [])
                deds.append(Deduction(pk, pg, pe, pe, 0, posted, cands, posted, f"{src} (re-billed in {key})"))
            if excess:
                deds.append(Deduction(key, gross, 0, 0, excess, posted, [posted], posted, f"{src}: excess"))
            continue
        kd = key_date(key)
        cands = [posted] + ([kd, month_before(kd)] if kd else [])
        shown = col_tds.get(key, 0)
        if TOL < shown < ded - excess - TOL:
            # the TDS column shows only part: that part was deducted at the invoice, the rest earlier (provision)
            deds.append(Deduction(key, gross, shown, shown, 0, posted, [posted], posted, f"{src} (TDS column part)"))
            rest = ded - excess - shown
            deds.append(Deduction(key, gross, exp - shown, rest, excess, posted, cands, posted,
                                  f"{src} (balance beyond the TDS column)"))
            continue
        deds.append(Deduction(key, gross, exp, ded - excess, excess, posted, cands, posted, src))

    w_tds = [e for e in res.W.entries if e.kind == TDS and e.date]
    rep = TdsReport(rate, div, deds, w_tds, other)
    _timing_test(rep)
    if other:
        rep.notes.append(f"{len(other)} counterparty deduction(s) do not match the TDS rate and are listed separately.")
    return rep


def _fy_table(rep: TdsReport) -> dict[str, list[int]]:
    t: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for d in rep.deductions:
        if d.deducted:
            t[fy_of(d.assigned)][0] += d.deducted
    for e in rep.walkover_entries:
        # TDS credited to the customer's account reduces what it owes (credit); a reversal of TDS (moved to TDS
        # refundable, or a wrong 26AS entry undone) is a debit and takes it back. Signed, not absolute.
        t[fy_of(e.date)][1] += e.cr - e.dr
    return t


def _timing_test(rep: TdsReport) -> None:
    """Greedy: move a deduction to another candidate period only if total |FY difference| falls."""
    def objective() -> int:
        return sum(abs(w - c) for c, w in _fy_table(rep).values())
    improved = True
    while improved:
        improved = False
        base = objective()
        best = None
        for d in rep.deductions:
            for cd in d.candidates:
                if fy_of(cd) == fy_of(d.assigned):
                    continue
                old = d.assigned
                d.assigned = cd
                gain = base - objective()
                d.assigned = old
                if gain > TOL and (best is None or gain > best[0]):
                    best = (gain, d, cd)
        if best:
            best[1].assigned = best[2]
            improved = True
    rows = []
    t = _fy_table(rep)
    for fy in sorted(t):
        c, w = t[fy]
        moved = [d for d in rep.deductions if d.moved and (fy_of(d.assigned) == fy or fy_of(d.posted) == fy)]
        rows.append({"fy": fy, "counterparty": c, "walkover": w, "diff": w - c,
                     "moved": ", ".join(sorted({f"{d.key} ({d.deducted / 100:,.0f})" for d in moved
                                                if fy_of(d.assigned) == fy}))})
    rep.fy_rows = rows
    n_moved = sum(1 for d in rep.deductions if d.moved)
    if n_moved:
        rep.notes.append(f"{n_moved} deduction(s) placed in the year of their provision/usage month, because that "
                         f"makes the yearly totals agree with Walkover's TDS entries.")
