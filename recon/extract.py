"""AI extraction: AI reads each workbook and fills a fixed extraction format; code copies the data.

Step A - structure. AI receives every sheet's top and bottom rows and returns an extraction spec:
         which sheet is the ledger, its table(s) (header row, first/last data row) and which column holds
         each field, plus the party account for GL dumps. Code then reads every row with that spec, so
         amounts and dates are copied from the cells exactly; AI never types a number.
Step B - row meaning. AI receives the extracted rows in batches and returns, per row, its type (invoice,
         payment, TDS, credit note, ...) and the invoice number it refers to. An invoice number is kept
         only if it appears in that row's own text, so nothing can be invented.

The file's own balances and totals are still checked and shown, so a wrong extraction is visible
before anyone reconciles. Reconciliation itself (match.py, bridge.py, tds.py) uses no AI.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .ai import AIStats, _col_index, _col_letter
from .classify import CN, DN, INVOICE, JOURNAL, OPENING, PAYMENT, REFUND, TDS, classify, invoice_key_in, norm_key
from .detect import ALL_ROLES, Table, _build_table, _decide_layout
from .grid import EMPTY, Sheet, load
from .parser import Ledger, RED, _parse_sheet

KINDS = [INVOICE, PAYMENT, TDS, CN, DN, JOURNAL, REFUND, OPENING, "unknown"]
BATCH = 150

# ============================================================================ A. structure
SPEC_SYSTEM = (
    "You extract accounting ledgers that companies export from any system (Tally, Zoho, SAP, Oracle, Navision, Busy, "
    "hand-made Excel, Google Sheets) into a fixed format. You are shown every sheet of one workbook: its top rows and "
    "its last rows, as cells with column letters. Decide which sheet holds the ledger of transactions, where its "
    "table or tables are (a sheet can restart with a new header half-way down: return each restart as its own table), "
    "and which column holds each field. "
    "Ignore helper columns someone added for their own checks (lookups, TRUE/FALSE, differences), notes sheets and "
    "pivot summaries. For a general-ledger dump that lists every account (bank, expense, tax, control), name the "
    "party's own account (for example the creditors or debtors control account) in party_account. Only map columns "
    "you are sure of; leave a field out rather than guess. For each table give date_format: how its dates are "
    "written, judged from all the date values you see and from the system that made the file (US-style exports "
    "such as Microsoft Dynamics or QuickBooks write MM/DD/YYYY; Indian systems such as Tally write DD/MM/YYYY); "
    "'unknown' if you cannot tell.")

FIELD_HELP = (
    "Fields: date (posting/voucher date, required), doc_date (invoice/document date), vtype (voucher or document "
    "type), ref (invoice or voucher number), ref2 (a second reference such as document no or entry no), narration "
    "(particulars/description), account (GL or offset account), debit, credit, amount (one amount column, signed or "
    "with a separate Dr/Cr column), drcr (that Dr/Cr column), tds (TDS deducted), balance (running balance), "
    "voucher_total (total of a voucher spread over several rows), base (taxable value), gst, gross (invoice value "
    "incl. GST), net (net payable after TDS). In a payable sheet, 'amount payable' is credit and 'amount paid' is "
    "debit.")

_FIELD_ENUM = [r for r in ALL_ROLES if r not in ("pay_date", "party", "status")]
SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "ledger_sheet": {"type": "string"},
        "tables": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "header_row": {"type": "integer", "description": "1-based; 0 if the table has no header row"},
                "first_data_row": {"type": "integer"},
                "last_data_row": {"type": "integer", "description": "0 means to the end of the sheet"},
                "date_format": {"type": "string",
                                "enum": ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD", "text month", "unknown"]},
                "columns": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"field": {"type": "string", "enum": _FIELD_ENUM},
                                   "column": {"type": "string"}},
                    "required": ["field", "column"], "additionalProperties": False}},
            },
            "required": ["header_row", "first_data_row", "last_data_row", "date_format", "columns"],
            "additionalProperties": False}},
        "party_account": {"type": "string", "description": "account value to keep in a GL dump, else empty"},
        "notes": {"type": "string"},
    },
    "required": ["ledger_sheet", "tables", "party_account", "notes"],
    "additionalProperties": False,
}


def _sheet_text(sheet: Sheet, head: int = 40, tail: int = 12, max_cols: int = 40) -> str:
    """The top `head` distinct rows and the last `tail` rows. Rows that repeat an earlier row (a title block
    printed again, a PDF page header) are summarised, so a header row far down still reaches the AI."""
    rows = [r for r in range(sheet.nrows) if any(c.kind != EMPTY for c in sheet.rows[r])]

    def line(r):
        return "; ".join(f"{_col_letter(c.c)}={c.text[:45]!r}" for c in sheet.rows[r][:max_cols]
                         if c.kind != EMPTY and c.text)

    out, seen, shown, skipped, last = [], {}, 0, [], None
    for r in rows:
        if shown >= head:
            break
        key = " ".join(c.text for c in sheet.rows[r] if c.text)
        if key in seen:
            skipped.append(r + 1)
        else:
            if skipped:
                out.append(f"(rows {skipped[0]}-{skipped[-1]}: {len(skipped)} rows repeating earlier rows)")
                skipped = []
            seen[key] = r
            out.append(f"row {r + 1}: " + line(r))
            shown += 1
        last = r
    if skipped:
        out.append(f"(rows {skipped[0]}-{skipped[-1]}: {len(skipped)} rows repeating earlier rows)")
    rest = [r for r in rows if last is not None and r > last]
    if len(rest) > tail:
        out.append(f"... ({len(rest) - tail} more rows) ...")
    out += [f"row {r + 1}: " + line(r) for r in rest[-tail:]]
    return "\n".join(out)


def _check_party_filter(led, sheet: Sheet, side: str, path, spec: dict, stats: AIStats):
    """The AI named an account to keep (a GL dump with many accounts). Wrong here, it drops most of a party
    ledger (Tally prints the offset account in the same column). Read the table without the filter too and
    keep the reading the file's own numbers support better; on a tie keep the AI's."""
    plain = tables_from_spec(sheet, {**spec, "party_account": ""})
    alt = _parse_sheet(sheet, side, path, None, None, plain) if plain else None
    if alt is None:
        return led
    rank = {"green": 2, "amber": 1, "red": 0}
    if led is None or rank[alt.confidence] > rank[led.confidence]:
        stats.log.append(f"structure: kept every row; filtering to account '{spec['party_account']}' left "
                         f"{len(led.entries) if led else 0} row(s) the file's balances did not support "
                         f"({led.confidence if led else 'nothing'} vs {alt.confidence}).")
        return alt
    return led


def request_spec(sheets: list[Sheet], ai, stats: AIStats, hint: str = "") -> Optional[dict]:
    parts = [f"=== Sheet '{s.name}' ({s.nrows} rows) ===\n{_sheet_text(s)}" for s in sheets[:6]]
    user = (f"{FIELD_HELP}\n{hint}\n\nWorkbook:\n\n" + "\n\n".join(parts))
    spec = ai.json("extract-structure", SPEC_SYSTEM, user, SPEC_SCHEMA, effort="high", max_tokens=8000)
    if spec:
        stats.note(f"structure: AI chose sheet '{spec.get('ledger_sheet')}' with {len(spec.get('tables', []))} "
                   f"table(s). {spec.get('notes', '')[:200]}")
    return spec


_AI_ORDER = {"DD/MM/YYYY": "dmy", "MM/DD/YYYY": "mdy"}
_ORDER_WORDS = {"dmy": "day first (DD/MM/YYYY)", "mdy": "month first (MM/DD/YYYY)"}


def _date_order_from_ai(sheet: Sheet, t: Table, roles: dict, said: str) -> None:
    """The AI's stated date format is a second opinion. The column's own values decide first: a value only one
    order can read is proof, and an order that keeps the dates in sequence beats the other. The AI decides only
    when the values cannot (every date ambiguous, sequence no help); a disagreement is written in the notes."""
    from .grid import apply_date_order
    hint = _AI_ORDER.get(said)
    for role in ("date", "doc_date"):
        c = roles.get(role)
        info = sheet.date_info.get(c) if c is not None else None
        if not info or not hint:
            continue
        order, basis = info
        if basis == "default" and hint != order:
            apply_date_order(sheet, c, hint, "ai")
            t.notes.append(f"Dates in column {_col_letter(c)} could be read either way; the AI says this file writes "
                           f"them {_ORDER_WORDS[hint]}, so they were read that way.")
        elif basis in ("proof", "sequence") and hint == order:
            t.notes.append(f"Dates in column {_col_letter(c)} are {_ORDER_WORDS[order]}: the dates' own values and the "
                           f"AI agree.")
        elif basis in ("proof", "sequence") and hint != order:
            why = "a value that can only be read one way" if basis == "proof" else "the dates' own sequence"
            t.notes.append(f"The AI said column {_col_letter(c)} is {_ORDER_WORDS[hint]}, but {why} shows "
                           f"{_ORDER_WORDS[order]}; the values were followed.")


def tables_from_spec(sheet: Sheet, spec: dict) -> list[Table]:
    """AI says where each table's header and data are. The body is read from just below the header to just above
    the next table (or the sheet's end): opening, total and closing rows stay in, because the row reader uses them
    for the opening balance and the totals cross-check."""
    tables = []
    specs = sorted(spec.get("tables", []), key=lambda ts: int(ts.get("header_row") or ts.get("first_data_row") or 0))
    for i, ts in enumerate(specs):
        h = int(ts.get("header_row") or 0) - 1
        start = h + 1 if h >= 0 else int(ts.get("first_data_row") or 1) - 1
        nxt = specs[i + 1] if i + 1 < len(specs) else None
        end = (int(nxt.get("header_row") or nxt.get("first_data_row") or 0) - 2) if nxt else sheet.nrows - 1
        if end < 0 or end >= sheet.nrows:
            end = sheet.nrows - 1
        if start < 0 or start > end:
            continue
        t = _build_table(sheet, h if h >= 0 else start - 1, start, end)
        roles: dict[str, int] = {}
        for col in ts.get("columns", []):
            f, c = col.get("field"), _col_index(col.get("column", ""))
            if f in ALL_ROLES and c is not None and c < sheet.ncols and f not in roles and c not in roles.values():
                roles[f] = c
                t.labels.setdefault(c, "")
        if "date" not in roles:
            continue
        _date_order_from_ai(sheet, t, roles, ts.get("date_format") or "unknown")
        # the headers outrank the AI on direction: a column headed "Debit" is never the credit column. (A
        # T-format ledger balances either way round, so the totals cannot catch a swap.)
        if "debit" in roles and "credit" in roles:
            dl, cl = (t.labels.get(roles["debit"], "") or "").lower(), (t.labels.get(roles["credit"], "") or "").lower()
            says_dr = lambda x: bool(re.search(r"\bdebit|\bdr\b|\bwithdraw", x))
            says_cr = lambda x: bool(re.search(r"\bcredit|\bcr\b|\bdeposit", x))
            if says_cr(dl) and not says_dr(dl) and says_dr(cl) and not says_cr(cl):
                roles["debit"], roles["credit"] = roles["credit"], roles["debit"]
                t.notes.append("AI had the Debit and Credit columns the wrong way round; the column headers were "
                               "followed instead.")
        # one amount column and nothing else to say debit or credit: it is a list of invoices (a register)
        amount_roles = {"debit", "credit"} & set(roles)
        if len(amount_roles) == 1 and not ({"balance", "drcr", "amount", "gross", "net"} & set(roles)):
            roles["gross"] = roles.pop(amount_roles.pop())
        t.roles = roles
        t.role_scores = {r: 1.0 for r in roles}
        _decide_layout(t)
        # columns AI left out but full of numbers may still be a running balance; Stage 2 tests them
        from .detect import profile
        t.meta["free_numeric_columns"] = [c for c in t.labels if c not in roles.values()
                                          and profile(sheet, t, c).share("amount") > 0.8]
        t.meta["ai_layout"] = True
        t.meta["ai_columns"] = dict(roles)
        if spec.get("party_account"):
            t.meta["party_account"] = spec["party_account"]
        t.notes.append("Columns chosen by AI extraction; every amount and date copied from the cells.")
        tables.append(t)
    return _split_restarts(sheet, tables)


def _split_restarts(sheet: Sheet, tables: list[Table]) -> list[Table]:
    """A sheet that restarts with a new header row half-way down holds two tables, each with its own totals and
    balance. If AI returned them as one, split at the new header and keep AI's columns for both parts."""
    from .detect import header_score
    out = []
    for t in tables:
        cuts = [r for r in range(t.start + 1, t.end + 1) if header_score(sheet, r)[0] >= 4
                and any(sheet.cell(k, t.roles["date"]).kind == "date" for k in range(r + 1, min(r + 6, t.end + 1)))]
        if not cuts:
            out.append(t)
            continue
        bounds = [(t.header_row, t.start)] + [(c, c + 1) for c in cuts]
        for i, (h, start) in enumerate(bounds):
            end = (bounds[i + 1][0] - 1) if i + 1 < len(bounds) else t.end
            part = _build_table(sheet, h, start, end)
            for c in t.roles.values():
                part.labels.setdefault(c, "")
            part.roles, part.role_scores = dict(t.roles), dict(t.role_scores)
            _decide_layout(part)
            part.meta, part.notes = dict(t.meta), list(t.notes)
            from .detect import profile
            part.meta["free_numeric_columns"] = [c for c in part.labels if c not in part.roles.values()
                                                 and profile(sheet, part, c).share("amount") > 0.8]
            if i:
                part.notes.append(f"A new header at row {h + 1} starts a second table; read separately.")
            out.append(part)
    return out


# ============================================================================ B. row meaning
TYPE_SYSTEM = (
    "You label rows of one company's ledger. For each row give its type and, when the row is about a specific "
    "invoice, that invoice's number exactly as written in the row (in the reference columns or the narration). "
    "Types: invoice (a bill raised), payment (money paid or received), tds (tax deducted at source), credit_note, "
    "debit_note, journal (provisions, round-off, tax adjustments), refund (money returned), opening (opening "
    "balance), unknown. Use unknown rather than guessing. Leave invoice_ref empty when the row names no invoice.")

TYPE_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "type": {"type": "string", "enum": KINDS},
                       "invoice_ref": {"type": "string"}},
        "required": ["id", "type", "invoice_ref"], "additionalProperties": False}}},
    "required": ["rows"], "additionalProperties": False,
}


