"""Read text that a PDF draws in a font with no character map.

Some exporters (Tally prints are a common case) embed a bold font with no ToUnicode map and no usable cmap.
The PDF still *draws* the right letters, but text extraction yields "(cid:7)" or arbitrary symbols ("#$%&"),
so an opening balance or a column heading silently disappears.

This module reads those glyphs by their shapes. No AI, no OCR engine:
  1. find the suspect fonts: no ToUnicode map, or text that comes out as "(cid:N)";
  2. draw the pages that use them (pdfium) and cut out every glyph; every occurrence of the same glyph code
     in a font is the same drawing, so all its occurrences are averaged into one image;
  3. compare that image with reference glyphs whose letters are known: first the readable fonts of the same
     PDF (same design, often the regular weight of the same family), then two bundled open fonts (Bitstream
     Vera, regular and bold); the glyph's advance width must also fit the candidate;
  4. a suspect font whose extracted text already agrees with its shapes is left alone;
  5. look-alikes are settled by context later (pdf.py): O/0, I/l/1 inside a number are digits, inside a word
     letters; I or l from the case of the neighbouring letters.
Every cell built from such glyphs is flagged, and the ledger's own arithmetic (running balance, column totals,
opening + movements = closing) has to confirm the numbers before a ledger can show as proven.
"""
from __future__ import annotations

import collections
from pathlib import Path
from typing import Optional

FONT_DIR = Path(__file__).parent / "fonts"
CANDIDATES = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
              ".,-/:()&'+%@#*_")
W, H = 20, 30                      # comparison grid
EM_PX = 64                         # glyphs are drawn so that one em is about this many pixels
BAND_PT = 60                       # pages are drawn in horizontal strips of this many points (memory)
MAX_SAMPLES = 12                   # occurrences averaged per glyph / template
TILE_PT = 160                      # glyphs are drawn in tiles at most this wide (points), to bound memory
MIN_SCORE = 0.70                   # below this a glyph is "not sure"
AGREE_KEEP = 0.85                  # a suspect font whose text agrees this well with its shapes is kept as is


def _np():
    import numpy as np
    return np


def _feature(a):
    """Glyph box image -> normalised vector (ink positive, mean removed)."""
    np = _np()
    from PIL import Image, ImageFilter
    if a.size == 0 or a.shape[0] < 2 or a.shape[1] < 1:
        return None
    im = Image.fromarray(a).resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.5))
    v = 255.0 - np.asarray(im, dtype=np.float32)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n else None


def _shape(a):
    """The ink alone, cropped to its bounding box and centred in a fixed frame: compares letter shapes across
    fonts whose spacing differs (the bundled fonts)."""
    np = _np()
    from PIL import Image, ImageFilter
    if a is None or a.size == 0:
        return None
    ink = a < 128
    ys, xs = np.nonzero(ink)
    if len(ys) == 0:
        return None
    sub = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    h, w = sub.shape
    side = max(h, w)
    canvas = np.full((side, side), 255, dtype=np.uint8)
    canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = sub
    im = Image.fromarray(canvas).resize((24, 24), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))
    v = 255.0 - np.asarray(im, dtype=np.float32)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n else None


def _topology(a) -> tuple[int, int] | None:
    """(pieces, holes) of the ink: 'i' is (2, 0), 'l' (1, 0), 'o' (1, 1), '8' (1, 2), '%' (3, 2). Font-independent,
    so it separates look-alikes (S/8, c/o, i/l) that pixel similarity can confuse. Specks are ignored."""
    np = _np()
    from scipy import ndimage
    if a is None or a.size == 0 or a.shape[0] < 4 or a.shape[1] < 2:
        return None
    ink = a < 128
    total = int(ink.sum())
    if total == 0:
        return None
    lab, n = ndimage.label(ink, structure=np.ones((3, 3)))
    sizes = ndimage.sum(ink, lab, range(1, n + 1)) if n else []
    pieces = int(sum(1 for z in sizes if z >= 0.03 * total))
    bg, m = ndimage.label(~ink)                              # 4-connected background
    border = set(np.unique(np.concatenate([bg[0, :], bg[-1, :], bg[:, 0], bg[:, -1]]))) - {0}
    bsizes = ndimage.sum(~ink, bg, range(1, m + 1)) if m else []
    holes = int(sum(1 for i, z in enumerate(bsizes, start=1) if i not in border and z >= 0.01 * total))
    return pieces, holes


