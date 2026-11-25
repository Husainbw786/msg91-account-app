"""Payment check: one Walkover ledger, one row per invoice, checked against the payment received on that row.

Reading the file
  1. Rules find the header row and name each column from its header (Invoice Amount, TDS, Amount after, Payment...).
  2. The maths checks that reading against the sheet's own numbers (amount after = invoice - TDS, column totals).
  3. Only when the rules cannot name the required columns, or the numbers do not hold up, AI is asked once to map
     the columns from the header and a few sample rows. AI never reads or types an amount: code copies every cell.

Checking each row (maths only, amounts in paise)
  TDS is a percentage of the taxable value (invoice / 1.18, i.e. before 18% GST), rounded to the rupee.
  Expected receipt = invoice - TDS. The payment is tried against 2% TDS, then 10% TDS, then 0% (paid in full).
  Within Rs 1 of one of them -> remaining 0. Otherwise remaining = expected receipt - payment, using the rate the
  sheet's own TDS column shows for that row (2% when it shows none). No payment -> the whole expected receipt.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from .grid import AMOUNT, DATE, EMPTY, Cell, Sheet, load

GST = 1.18
TOL = 100                                   # Rs 1, in paise
TRY_RATES = (("2%", 2), ("10%", 10))        # tried in this order; then "paid in full" (0%)
ROLES = ("invoice_date", "invoice_no", "invoice_amount", "tds", "net", "received_date", "payment", "remaining")
LABELS = {"invoice_date": "Invoice date", "invoice_no": "Invoice number", "invoice_amount": "Invoice amount",
          "tds": "TDS (in the sheet)", "net": "Amount after TDS (in the sheet)", "received_date": "Payment date",
          "payment": "Payment received", "remaining": "Remaining"}
REQUIRED = ("invoice_amount", "payment")


def col_letter(c: int) -> str:
    s, c = "", c + 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letter: str) -> Optional[int]:
    letter = (letter or "").strip().upper()
    if not letter.isalpha() or len(letter) > 3:
        return None
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# ----------------------------------------------------------------------------------------------- column names
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%#]+", " ", (s or "").lower())).strip()


def role_of(header: str) -> Optional[str]:
    """What a header names. 'date' means a date column whose role is decided by position."""
    t = _norm(header)
    if not t:
        return None
    words = set(t.split())
    if "date" in words or t.endswith(" dt") or t == "dt":
        if re.search(r"receiv|payment|paid|realis|realiz|credit|utr|clear", t):
            return "received_date"
        if re.search(r"invoice|inv|bill", t):
            return "invoice_date"
        return "date"
    if re.search(r"\b(received|receipt|paid|payment|realis\w*|realiz\w*|credited|collected) on\b", t):
        return "received_date"
    if "tds" in words or "tax deducted" in t or "withholding" in t:
        return "tds"
    if re.search(r"remain|outstanding|pending|balance|due|short", t):
        return "remaining"
    if re.search(r"\bafter\b|\bnet\b|receivable|less tds", t):
        return "net"
    if re.search(r"\b(inv|invoice|bill)\b.*\b(no|number|num|#|ref)\b|^(inv|invoice|bill) ?#|^voucher|^invoice id", t):
        return "invoice_no"
    if re.search(r"payment|received|receipt|\bpaid\b|realis|realiz|collected|credited", t):
        return "payment"
    if re.search(r"amount|amt|value|gross|total", t):
        return "invoice_amount"
    return None


def rules_mapping(sheet: Sheet) -> tuple[Optional[int], dict]:
    """Best header row in the first 40 rows, and the columns its headers name."""
    best = (0, None, {})
    for r in range(min(40, sheet.nrows)):
        found: dict[str, int] = {}
        dates: list[int] = []
        for c, cell in enumerate(sheet.rows[r]):
            if cell.kind == EMPTY or not isinstance(cell.value, str):
                continue
            role = role_of(cell.value)
            if role == "date":
                dates.append(c)
            elif role and role not in found:
                found[role] = c
        for c in dates:                       # plain "Date" columns: the first is the invoice date, the next the payment's
            if "invoice_date" not in found:
                found["invoice_date"] = c
            elif "received_date" not in found:
                found["received_date"] = c
        score = len(found) + sum(2 for k in REQUIRED if k in found)
        if score > best[0]:
            best = (score, r, found)
    return best[1], best[2]


# ----------------------------------------------------------------------------------------------- rows
@dataclass
class Row:
    row: int                                  # 1-based sheet row
    invoice_no: str = ""
    invoice_date: Optional[str] = None
    received_date: Optional[str] = None
    invoice: Optional[int] = None             # paise
    tds_sheet: Optional[int] = None
    net_sheet: Optional[int] = None
    payment: Optional[int] = None
    remaining_sheet: Optional[int] = None
    # results
    status: str = ""                          # matched | paid_full | unpaid | short | excess
    rate: str = ""                            # 2% / 10% / 0%
    expected: Optional[int] = None
    remaining: int = 0
    note: str = ""


def _amount(cell: Cell) -> Optional[int]:
    if cell.kind == AMOUNT and cell.paise is not None:
        return cell.paise
    return None


def _date(cell: Cell) -> Optional[str]:
    return cell.date.isoformat() if cell.kind == DATE and cell.date else None


def _is_total(sheet: Sheet, r: int) -> bool:
    return any(isinstance(c.value, str) and re.search(r"\btotal\b", c.value, re.I) for c in sheet.rows[r])


def read_rows(sheet: Sheet, header: int, cols: dict) -> tuple[list[Row], Optional[int]]:
    """Data rows under the header, down to a Total row or the end. Returns (rows, total row index or None)."""
    rows, total = [], None
    blank_run = 0
    for r in range(header + 1, sheet.nrows):
        if _is_total(sheet, r):
            total = r
            break
        get = lambda role: sheet.cell(r, cols[role]) if role in cols else Cell(r, -1, None)
        inv = _amount(get("invoice_amount"))
        if inv is None or inv == 0:
            blank_run = blank_run + 1 if all(c.kind == EMPTY for c in sheet.rows[r]) else 0
            if blank_run >= 5:
                break
            continue
        blank_run = 0
        rows.append(Row(row=r + 1, invoice_no=get("invoice_no").text, invoice_date=_date(get("invoice_date")),
                        received_date=_date(get("received_date")), invoice=inv, tds_sheet=_amount(get("tds")),
                        net_sheet=_amount(get("net")), payment=_amount(get("payment")),
                        remaining_sheet=_amount(get("remaining"))))
    return rows, total


# ----------------------------------------------------------------------------------------------- maths
def tds_for(invoice: int, pct: int) -> int:
    """TDS in paise: pct% of the taxable value (invoice / 1.18), rounded to the rupee."""
    return round(invoice / GST * pct / 100 / 100) * 100


def check_row(x: Row) -> Row:
    tries = [(label, x.invoice - tds_for(x.invoice, pct)) for label, pct in TRY_RATES] + [("0%", x.invoice)]
    # the rate used when nothing matches: the one the sheet's TDS figure shows, else 2%
    default = "2%"
    if x.tds_sheet:
        for label, pct in TRY_RATES:
            if abs(x.tds_sheet - tds_for(x.invoice, pct)) <= TOL:
                default = label
    expected = dict(tries)
    if not x.payment:
        x.status, x.rate, x.expected = "unpaid", default, expected[default]
        x.remaining = expected[default]
        x.note = "No payment received yet"
        return x
    for label, exp in tries:
        if abs(x.payment - exp) <= TOL:
            x.status = "paid_full" if label == "0%" else "matched"
            x.rate, x.expected, x.remaining = label, exp, 0
            x.note = ("Customer paid the full amount, no TDS deducted" if label == "0%"
                      else f"Payment matches invoice less {label} TDS")
            return x
    x.rate, x.expected = default, expected[default]
    x.remaining = expected[default] - x.payment
    x.status = "short" if x.remaining > 0 else "excess"
    x.note = (f"Short by {x.remaining / 100:,.2f} against invoice less {default} TDS" if x.remaining > 0
              else f"Paid {-x.remaining / 100:,.2f} more than invoice less {default} TDS")
    return x


def proofs(rows: list[Row], sheet: Sheet, total: Optional[int], cols: dict) -> list[dict]:
    """Checks of the column reading against the sheet's own numbers."""
    out = []
    pairs = [x for x in rows if x.tds_sheet is not None and x.net_sheet is not None]
    if pairs:
        ok = sum(abs(x.invoice - x.tds_sheet - x.net_sheet) <= TOL for x in pairs)
        out.append({"name": "Amount after TDS = invoice amount − TDS", "ok": ok == len(pairs),
                    "detail": f"{ok} of {len(pairs)} rows agree"})
    tds_rows = [x for x in rows if x.tds_sheet]
    if tds_rows:
        ok = sum(any(abs(x.tds_sheet - tds_for(x.invoice, p)) <= TOL for _, p in TRY_RATES) for x in tds_rows)
        out.append({"name": "TDS is 2% or 10% of the amount before GST", "ok": ok == len(tds_rows),
                    "detail": f"{ok} of {len(tds_rows)} rows agree", "informational": ok != len(tds_rows) and ok > 0})
    if total is not None:
        agree, seen = [], 0
        for role, attr in (("invoice_amount", "invoice"), ("tds", "tds_sheet"), ("net", "net_sheet"),
                           ("payment", "payment")):
            if role not in cols:
                continue
            printed = _amount(sheet.cell(total, cols[role]))
            if printed is None:
                continue
            seen += 1
            s = sum(getattr(x, attr) or 0 for x in rows)
            agree.append((LABELS[role], abs(s - printed) <= max(TOL, 100 * len(rows) // 2), s, printed))
        if seen:
            out.append({"name": "Column totals match the Total row", "ok": all(a[1] for a in agree),
                        "detail": "; ".join(f"{n}: {s / 100:,.2f} vs printed {p / 100:,.2f}" for n, _, s, p in agree)})
    return out


def confidence(cols: dict, rows: list[Row], checks: list[dict]) -> str:
    if any(k not in cols for k in REQUIRED) or not rows:
        return "red"
    hard = [c for c in checks if not c.get("informational")]
    if hard and all(c["ok"] for c in hard):
        return "green"
    if not hard:
        return "amber"                        # nothing in the sheet to prove the reading against
    return "amber" if any(c["ok"] for c in hard) else "red"


# ----------------------------------------------------------------------------------------------- AI mapping
AI_SYSTEM = (
    "You map the columns of an accounts-receivable sheet. Each data row is one sales invoice of the company and "
    "the payment received against it. Answer with the column LETTER for each field, or an empty string if the "
    "sheet has no such column. Fields: invoice_date (date of the invoice), invoice_no, invoice_amount (gross "
    "invoice value), tds (TDS deducted), net (amount after TDS / net receivable), received_date (date the payment "
    "came in), payment (amount actually received), remaining (balance / outstanding). header_row is the 1-based "
    "row number of the column headers. Never invent a column; judge from the headers and the sample values.")
AI_SCHEMA = {"type": "object", "additionalProperties": False,
             "required": ["header_row", *ROLES],
             "properties": {"header_row": {"type": "integer"}, **{r: {"type": "string"} for r in ROLES}}}


def ai_mapping(sheet: Sheet, ai, stats) -> tuple[Optional[int], dict]:
    lines = []
    for r in range(min(25, sheet.nrows)):
        cells = [f"{col_letter(c)}={str(cell.value)[:40]!r}" for c, cell in enumerate(sheet.rows[r]) if cell.kind != EMPTY]
        if cells:
            lines.append(f"row {r + 1}: " + ", ".join(cells))
    ans = ai.json("paycheck-columns", AI_SYSTEM, f"Sheet '{sheet.name}':\n" + "\n".join(lines), AI_SCHEMA,
                  "low", 2000)
    if not ans:
        return None, {}
    cols = {}
    for role in ROLES:
        c = col_index(ans.get(role) or "")
        if c is not None and c < sheet.ncols and c not in cols.values():
            cols[role] = c
    hr = ans.get("header_row")
    header = hr - 1 if isinstance(hr, int) and 1 <= hr <= sheet.nrows else None
    if stats is not None:
        stats.note("AI mapped the columns: " + ", ".join(f"{LABELS[k]} = {col_letter(v)}" for k, v in cols.items()))
    return header, cols


# ----------------------------------------------------------------------------------------------- whole file
@dataclass
class Check:
    sheet: str
    sheets: list
    header_row: Optional[int]                 # 1-based
    cols: dict                                # role -> 0-based column
    columns: list                             # [{col, letter, label, samples}]
    rows: list
    total_row: Optional[int]                  # 1-based
    checks: list
    confidence: str
    mapped_by: str                            # rules | ai | you
    notes: list = field(default_factory=list)


def _evaluate(sheet: Sheet, header: int, cols: dict):
    rows, total = read_rows(sheet, header, cols)
    checks = proofs(rows, sheet, total, cols)
    return rows, total, checks, confidence(cols, rows, checks)


def analyse(path: str, sheet_name: Optional[str] = None, override: Optional[dict] = None,
            ai=None, stats=None) -> Check:
    sheets = load(path)
    if not sheets:
        raise ValueError("the file has no sheets")
    names = [s.name for s in sheets]
    candidates = [s for s in sheets if s.name == sheet_name] or sheets
    notes = []
    if override:                               # columns chosen by the user
        sheet = candidates[0]
        header = int(override.get("header_row", 1)) - 1
        cols = {k: int(v) for k, v in override.get("cols", {}).items() if k in ROLES and v is not None}
        rows, total, checks, conf = _evaluate(sheet, header, cols)
        mapped = "you"
    else:
        best = None
        for sheet in candidates:               # the sheet whose reading proves best
            header, cols = rules_mapping(sheet)
            if header is None:
                continue
            rows, total, checks, conf = _evaluate(sheet, header, cols)
            rank = ({"green": 3, "amber": 2, "red": 1}[conf], len(rows))
            if best is None or rank > best[0]:
                best = (rank, sheet, header, cols, rows, total, checks, conf)
        if best is None:
            sheet, header, cols, rows, total, checks, conf = candidates[0], 0, {}, [], None, [], "red"
        else:
            _, sheet, header, cols, rows, total, checks, conf = best
        mapped = "rules"
        if conf != "green" and ai is not None:  # one AI call, only when the rules' reading does not prove
            a_header, a_cols = ai_mapping(sheet, ai, stats)
            if a_header is not None and all(k in a_cols for k in REQUIRED):
                a = _evaluate(sheet, a_header, a_cols)
                order = {"green": 3, "amber": 2, "red": 1}
                if order[a[3]] > order[conf] or (order[a[3]] == order[conf] and len(a[0]) > len(rows)):
                    header, cols, (rows, total, checks, conf) = a_header, a_cols, a
                    mapped = "ai"
                else:
                    notes.append("AI was asked to map the columns; its reading did not prove better, so the "
                                 "rules' reading was kept.")
    for x in rows:
        check_row(x)
    columns = []
    if header is not None and header < sheet.nrows:
        for c in range(sheet.ncols):
            h = sheet.cell(header, c)
            samples = [sheet.cell(r, c).text for r in range(header + 1, min(header + 6, sheet.nrows))
                       if sheet.cell(r, c).kind != EMPTY][:3]
            if h.kind == EMPTY and not samples:
                continue
            columns.append({"col": c, "letter": col_letter(c), "label": h.text, "samples": samples})
    return Check(sheet=sheet.name, sheets=names, header_row=header + 1 if header is not None else None, cols=cols,
                 columns=columns, rows=rows, total_row=(total + 1) if total is not None else None, checks=checks,
                 confidence=conf, mapped_by=mapped, notes=notes)


def summary(rows: list[Row]) -> dict:
    by = lambda s: [x for x in rows if x.status == s]
    open_rows = [x for x in rows if x.remaining != 0]
    return {"invoices": len(rows), "matched_2": sum(1 for x in by("matched") if x.rate == "2%"),
            "matched_10": sum(1 for x in by("matched") if x.rate == "10%"), "paid_full": len(by("paid_full")),
            "unpaid": len(by("unpaid")), "short": len(by("short")), "excess": len(by("excess")),
            "open": len(open_rows), "remaining_total": sum(x.remaining for x in rows),
            "invoice_total": sum(x.invoice for x in rows), "payment_total": sum(x.payment or 0 for x in rows),
            "all_clear": bool(rows) and not open_rows}


# ----------------------------------------------------------------------------------------------- output file
STATUS_TEXT = {"matched": "Matched", "paid_full": "Matched, full amount paid", "unpaid": "Not paid",
               "short": "Short paid", "excess": "Excess paid"}


def write_output(src: str, ext: str, chk: "Check | dict", out: str) -> None:
    """The user's own sheet with Remaining filled in and a check column added. xlsx keeps its formulas and
    formatting; other formats are rebuilt as a plain xlsx."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    d = chk if isinstance(chk, dict) else as_dict(chk)
    rows, cols, header = d["rows"], d["cols"], d["header_row"]
    if ext in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(src, keep_vba=ext == ".xlsm")
        ws = wb[d["sheet"]]
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = (d["sheet"] or "Sheet1")[:31]
        for sh in load(src):
            if sh.name == d["sheet"]:
                for r in sh.rows:
                    ws.append([c.value for c in r])
                break
    width = max(ws.max_column, max(cols.values(), default=0) + 1)
    rem = cols.get("remaining")
    if rem is None:
        rem = width
        width += 1
        ws.cell(row=header, column=rem + 1, value="Remaining").font = Font(bold=True)
    chk_col = width                                     # 0-based, next free column
    ws.cell(row=header, column=chk_col + 1, value="Payment check").font = Font(bold=True)
    yellow = PatternFill("solid", fgColor="FFFF00")
    blue = PatternFill("solid", fgColor="DDEBFF")
    for x in rows:
        r = x["row"]
        ws.cell(row=r, column=rem + 1, value=round(x["remaining"] / 100, 2))
        c = ws.cell(row=r, column=chk_col + 1, value=f"{STATUS_TEXT[x['status']]}: {x['note']}")
        if x["remaining"] != 0:
            ws.cell(row=r, column=rem + 1).fill = yellow
            c.fill = yellow
        elif x["status"] == "paid_full":                # matched, but flagged: no TDS was deducted
            c.fill = blue
    if d.get("total_row"):
        cell = ws.cell(row=d["total_row"], column=rem + 1)
        if not (isinstance(cell.value, str) and cell.value.startswith("=")):
            cell.value = round(sum(x["remaining"] for x in rows) / 100, 2)
        cell.font = Font(bold=True)
    ws.column_dimensions[col_letter(chk_col)].width = 48
    wb.save(out)


def as_dict(c: Check) -> dict:
    return {"sheet": c.sheet, "sheets": c.sheets, "header_row": c.header_row, "cols": c.cols, "columns": c.columns,
            "rows": [x.__dict__ for x in c.rows], "total_row": c.total_row, "checks": c.checks,
            "confidence": c.confidence, "mapped_by": c.mapped_by, "notes": c.notes, "summary": summary(c.rows)}
