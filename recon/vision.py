"""Second reader for PDF ledgers: the page images, read by a vision model. Used only when the text reading
(pdf.py) does not give a ledger its own totals prove.

  1. each page is drawn as a PNG (pypdfium2) and published at a short-lived link (the web app serves it; the
     link expires in minutes and is random), because viaSocket's AI takes attachments by URL only;
  2. the model gets a few pages at a time and one fixed instruction: copy the ledger as rows (date, particulars,
     voucher type, voucher no., bill reference, debit, credit, balance) plus its opening / total / closing rows,
     exactly as printed, never computing anything;
  3. every amount it returns must also be in the PDF's own text on those pages (when the PDF has text). An
     amount that is not found is counted, and the ledger cannot show as proven unless all amounts are found;
  4. the rows become an ordinary sheet with standard headers, read by the same row reader and the same checks
     (running balance, column totals, opening + movements = closing) as any Excel file.
"""
from __future__ import annotations

import io
import os
from typing import Callable, Optional

from .grid import Cell, Sheet, parse_amount, type_cell

PAGES_PER_CALL = int(os.environ.get("VISION_PAGES_PER_CALL", "3"))
MAX_PAGES = int(os.environ.get("VISION_MAX_PAGES", "40"))
LONG_EDGE_PX = 1800
HEADERS = ["Date", "Particulars", "Vch Type", "Vch No.", "Bill Ref", "Debit", "Credit", "Balance"]

_publisher: Optional[Callable[[bytes, str], str]] = None


def set_publisher(fn: Optional[Callable[[bytes, str], str]]) -> None:
    """fn(bytes, content_type) -> public https URL, valid for a few minutes. Set by the web app."""
    global _publisher
    _publisher = fn


def enabled(ai) -> bool:
    return (os.environ.get("VISION", "on").lower() != "off" and _publisher is not None
            and ai is not None and getattr(ai, "supports_attachments", False))


SYSTEM = (
    "You copy accounting ledgers from page images into rows. Copy exactly what is printed; never calculate, "
    "round, re-sign or guess a value. One row per voucher (transaction) of the ledger account: its date, "
    "particulars (the other account or description), voucher type, voucher number, the bill/invoice number it "
    "refers to if the voucher shows one (e.g. 'New Ref 250405-146' or 'Agst Ref ...' -> 250405-146), and the amount "
    "in the Debit or Credit column it is printed in, plus the running balance if the ledger prints one (with its "
    "Dr/Cr). Breakdown lines inside a voucher (expense heads, GST input, TDS lines, round off, bill allocation "
    "lines, narration) are NOT rows: put the bill number they mention in bill_ref of their voucher. When a date is "
    "printed only once for several vouchers, repeat it on each. Also give the opening balance row, every total "
    "row (including 'carried over' / 'brought forward' page totals) and the closing balance row, with kind "
    "opening / total / closing and their amounts in the columns they are printed in. Amounts exactly as printed, "
    "with their commas and decimals. Leave a field empty if it is not printed."
)

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["rows", "notes"],
    "properties": {
        "rows": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "date", "particulars", "vch_type", "vch_no", "bill_ref", "debit", "credit",
                         "balance"],
            "properties": {
                "kind": {"type": "string", "enum": ["entry", "opening", "total", "closing"]},
                "date": {"type": "string"}, "particulars": {"type": "string"}, "vch_type": {"type": "string"},
                "vch_no": {"type": "string"}, "bill_ref": {"type": "string"}, "debit": {"type": "string"},
                "credit": {"type": "string"}, "balance": {"type": "string"}}}},
        "notes": {"type": "string"},
    },
}


def _page_pngs(path: str) -> list[bytes]:
    import pypdfium2 as pdfium
    out = []
    doc = pdfium.PdfDocument(path)
    try:
        for i in range(min(len(doc), MAX_PAGES)):
            page = doc[i]
            w, h = page.get_size()
            scale = min(3.0, LONG_EDGE_PX / max(w, h))
            img = page.render(scale=scale).to_pil().convert("L")
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            out.append(buf.getvalue())
            del img
    finally:
        doc.close()
    return out