def _ink(a) -> float:
    """Ink share in the middle of the box (neighbours' edges bleed into the sides of a tight space)."""
    if a is None or a.size == 0:
        return 0.0
    h, w = a.shape
    mid = a[int(h * 0.15):max(int(h * 0.15) + 1, int(h * 0.85)), int(w * 0.25):max(int(w * 0.25) + 1, int(w * 0.75))]
    return float((mid < 128).mean()) if mid.size else 0.0


def _suspect_fonts(pdf) -> dict[str, str]:
    """fontname -> reason, for fonts whose extracted text cannot be trusted."""
    out: dict[str, str] = {}
    try:
        from pdfminer.pdftypes import resolve1
    except ImportError:                                   # pragma: no cover
        return out
    seen = set()
    for page in pdf.pages[:50]:
        try:
            fonts = resolve1(resolve1(page.page_obj.resources).get("Font")) or {}
        except Exception:
            continue
        for ref in fonts.values():
            try:
                f = resolve1(ref)
                base = f.get("BaseFont")
                name = base.name if hasattr(base, "name") else str(base or "").lstrip("/")
            except Exception:
                continue
            if not name or name in seen:
                continue
            seen.add(name)
            has_map = "ToUnicode" in f
            enc = f.get("Encoding")
            enc_name = enc.name if hasattr(enc, "name") else None
            embedded = True
            try:
                desc = f
                if "DescendantFonts" in f:
                    desc = resolve1(resolve1(f["DescendantFonts"])[0])
                fd = resolve1(desc.get("FontDescriptor") or {})
                embedded = any(k in fd for k in ("FontFile", "FontFile2", "FontFile3"))
            except Exception:
                pass
            if not has_map and embedded and enc_name not in ("WinAnsiEncoding", "MacRomanEncoding",
                                                             "StandardEncoding"):
                out[name] = "no character map"
    # any font whose text comes out as (cid:N) is suspect whatever its dictionary says
    for page in pdf.pages[:50]:
        for c in page.chars:
            if c["text"].startswith("(cid:"):
                out.setdefault(c["fontname"], "unmapped glyph codes")
        page.flush_cache()
    return out


def _hscale(c) -> float:
    """Horizontal scaling of the text (Tally prints condensed text at 70-80% width)."""
    m = c.get("matrix") or (1, 0, 0, 1)
    try:
        a, d = float(m[0]), float(m[3])
    except (TypeError, ValueError, IndexError):
        return 1.0
    return abs(a / d) if d else 1.0


