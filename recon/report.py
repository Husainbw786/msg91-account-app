"""Excel output: Summary bridge, matches, unmatched items, parse report and normalised entries."""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L
from openpyxl.workbook.properties import CalcProperties

from .bridge import Bridge
from .details import bridge_details, line_summary
from .match import Result, vw
from .parser import Ledger
from .rows import Entry

F = "Arial"
NUM = '#,##0.00;(#,##0.00);"-"'
HDR = Font(name=F, bold=True, color="FFFFFF")
HFILL = PatternFill("solid", fgColor="1F3864")
BASE = Font(name=F)
BOLD = Font(name=F, bold=True)
CONF_FILL = {"green": "E2EFDA", "amber": "FFF2CC", "red": "FCE4D6"}


def rs(p: int | None):
    return None if p is None else p / 100


def _header(ws, row: int, cols: list[str], widths: list[int]) -> None:
    for j, (c, w) in enumerate(zip(cols, widths), 1):
        x = ws.cell(row, j, c)
        x.font, x.fill = HDR, HFILL
        x.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[L(j)].width = w
    ws.freeze_panes = ws.cell(row + 1, 1)


def _fmt(ws, r0: int, r1: int, num_cols: tuple[int, ...], date_cols: tuple[int, ...] = ()) -> None:
    for r in range(r0, r1 + 1):
        for c in num_cols:
            ws.cell(r, c).number_format = NUM
        for c in date_cols:
            ws.cell(r, c).number_format = "DD-MMM-YY"
        for cell in ws[r]:
            if cell.font is None or not cell.font.bold:
                cell.font = BASE


def _refs(es: list[Entry]) -> str:
    return ", ".join(dict.fromkeys(e.ref or e.ref2 or e.narration[:25] for e in es))