def _text_amounts(path: str) -> set[int]:
    """Every amount in the PDF's text, as the text reader decodes it (glyph recovery included). Empty for a scan."""
    from .pdf import PdfError, read_pdf_ex
    found: set[int] = set()
    try:
        rows, _, _ = read_pdf_ex(path)
    except PdfError:
        return found
    for r in rows:
        for cell in r:
            for tok in (cell or "").replace(" Dr", "Dr").replace(" Cr", "Cr").split():
                v = parse_amount(tok)
                if v is not None and v[0]:
                    found.add(abs(v[0]))
    return found


def _paise(s: str) -> Optional[int]:
    if not s or not s.strip():
        return None
    v = parse_amount(s.strip())
    return abs(v[0]) if v is not None else None


def read(path: str, ai, stats) -> tuple[Optional[Sheet], list[str], tuple[int, int, bool]]:
    """(sheet, notes, (amounts found in the PDF text, amounts returned, the PDF has text))."""
    pngs = _page_pngs(path)
    try:
        text_amounts = _text_amounts(path)
    except Exception:
        text_amounts = set()
    has_text = bool(text_amounts)
    rows: list[dict] = []
    notes: list[str] = []
    found = total = 0
    for start in range(0, len(pngs), PAGES_PER_CALL):
        batch = pngs[start:start + PAGES_PER_CALL]
        urls = [_publisher(png, "image/png") for png in batch]
        first, last = start + 1, start + len(batch)
        user = (f"Pages {first}-{last} of {len(pngs)} of a ledger (images attached in page order). Copy the rows. "
                f"A 'brought forward' total at the top of a page is a total row.")
        out = ai.json("vision-ledger", SYSTEM, user, SCHEMA, attachments=[{"url": u, "type": "image"} for u in urls])
        if not out or not isinstance(out.get("rows"), list):
            notes.append(f"Pages {first}-{last}: the vision model gave no rows.")
            continue
        for r in out["rows"]:
            for f in ("debit", "credit", "balance"):
                p = _paise(r.get(f, ""))
                if p is None or p == 0:
                    continue
                total += 1
                if not has_text or p in text_amounts:
                    found += 1
                else:
                    r.setdefault("_unfound", []).append(r.get(f))
            rows.append(r)
    if not rows:
        return None, notes, (0, 0, has_text)

    grid: list[list[Cell]] = [[type_cell(Cell(0, c, h)) for c, h in enumerate(HEADERS)]]
    for r in rows:
        kind = r.get("kind", "entry")
        label = {"opening": "Opening Balance", "total": "Total", "closing": "Closing Balance"}.get(kind)
        part = (r.get("particulars") or "").strip()
        if label and not part.lower().startswith(label.lower().split()[0]):
            part = f"{label} {part}".strip()
        vals = [r.get("date") if kind in ("entry", "opening") else "", part, r.get("vch_type"), r.get("vch_no"),
                r.get("bill_ref"), r.get("debit"), r.get("credit"), r.get("balance")]
        ri = len(grid)
        grid.append([type_cell(Cell(ri, c, (v.strip() if isinstance(v, str) and v.strip() else None)))
                     for c, v in enumerate(vals)])
    sheet = Sheet("Read from page images", grid, [])
    unfound = [a for r in rows for a in r.get("_unfound", [])]
    msg = f"Read from the page images by a vision model ({len(pngs)} page(s), {len(rows)} rows)."
    if has_text:
        msg += f" {found} of {total} amounts were found in the PDF's own text"
        msg += "." if not unfound else f"; not found: {', '.join(unfound[:8])}."
    else:
        msg += " The PDF has no text layer (a scan), so the amounts rest on the ledger's own totals only."
    sheet.notes.append(msg)
    return sheet, notes + [msg], (found, total, has_text)
