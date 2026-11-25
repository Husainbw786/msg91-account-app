"""PDF ledgers -> one grid of text cells, so the rest of the engine reads a PDF like a CSV.

Works on PDFs that carry text (exported from Tally, SAP, Busy, bank portals, vendor statements), ruled or not:
  1. words, with their positions, page by page;
  2. words on the same baseline form a line; words close together on a line form a phrase ("Being payment");
  3. columns are the x-ranges that phrases of table-like lines (3+ phrases) occupy, cut at the vertical gaps
     that run down the whole table; the ruling lines are not needed;
  4. each phrase goes to the column it overlaps most;
  5. clean-up: page numbers and "continued" lines go; page headers and column titles repeated on later pages
     go; a text-only line under an entry (wrapped narration) is joined onto that entry.

Numbers stay as printed text ("1,23,456.00 Dr"); the grid's cell typing reads them exactly as it does a CSV.
A scanned PDF has no text to read: it raises PdfError with a plain message instead of guessing.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

MAX_PAGES = int(__import__("os").environ.get("PDF_MAX_PAGES", "400"))
_PAGE_RE = re.compile(r"^\s*(page\s*\d+(\s*(of|/)\s*\d+)?|\d+\s*(of|/)\s*\d+|continued\.*|contd\.*|\(?continued\)?.*)\s*$",
                      re.I)
_NUM_RE = re.compile(r"\d")


class PdfError(ValueError):
    pass


def _lines(words: list[dict]) -> list[list[dict]]:
    """Words -> lines (same baseline), each sorted left to right."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        tol = max(2.0, 0.45 * float(w.get("size") or 8))
        if out and abs(out[-1][0]["top"] - w["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _phrases(line: list[dict]) -> list[dict]:
    """Join words separated by about one space; a wider gap starts a new phrase."""
    out: list[dict] = []
    for w in line:
        gap_max = max(2.5, 0.4 * float(w.get("size") or 8))
        word = {"text": w["text"], "x0": w["x0"], "x1": w["x1"], "rec": w.get("recovered", 0)}
        if out and w["x0"] - out[-1]["x1"] <= gap_max:
            out[-1]["text"] += " " + w["text"]
            out[-1]["x1"] = max(out[-1]["x1"], w["x1"])
            out[-1]["rec"] = max(out[-1]["rec"], word["rec"])
            out[-1]["words"].append(word)
        else:
            out.append({**word, "words": [word]})
    return out


def _columns(lines: list[list[dict]], width: float) -> list[tuple[float, float]]:
    """X-ranges of the columns, from the phrases of table-like lines."""
    table = [l for l in lines if len(l) >= 3] or lines
    n = int(width) + 2
    cover = [0] * n
    for l in table:
        seen = set()
        for p in l:
            for x in range(max(0, int(p["x0"])), min(n, int(p["x1"]) + 1)):
                seen.add(x)
        for x in seen:
            cover[x] += 1
    noise = max(0, int(0.03 * len(table)))       # a few stray long phrases may cross a gap
    cols, start = [], None
    for x in range(n):
        if cover[x] > noise and start is None:
            start = x
        elif cover[x] <= noise and start is not None:
            cols.append((start, x - 1))
            start = None
    if start is not None:
        cols.append((start, n - 1))
    return cols or [(0, n)]


def _col_of(p: dict, cols: list[tuple[float, float]]) -> int:
    best, best_ov = 0, -1e9
    for i, (a, b) in enumerate(cols):
        ov = min(b, p["x1"]) - max(a, p["x0"])
        if ov < 0:                                   # no overlap: distance counts against
            ov = -min(abs(p["x0"] - b), abs(a - p["x1"]))
        if ov > best_ov:
            best, best_ov = i, ov
    return best


def _assign(phrases: list[dict], cols: list[tuple[float, float]], rec: set | None = None) -> list[str | None]:
    """Each phrase to the column it overlaps most. A phrase that crosses a column gap (two header labels
    printed close together) is split, and each word goes to its own column."""
    pieces = []
    for p in phrases:
        hit = {i for i, (a, b) in enumerate(cols) if min(b, p["x1"]) - max(a, p["x0"]) > 0}
        if len(hit) > 1 and len(p.get("words", [])) > 1:
            pieces += p["words"]
        else:
            pieces.append(p)
    row: list[str | None] = [None] * len(cols)
    for p in pieces:
        i = _col_of(p, cols)
        row[i] = p["text"] if row[i] is None else row[i] + " " + p["text"]
        if rec is not None and p.get("rec"):
            rec.add(i)
    return row



def _is_texty(s: str) -> bool:
    return not _NUM_RE.search(s or "")


def read_pdf(path: str | Path) -> list[list[str | None]]:
    """The PDF as rows of cell texts (None = empty)."""
    return read_pdf_ex(path)[0]


def read_pdf_ex(path: str | Path):
    """(rows, notes, flagged): rows of cell texts; notes for the review; flagged = {(row, col)} of cells read
    from glyph shapes (a font with no character map). Uses the gap-method grid unless only the alignment
    method finds dated entries; grid._load_pdf chooses between them by the ledger checks instead."""
    cands, notes = _read(path, all_candidates=True)
    best = max(cands, key=lambda c: (_grid_score(c[0]) > _grid_score(cands[0][0]) + 5, c is cands[0]))
    if best[2] == "aligned":
        notes = notes + [ALIGNED_NOTE]
    return best[0], notes, best[1]


def _read(path: str | Path, all_candidates: bool = False):
    try:
        import pdfplumber
    except ImportError as exc:                       # pragma: no cover
        raise PdfError("PDF support is not installed on this server (pdfplumber).") from exc
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        if "password" in msg.lower() or "encrypt" in msg.lower() or exc.__class__.__name__ == "PDFPasswordIncorrect":
            raise PdfError("This PDF is password-protected. Remove the password (or export it again) and upload it.")
        raise PdfError(f"This PDF could not be opened: {msg}")
    pages: list[tuple[list[list[dict]], float]] = []
    width = 0.0
    notes: list[str] = []
    with pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise PdfError(f"This PDF has {len(pdf.pages)} pages; the limit is {MAX_PAGES}. Split it by period.")
        from .glyphs import GlyphReader
        reader = GlyphReader(pdf, str(path))
        for page in pdf.pages:
            page.flush_cache()
        if reader.note():
            notes.append(reader.note())
        for page in pdf.pages:
            for c in page.chars:                     # glyphs from a font with no character map: read by shape
                d = reader.decode(c["fontname"], c["text"]) if reader.active() else None
                if d is not None:                    # one flag value, so words are not split at unsure glyphs
                    c["text"], c["recovered"] = d[0], 1
                else:
                    c["recovered"] = 0
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False,
                                       use_text_flow=False, extra_attrs=["size", "recovered"])
            width = max(width, float(page.width))
            pages.append(([_phrases(l) for l in _lines(words)], float(page.width)))
            page.flush_cache()
    if not any(lines for lines, _ in pages):
        raise PdfError("This PDF has no readable text: it looks like a scanned image. Upload the ledger as Excel, "
                       "CSV or a PDF exported from the accounting software (not a scan).")

    return _candidates(pages, width), notes


ALIGNED_NOTE = "Columns were found from how the text is aligned (dense report layout)."


def read_pdf_candidates(path: str | Path):
    """(candidates, notes): the PDF read with both column methods, each as (rows, flagged, label). The caller
    keeps the reading the ledger's own totals prove best (grid._load_pdf)."""
    return _read(path, all_candidates=True)


def _finish(rows, recs, ncols):
    from .glyphs import fix_cell
    for r, rec in zip(rows, recs):
        for i in rec:
            if r[i]:
                r[i] = fix_cell(r[i])
    keep = [i for i in range(ncols) if any(r[i] for r in rows)]
    pos = {c: k for k, c in enumerate(keep)}
    flagged = {(ri, pos[i]) for ri, rec in enumerate(recs) for i in rec if i in pos}
    return [[r[i] for i in keep] for r in rows], flagged


def _candidates(pages, width) -> list:
    """Grids from the two column methods, the gap method first. Order of preference when nothing else decides:
    gaps (clean tables), then alignment (dense Tally detail prints)."""
    out = []
    cov_cols = _columns([l for lines, _ in pages for l in lines], width)
    rows, recs, n = _build(pages, len(cov_cols), lambda phrases, rec: _assign(phrases, cov_cols, rec))
    out.append((*_finish(rows, recs, n), "gaps"))
    try:
        typed_pages = [([_split_typed(l) for l in lines], w) for lines, w in pages]
        acols = _align_columns([l for lines, _ in typed_pages for l in lines])
        if acols:
            rows, recs, n = _build(typed_pages, len(acols), lambda phrases, rec: _assign_aligned(phrases, acols, rec))
            out.append((*_finish(rows, recs, n), "aligned"))
    except Exception:                                # the gap reading stands
        pass
    return out


def _build(pages, ncols: int, assign):
    """Rows of cell texts from lines of phrases, given a phrase -> column assignment."""
    rows: list[list[str | None]] = []
    recs: list[set] = []                             # per row: columns read from glyph shapes
    seen_texty: set[str] = set()                     # header/title lines already kept
    for pno, (lines, _) in enumerate(pages):
        for li, phrases in enumerate(lines):
            text = " ".join(p["text"] for p in phrases).strip()
            if not text or _PAGE_RE.match(text):
                continue
            key = re.sub(r"\s+", " ", text.lower())
            texty = _is_texty(text)
            # a page header or the column titles again on a later page
            if pno > 0 and key in seen_texty:
                continue
            rec: set = set()
            row = assign(phrases, rec)
            # wrapped narration: a text-only line with few phrases right under an entry that has numbers
            if (texty and rows and len(phrases) <= max(1, ncols // 3) and li > 0
                    and any(c and not _is_texty(c) for c in rows[-1])):
                prev = rows[-1]
                fits = all(prev[i] is not None and _is_texty(prev[i]) for i, v in enumerate(row) if v is not None)
                if fits:
                    for i, v in enumerate(row):
                        if v is not None:
                            prev[i] = prev[i] + " " + v
                    recs[-1] |= rec
                    continue
            if texty:
                seen_texty.add(key)
            rows.append(row)
            recs.append(rec)
    return rows, recs, ncols


# ----------------------------------------------------------------------------- columns from alignment
_DATE_TOK = re.compile(r"^\d{1,2}[-/.](\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
_AMT_TOK = re.compile(r"^\(?-?(₹|rs\.?)?\d{1,3}(,\d{2,3})+(\.\d{1,2})?\)?$|^\(?-?(₹|rs\.?)?\d+\.\d{2}\)?$", re.I)
_SUFFIX = {"dr", "cr", "dr.", "cr."}
_PREFIX = {"to", "by", "dr", "cr"}


def _piece(words: list[dict], kind: str, anchor_word: dict) -> dict:
    return {"text": " ".join(w["text"] for w in words), "x0": words[0]["x0"], "x1": words[-1]["x1"],
            "rec": max(w.get("rec", 0) for w in words), "words": words, "kind": kind,
            "ax": anchor_word["x1"] if kind == "amt" else words[0]["x0"]}


def _split_typed(line: list[dict]) -> list[dict]:
    """Phrases split so that a date and an amount are always cells of their own ('4982 1,86,204.00' is two
    cells; an amount keeps a trailing Dr/Cr), and a To/By/Dr/Cr marker after a date is its own cell."""
    out: list[dict] = []
    for ph in line:
        words = ph.get("words") or [ph]
        cur: list[dict] = []
        for w in words:
            t = w["text"]
            if _DATE_TOK.match(t) or _AMT_TOK.match(t):
                if cur:
                    out.append(_piece(cur, "text", cur[0]))
                    cur = []
                out.append(_piece([w], "date" if _DATE_TOK.match(t) else "amt", w))
            elif t.lower() in _SUFFIX and not cur and out and out[-1]["kind"] == "amt" and len(out[-1]["words"]) == 1:
                last = out.pop()
                out.append(_piece(last["words"] + [w], "amt", last["words"][0]))
            else:
                cur.append(w)
        if cur:
            out.append(_piece(cur, "text", cur[0]))
    # a To/By/Dr/Cr marker standing alone after a date belongs to the particulars that follow it
    merged: list[dict] = []
    i = 0
    while i < len(out):
        p = out[i]
        if (p["kind"] == "text" and p["text"].lower() in _PREFIX and i + 1 < len(out) and out[i + 1]["kind"] == "text"
                and merged and merged[-1]["kind"] == "date"):
            nxt = out[i + 1]
            merged.append(_piece(p["words"] + nxt["words"], "text", nxt["words"][0]))
            i += 2
            continue
        merged.append(p)
        i += 1
    return merged


def _align_columns(lines: list[list[dict]]) -> list[dict]:
    """Columns as alignment lines: text and dates start at the same x, amounts end at the same x. Built from the
    dated lines (the entries) first; what else lines up becomes extra columns (bill-wise detail lines)."""
    import statistics
    sizes = [w.get("size", 0) for l in lines for p in l for w in p["words"] if w.get("size")]
    size = statistics.median(sizes) if sizes else 9.0
    tol = max(2.0, 0.3 * size)
    dated = [l for l in lines if any(p["kind"] == "date" for p in l)]
    if len(dated) < 5:
        return []

    def clusters(items, minsup):
        items = sorted(items, key=lambda t: t[0])
        out: list[dict] = []
        for x, lid, ph in items:
            if out and x - out[-1]["xs"][-1] <= tol:
                out[-1]["xs"].append(x)
                out[-1]["lines"].add(lid)
                out[-1]["ph"].append(ph)
            else:
                out.append({"xs": [x], "lines": {lid}, "ph": [ph]})
        return [c for c in out if len(c["lines"]) >= minsup]

    def make(c, side, primary):
        return {"side": side, "x": statistics.median(c["xs"]), "primary": primary,
                "x0": min(p["x0"] for p in c["ph"]), "x1": max(p["x1"] for p in c["ph"]),
                "order": statistics.median(p["x0"] for p in c["ph"])}

    minsup = max(2, int(0.05 * len(dated)))
    cols = [make(c, "L", True) for c in clusters([(p["ax"], i, p) for i, l in enumerate(dated) for p in l
                                                   if p["kind"] != "amt"], minsup)]
    cols += [make(c, "R", True) for c in clusters([(p["ax"], i, p) for i, l in enumerate(dated) for p in l
                                                    if p["kind"] == "amt"], minsup)]
    # everything else that lines up: amounts and text of the undated lines that fit no column above
    extra_l, extra_r = [], []
    for i, l in enumerate(lines):
        for p in l:
            if _nearest(p, cols, tol) is None:
                (extra_r if p["kind"] == "amt" else extra_l).append((p["ax"], i, p))
    cols += [make(c, "R", False) for c in clusters(extra_r, 2)]
    cols += [make(c, "L", False) for c in clusters(extra_l, 2)]
    cols.sort(key=lambda c: c["order"])
    for c in cols:
        c["tol"] = tol
        c["xs"] = [c["x"]]
    return _merge_ragged(cols, lines)


def _merge_ragged(cols: list[dict], lines: list[list[dict]]) -> list[dict]:
    """Two text columns that overlap on the page but are never filled on the same line are one column whose
    text is not aligned on one edge (Tally's Vch No.: 'PB/25-26/7' and '4982'). Amount columns never merge:
    a different right edge is a different column (main amounts vs bill-wise detail)."""
    while True:
        occ = [set() for _ in cols]
        for li, l in enumerate(lines):
            for p in l:
                k = _nearest(p, cols, cols[0]["tol"] if cols else 3.0)
                if k is not None:
                    occ[k].add(li)
        best = None
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                if a["side"] != "L" or b["side"] != "L" or not occ[i] or not occ[j]:
                    continue
                overlap = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
                if overlap <= 0.3 * min(a["x1"] - a["x0"], b["x1"] - b["x0"]):
                    continue
                both = len(occ[i] & occ[j])
                if both <= 0.01 * min(len(occ[i]), len(occ[j])):
                    if best is None or overlap > best[0]:
                        best = (overlap, i, j)
        if best is None:
            return cols
        _, i, j = best
        a, b = cols[i], cols[j]
        a.update(x0=min(a["x0"], b["x0"]), x1=max(a["x1"], b["x1"]), xs=a["xs"] + b["xs"],
                 primary=a["primary"] or b["primary"], order=min(a["order"], b["order"]))
        del cols[j]


def _nearest(p: dict, cols: list[dict], tol: float) -> Optional[int]:
    side = "R" if p["kind"] == "amt" else "L"
    best, bd = None, None
    for i, c in enumerate(cols):
        if c["side"] != side:
            continue
        d = min(abs(p["ax"] - x) for x in c.get("xs", [c["x"]]))
        if d <= 2 * tol and (bd is None or d < bd):
            best, bd = i, d
    if best is None and p["kind"] == "text":
        # text (headers, particulars) that is not left-aligned with a column: the column it overlaps most
        ov_best = 0.0
        for i, c in enumerate(cols):
            ov = min(c["x1"], p["x1"]) - max(c["x0"], p["x0"])
            ov *= 1.5 if c["primary"] else 1.0
            if ov > ov_best:
                best, ov_best = i, ov
    return best


def _assign_aligned(phrases: list[dict], cols: list[dict], rec: set | None = None) -> list[str | None]:
    row: list[str | None] = [None] * len(cols)
    for p in phrases:
        i = _nearest(p, cols, cols[0]["tol"] if cols else 3.0)
        if i is None:                                # nothing lines up: the closest column by position
            i = min(range(len(cols)), key=lambda k: abs(cols[k]["order"] - p["x0"]))
        row[i] = p["text"] if row[i] is None else row[i] + " " + p["text"]
        if rec is not None and p.get("rec"):
            rec.add(i)
    return row


def _grid_score(rows: list[list[str | None]]) -> float:
    """How cleanly a grid holds ledger entries: rows with a cell that is just a date and a cell that is just an
    amount, minus cells that mix a date or an amount with other words."""
    good, mixed = 0, 0
    for r in rows:
        cells = [c.strip() for c in r if c and c.strip()]
        has_date = any(_DATE_TOK.match(c) for c in cells)
        has_amt = any(_AMT_TOK.match(c.split(" ")[0]) and len(c.split(" ")) <= 2 for c in cells)
        if has_date and has_amt:
            good += 1
        for c in cells:
            toks = c.split(" ")
            if len(toks) > 1 and any(_DATE_TOK.match(t) for t in toks):
                mixed += 1
            if sum(1 for t in toks if _AMT_TOK.match(t)) > 1:
                mixed += 1
    return good - 0.5 * mixed
