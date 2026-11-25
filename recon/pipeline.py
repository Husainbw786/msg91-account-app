"""Two stages, kept apart on purpose:

  extract()            AI reads each workbook into the standard ledger format (rule-based reading only when
                       EXTRACTION=rules is set, e.g. for local development without an API key).
  reconcile_ledgers()  pure maths on the extracted ledgers: matching, bridge, TDS. No AI here.

Used by both the command line and the web app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .ai import AIStats
from .bridge import Bridge, build_bridge
from .classify import classify
from .match import Result, reconcile
from .parser import Ledger, parse_file
from .profiles import ProfileStore
from .tds import TdsReport, analyse_tds


def extraction_mode() -> str:
    return "rules" if os.environ.get("EXTRACTION", "ai").lower() == "rules" else "ai"


@dataclass
class Outcome:
    W: Ledger
    C: Ledger
    res: Result
    bridge: Bridge
    tds: TdsReport
    ai: AIStats = field(default_factory=AIStats)


class ExtractionUnavailable(RuntimeError):
    pass


def extract(path, side: str, counterparty: str, store: Optional[ProfileStore] = None, ai=None,
            stats: Optional[AIStats] = None, sheet: Optional[str] = None, overrides=None) -> Ledger:
    stats = stats or AIStats()
    if extraction_mode() == "rules":
        led = parse_file(path, side, sheet or None, store=store, role_overrides=overrides or None)
        info = classify(led.entries, led.party_account or "")
        led.meta = {"invoice_side": info["invoice_side"], "extraction": "rules"}
        led.notes.insert(0, "Extracted with rules (EXTRACTION=rules); AI extraction is switched off.")
        return led
    if ai is None:
        raise ExtractionUnavailable("AI extraction is not configured on the server: set the viaSocket keys "
                                    "(VIASOCKET_ORG_ID, VIASOCKET_PROJECT_ID, VIASOCKET_EMBED_SECRET).")
    from .extract import extract_ledger
    label = "Walkover's books" if side == "W" else f"{counterparty}'s books"
    is_pdf = str(path).lower().endswith(".pdf")
    first_error = None
    try:
        led = extract_ledger(str(path), side, ai, stats, store, sheet, overrides, label)
        led.meta["extraction"] = "ai"
    except Exception as exc:
        if not is_pdf:
            raise
        led, first_error = None, exc
    # a PDF whose text reading is not proven (or unreadable, e.g. a scan): read the page images instead, and keep
    # that reading only if the ledger's own totals prove it better. Never used for a user's own column choice.
    from . import vision
    if is_pdf and not overrides and (led is None or led.confidence != "green") and vision.enabled(ai):
        alt = _vision_ledger(str(path), side, ai, stats)
        rank = {"green": 2, "amber": 1, "red": 0}
        if alt is not None and (led is None or rank[alt.confidence] > rank[led.confidence]):
            if led is not None:
                alt.notes.insert(1, f"The text reading of this PDF was {RANK_WORD[led.confidence]}; the page-image "
                                    f"reading is used instead.")
            led = alt
        elif led is not None:
            stats.note("vision: the page-image reading was not better than the text reading; kept the text reading")
    if led is None:
        raise first_error
    return led


RANK_WORD = {"green": "proven", "amber": "only partly proven", "red": "not proven"}


def _vision_ledger(path: str, side: str, ai, stats: AIStats) -> Optional[Ledger]:
    from . import vision
    from .extract import _direction_guard
    from .parser import _parse_sheet
    from .validate import Check, confidence
    try:
        sheet, notes, (found, total, has_text) = vision.read(path, ai, stats)
    except Exception as exc:                      # the vision reader is a second chance, never a new failure
        stats.note(f"vision: could not read the page images ({type(exc).__name__}: {str(exc)[:120]})")
        return None
    if sheet is None:
        for n in notes:
            stats.note("vision: " + n)
        return None
    led = _parse_sheet(sheet, side, path, None, None, None)
    if led is None:
        stats.note("vision: the rows read from the page images held no ledger")
        return None
    if has_text and total:
        # every amount must also be printed in the PDF's own text; one that is not keeps the ledger unproven
        led.checks.append(Check("amounts found in the PDF's text", found, total,
                                f"{found} of {total} amounts read from the images are in the PDF's text"))
    led.confidence = confidence(led.checks)
    if has_text and total and found < total and led.confidence == "green":
        led.confidence = "amber"
    info = classify(led.entries, led.party_account or "")
    _direction_guard(led.entries, info["invoice_side"], stats)
    led.meta = {"invoice_side": info["invoice_side"], "extraction": "vision"}
    led.notes = notes + led.notes
    return led


def reconcile_ledgers(W: Ledger, C: Ledger, counterparty: str, store: Optional[ProfileStore] = None,
                      stats: Optional[AIStats] = None) -> Outcome:
    """Pure maths: no AI is called in this function or anything it calls."""
    res = reconcile(W, C)
    br = build_bridge(res)
    tds = analyse_tds(res)
    if store is not None:
        store.put_counterparty(counterparty, orient=res.orient, tds_basis=res.c_amount_basis, tds_rate=tds.rate,
                               tds_divisor=tds.divisor, party_account=C.party_account,
                               invoice_side=(C.meta or {}).get("invoice_side"))
    return Outcome(W, C, res, br, tds, stats or AIStats())


def run(w_path, c_path, counterparty: str, store: Optional[ProfileStore] = None, ai=None,
        w_sheet=None, c_sheet=None, w_overrides=None, c_overrides=None) -> Outcome:
    stats = getattr(ai, "stats", None) or AIStats()
    W = extract(w_path, "W", counterparty, store, ai, stats, w_sheet, w_overrides)
    C = extract(c_path, "C", counterparty, store, ai, stats, c_sheet, c_overrides)
    return reconcile_ledgers(W, C, counterparty, store, stats)