def _crops(path: str, page_no: int, page_height: float, chars: list[dict], page_width: float = 0.0) -> list:
    """Image of each char's box. The page is drawn only in the strips that hold these chars, at a scale chosen
    from their font size (about EM_PX pixels per em), so small print is sharp and memory stays low."""
    np = _np()
    import pypdfium2 as pdfium
    out = [None] * len(chars)
    order = sorted(range(len(chars)), key=lambda i: chars[i]["top"])
    doc = pdfium.PdfDocument(path)
    try:
        page = doc[page_no]
        page_width = page_width or float(page.get_width())
        i = 0
        while i < len(order):
            band_top = chars[order[i]]["top"] - 2
            j = i
            while j < len(order) and chars[order[j]]["bottom"] <= band_top + BAND_PT:
                j += 1
            j = max(j, i + 1)
            band = sorted((order[k] for k in range(i, j)), key=lambda k: chars[k]["x0"])
            # split the strip into narrow tiles so a wide page is never drawn whole at high resolution
            tiles, cur = [], []
            for k in band:
                if cur and chars[k]["x1"] - chars[cur[0]]["x0"] > TILE_PT:
                    tiles.append(cur)
                    cur = []
                cur.append(k)
            if cur:
                tiles.append(cur)
            for group in tiles:
                top = max(0.0, min(chars[k]["top"] for k in group) - 2)
                bottom = max(chars[k]["bottom"] for k in group) + 2
                left = max(0.0, min(chars[k]["x0"] for k in group) - 2)
                right = max(chars[k]["x1"] for k in group) + 2
                size = float(np.median([chars[k]["size"] for k in group])) or 10.0
                scale = min(16.0, max(3.0, EM_PX / size))
                crop = (left, max(0.0, page_height - bottom), max(0.0, page_width - right), top)
                img = np.asarray(page.render(scale=scale, crop=crop, grayscale=True).to_pil().convert("L"))
                for k in group:
                    c = chars[k]
                    x0, x1 = int(round((c["x0"] - left) * scale)), int(round((c["x1"] - left) * scale))
                    t = int(round((c["top"] - top) * scale))
                    b = int(round((c["bottom"] - top) * scale))
                    a = img[max(0, t):max(0, b), max(0, x0):max(0, x1)].copy()
                    hs = _hscale(c)
                    if a.size and abs(hs - 1.0) > 0.03:      # undo condensing: compare at natural width
                        from PIL import Image
                        a = np.asarray(Image.fromarray(a).resize((max(1, int(round(a.shape[1] / hs))), a.shape[0]),
                                                                 Image.LANCZOS))
                    out[k] = a
                del img
            i = j
    finally:
        doc.close()
    return out


def _bundled_templates() -> list[tuple]:
    """(char, feature, advance/size) drawn from the bundled fonts, laid out like a PDF glyph box."""
    np = _np()
    from PIL import Image, ImageDraw, ImageFont
    out = []
    size = 60
    fonts = []
    for ff in ("VeraBd.ttf", "Vera.ttf"):
        if (FONT_DIR / ff).exists():
            fonts.append(ImageFont.truetype(str(FONT_DIR / ff), size))
    try:                                             # Pillow's built-in Aileron: Helvetica/Arial-like shapes
        aileron = ImageFont.load_default(size)
        if hasattr(aileron, "getbbox") and hasattr(aileron, "getlength"):
            fonts.append(aileron)
    except Exception:
        pass
    for font in fonts:
        base = int(round(size * 0.79))                   # pdfminer box: baseline at ~0.21 em above its bottom
        for ch in CANDIDATES:
            adv = font.getlength(ch)
            im = Image.new("L", (max(1, int(round(adv))), size), 255)
            ImageDraw.Draw(im).text((0, base), ch, font=font, fill=0, anchor="ls")
            arr = np.asarray(im)
            f = _feature(arr)
            if f is not None:
                out.append((ch, f, adv / size, _topology(arr), _shape(arr)))
    return out


