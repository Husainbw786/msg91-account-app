"""Stage 1.1 / 1.2 - load any spreadsheet into a typed grid.

Every cell keeps its raw value, its formula (if any) and a parsed type:
date, amount, ref, status, bool, text or empty. Amounts are integer paise.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

EMPTY, DATE, AMOUNT, REF, TEXT, STATUS, BOOL = "empty", "date", "amount", "ref", "text", "status", "bool"

STATUS_WORDS = {"o/s", "os", "paid", "unpaid", "pending", "cleared", "true", "false", "yes", "no", "na", "n/a", "-"}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Cell:
    r: int
    c: int
    value: Any
    kind: str = EMPTY
    paise: Optional[int] = None      # for AMOUNT
    date: Optional[dt.date] = None   # for DATE
    drcr: Optional[str] = None       # 'Dr' / 'Cr' suffix found in the cell
    formula: Optional[str] = None
    repaired: bool = False           # value needed a repair to parse (e.g. 17/-2/2021)
    ambiguous_date: bool = False     # day and month both <= 12 in a text date
    span_end: Optional[int] = None   # last column covered when the cell is a horizontal merge anchor

    @property
    def text(self) -> str:
        if self.value is None:
            return ""
        if isinstance(self.value, (dt.datetime, dt.date)):
            return self.value.strftime("%Y-%m-%d")
        return str(self.value).strip()


@dataclass
class Sheet:
    name: str
    rows: list[list[Cell]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # e.g. how a PDF was read
    # per column with text dates: (order, basis) where order is 'dmy' or 'mdy' and basis says what decided it:
    # 'proof' (a value only one order can read), 'sequence' (only one order keeps the dates in date order),
    # 'default' (nothing decided: Indian day-first), 'mixed' (values prove both orders), 'ai' (the AI said)
    date_info: dict = field(default_factory=dict)

    @property
    def nrows(self) -> int:
        return len(self.rows)

    @property
    def ncols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, r: int, c: int) -> Cell:
        if r < len(self.rows) and c < len(self.rows[r]):
            return self.rows[r][c]
        return Cell(r, c, None)


# --------------------------------------------------------------------------- parsing helpers
_AMT_RE = re.compile(r"^\(?-?\s*(?:₹|rs\.?|inr)?\s*-?\s*[\d,]*\.?\d+\s*\)?\s*(dr\.?|cr\.?|d|c)?$", re.I)


def to_paise(x: float) -> int:
    # round half away from zero on the paisa
    return int(round(x * 100 + (1e-9 if x >= 0 else -1e-9)))


def parse_amount(s: str):
    """'1,23,456.78 Dr.' -> (12345678, 'Dr'); '(5,000)' -> (-500000, None). None if not an amount."""
    t = s.strip().replace(" ", " ")
    if not t or t in {"-", "–"}:
        return None
    if not _AMT_RE.match(t):
        return None
    drcr = None
    m = re.search(r"(dr\.?|cr\.?)\s*$", t, re.I)
    if m:
        drcr = "Dr" if m.group(1).lower().startswith("d") else "Cr"
        t = t[: m.start()]
    neg = t.strip().startswith("(") and t.strip().endswith(")")
    t = re.sub(r"[()₹,\s]|rs\.?|inr", "", t, flags=re.I)
    if t.count("-") > 1 or not re.search(r"\d", t):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    if neg:
        v = -abs(v)
    p = to_paise(v)
    if drcr == "Cr":
        p = -abs(p)
    return p, drcr


_DATE_PATTERNS = [
    re.compile(r"^(\d{1,2})[/\-.](-?\d{1,2})[/\-.](\d{2,4})(?:\s.*)?$"),          # 16/04/2018, 17/-2/2021
    re.compile(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?:[ T].*)?$"),            # 2023-04-01
    re.compile(r"^(\d{1,2})[\s\-/]([A-Za-z]{3,9})[\s\-/,]*(\d{2,4})(?:\s.*)?$"),  # 01-APR-23, 29-SEP-023 11:35
    re.compile(r"^([A-Za-z]{3,9})[\s\-/]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-/]+(\d{2,4})$"),  # Apr 01 2023
]


def _year(y: str) -> int:
    n = int(y)
    if len(y) == 3:          # '023' seen in Oracle run dates
        n = int(y[-2:])
    if n < 100:
        n += 2000 if n < 70 else 1900
    return n


def parse_date(s: str):
    """Return (date, repaired, ambiguous) or None. Day-first (Indian convention) by default."""
    t = s.strip()
    if len(t) < 6 or len(t) > 30:
        return None
    m = _DATE_PATTERNS[0].match(t)
    if m:
        d, mo, y = m.groups()
        repaired = mo.startswith("-")
        mo = mo.lstrip("-")
        d, mo = int(d), int(mo)
        try:
            return dt.date(_year(y), mo, d), repaired, (d <= 12 and mo <= 12 and d != mo)
        except ValueError:
            try:  # maybe month-first
                return dt.date(_year(y), d, mo), True, False
            except ValueError:
                return None
    m = _DATE_PATTERNS[1].match(t)
    if m:
        y, mo, d = m.groups()
        try:
            return dt.date(int(y), int(mo), int(d)), False, False
        except ValueError:
            return None
    m = _DATE_PATTERNS[2].match(t)
    if m:
        d, mon, y = m.groups()
        mo = MONTHS.get(mon[:3].lower())
        if mo:
            try:
                return dt.date(_year(y), mo, int(d)), False, False
            except ValueError:
                return None
    m = _DATE_PATTERNS[3].match(t)
    if m:
        mon, d, y = m.groups()
        mo = MONTHS.get(mon[:3].lower())
        if mo:
            try:
                return dt.date(_year(y), mo, int(d)), False, False
            except ValueError:
                return None
    return None


_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/\-_.#]{2,49}$")


def type_cell(cell: Cell) -> Cell:
    v = cell.value
    if v is None or (isinstance(v, str) and not v.strip()):
        cell.kind = EMPTY
        return cell
    if isinstance(v, bool):
        cell.kind = BOOL
        return cell
    if isinstance(v, dt.datetime) or isinstance(v, dt.date):
        cell.kind = DATE
        cell.date = v.date() if isinstance(v, dt.datetime) else v
        return cell
    if isinstance(v, (int, float)):
        cell.kind = AMOUNT
        cell.paise = to_paise(float(v))
        return cell
    s = str(v).strip().replace(" ", " ")
    low = s.lower()
    if low in STATUS_WORDS:
        cell.kind = STATUS
        if low in {"-"}:
            cell.kind = EMPTY
        return cell
    pd_ = parse_date(s)
    if pd_:
        cell.kind = DATE
        cell.date, cell.repaired, cell.ambiguous_date = pd_
        return cell
    pa = parse_amount(s)
    if pa:
        cell.kind = AMOUNT
        cell.paise, cell.drcr = pa
        return cell
    if _REF_RE.match(s) and re.search(r"\d", s) and " " not in s:
        cell.kind = REF
        return cell
    cell.kind = TEXT
    return cell


# --------------------------------------------------------------------------- loaders
def _load_xlsx(path: Path) -> list[Sheet]:
    import openpyxl

    wb_v = openpyxl.load_workbook(path, data_only=True)
    try:
        wb_f = openpyxl.load_workbook(path, data_only=False)
    except Exception:  # pragma: no cover - formulas are a bonus
        wb_f = None
    sheets = []
    for ws in wb_v.worksheets:
        wsf = wb_f[ws.title] if wb_f else None
        merged = list(ws.merged_cells.ranges)
        grid: list[list[Cell]] = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=True)):
            grid.append([Cell(r_idx, c_idx, val) for c_idx, val in enumerate(row)])
        # unmerge: copy the anchor value down vertical merges only. Horizontal merges (one row,
        # many columns) are layout padding; copying them would create duplicate columns.
        for rng in merged:
            r0, c0 = rng.min_row - 1, rng.min_col - 1
            if r0 >= len(grid) or c0 >= len(grid[r0]):
                continue
            anchor = grid[r0][c0].value
            # copy down the anchor column only; record the horizontal span on each row's anchor cell
            for r in range(r0, min(rng.max_row, len(grid))):
                if c0 < len(grid[r]):
                    if grid[r][c0].value is None:
                        grid[r][c0].value = anchor
                    if rng.max_col > rng.min_col:
                        grid[r][c0].span_end = rng.max_col - 1
        if wsf is not None:
            for r_idx, row in enumerate(wsf.iter_rows()):
                if r_idx >= len(grid):
                    break
                for c_idx, c in enumerate(row):
                    if c_idx < len(grid[r_idx]) and isinstance(c.value, str) and c.value.startswith("="):
                        grid[r_idx][c_idx].formula = c.value
        for row in grid:
            for cell in row:
                type_cell(cell)
        sheets.append(Sheet(ws.title, grid))
    return sheets


def _load_xls(path: Path) -> list[Sheet]:
    import xlrd

    book = xlrd.open_workbook(str(path))
    out = []
    for sh in book.sheets():
        grid = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                v = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    v = xlrd.xldate_as_datetime(v, book.datemode)
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    v = None
                row.append(type_cell(Cell(r, c, v)))
            grid.append(row)
        out.append(Sheet(sh.name, grid))
    return out


def _load_csv(path: Path) -> list[Sheet]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    grid = []
    for r, row in enumerate(csv.reader(text.splitlines(), dialect)):
        grid.append([type_cell(Cell(r, c, v if v != "" else None)) for c, v in enumerate(row)])
    return [Sheet(path.stem, grid)]


def _load_pdf(path: Path) -> list[Sheet]:
    """A PDF read two ways (columns from gaps, columns from alignment); the ledger checks pick the reading."""
    from .pdf import ALIGNED_NOTE, read_pdf_candidates
    cands, notes = read_pdf_candidates(path)
    sheets = []
    for rows, flagged, label in cands:
        grid = [[type_cell(Cell(r, c, v)) for c, v in enumerate(row)] for r, row in enumerate(rows)]
        n = list(notes) + ([ALIGNED_NOTE] if label == "aligned" else [])
        if flagged:
            cells = sorted(flagged)[:12]
            n.append("Cells read from glyph shapes: " + ", ".join(
                f"row {r + 1} '{rows[r][c]}'" for r, c in cells) + (" ..." if len(flagged) > 12 else ""))
        sheets.append(Sheet(path.stem[:31] or "PDF", grid, n))
    if len(sheets) == 1:
        return sheets
    return [_pick_pdf_reading(sheets)]


def _pick_pdf_reading(sheets: list[Sheet]) -> Sheet:
    """Keep the reading whose ledger the file's own numbers prove best; the gap reading wins ties."""
    from .parser import _parse_sheet
    rank = {"green": 2, "amber": 1, "red": 0}
    best, best_key = sheets[0], None
    for i, sh in enumerate(sheets):
        try:
            led = _parse_sheet(sh, "C", "", None, None, None)
        except Exception:
            led = None
        if led is None:
            key = (-1, 0.0, 0, -i)
        else:
            score = sum(c.ratio for c in led.checks if c.total and not c.informational)
            key = (rank.get(led.confidence, 0), round(score, 3), len(led.entries), -i)
        if best_key is None or key > best_key:
            best, best_key = sh, key
    return best