def type_entries(entries, ai, stats: AIStats, label: str) -> None:
    """AI types every row; references are kept only if they occur in the row's own text."""
    for i in range(0, len(entries), BATCH):
        chunk = entries[i:i + BATCH]
        rows = [{"id": e.idx, "date": str(e.date), "vtype": e.vtype, "ref": e.ref, "ref2": e.ref2,
                 "account": e.account[:60], "narration": e.narration[:160],
                 "debit": round(e.dr / 100, 2) if e.dr else 0, "credit": round(e.cr / 100, 2) if e.cr else 0,
                 "tds": round(e.tds / 100, 2) if e.tds else 0} for e in chunk]
        ans = ai.json("extract-types", TYPE_SYSTEM, f"Ledger: {label}\nRows:\n{json.dumps(rows, ensure_ascii=False)}",
                      TYPE_SCHEMA, effort="medium", max_tokens=16000)
        if not ans:
            stats.note(f"types: batch {i // BATCH + 1} not answered; rule-based types kept for those rows")
            continue
        by_id = {e.idx: e for e in chunk}
        for r in ans.get("rows", []):
            e = by_id.get(r.get("id"))
            if e is None:
                continue
            k = r.get("type")
            if k in KINDS and k != "unknown":
                e.kind, e.kind_why = k, "AI extraction"
            ref = (r.get("invoice_ref") or "").strip()
            if ref:
                text = " ".join([e.ref, e.ref2, e.narration, e.account])
                if _norm(ref) and _norm(ref) in _norm(text):         # must be copied from the row, not invented
                    key = invoice_key_in(ref) or norm_key(ref)
                    if key and e.kind in (CN, DN) and _norm(ref) not in _norm(e.ref + e.ref2):
                        # a credit/debit note keeps its own number; the invoices it names are a revision chain
                        if key not in e.refs_to:
                            e.refs_to.append(key)
                    elif key:
                        e.inv_key = key
                        if key not in e.keys:
                            e.keys = [key] + e.keys
                else:
                    stats.rejected += 1
                    stats.note(f"types: invoice ref '{ref[:30]}' for row {e.rows[0]} not found in that row; ignored")
        stats.accepted += 1


