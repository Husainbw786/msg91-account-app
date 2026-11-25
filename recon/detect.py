"""Stage 1.3 - 1.5: find tables, header rows and the role of every column.

Evidence for a column's role = header-name similarity + content fit + formula hints,
resolved for all columns at once with the Hungarian algorithm.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from .grid import AMOUNT, BOOL, DATE, EMPTY, REF, STATUS, TEXT, Sheet

# role -> header synonyms (lower case). Order inside a list does not matter.
SYNONYMS: dict[str, list[str]] = {
    "date": ["date", "posting date", "gl date", "vch date", "voucher date", "txn date", "transaction date",
             "entry date", "dt", "tarikh"],
    "doc_date": ["document date", "invoice date", "bill date", "doc date", "inv date"],
    "pay_date": ["payment date", "paid date", "clearing date"],
    "vtype": ["vch type", "voucher type", "document type", "doc type", "type", "invoice type", "transaction type",
              "entry type"],
    "ref": ["voucher number", "voucher no", "vch no", "document no", "doc no", "invoice no", "invoice number",
            "bill no", "reference", "ref no", "ref", "external doc no", "vendor invoice no", "inv no",
            "description invoice"],
    "ref2": ["document no", "ap voucher no", "entry no", "voucher no", "transaction id", "utr", "cheque no"],
    "narration": ["particulars", "narration", "description", "details", "remarks", "memo", "particular"],
    "account": ["account name", "account", "offset ledger", "ledger", "gl account", "contra account", "head"],
    "party": ["vendor name", "customer name", "party", "party name", "supplier"],
    "debit": ["debit", "dr", "debit amount", "total of dr", "withdrawal", "amount paid", "paid", "payment",
              "dr amount"],
    "credit": ["credit", "cr", "credit amount", "total of cr", "deposit", "amount payable", "payable",
               "amount paybale", "cr amount"],
    "amount": ["amount", "value", "invoice amount", "bill amount", "amt"],
    "drcr": ["dr/cr", "dr cr", "debit/credit", "d/c", "sign"],
    "tds": ["tds", "tds amount", "tds deducted", "withholding", "tds amt"],
    "balance": ["balance", "running balance", "closing balance", "bal", "net balance"],
    "voucher_total": ["voucher amount", "voucher total", "bill total"],
    "base": ["base amount", "taxable value", "taxable amount", "base", "basic amount", "net amount"],
    "gst": ["gst", "igst", "tax amount", "gst amount", "tax"],
    "gross": ["gross", "total", "invoice value", "total amount", "gross amount"],
    "net": ["net payable", "net amount payable", "amount after tds", "net"],
    "status": ["status", "remark", "o/s"],
}
AMOUNT_ROLES = {"debit", "credit", "amount", "tds", "balance", "voucher_total", "base", "gst", "gross", "net"}
DATE_ROLES = {"date", "doc_date", "pay_date"}
TEXT_ROLES = {"vtype", "narration", "account", "party", "status", "drcr"}
REF_ROLES = {"ref", "ref2"}
ALL_ROLES = list(SYNONYMS)

CONTROL_RE = re.compile(r"^\s*(grand\s+)?(total|opening balance|closing balance|balance to pay|reckoning|page no"
                        r"|carried (forward|over)|brought (forward|over)|c/f|b/f|c/o|b/o|total paid|sub\s*total"
                        r"|net balance)", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9/ ]+", " ", s.lower()).strip()


def name_score(label: str, role: str) -> float:
    lab = _norm(label)
    if not lab:
        return 0.0
    best = 0.0
    toks = set(lab.replace("/", " ").split())
    for syn in SYNONYMS[role]:
        if lab == syn:
            return 1.0
        s = fuzz.token_sort_ratio(lab, syn) / 100
        s = s if s >= 0.85 else 0.0
        stoks = set(syn.replace("/", " ").split())
        if stoks and stoks <= toks:
            s = max(s, 0.9 - 0.05 * (len(toks) - len(stoks)))
        best = max(best, s)
    return best


@dataclass
class Table:
    sheet: str
    header_row: int                     # index into sheet.rows
    start: int                          # first data row
    end: int                            # last data row (inclusive)
    labels: dict[int, str]              # data column -> header label
    roles: dict[str, int] = field(default_factory=dict)   # role -> column
    role_scores: dict[str, float] = field(default_factory=dict)
    layout: str = ""                    # drcr | flag | signed | register
    meta: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def header_score(sheet: Sheet, r: int) -> tuple[float, int]:
    """How much row r looks like a header: number of distinct roles named."""
    roles_hit: set[str] = set()
    texts = 0
    for cell in sheet.rows[r]:
        if cell.kind in (TEXT, REF, STATUS) and cell.text and len(cell.text) <= 40:
            texts += 1
            for role in ALL_ROLES:
                if name_score(cell.text, role) >= 0.85:
                    roles_hit.add(role)
    # header rows hold labels, not dates or amounts
    if any(c.kind == DATE for c in sheet.rows[r]):
        return 0, texts
    has_amount = bool(roles_hit & AMOUNT_ROLES)
    has_date = bool(roles_hit & DATE_ROLES)
    score = len(roles_hit) + (1 if has_amount else 0) + (1 if has_date else 0)
    # a header row is mostly labels, not numbers
    nums = sum(1 for c in sheet.rows[r] if c.kind in (AMOUNT, DATE))
    if nums > texts:
        score = 0
    return score, texts


def _data_like(sheet: Sheet, r: int) -> bool:
    row = sheet.rows[r]
    return any(c.kind == DATE for c in row) and any(c.kind == AMOUNT for c in row)


def find_tables(sheet: Sheet) -> list[Table]:
    """Header rows followed by data rows. A sheet can contain several tables."""
    cand = []
    for r in range(sheet.nrows):
        s, texts = header_score(sheet, r)
        if s >= 4 and texts >= 3:
            # must be followed by data within 4 rows
            if any(_data_like(sheet, k) for k in range(r + 1, min(r + 6, sheet.nrows))):
                cand.append(r)
    tables = []
    for i, h in enumerate(cand):
        nxt = cand[i + 1] if i + 1 < len(cand) else sheet.nrows
        # collapse header rows that are back to back (2-row headers)
        if i + 1 < len(cand) and cand[i + 1] - h <= 1:
            continue
        end = nxt - 1
        while end > h and not any(c.kind != EMPTY for c in sheet.rows[end]):
            end -= 1
        tables.append(_build_table(sheet, h, h + 1, end))
    return tables


def _build_table(sheet: Sheet, h: int, start: int, end: int) -> Table:
    # data columns = columns with values in the body
    occ: dict[int, int] = {}
    for r in range(start, end + 1):
        for cell in sheet.rows[r]:
            if cell.kind != EMPTY:
                occ[cell.c] = occ.get(cell.c, 0) + 1
    body = max(1, end - start + 1)
    header_cols = {cell.c for cell in sheet.rows[h] if cell.kind != EMPTY} if 0 <= h < sheet.nrows else set()
    # a sparse column still counts when the header names it (a credit column with one credit in it)
    data_cols = sorted(c for c, n in occ.items()
                       if n >= (1 if body <= 3 else max(2, 0.02 * body)) or c in header_cols)
    # each data column's horizontal span (merged cells), taken as the most common span in the body
    dspan: dict[int, int] = {}
    for dc in data_cols:
        ends: dict[int, int] = {}
        for r in range(start, end + 1):
            cell = sheet.cell(r, dc)
            if cell.kind != EMPTY:
                e = cell.span_end if cell.span_end is not None else dc
                ends[e] = ends.get(e, 0) + 1
        dspan[dc] = max(ends, key=ends.get) if ends else dc
    # header labels from the header row plus up to 2 rows above (multi-row headers)
    labels_at: dict[int, tuple[str, int]] = {}
    for rr in (h - 2, h - 1, h):
        if rr < 0:
            continue
        for cell in sheet.rows[rr]:
            if cell.kind in (TEXT, REF, STATUS) and cell.text and len(cell.text) < 60:
                if rr != h and not any(name_score(cell.text, ro) for ro in ALL_ROLES) \
                        and header_score(sheet, rr)[0] < 2:
                    continue
                labels_at[cell.c] = (cell.text, cell.span_end if cell.span_end is not None else cell.c)
    # attach each header label to the data column it overlaps (exports often shift headers vs data)
    labels: dict[int, str] = {}
    hcols = sorted(labels_at)
    if hcols and data_cols:
        cost = np.full((len(hcols), len(data_cols)), 1e6)
        for i, hc in enumerate(hcols):
            h0, h1 = hc, labels_at[hc][1]
            for j, dc in enumerate(data_cols):
                d0, d1 = dc, dspan[dc]
                overlap = min(h1, d1) - max(h0, d0) + 1
                dist = dc - hc
                if overlap > 0:
                    cost[i, j] = -10 * overlap + abs(dist) * 0.5
                elif abs(dist) <= 3:
                    cost[i, j] = abs(dist) + (0.1 if dist > 0 else 0)
        ri, ci = linear_sum_assignment(cost)
        for i, j in zip(ri, ci):
            if cost[i, j] < 1e6:
                labels[data_cols[j]] = labels_at[hcols[i]][0]
    for dc in data_cols:
        labels.setdefault(dc, "")
    t = Table(sheet.name, h, start, end, labels)
    return t


def headerless_table(sheet: Sheet) -> Optional[Table]:
    """Fallback when no header row is found: use the block of rows that have a date and an amount."""
    rows = [r for r in range(sheet.nrows) if _data_like(sheet, r)]
    if len(rows) < 3:
        return None
    t = _build_table(sheet, rows[0] - 1, rows[0], rows[-1])
    t.notes.append("No header row found; columns identified from their contents only.")
    return t


# --------------------------------------------------------------------------- column profiles
@dataclass
class ColProfile:
    col: int
    n: int
    kinds: dict[str, int]
    distinct: int
    avg_len: float
    neg_share: float
    formulas: list[str]
    drcr_share: float

    def share(self, *kinds: str) -> float:
        return sum(self.kinds.get(k, 0) for k in kinds) / self.n if self.n else 0.0


def profile(sheet: Sheet, t: Table, col: int) -> ColProfile:
    kinds: dict[str, int] = {}
    vals = set()
    lens = []
    neg = 0
    amt = 0
    drcr = 0
    forms = []
    n = 0
    for r in range(t.start, t.end + 1):
        cell = sheet.cell(r, col)
        if cell.kind == EMPTY:
            continue
        n += 1
        kinds[cell.kind] = kinds.get(cell.kind, 0) + 1
        vals.add(cell.text.lower())
        lens.append(len(cell.text))
        if cell.kind == AMOUNT:
            amt += 1
            if cell.paise is not None and cell.paise < 0:
                neg += 1
            if cell.drcr:
                drcr += 1
        if cell.text.lower() in {"dr", "cr", "d", "c", "debit", "credit"}:
            drcr += 1
        if cell.formula and len(forms) < 5:
            forms.append(cell.formula.upper())
    return ColProfile(col, n, kinds, len(vals), sum(lens) / len(lens) if lens else 0, neg / amt if amt else 0,
                      forms, drcr / n if n else 0)


def formula_hint(p: ColProfile) -> dict[str, float]:
    """Formulas tell us what a column is: =C2*18% -> gst, =C2+D2 -> gross, =-C2*2% -> tds, VLOOKUP -> helper."""
    hints: dict[str, float] = {}
    if not p.formulas:
        return hints
    f = p.formulas[0]
    if re.search(r"VLOOKUP|HLOOKUP|MATCH\(|INDEX\(|XLOOKUP|COUNTIF|=\s*[A-Z]+\d+\s*=\s*[A-Z]+\d+", f):
        hints["__helper__"] = 1.0
    elif re.search(r"\*\s*(18|12|5|28)\s*%|\*\s*0?\.(18|12|05|28)\b", f):
        hints["gst"] = 0.9
    elif re.search(r"\*\s*(0?\.?\d+)\s*%|\*\s*0?\.0\d", f) and ("-" in f[:3]):
        hints["tds"] = 0.9
    elif re.fullmatch(r"=\+?[A-Z]+\d+\s*\+\s*[A-Z]+\d+\s*\+\s*[A-Z]+\d+", f):
        hints["net"] = 0.6
    elif re.fullmatch(r"=\+?[A-Z]+\d+\s*\+\s*[A-Z]+\d+", f):
        hints["gross"] = 0.7
    elif re.fullmatch(r"=\+?[A-Z]+\d+\s*-\s*[A-Z]+\d+", f):
        hints["__helper__"] = 0.6
    return hints


def content_score(p: ColProfile, role: str) -> float:
    if p.n == 0:
        return 0.0
    if role in DATE_ROLES:
        return p.share(DATE)
    if role in AMOUNT_ROLES:
        s = p.share(AMOUNT)
        if role == "balance":
            return s
        if role == "tds" and p.neg_share > 0.5:
            return s
        return s * (1 - 0.5 * p.neg_share) if role in ("debit", "credit") else s
    if role in REF_ROLES:
        return max(p.share(REF), p.share(AMOUNT) * 0.5 if p.avg_len >= 4 else 0,
                   p.share(TEXT) * 0.3 if p.avg_len <= 20 else 0)      # short codes like 'A1' type as text
    if role == "narration":
        return p.share(TEXT, REF) * min(1.0, p.avg_len / 8)
    if role == "vtype":
        ratio = p.distinct / p.n
        limit = 0.1 if p.n >= 30 else 0.7          # small files: few rows, so few repeats
        return p.share(TEXT, STATUS) * (1.0 if ratio < limit else 0.3)
    if role in ("account", "party"):
        ratio = p.distinct / p.n
        return p.share(TEXT) * (1.0 if ratio < 0.3 else 0.3)
    if role == "drcr":
        return p.drcr_share if p.share(TEXT, STATUS) > 0.5 else 0.0
    if role == "status":
        return p.share(STATUS, BOOL)
    return 0.0


def assign_roles(sheet: Sheet, t: Table) -> None:
    cols = sorted(t.labels)
    profs = {c: profile(sheet, t, c) for c in cols}
    roles = ALL_ROLES
    score = np.zeros((len(cols), len(roles)))
    helper_cols = set()
    for i, c in enumerate(cols):
        p = profs[c]
        hints = formula_hint(p)
        if hints.get("__helper__", 0) >= 0.6:
            helper_cols.add(c)
            continue
        if p.share(BOOL) > 0.5:
            helper_cols.add(c)
            continue
        for j, role in enumerate(roles):
            ns = name_score(t.labels[c], role)
            cs = content_score(p, role)
            hs = hints.get(role, 0.0)
            if cs < (0.25 if ns >= 0.95 else 0.5) and hs == 0:
                continue                        # content must fit the role (less strictly for an exact header)
            if ns == 0 and hs == 0:
                # unlabelled column: allowed only on strong content for a few roles
                if role in ("narration",) and cs > 0.6 and not t.labels[c]:
                    score[i, j] = 0.35 * cs
                continue
            score[i, j] = 0.55 * ns + 0.35 * cs + 0.6 * hs
    # one column per role, one role per column
    big = score.max() + 1 if score.size else 1
    ri, ci = linear_sum_assignment(big - score)
    for i, j in zip(ri, ci):
        if score[i, j] >= 0.45:
            t.roles[roles[j]] = cols[i]
            t.role_scores[roles[j]] = round(float(score[i, j]), 2)
    if helper_cols:
        t.meta["helper_columns"] = sorted(helper_cols)
        t.notes.append(f"Ignored {len(helper_cols)} helper/formula column(s): "
                       + ", ".join(t.labels[c] or f"col {c + 1}" for c in sorted(helper_cols)))
    _infer_unlabelled(sheet, t, profs, helper_cols)
    _decide_layout(t)


def _infer_unlabelled(sheet: Sheet, t: Table, profs: dict[int, ColProfile], helper_cols: set[int]) -> None:
    """Numeric columns without a usable header: try gross (= base + gst) and balance (running relation)."""
    used = set(t.roles.values()) | helper_cols
    free = [c for c in t.labels if c not in used and profs[c].share(AMOUNT) > 0.8]
    if "base" in t.roles and "gst" in t.roles and "gross" not in t.roles:
        for c in free:
            ok = tot = 0
            for r in range(t.start, t.end + 1):
                b, g, x = sheet.cell(r, t.roles["base"]), sheet.cell(r, t.roles["gst"]), sheet.cell(r, c)
                if b.paise is not None and g.paise is not None and x.paise is not None:
                    tot += 1
                    ok += abs(b.paise + g.paise - x.paise) <= 100
            if tot >= 3 and ok / tot > 0.9:
                t.roles["gross"] = c
                t.role_scores["gross"] = 0.9
                t.notes.append(f"Column {c + 1} has no header; identified as gross because base + GST = it on "
                               f"{ok}/{tot} rows.")
                free.remove(c)
                break
    t.meta["free_numeric_columns"] = free


def _decide_layout(t: Table) -> None:
    r = t.roles
    if "debit" in r and "credit" in r:
        t.layout = "drcr"
    elif "amount" in r and "drcr" in r:
        t.layout = "flag"
    elif any(k in r for k in ("gross", "base", "net")) or ("amount" in r and "debit" not in r and "credit" not in r):
        t.layout = "register"
        if "amount" in r and "gross" not in r:
            r["gross"] = r.pop("amount")
    elif "debit" in r or "credit" in r:
        t.layout = "drcr"
    else:
        t.layout = "unknown"


def detect(sheet: Sheet) -> list[Table]:
    tables = find_tables(sheet)
    if not tables:
        ht = headerless_table(sheet)
        tables = [ht] if ht else []
    for t in tables:
        assign_roles(sheet, t)
    return tables