def load(path: str | Path) -> list[Sheet]:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        sheets = _load_xlsx(p)
    elif ext == ".xls":
        sheets = _load_xls(p)
    elif ext in (".csv", ".txt", ".tsv"):
        sheets = _load_csv(p)
    elif ext == ".pdf":
        sheets = _load_pdf(p)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Upload xlsx, xls, csv or pdf.")
    for sh in sheets:
        settle_date_order(sh)
    return sheets


def _numeric_dates(sheet: Sheet, c: int) -> list:
    cells = []
    for row in sheet.rows:
        if c < len(row) and row[c].kind == DATE and isinstance(row[c].value, str):
            m = _DATE_PATTERNS[0].match(row[c].value.strip())
            if m and not m.group(2).startswith("-"):
                cells.append((row[c], int(m.group(1)), int(m.group(2)), _year(m.group(3))))
    return cells


def apply_date_order(sheet: Sheet, c: int, order: str, basis: str) -> None:
    """Re-read every numeric text date of column c as day-first ('dmy') or month-first ('mdy')."""
    for cell, a, b, y in _numeric_dates(sheet, c):
        try:
            cell.date = dt.date(y, a, b) if order == "mdy" else dt.date(y, b, a)
            cell.repaired = False
            cell.ambiguous_date = False
        except ValueError:
            pass
    sheet.date_info[c] = (order, basis)