def write_report(res: Result, br: Bridge, path: str, w_name: str = "Walkover", c_name: str = "Counterparty",
                 tds=None, ai=None, w_file: str | None = None, c_file: str | None = None) -> None:
    """w_file / c_file: the ledgers' file names as uploaded, used wherever the report says where to look."""
    wb = Workbook()
    W, C = res.W, res.C

    # ------------------------------------------------------------------ Summary
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"Ledger reconciliation: {w_name} vs {c_name}"
    ws["A1"].font = Font(name=F, bold=True, size=14)
    ws["A2"] = ("Amounts in INR, in Walkover's direction: + means the counterparty owes Walkover. "
                "Blue = values read from the ledgers; black = formulas.")
    ws["A2"].font = Font(name=F, italic=True, color="595959")
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 78
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 14
    r = 4
    for lab, led, fname in ((w_name, W, w_file), (c_name, C, c_file)):
        fname = fname or led.path.split('/')[-1]
        tab = "" if fname.lower().endswith(".pdf") else f" / tab '{led.sheet}'"
        ws.cell(r, 2, f"{lab}: {fname}{tab} - {len(led.entries)} entries, "
                      f"{led.first_date} to {led.last_date}").font = BASE
        c = ws.cell(r, 3, led.confidence.upper())
        c.font = BOLD
        c.fill = PatternFill("solid", fgColor=CONF_FILL[led.confidence])
        r += 1
    r += 1
    details = bridge_details(br, res, w_name, c_name, w_file, c_file)
    _header(ws, r, ["#", "Bridge", "Amount", "Items", "Category", "Where to find it"], [6, 78, 18, 10, 14, 90])
    ws.freeze_panes = None
    r += 1
    start = r
    ws.cell(r, 1, 1)
    ws.cell(r, 2, f"Closing balance per {w_name} ledger (computed from its entries)")
    c = ws.cell(r, 3, rs(br.w_closing))
    c.font = Font(name=F, color="0000FF")
    r += 1
    first_line = r
    for i, bl in enumerate(br.lines, 2):
        ws.cell(r, 1, i)
        ws.cell(r, 2, bl.label)
        ws.cell(r, 3, rs(bl.amount)).font = Font(name=F, color="0000FF")
        ws.cell(r, 4, bl.count)
        ws.cell(r, 5, bl.category)
        ws.cell(r, 6, line_summary(details[i - 2]))
        r += 1
    last_line = r - 1
    ws.cell(r, 2, f"Derived closing balance of {c_name} (in Walkover's direction)").font = BOLD
    ws.cell(r, 3, f"=C{start}+SUM(C{first_line}:C{last_line})" if last_line >= first_line else f"=C{start}").font = BOLD
    derived_row = r
    r += 1
    ws.cell(r, 2, f"Closing balance per {c_name} ledger (computed from its entries, Walkover's direction)")
    ws.cell(r, 3, rs(br.c_closing_w)).font = Font(name=F, color="0000FF")
    c_row = r
    r += 1
    ws.cell(r, 2, "Unexplained difference (must be 0)").font = BOLD
    ws.cell(r, 3, f"=C{c_row}-C{derived_row}").font = BOLD
    r += 2
    ws.cell(r, 2, "Printed closing balances (control check on the parse)").font = BOLD
    r += 1
    for lab, comp, printed in ((w_name, br.w_closing, br.w_closing_printed), (c_name, br.c_closing_w,
                                                                             br.c_closing_printed_w)):
        ws.cell(r, 2, f"{lab}: computed vs printed")
        ws.cell(r, 3, rs(comp))
        ws.cell(r, 4, rs(printed) if printed is not None else "not printed")
        r += 1
    r += 1
    ws.cell(r, 2, "Matching summary").font = BOLD
    r += 1
    tiers: dict[str, int] = {}
    for m in res.matches:
        tiers[m.tier] = tiers.get(m.tier, 0) + 1
    for t, n in sorted(tiers.items()):
        ws.cell(r, 2, t)
        ws.cell(r, 4, n)
        r += 1
    ws.cell(r, 2, "Self-cancelling sets (net nil within one ledger)")
    ws.cell(r, 4, len(res.self_cancel))
    r += 1
    ws.cell(r, 2, f"Unmatched {w_name} entries")
    ws.cell(r, 4, len(res.unmatched_w))
    r += 1
    ws.cell(r, 2, f"Unmatched {c_name} entries")
    ws.cell(r, 4, len(res.unmatched_c))
    r += 2
    for n in res.notes:
        ws.cell(r, 2, n).font = Font(name=F, italic=True)
        r += 1
    if ai is not None and ai.calls:
        r += 1
        ws.cell(r, 2, f"AI assistance: {ai.calls} call(s); {ai.accepted} suggestion(s) accepted after the maths "
                      f"re-check, {ai.rejected} rejected, {ai.failures} failed. AI-made matches are tagged "
                      f"'AI match (amounts verified)' on the Matches sheet.").font = Font(name=F, italic=True)
        r += 1
        for line in ai.log[:30]:
            ws.cell(r, 2, line).font = Font(name=F, italic=True, color="595959")
            r += 1
    _fmt(ws, start, r, (3,))
    for rr in range(start, r):
        ws.cell(rr, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(rr, 6).alignment = Alignment(wrap_text=True, vertical="top")

    # ------------------------------------------------------------------ Bridge details: every entry behind every line
    bd = wb.create_sheet("Bridge details", 1)
    _header(bd, 1, ["#", "Bridge line", "Whose ledger", "Where to find it (file, tab, row, date, number)", "Date", "Voucher / invoice no.",
                    "Matched with", "Type", "Amount", "Narration / note"], [5, 48, 14, 70, 11, 20, 20, 12, 16, 60])
    r = 2
    for i, (bl, det) in enumerate(zip(br.lines, details), 2):
        for d in det:
            bd.cell(r, 1, i)
            bd.cell(r, 2, bl.label)
            bd.cell(r, 3, d["ledger"])
            bd.cell(r, 4, d["where"])
            bd.cell(r, 5, d["date"])
            bd.cell(r, 6, d["ref"])
            bd.cell(r, 7, d["with"])
            bd.cell(r, 8, d["kind"])
            bd.cell(r, 9, rs(d["amount"]))
            bd.cell(r, 10, d["narration"])
            r += 1
    _fmt(bd, 2, r, (9,))
    for rr in range(2, r):
        for c in (2, 4, 10):
            bd.cell(rr, c).alignment = Alignment(wrap_text=True, vertical="top")

    # ------------------------------------------------------------------ Matches
    ms = wb.create_sheet("Matches")
    cols = ["Tier", "Key", f"{w_name} refs", f"{w_name} date", f"{w_name} amount", f"{c_name} refs", f"{c_name} date",
            f"{c_name} amount", "Difference", "Explained as", "Note"]
    _header(ms, 1, cols, [22, 14, 30, 12, 15, 30, 12, 15, 14, 70, 26])
    r = 2
    for m in sorted(res.matches, key=lambda m: (min((e.date for e in m.w if e.date), default=None) or
                                                 min((e.date for e in m.c if e.date), default=None)) or 0):
        ms.cell(r, 1, m.tier)
        ms.cell(r, 2, m.key)
        ms.cell(r, 3, _refs(m.w))
        ms.cell(r, 4, min((e.date for e in m.w if e.date), default=None))
        ms.cell(r, 5, rs(m.w_amount))
        ms.cell(r, 6, _refs(m.c))
        ms.cell(r, 7, min((e.date for e in m.c if e.date), default=None))
        ms.cell(r, 8, rs(m.c_amount))
        ms.cell(r, 9, f"=E{r}-H{r}")
        ms.cell(r, 10, "; ".join(f"{lab}: {amt / 100:,.2f}" for lab, amt in m.parts) or "Exact")
        ms.cell(r, 11, m.note)
        r += 1
    ms.cell(r, 1, "Total").font = BOLD
    for c in (5, 8, 9):
        ms.cell(r, c, f"=SUM({L(c)}2:{L(c)}{r - 1})").font = BOLD
    _fmt(ms, 2, r, (5, 8, 9), (4, 7))
    if res.chains:
        r += 2
        ms.cell(r, 1, "Revision chains (credit note cancelling invoices)").font = BOLD
        r += 1
        for ch in res.chains:
            ms.cell(r, 1, w_name if ch.side == "W" else c_name)
            ms.cell(r, 2, ch.credit_note.ref)
            ms.cell(r, 3, "cancels " + ", ".join(x.inv_key for x in ch.cancelled))
            ms.cell(r, 6, ("re-billed as " + ", ".join(x.inv_key for x in ch.rebill)) if ch.rebill else "")
            net = (ch.credit_note.dr - ch.credit_note.cr) + sum(x.dr - x.cr for x in ch.cancelled)
            ms.cell(r, 10, "credit note equals the invoices it cancels" if ch.net_nil else
                    f"credit note and cancelled invoices differ by {abs(net) / 100:,.2f} in these books")
            for cell in ms[r]:
                cell.font = BASE
            r += 1

    # ------------------------------------------------------------------ Unmatched
    us = wb.create_sheet("Unmatched")
    _header(us, 1, ["Ledger", "Date", "Ref", "Type", "Amount (Walkover direction)", "Bridge bucket", "Narration",
                    "Source rows", "Notes"], [14, 12, 22, 12, 18, 60, 50, 12, 60])
    from .bridge import _c_bucket, _w_bucket
    r = 2
    for e in res.unmatched_w:
        us.cell(r, 1, w_name)
        us.cell(r, 2, e.date)
        us.cell(r, 3, e.ref or e.ref2)
        us.cell(r, 4, e.kind)
        us.cell(r, 5, rs(vw(e)))
        us.cell(r, 6, _w_bucket(e, C.last_date)[0])
        us.cell(r, 7, e.narration[:120])
        us.cell(r, 8, ",".join(map(str, e.rows[:3])))
        us.cell(r, 9, "; ".join(f for f in e.flags if f.startswith(("AI", "type set by AI"))))
        r += 1
    for e in res.unmatched_c:
        us.cell(r, 1, c_name)
        us.cell(r, 2, e.date)
        us.cell(r, 3, e.ref or e.ref2)
        us.cell(r, 4, e.kind)
        us.cell(r, 5, rs(res.orient * (e.dr - e.cr)))
        us.cell(r, 6, _c_bucket(e, W.last_date, W.first_date)[0])
        us.cell(r, 7, e.narration[:120])
        us.cell(r, 8, ",".join(map(str, e.rows[:3])))
        us.cell(r, 9, "; ".join(f for f in e.flags if f.startswith(("AI", "type set by AI"))))
        r += 1
    for grp in res.self_cancel:
        for e in grp:
            us.cell(r, 1, f"{e.side} (self-cancelling)")
            us.cell(r, 2, e.date)
            us.cell(r, 3, e.ref or e.ref2)
            us.cell(r, 4, e.kind)
            us.cell(r, 5, rs(vw(e) if e.side == "W" else res.orient * (e.dr - e.cr)))
            us.cell(r, 6, "Nets to nil with the other lines of this set; no effect on the balance")
            us.cell(r, 7, e.narration[:120])
            r += 1
    _fmt(us, 2, r, (5,), (2,))

    # ------------------------------------------------------------------ TDS
    if tds is not None:
        ts = wb.create_sheet("TDS", 1)
        ts["A1"] = f"TDS: deducted by {c_name} vs booked by {w_name}"
        ts["A1"].font = Font(name=F, bold=True, size=13)
        ts["A2"] = (f"Rate inferred from the data: {tds.rate_label}. Deductions are placed in the year of their "
                    f"provision/usage month only where that makes the years agree (see 'Moved').")
        ts["A2"].font = Font(name=F, italic=True, color="595959")
        _header(ts, 4, ["Financial year", f"Deducted by {c_name}", f"Booked by {w_name}", "Difference",
                        "Moved into this year"], [16, 20, 20, 16, 70])
        ts.freeze_panes = None
        r = 5
        for row in tds.fy_rows:
            ts.cell(r, 1, row["fy"])
            ts.cell(r, 2, rs(row["counterparty"])).font = Font(name=F, color="0000FF")
            ts.cell(r, 3, rs(row["walkover"])).font = Font(name=F, color="0000FF")
            ts.cell(r, 4, f"=C{r}-B{r}")
            ts.cell(r, 5, row["moved"])
            r += 1
        ts.cell(r, 1, "Total").font = BOLD
        for c in (2, 3, 4):
            ts.cell(r, c, f"=SUM({L(c)}5:{L(c)}{r - 1})").font = BOLD
        _fmt(ts, 5, r, (2, 3, 4))
        r += 2
        hdr = ["Invoice", "Gross", "Expected TDS", "Deducted (TDS part)", "Excess withheld", "Posted by counterparty",
               "Year used", "Moved", "Source"]
        for j, h in enumerate(hdr, 1):
            x = ts.cell(r, j, h)
            x.font, x.fill = HDR, HFILL
        ts.column_dimensions["F"].width = 14
        ts.column_dimensions["G"].width = 12
        ts.column_dimensions["H"].width = 8
        ts.column_dimensions["I"].width = 50
        r0 = r + 1
        r += 1
        from .tds import fy_of
        for d in sorted(tds.deductions, key=lambda d: d.posted):
            vals = [d.key, rs(d.gross), rs(d.expected), rs(d.deducted), rs(d.excess) or None, d.posted,
                    fy_of(d.assigned), "yes" if d.moved else "", d.source]
            for j, v in enumerate(vals, 1):
                ts.cell(r, j, v)
            if d.excess:
                ts.cell(r, 5).fill = PatternFill("solid", fgColor="FCE4D6")
            r += 1
        _fmt(ts, r0, r, (2, 3, 4, 5), (6,))
        if tds.other_deductions:
            r += 1
            ts.cell(r, 1, "Other deductions by the counterparty (not TDS at the inferred rate)").font = BOLD
            r += 1
            for k, amt, narr in tds.other_deductions:
                ts.cell(r, 1, k)
                ts.cell(r, 2, rs(amt)).number_format = NUM
                ts.cell(r, 3, narr)
                r += 1
        for n in tds.notes:
            r += 1
            ts.cell(r, 1, n).font = Font(name=F, italic=True)

    # ------------------------------------------------------------------ Parse report
    ps = wb.create_sheet("Parse report")
    _header(ps, 1, ["Ledger", "Item", "Result", "Detail"], [16, 40, 12, 110])
    r = 2
    for lab, led in ((w_name, W), (c_name, C)):
        for t in led.tables:
            ps.cell(r, 1, lab)
            ps.cell(r, 2, f"Table at row {t.header_row + 1} ({t.layout})")
            ps.cell(r, 4, "; ".join(f"{k} = col {v + 1} '{t.labels.get(v, '')}'" for k, v in t.roles.items()))
            r += 1
        for ch in led.checks:
            ps.cell(r, 1, lab)
            ps.cell(r, 2, ch.name)
            ps.cell(r, 3, "PASS" if ch.ok else f"{ch.passed}/{ch.total}")
            ps.cell(r, 3).fill = PatternFill("solid", fgColor="E2EFDA" if ch.ok else "FCE4D6")
            ps.cell(r, 4, ch.detail)
            r += 1
        for n in led.notes:
            ps.cell(r, 1, lab)
            ps.cell(r, 2, "Note")
            ps.cell(r, 4, n)
            r += 1
        flagged = [e for e in led.entries if e.flags]
        for e in flagged[:200]:
            ps.cell(r, 1, lab)
            ps.cell(r, 2, f"Flag on row {e.rows[0]} ({e.ref or e.narration[:20]})")
            ps.cell(r, 4, "; ".join(e.flags))
            r += 1
    _fmt(ps, 2, r, ())
    for rr in range(2, r):
        ps.cell(rr, 4).alignment = Alignment(wrap_text=True, vertical="top")

    # ------------------------------------------------------------------ Entries
    for lab, led in ((w_name, W), (c_name, C)):
        es = wb.create_sheet(f"{lab[:20]} entries")
        _header(es, 1, ["Date", "Doc date", "Type", "Ref", "Ref 2", "Key", "Debit", "Credit", "TDS col",
                        "Kind", "Why", "Narration", "Rows", "Flags"],
                [12, 12, 14, 22, 16, 14, 15, 15, 12, 12, 40, 50, 10, 40])
        r = 2
        for e in led.entries:
            vals = [e.date, e.doc_date, e.vtype, e.ref, e.ref2, e.inv_key, rs(e.dr), rs(e.cr), rs(e.tds) or None,
                    e.kind, e.kind_why, e.narration[:150], ",".join(map(str, e.rows[:4])), "; ".join(e.flags)]
            for j, v in enumerate(vals, 1):
                es.cell(r, j, v)
            r += 1
        es.cell(r, 1, "Total").font = BOLD
        for c in (7, 8, 9):
            es.cell(r, c, f"=SUM({L(c)}2:{L(c)}{r - 1})").font = BOLD
        _fmt(es, 2, r, (7, 8, 9), (1, 2))

    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    wb.save(path)