class GlyphReader:
    """Decodes the suspect fonts of one PDF. Use .decode(fontname, text) -> (text, sure)."""

    def __init__(self, pdf, path: str):
        self.map: dict[tuple[str, str], tuple[str, bool]] = {}
        self.fonts_fixed: list[str] = []
        self.unsure: int = 0
        self.suspects = _suspect_fonts(pdf)
        if self.suspects:
            try:
                self._learn(pdf, path)
            except Exception as exc:                     # never break reading over this; the checks will show it
                self.error = f"{type(exc).__name__}: {exc}"

    def _learn(self, pdf, path: str) -> None:
        np = _np()
        samples = collections.defaultdict(list)          # (font, text) -> [(crop, width/size)]
        tmpl = collections.defaultdict(list)             # char -> [feature]
        tmpl_w = collections.defaultdict(list)
        tmpl_topo = collections.defaultdict(list)
        tmpl_shape = collections.defaultdict(list)
        used = 0
        for pno, page in enumerate(pdf.pages):       # one page in memory at a time (a wide page is ~60 MB)
            if used >= 40 or (used >= 3 and samples and all(len(v) >= MAX_SAMPLES for v in samples.values())):
                break
            if not any(c["fontname"] in self.suspects for c in page.chars):
                page.flush_cache()
                continue
            used += 1
            want, kinds = [], []
            pending = collections.Counter()
            for c in page.chars:
                key = (c["fontname"], c["text"])
                if not c.get("size"):
                    continue
                if c["fontname"] in self.suspects:
                    if len(samples[key]) + pending[("s", key)] < MAX_SAMPLES:
                        want.append(c)
                        kinds.append(("s", key))
                        pending[("s", key)] += 1
                elif len(c["text"]) == 1 and c["text"].strip() and \
                        len(tmpl[c["text"]]) + pending[("t", c["text"])] < MAX_SAMPLES:
                    want.append(c)
                    kinds.append(("t", c["text"]))
                    pending[("t", c["text"])] += 1
            if not want:
                page.flush_cache()
                continue
            crops = _crops(path, pno, float(page.height), want, float(page.width))
            page.flush_cache()
            for c, (kind, key), a in zip(want, kinds, crops):
                w = (c["x1"] - c["x0"]) / (c["size"] * _hscale(c))
                if kind == "s":
                    samples[key].append((a, w))
                else:
                    f = _feature(a)
                    if f is not None:
                        tmpl[key].append(f)
                        tmpl_w[key].append(w)
                        tmpl_topo[key].append(_topology(a))
                        sh = _shape(a)
                        if sh is not None:
                            tmpl_shape[key].append(sh)
        def vote(ts):
            ts = [t for t in ts if t is not None]
            return collections.Counter(ts).most_common(1)[0][0] if ts else None
        def mean_or_none(v):
            return np.mean(v, 0) if v else None
        refs = [(ch, np.mean(fs, 0), float(np.mean(tmpl_w[ch])), 0.0, vote(tmpl_topo[ch]), mean_or_none(tmpl_shape[ch]),
                 True) for ch, fs in tmpl.items()]
        refs += [(ch, f, w, 0.03, topo, sh, False) for ch, f, w, topo, sh in _bundled_templates()]   # small handicap

        decoded: dict[tuple[str, str], tuple[str, float, bool]] = {}
        for key, items in samples.items():
            crops = [a for a, _ in items]
            width = float(np.mean([w for _, w in items]))
            if np.mean([_ink(a) for a in crops]) < 0.01:
                decoded[key] = (" ", 1.0, True)
                continue
            feats = [f for f in (_feature(a) for a in crops) if f is not None]
            if not feats:
                decoded[key] = (key[1], 0.0, False)
                continue
            f = np.mean(feats, 0)
            f = f / (np.linalg.norm(f) or 1.0)
            topo = vote([_topology(a) for a in crops])
            shapes = [x for x in (_shape(a) for a in crops) if x is not None]
            shp = np.mean(shapes, 0) if shapes else None
            if shp is not None:
                shp = shp / (np.linalg.norm(shp) or 1.0)
            best: list[tuple[float, str]] = []
            for ch, t, tw, handicap, ttopo, tshape, same_doc in refs:
                if topo is not None and ttopo is not None and ttopo != topo:
                    continue                                 # wrong number of pieces or holes: cannot be it
                box = float((f * t).sum() / (np.linalg.norm(t) or 1.0))
                if shp is not None and tshape is not None:
                    ink_shape = float((shp * tshape).sum() / (np.linalg.norm(tshape) or 1.0))
                    # same document: spacing is comparable, so the box counts; other fonts: the shape counts
                    score = (0.5 * box + 0.5 * ink_shape) if same_doc else (0.3 * box + 0.7 * ink_shape)
                else:
                    score = box
                score -= handicap
                r = abs(width - tw) / max(tw, 1e-6)
                score -= max(0.0, r - 0.25) * 1.5            # bold letters run ~25% wider than regular ones
                best.append((score, ch))
            if not best:
                decoded[key] = (key[1], 0.0, False)
                continue
            best.sort(reverse=True)
            top, ch = best[0]
            decoded[key] = (ch, top, top >= MIN_SCORE)

        # a suspect font whose own text already agrees with its shapes keeps its text
        by_font = collections.defaultdict(list)
        for (font, text), (ch, _, _) in decoded.items():
            by_font[font].append((text, ch, len(samples[(font, text)])))
        for font, rows in by_font.items():
            total = sum(n for _, _, n in rows)
            agree = sum(n for text, ch, n in rows if text == ch or (text.lower() == ch.lower() and ch in "IlO0o"))
            if total and agree / total >= AGREE_KEEP and not any(t.startswith("(cid:") for t, _, _ in rows):
                continue
            only_cid = self.suspects.get(font) == "unmapped glyph codes"
            if only_cid:
                # the font has a working map; only a few symbols (a rupee sign, say) lack one: decode just those
                rows = [r for r in rows if r[0].startswith("(cid:")]
                if not rows:
                    continue
            self.fonts_fixed.append(font)
            for text, ch, _ in rows:
                d = decoded[(font, text)]
                self.map[(font, text)] = (d[0], d[2])
                if not d[2]:
                    self.unsure += 1

    def active(self) -> bool:
        return bool(self.map)

    def decode(self, fontname: str, text: str) -> Optional[tuple[str, bool]]:
        return self.map.get((fontname, text))

    def note(self) -> Optional[str]:
        if not self.fonts_fixed:
            return None
        names = ", ".join(sorted({f.split("+")[-1] for f in self.fonts_fixed}))
        msg = (f"Some text in this PDF uses a font with no character map ({names}); it was read from the glyph "
               f"shapes. Cells read this way are marked, and the file's own totals and balances must confirm them.")
        if self.unsure:
            msg += f" {self.unsure} glyph(s) could not be read with certainty."
        return msg