def settle_date_order(sheet: Sheet) -> None:
    """Day-first or month-first, decided per column from the column's own values (a single cell cannot tell
    2/5/2026 apart). In order of strength: a value only one order can read (1/15/2026 proves month-first; 15/1/2026
    day-first); else the order that keeps the dates in sequence (ledgers are chronological, oldest or newest first);
    else the Indian day-first default, which the AI's stated format may still overrule (extract.py)."""
    for c in range(sheet.ncols):
        cells = _numeric_dates(sheet, c)
        if len(cells) < 2:
            continue
        mdy_proof = sum(1 for _, a, b, _ in cells if a <= 12 < b)
        dmy_proof = sum(1 for _, a, b, _ in cells if b <= 12 < a)
        if mdy_proof and dmy_proof:
            sheet.date_info[c] = ("dmy", "mixed")         # leave each cell as read; the checks will tell
            continue
        if mdy_proof or dmy_proof:
            apply_date_order(sheet, c, "mdy" if mdy_proof else "dmy", "proof")
            continue

        def breaks(order):
            ds = []
            for _, a, b, y in cells:
                try:
                    ds.append(dt.date(y, b, a) if order == "dmy" else dt.date(y, a, b))
                except ValueError:
                    return 10 ** 6
            up = sum(1 for x, z in zip(ds, ds[1:]) if z > x)
            down = sum(1 for x, z in zip(ds, ds[1:]) if z < x)
            return min(up, down)                           # steps against the file's own direction
        bm, bd = breaks("mdy"), breaks("dmy")
        if bm != bd:
            apply_date_order(sheet, c, "mdy" if bm < bd else "dmy", "sequence")
        else:
            apply_date_order(sheet, c, "dmy", "default")
