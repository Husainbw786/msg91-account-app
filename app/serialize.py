"""Engine objects -> JSON for the web app. Amounts go out in rupees (2 decimals)."""
from __future__ import annotations

from collections import Counter

from recon.bridge import Bridge, _c_bucket, _w_bucket
from recon.detect import ALL_ROLES
from recon.details import bridge_details, line_summary
from recon.match import Result, vw
from recon.parser import Ledger
from recon.rows import Entry
from recon.tds import TdsReport, fy_of

ROLE_LABELS = {
    "date": "Date", "doc_date": "Document date", "pay_date": "Payment date", "vtype": "Voucher type",
    "ref": "Reference / invoice no", "ref2": "Second reference", "narration": "Narration", "account": "Account",
    "party": "Party name", "debit": "Debit", "credit": "Credit", "amount": "Amount", "drcr": "Dr/Cr flag",
    "tds": "TDS", "balance": "Running balance", "voucher_total": "Voucher total", "base": "Base amount",
    "gst": "GST", "gross": "Gross", "net": "Net payable", "status": "Status",
}


def rs(p):
    return None if p is None else round(p / 100, 2)


def _d(d):
    return d.isoformat() if d else None


def entry_json(e: Entry, amount=None) -> dict:
    return {"date": _d(e.date), "ref": e.ref or e.ref2, "kind": e.kind, "narration": e.narration[:140],
            "debit": rs(e.dr), "credit": rs(e.cr), "tds": rs(e.tds) or None, "key": e.inv_key,
            "amount": rs(amount) if amount is not None else None, "rows": e.rows[:4], "flags": e.flags}


def ledger_preview(led: Ledger, sheets: list[str]) -> dict:
    tables = []
    for ti, t in enumerate(led.tables):
        role_of = {c: r for r, c in t.roles.items()}
        tables.append({
            "index": ti, "header_row": t.header_row + 1, "layout": t.layout,
            "known_format": bool(t.meta.get("known_format")), "user_mapping": bool(t.meta.get("user_mapping")),
            "columns": [{"col": c, "letter": _col_letter(c), "label": t.labels.get(c, ""),
                         "role": role_of.get(c, ""), "samples": t.meta.get("samples", {}).get(c, []),
                         "helper": c in (t.meta.get("helper_columns") or [])}
                        for c in sorted(t.labels)],
        })
    kinds = Counter(e.kind for e in led.entries)
    return {
        "sheet": led.sheet, "sheets": sheets, "confidence": led.confidence, "entries": len(led.entries),
        "first_date": _d(led.first_date), "last_date": _d(led.last_date),
        "opening": rs(led.opening), "closing": rs(led.closing), "closing_printed": rs(led.closing_printed),
        "party_account": led.party_account,
        "checks": [{"name": c.name, "ok": c.ok, "passed": c.passed, "total": c.total, "detail": c.detail,
                    "informational": c.informational}
                   for c in led.checks],
        "notes": led.notes[:40], "kinds": dict(kinds), "tables": tables,
        "sample_entries": [entry_json(e) for e in led.entries[:12]],
        "all_entries": [entry_json(e) for e in led.entries[:3000]],
        "flagged": [entry_json(e) for e in led.entries if e.flags][:100],
    }


def _col_letter(c: int) -> str:
    s = ""
    c += 1
    while c:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def result_json(res: Result, br: Bridge, tds: TdsReport, c_name: str = "Counterparty") -> dict:
    W, C, o = res.W, res.C, res.orient

    def vc(e):
        return o * (e.dr - e.cr)

    matches = []
    for m in res.matches:
        matches.append({
            "tier": m.tier, "key": m.key, "note": m.note,
            "w_refs": [e.ref or e.ref2 for e in m.w], "c_refs": [e.ref or e.ref2 or e.narration[:30] for e in m.c],
            "w_date": _d(min((e.date for e in m.w if e.date), default=None)),
            "c_date": _d(min((e.date for e in m.c if e.date), default=None)),
            "w_amount": rs(m.w_amount), "c_amount": rs(m.c_amount), "diff": rs(m.diff),
            "parts": [{"label": lab, "amount": rs(a)} for lab, a in m.parts],
        })
    matches.sort(key=lambda m: m["w_date"] or m["c_date"] or "")
    unmatched = []
    for e in res.unmatched_w:
        lab, cat = _w_bucket(e, C.last_date)
        unmatched.append({"side": "W", "bucket": lab, "category": cat, **entry_json(e, vw(e))})
    for e in res.unmatched_c:
        lab, cat = _c_bucket(e, W.last_date, W.first_date)
        unmatched.append({"side": "C", "bucket": lab, "category": cat, **entry_json(e, vc(e))})
    unmatched.sort(key=lambda u: u["date"] or "")
    return {
        "orient": o, "tds_basis": res.c_amount_basis, "notes": res.notes,
        "bridge": {
            "w_closing": rs(br.w_closing), "c_closing": rs(br.c_closing_w), "derived": rs(br.derived),
            "residual": rs(br.residual), "w_printed": rs(br.w_closing_printed), "c_printed": rs(br.c_closing_printed_w),
            "lines": [{"label": b.label, "amount": rs(b.amount), "count": b.count, "category": b.category,
                       "where": line_summary(det),
                       "details": [{**d, "amount": rs(d["amount"])} for d in det[:300]]}
                      for b, det in zip(br.lines, bridge_details(br, res, "Walkover", c_name))],
        },
        "tiers": dict(Counter(m.tier for m in res.matches)),
        "matches": matches, "unmatched": unmatched,
        "self_cancel": [[entry_json(e) for e in g] for g in res.self_cancel],
        "chains": [{"side": ch.side, "credit_note": ch.credit_note.ref, "cancels": [x.inv_key for x in ch.cancelled],
                    "rebill": [x.inv_key for x in ch.rebill], "net_nil": ch.net_nil} for ch in res.chains],
        "tds": {
            "rate": tds.rate_label,
            "years": [{"fy": r["fy"], "counterparty": rs(r["counterparty"]), "walkover": rs(r["walkover"]),
                       "diff": rs(r["diff"]), "moved": r["moved"]} for r in tds.fy_rows],
            "excess": [{"key": d.key, "amount": rs(d.excess), "source": d.source} for d in tds.deductions if d.excess],
            "other": [{"key": k, "amount": rs(a), "narration": n} for k, a, n in tds.other_deductions],
            "deductions": [{"key": d.key, "gross": rs(d.gross), "expected": rs(d.expected), "deducted": rs(d.deducted),
                            "excess": rs(d.excess), "posted": _d(d.posted), "fy": fy_of(d.assigned),
                            "moved": d.moved, "source": d.source} for d in tds.deductions],
            "notes": tds.notes,
        },
    }


ROLE_CHOICES = [{"value": r, "label": ROLE_LABELS.get(r, r)} for r in ALL_ROLES]