# ----------------------------------------------------------------------------- context clean-up for a cell
NUMERIC_NB = set("0123456789,.")


def fix_cell(text: str) -> str:
    """Settle look-alikes inside one cell read from glyph shapes, from each character's own neighbours:
    O between digits is 0, 0 between letters is O, l/I between digits is 1, 1 between letters is I or l (by the
    neighbours' case); a word-initial 'l' before a consonant is 'I' ("lnvoice" -> "Invoice")."""
    out = []
    for tok in text.split(" "):
        chars = list(tok)
        n = len(chars)
        for i, ch in enumerate(chars):
            left = chars[i - 1] if i > 0 else ""
            right = chars[i + 1] if i + 1 < n else ""
            nbs = [c for c in (left, right) if c]
            if not nbs:
                continue
            if len(nbs) == 1:                        # token edge: the whole rest of the token must agree
                rest = chars[:i] + chars[i + 1:]
                if n < 3:
                    continue
                numeric = all(c in NUMERIC_NB for c in rest) and any(c.isdigit() for c in rest)
                alpha = all(c.isalpha() for c in rest)
            else:
                numeric = all(c in NUMERIC_NB for c in nbs) and any(c.isdigit() for c in nbs)
                alpha = all(c.isalpha() for c in nbs)
            if ch in "OoDQ" and numeric:
                chars[i] = "0"
            elif ch in "lI|" and numeric:
                chars[i] = "1"
            elif ch == "0" and alpha:
                chars[i] = "O" if all(c.isupper() for c in nbs) else "o"
            elif ch == "1" and alpha:
                chars[i] = "I" if all(c.isupper() for c in nbs) else "l"
        for i, ch in enumerate(chars):
            if ch in "Il":
                nb = [c for c in chars[max(0, i - 2):i] + chars[i + 1:i + 3] if c.isalpha() and c not in "Il"]
                if nb:
                    chars[i] = "I" if all(c.isupper() for c in nb) else "l"
        if n > 1 and chars[0] == "l" and chars[1].isalpha() and chars[1].lower() not in "aeiouyl":
            chars[0] = "I"
        out.append("".join(chars))
    return " ".join(out)
