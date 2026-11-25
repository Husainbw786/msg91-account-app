"""Stage 4: matching funnel (phase 1: T1 exact reference, T2 reference + tax arithmetic,
T3 bank reference, T4 amount + date window, plus same-side self-cancelling sets).

All amounts are compared in Walkover's direction: value_W = W.dr - W.cr and
value_C = orient * (C.dr - C.cr), where orient (+1 / -1) is chosen from the data.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

import numpy as np
from scipy.optimize import linear_sum_assignment

from .classify import CN, DN, INVOICE, JOURNAL, OPENING, PAYMENT, REFUND, TDS, invoice_key_in
from .parser import Ledger
from .rows import Entry

TOL = 100                      # 1 rupee, in paise
TDS_RATES = (0.001, 0.01, 0.02, 0.05, 0.1)
GST_DIVISORS = (1.0, 1.05, 1.12, 1.18, 1.28)
DATE_WINDOW = 7


@dataclass
class Match:
    tier: str
    w: list[Entry]
    c: list[Entry]
    w_amount: int                       # Walkover direction
    c_amount: int                       # Walkover direction
    parts: list[tuple[str, int]] = field(default_factory=list)   # explained pieces of (w - c)
    note: str = ""

    @property
    def diff(self) -> int:
        return self.w_amount - self.c_amount

    @property
    def key(self) -> str:
        for e in self.w + self.c:
            if e.inv_key:
                return e.inv_key
        return (self.w or self.c)[0].label()


@dataclass
class Chain:
    side: str
    credit_note: Entry
    cancelled: list[Entry]
    rebill: list[Entry]
    net_nil: bool


@dataclass
class Result:
    W: Ledger
    C: Ledger
    orient: int
    c_amount_basis: str                 # 'net' (amount posted after TDS) or 'gross'
    matches: list[Match]
    unmatched_w: list[Entry]
    unmatched_c: list[Entry]
    self_cancel: list[list[Entry]]
    notes: list[str] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)


def vw(e: Entry) -> int:
    return e.dr - e.cr


def _tds_pattern(gross: int, diff: int) -> Optional[str]:
    """Does diff equal a TDS deduction on gross? Returns a label like '2% on taxable (gross/1.18)'."""
    if diff <= 0:
        return None
    best = None                                     # the closest fit, not the first within tolerance
    for r in TDS_RATES:
        for g in GST_DIVISORS:
            err = abs(round(gross / g * r) - diff)
            if err <= TOL and (best is None or err < best[0]):
                best = (err, r, g)
    if best is None:
        return None
    _, r, g = best
    base = "gross incl. GST" if g == 1.0 else f"taxable value (gross/{g})"
    return f"{r * 100:g}% on {base}"


def choose_orientation(W: list[Entry], C: list[Entry]) -> int:
    """+1 or -1: the sign that makes key-matched documents agree in direction."""
    wk = defaultdict(int)
    for e in W:
        if e.inv_key:
            wk[e.inv_key] += vw(e)
    votes = 0
    for e in C:
        if e.inv_key in wk and wk[e.inv_key] and vw(e):
            votes += 1 if (wk[e.inv_key] > 0) == (vw(e) > 0) else -1
    if votes == 0:
        # fall back on totals of payments / invoices
        return -1
    return 1 if votes > 0 else -1


def _promote_by_walkover_key(W: Ledger, C: Ledger, orient: int) -> None:
    """A counterparty entry that carries Walkover's own document number (250405-138) and moves the same way
    is that document, whatever the counterparty called it ("Journal", "Other subscription expenses",
    "Purchase"). Many companies book supplier invoices as journals. Payments, TDS, refunds and openings keep
    their kind; amounts are still matched by arithmetic in T2."""
    w_by_key: dict[str, Entry] = {}
    for e in W.entries:
        if e.kind in (INVOICE, CN, DN):
            for k in [e.inv_key] + list(e.keys or []):
                if k:
                    w_by_key.setdefault(k, e)
    inv_dir = sum(1 if (e.dr - e.cr) > 0 else -1 for e in W.entries if e.kind == INVOICE and e.dr != e.cr)
    for e in C.entries:
        if e.kind in (INVOICE, CN, DN, PAYMENT, TDS, REFUND, OPENING):
            continue
        # a journal in the invoice direction that quotes a Walkover-style invoice number Walkover does not have
        # is still an invoice (missing in Walkover's books), not a deduction
        key = invoice_key_in(e.ref) if e.kind == JOURNAL else None
        cv = orient * (e.dr - e.cr)
        if key and key not in w_by_key and inv_dir and cv and (cv > 0) == (inv_dir > 0):
            e.kind, e.inv_key = INVOICE, key
            e.kind_why = (getattr(e, "kind_why", "") + f"; Walkover-style invoice number {key}").lstrip("; ")
            continue
        for k in [e.inv_key] + list(e.keys or []):
            w = w_by_key.get(k) if k else None
            if w is None:
                continue
            wv, cv = w.dr - w.cr, orient * (e.dr - e.cr)
            if wv and cv and (wv > 0) == (cv > 0):
                e.kind = w.kind
                e.inv_key = w.inv_key or k
                e.kind_why = (getattr(e, "kind_why", "") + f"; carries Walkover's {w.kind} number {k}").lstrip("; ")
                break


def reconcile(W: Ledger, C: Ledger) -> Result:
    orient = choose_orientation(W.entries, C.entries)

    def vc(e: Entry) -> int:
        return orient * (e.dr - e.cr)

    _promote_by_walkover_key(W, C, orient)
    used_w: set[int] = set()
    used_c: set[int] = set()
    matches: list[Match] = []
    notes: list[str] = []

    # ---------------------------------------------------------------- T1 / T2: documents by key
    doc_kinds = (INVOICE, CN, DN)
    w_docs: dict[str, list[Entry]] = defaultdict(list)
    c_docs: dict[str, list[Entry]] = defaultdict(list)
    for e in W.entries:
        if e.kind in doc_kinds:
            k = e.inv_key or (e.keys[0] if e.keys else "")
            if k:
                w_docs[k].append(e)
    for e in C.entries:
        if e.kind in doc_kinds:
            # of the entry's references, the one Walkover has (a Tally bill ref beats the buyer's own vch no.)
            k = e.inv_key if e.inv_key in w_docs else \
                next((kk for kk in (e.keys or []) if kk in w_docs), e.inv_key or "")
            if k and k != e.inv_key and k in w_docs:
                e.inv_key = k
            if not k:
                # a counterparty credit memo quoting Walkover's CN number etc.
                for kk in e.keys:
                    if kk in w_docs:
                        k = kk
                        break
            if k:
                c_docs[k].append(e)

    # Is the counterparty's invoice amount net of its TDS column or gross? Decide from the data.
    net_votes = gross_votes = 0
    for k, ces in c_docs.items():
        if k not in w_docs:
            continue
        w_amt = sum(vw(e) for e in w_docs[k])
        c_amt = sum(vc(e) for e in ces)
        tds = sum(e.tds for e in ces)
        wt = max((e.voucher_total or 0) for e in w_docs[k]) or abs(w_amt)
        if tds:
            if abs(abs(c_amt) + tds - abs(wt)) <= TOL or abs(abs(c_amt) + tds - abs(w_amt)) <= TOL:
                net_votes += 1
            elif abs(abs(c_amt) - abs(wt)) <= TOL or abs(abs(c_amt) - abs(w_amt)) <= TOL:
                gross_votes += 1
    basis = "net" if net_votes > gross_votes else "gross"
    if net_votes or gross_votes:
        notes.append(f"Counterparty books invoices {'net of' if basis == 'net' else 'gross of'} TDS "
                     f"({max(net_votes, gross_votes)} invoices agree).")

    for k in sorted(set(w_docs) & set(c_docs)):
        wes, ces = w_docs[k], c_docs[k]
        w_amt = sum(vw(e) for e in wes)
        c_amt = sum(vc(e) for e in ces)
        tds = sum(e.tds for e in ces)
        sign = 1 if w_amt >= 0 else -1
        diff = w_amt - c_amt
        parts: list[tuple[str, int]] = []
        vt = max((e.voucher_total or 0) for e in wes)
        c_gross = abs(c_amt) + (tds if basis == "net" else 0)
        rest = diff
        # partly posted Walkover voucher: the counterparty has the full voucher amount
        if vt and abs(abs(w_amt) - vt) > TOL and (abs(c_gross - vt) <= TOL
                                                   or _tds_pattern(vt, vt - abs(c_amt))):
            pp = sign * (vt - abs(w_amt))
            parts.append(("Invoice partly posted to another Walkover ledger", -pp))
            rest += pp
        if basis == "net" and tds and abs(rest - sign * tds) <= TOL:
            parts.append(("TDS deducted by counterparty (invoice booked net)", sign * tds))
            rest -= sign * tds
        elif rest and sign * rest > 0:
            lab = _tds_pattern(abs(w_amt) if not vt else vt, abs(rest))
            if lab:
                extra = f"; its TDS column shows only {tds / 100:,.2f}" if tds and abs(abs(rest) - tds) > TOL else ""
                parts.append((f"TDS deducted by counterparty on invoice ({lab}{extra})", rest))
                rest = 0
        if rest and abs(rest) > TOL:
            parts.append(("Amount differs", rest))
        elif rest:
            parts.append(("Rounding", rest))
        tier = "T1 reference" if not parts or all(p[0] == "Rounding" for p in parts) else "T2 reference + arithmetic"
        matches.append(Match(tier, wes, ces, w_amt, c_amt, parts))
        used_w.update(e.idx for e in wes)
        used_c.update(e.idx for e in ces)

    # ---------------------------------------------------------------- T3: bank reference (UTR)
    pay_kinds = (PAYMENT, REFUND)
    w_pay = [e for e in W.entries if e.kind in pay_kinds and e.idx not in used_w]
    c_pay = [e for e in C.entries if e.kind in pay_kinds and e.idx not in used_c]
    c_by_utr: dict[str, list[Entry]] = defaultdict(list)
    for e in c_pay:
        for u in e.utrs:
            c_by_utr[u].append(e)
    for e in w_pay:
        for u in e.utrs:
            cands = [c for c in c_by_utr.get(u, []) if c.idx not in used_c and abs(vw(e) - vc(c)) <= TOL]
            if cands:
                c = cands[0]
                matches.append(Match("T3 bank reference", [e], [c], vw(e), vc(c),
                                     [("Rounding", vw(e) - vc(c))] if vw(e) != vc(c) else [], f"UTR {u}"))
                used_w.add(e.idx)
                used_c.add(c.idx)
                break

    # ---------------------------------------------------------------- T4: amount + date window
    w_rest = [e for e in W.entries if e.idx not in used_w and e.kind in pay_kinds + (CN, DN, INVOICE)]
    c_rest = [e for e in C.entries if e.idx not in used_c and e.kind in pay_kinds + (CN, DN, INVOICE)]
    by_amt: dict[int, tuple[list[Entry], list[Entry]]] = defaultdict(lambda: ([], []))
    for e in w_rest:
        by_amt[round(vw(e) / TOL)][0].append(e)
    for e in c_rest:
        by_amt[round(vc(e) / TOL)][1].append(e)
    for amt, (ws, cs) in by_amt.items():
        if not ws or not cs or amt == 0:
            continue
        cost = np.full((len(ws), len(cs)), 1e6)
        for i, a in enumerate(ws):
            for j, b in enumerate(cs):
                if a.date and b.date and _kind_compatible(a, b):
                    d = abs((a.date - b.date).days)
                    if d <= DATE_WINDOW:
                        cost[i, j] = d
        ri, ci = linear_sum_assignment(cost)
        for i, j in zip(ri, ci):
            if cost[i, j] < 1e6:
                a, b = ws[i], cs[j]
                if a.idx in used_w or b.idx in used_c:
                    continue
                matches.append(Match("T4 amount + date", [a], [b], vw(a), vc(b),
                                     [("Rounding", vw(a) - vc(b))] if vw(a) != vc(b) else [],
                                     f"{int(cost[i, j])} day(s) apart"))
                used_w.add(a.idx)
                used_c.add(b.idx)

    # ---------------------------------------------------------------- T5: one entry = several on the other side
    t5_kinds = pay_kinds + (TDS, INVOICE, CN, DN)
    for big_side in ("W", "C"):
        bigs = [e for e in (W.entries if big_side == "W" else C.entries)
                if (e.idx not in (used_w if big_side == "W" else used_c)) and e.kind in pay_kinds + (INVOICE,)]
        smalls_all = [e for e in (C.entries if big_side == "W" else W.entries)
                      if (e.idx not in (used_c if big_side == "W" else used_w)) and e.kind in t5_kinds]
        val_big = vw if big_side == "W" else vc
        val_small = vc if big_side == "W" else vw
        for b in sorted(bigs, key=lambda e: -abs(val_big(e))):
            target = val_big(b)
            if not target or not b.date:
                continue
            used_small = used_c if big_side == "W" else used_w
            window = 31 if b.kind == INVOICE else 30
            cands = [e for e in smalls_all if e.idx not in used_small and e.date
                     and abs((e.date - b.date).days) <= window and val_small(e)
                     and (val_small(e) > 0) == (target > 0) and abs(val_small(e)) < abs(target) + TOL
                     and _t5_compatible(b, e)]
            combo = find_subset(target, cands, val_small, b.date)
            if combo:
                ws, cs = ([b], combo) if big_side == "W" else (combo, [b])
                wa, ca = sum(vw(x) for x in ws), sum(vc(x) for x in cs)
                matches.append(Match("T5 one-to-many", ws, cs, wa, ca,
                                     [("Rounding", wa - ca)] if wa != ca else [],
                                     f"1 {big_side} entry = {len(combo)} entries on the other side"))
                used_w.update(x.idx for x in ws)
                used_c.update(x.idx for x in cs)

    # ---------------------------------------------------------------- T6: invoice booked under the counterparty's own number
    # Counterparties often book our invoice under their own voucher number (no shared invoice number), weeks later
    # (outside the date windows above) and at a different amount (e.g. net of a deduction). Pair them only on
    # evidence found in these two ledgers: one ratio, counterparty amount / Walkover amount, that holds for at least
    # three different invoices. Walkover entries with the same voucher number and date count as one invoice.
    w_groups: dict = defaultdict(list)
    for e in W.entries:
        if e.idx not in used_w and e.kind == INVOICE and e.date and vw(e) > 0:
            w_groups[(e.ref or f"#{e.idx}", e.date)].append(e)
    wg = list(w_groups.values())
    c_inv = [e for e in C.entries if e.idx not in used_c and e.kind == INVOICE and e.date and vc(e) > 0]

    def _gap(ws: list, c: Entry) -> int:
        return (c.date - ws[0].date).days

    votes: dict = defaultdict(list)
    for gi, ws in enumerate(wg):
        wa = sum(vw(x) for x in ws)
        for cj, c in enumerate(c_inv):
            if -7 <= _gap(ws, c) <= 120:
                r = vc(c) / wa
                if 0.8 <= r <= 1.0001:
                    votes[round(r, 3)].append((r, gi, cj))
    if votes:
        _, hits = max(votes.items(), key=lambda kv: len({h[2] for h in kv[1]}))
        if len({h[1] for h in hits}) >= 3 and len({h[2] for h in hits}) >= 3:
            ratio = sorted(h[0] for h in hits)[len(hits) // 2]
            cost = np.full((len(wg), len(c_inv)), 1e6)
            for gi, ws in enumerate(wg):
                wa = sum(vw(x) for x in ws)
                for cj, c in enumerate(c_inv):
                    if -7 <= _gap(ws, c) <= 120 and abs(vc(c) - wa * ratio) <= 2 * TOL:
                        cost[gi, cj] = abs(_gap(ws, c))
            for gi, cj in zip(*linear_sum_assignment(cost)):
                if cost[gi, cj] >= 1e6:
                    continue
                ws, c = wg[gi], c_inv[cj]
                wa, ca = sum(vw(x) for x in ws), vc(c)
                diff = wa - ca
                if abs(diff) <= TOL:
                    parts = [("Rounding", diff)] if diff else []
                else:
                    lab = _tds_pattern(wa, diff)
                    parts = [(f"TDS deducted by counterparty on invoice ({lab})" if lab else
                              f"Counterparty books our invoices {(1 - ratio) * 100:.2f}% lower (same on every invoice)",
                              diff)]
                refs = ", ".join(dict.fromkeys(x.ref for x in ws if x.ref))
                matches.append(Match("T6 booked under counterparty's number", ws, [c], wa, ca, parts,
                                     f"{refs} booked by the counterparty as {c.ref or 'an unnumbered entry'}, "
                                     f"{_gap(ws, c)} day(s) later"))
                used_w.update(x.idx for x in ws)
                used_c.add(c.idx)

    # ---------------------------------------------------------------- T7: revision chains (credit note cancels invoices)
    chains: list[Chain] = []
    for side, entries, used, val in (("W", W.entries, used_w, vw), ("C", C.entries, used_c, vc)):
        by_key: dict[str, list[Entry]] = defaultdict(list)
        for e in entries:
            if e.kind == INVOICE and e.inv_key:
                by_key[e.inv_key].append(e)
        for cn in [e for e in entries if e.kind in (CN, DN) and e.refs_to]:
            cancelled = [x for k in cn.refs_to for x in by_key.get(k, [])]
            if not cancelled:
                continue
            rebill = [e for e in entries if e.kind == INVOICE and e is not cn and set(e.refs_to) & set(cn.refs_to)]
            net = val(cn) + sum(val(x) for x in cancelled)
            ch = Chain(side, cn, cancelled, rebill, abs(net) <= TOL)
            chains.append(ch)
            # all still unmatched and netting to nil on this side -> no effect on the balance
            group = [cn] + cancelled
            if ch.net_nil and all(x.idx not in used for x in group):
                used.update(x.idx for x in group)
                matches.append(Match("T7 revision chain", group if side == "W" else [],
                                     group if side == "C" else [], 0, 0, [],
                                     f"{cn.ref} cancels {', '.join(cn.refs_to)} within {side} books"))
    chain_note = {}
    for ch in chains:
        txt = (f"revision chain: {ch.credit_note.ref} cancels {', '.join(ch.credit_note.refs_to)}"
               + (f"; re-billed as {', '.join(x.inv_key or x.ref for x in ch.rebill)}" if ch.rebill else ""))
        for x in [ch.credit_note] + ch.cancelled + ch.rebill:
            chain_note[x.inv_key or x.ref] = txt
    for m in matches:
        if m.key in chain_note and chain_note[m.key] not in m.note:
            m.note = (m.note + "; " if m.note else "") + chain_note[m.key]

    # ---------------------------------------------------------------- same-side self-cancelling sets
    self_cancel: list[list[Entry]] = []
    for side_entries, used, val in ((W.entries, used_w, vw), (C.entries, used_c, vc)):
        by_date: dict = defaultdict(list)
        for e in side_entries:
            if e.idx not in used and e.date and e.kind != OPENING and val(e):
                by_date[e.date].append(e)
        for d, es in by_date.items():
            if len(es) < 2:
                continue
            for n in range(min(4, len(es)), 1, -1):
                found = False
                for combo in combinations(es, n):
                    if any(x.idx in used for x in combo):
                        continue
                    if abs(sum(val(x) for x in combo)) <= TOL:
                        self_cancel.append(list(combo))
                        used.update(x.idx for x in combo)
                        found = True
                if found:
                    break

    # ---------------------------------------------------------------- T9: period totals (no references on one side)
    for period in ("month", "quarter"):
        def pkey(e: Entry):
            d = e.doc_date or e.date
            if d is None:
                return None
            return (d.year, d.month) if period == "month" else (d.year, (d.month - 1) // 3)
        for kinds in ((INVOICE,), pay_kinds):
            gw: dict = defaultdict(list)
            gc: dict = defaultdict(list)
            for e in W.entries:
                if e.idx not in used_w and e.kind in kinds and pkey(e):
                    gw[pkey(e)].append(e)
            for e in C.entries:
                if e.idx not in used_c and e.kind in kinds and pkey(e):
                    gc[pkey(e)].append(e)
            for k in set(gw) & set(gc):
                ws, cs = gw[k], gc[k]
                if len(ws) + len(cs) < 3:
                    continue
                wa, ca = sum(vw(x) for x in ws), sum(vc(x) for x in cs)
                if wa and abs(wa - ca) <= TOL * (len(ws) + len(cs)):
                    lab = f"{k[0]}-{k[1]:02d}" if period == "month" else f"{k[0]} Q{k[1] + 1} (calendar)"
                    matches.append(Match("T9 period total", ws, cs, wa, ca,
                                         [("Rounding", wa - ca)] if wa != ca else [],
                                         f"{period} {lab}: {len(ws)} vs {len(cs)} entries"))
                    used_w.update(x.idx for x in ws)
                    used_c.update(x.idx for x in cs)

    unmatched_w = [e for e in W.entries if e.idx not in used_w and vw(e)]
    unmatched_c = [e for e in C.entries if e.idx not in used_c and vc(e)]
    return Result(W, C, orient, basis, matches, unmatched_w, unmatched_c, self_cancel, notes, chains)


def _t5_compatible(big: Entry, small: Entry) -> bool:
    if big.kind == INVOICE:
        return small.kind == INVOICE and not small.inv_key or (small.kind == INVOICE and not big.inv_key)
    return small.kind in (PAYMENT, REFUND, TDS)


def find_subset(target: int, cands: list[Entry], val, near, max_items: int = 6, max_cands: int = 18):
    """Smallest set of candidates whose values add up to target (within tolerance).
    Candidates are taken nearest-in-time first; search is bounded so it stays fast."""
    cands = sorted(cands, key=lambda e: abs((e.date - near).days))[:max_cands]
    vals = [val(e) for e in cands]
    tol = TOL
    for n in range(2, min(max_items, len(cands)) + 1):
        # prune: the n largest must reach the target
        if sum(sorted((abs(v) for v in vals), reverse=True)[:n]) < abs(target) - tol:
            continue
        for combo in combinations(range(len(cands)), n):
            if abs(sum(vals[i] for i in combo) - target) <= tol:
                return [cands[i] for i in combo]
    return None


def _kind_compatible(a: Entry, b: Entry) -> bool:
    groups = [{PAYMENT, REFUND}, {INVOICE}, {CN, DN}]
    for g in groups:
        if a.kind in g:
            return b.kind in g
    return True