def _direction_guard(entries, inv_side: str, stats: AIStats) -> None:
    """An 'invoice' on the payment side of the ledger is a payment against that invoice (common in GL dumps where
    the invoice and its payment carry the same number and text). Maths decides direction, not AI."""
    fixed = 0
    for e in entries:
        on_inv_side = (e.dr >= e.cr) == (inv_side == "dr")
        if e.kind == INVOICE and not on_inv_side and (e.dr or e.cr):
            e.kind, e.kind_why = PAYMENT, "AI said invoice; it sits on the payment side, so payment"
            fixed += 1
    if fixed:
        stats.note(f"types: {fixed} row(s) labelled invoice but on the payment side were set to payment")


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# ============================================================================ end to end
def extract_ledger(path: str, side: str, ai, stats: AIStats, store=None, sheet_name: Optional[str] = None,
                   role_overrides: Optional[dict] = None, label: str = "") -> Ledger:
    """AI-driven extraction of one workbook into the standard ledger format."""
    from .profiles import fingerprint

    p = str(path).split("#", 1)[0]
    sheets = load(p)
    led: Optional[Ledger] = None
    if role_overrides and sheet_name:
        # the user corrected columns in step 1: re-read deterministically with their mapping
        from .parser import parse_file
        led = parse_file(p, side, sheet_name, store=None, role_overrides=role_overrides)
    else:
        cand = [s for s in sheets if not sheet_name or s.name.lower() == sheet_name.lower()] or sheets
        hint = f"The user picked sheet '{sheet_name}'." if sheet_name else ""
        spec = request_spec(cand, ai, stats, hint)
        if spec:
            target = next((s for s in cand if s.name == spec.get("ledger_sheet")), None) or \
                next((s for s in cand if s.name.lower() == str(spec.get("ledger_sheet", "")).lower()), cand[0])
            tables = tables_from_spec(target, spec)
            if tables:
                led = _parse_sheet(target, side, p, None, None, tables)
                if spec.get("party_account"):
                    led = _check_party_filter(led, target, side, p, spec, stats)
        if led is None:
            stats.failures += 1
            raise ValueError("AI extraction could not find a ledger table in this file. Check that it holds "
                             "transactions with dates and amounts, or set the columns by hand.")
        led.other_sheets = [s.name for s in sheets if s.name != led.sheet]
    info = classify(led.entries, led.party_account or "")            # keys, UTRs and a first type for every row
    type_entries(led.entries, ai, stats, label or ("Walkover's books" if side == "W" else "counterparty's books"))
    _direction_guard(led.entries, info["invoice_side"], stats)
    led.notes.insert(0, "Extracted with AI: AI chose the sheet, table and columns and labelled each row; "
                        "all amounts and dates were copied from the cells by code.")
    if led.confidence == RED:
        led.notes.insert(1, "The file's own balances/totals could not confirm this extraction. Check the columns "
                            "below before reconciling.")
    if store is not None:
        for t in led.tables:
            t.meta.setdefault("fingerprint", fingerprint(t))
    led.meta = {"invoice_side": info["invoice_side"]}
    return led
