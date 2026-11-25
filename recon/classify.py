"""Stage 3: what is each entry? Voting between independent signals, plus learned document prefixes.

Kinds: invoice, payment, tds, credit_note, debit_note, journal, refund, opening, unknown.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .rows import Entry

INVOICE, PAYMENT, TDS, CN, DN, JOURNAL, REFUND, OPENING, UNKNOWN = (
    "invoice", "payment", "tds", "credit_note", "debit_note", "journal", "refund", "opening", "unknown")

VTYPE_MAP = [
    (re.compile(r"credit\s*(note|memo)|^cn$", re.I), CN),
    (re.compile(r"debit\s*(note|memo)|^dn$", re.I), DN),
    (re.compile(r"sales|invoice|purchase|^bill|standard|tax inv", re.I), INVOICE),
    (re.compile(r"receipt|payment|bank|contra", re.I), PAYMENT),
    (re.compile(r"journal|^jv$|adjust", re.I), JOURNAL),
]
KEYWORDS = [
    (re.compile(r"\btds\b|194[a-z]?|26as|-tds-|tds receivable|withholding", re.I), TDS, 4),
    (re.compile(r"^\s*(cr|cn|crn)[-/]\d", re.I), CN, 3),
    (re.compile(r"\brfnd_|refund", re.I), REFUND, 3),
    (re.compile(r"opening balance|\bb/f\b", re.I), OPENING, 3),
    (re.compile(r"credit note|\bcn\b|reversal|revision", re.I), CN, 1),
    (re.compile(r"debit note|\bdn\b|penalty|short supply", re.I), DN, 2),
    (re.compile(r"\b(neft|rtgs|imps|utr|chq|cheque|upi)\b|kkbk|hdfc|icici|sbi\b|axis|kotak|bank|e-receipt|razorpay",
                re.I), PAYMENT, 2),
    (re.compile(r"provision|round\s*off|igst input|input reversal|finance cost", re.I), JOURNAL, 2),
]
ACCOUNT_MAP = [
    (re.compile(r"tds", re.I), TDS),
    (re.compile(r"bank|cash", re.I), PAYMENT),
    (re.compile(r"provision|igst|cgst|sgst|round", re.I), JOURNAL),
]

# Walkover / most Indian invoice numbers: YYMMDD-N or YYYYMMDD-N
DATE_KEY_RE = re.compile(r"(?<![\dA-Za-z])(?:20)?(\d{2})(\d{2})(\d{2})-(\d{1,5})(?!\d)")
UTR_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}[A-Z0-9]{8,26})(?![A-Z0-9])")
GATEWAY_RE = re.compile(r"\b(pay_[A-Za-z0-9]{10,}|rfnd_[A-Za-z0-9]{10,}|order_[A-Za-z0-9]{10,})")


def norm_key(s: str) -> str:
    """Canonical document key: YYMMDD-N for date-coded numbers, else alnum upper-case without leading zeros."""
    if not s:
        return ""
    m = DATE_KEY_RE.search(s)
    if m and not re.match(r"^\s*(RCPT|PAY|JV|CR|DN|CN)[-/]", s, re.I):
        yy, mm, dd, n = m.groups()
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            return f"{yy}{mm}{dd}-{int(n)}"
    parts = re.findall(r"[A-Za-z]+|\d+", s.upper())
    return "-".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


BILL_REF_RE = re.compile(r"\b(New|Agst|Advance)\s+Ref\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9/_.\-]*)", re.I)


def invoice_key_in(s: str) -> str:
    """An invoice key embedded anywhere in a string (e.g. '20210715-47-TDS-CM-440572' -> '210715-47')."""
    if not s:
        return ""
    for m in DATE_KEY_RE.finditer(s):
        start = m.start()
        prefix = s[max(0, start - 5):start].upper()
        if re.search(r"(RCPT|PAY|JV|CR|DN|CN)[-/]$", prefix):
            continue
        yy, mm, dd, n = m.groups()
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            return f"{yy}{mm}{dd}-{int(n)}"
    return ""


def utrs_in(*texts: str) -> list[str]:
    out = []
    for t in texts:
        if not t:
            continue
        up = t.upper()
        for m in UTR_RE.finditer(up):
            tok = m.group(1)
            if sum(ch.isdigit() for ch in tok) >= 8:
                out.append(tok)
        out += [g.lower() for g in GATEWAY_RE.findall(t)]
    return list(dict.fromkeys(out))


def prefix_of(ref: str) -> str:
    m = re.match(r"^\s*([A-Za-z]{2,6})[\-/]", ref or "")
    return m.group(1).upper() if m else ""


PAYABLE_RE = re.compile(r"creditor|payable|vendor|supplier", re.I)
RECEIVABLE_RE = re.compile(r"debtor|receivable|customer", re.I)


def classify(entries: list[Entry], account_hint: str = "", known_prefixes: dict | None = None) -> dict:
    """Tag every entry; returns a summary (invoice side, learned prefixes).
    `account_hint` is the party account name of a GL dump (e.g. 'CREDITORS CONTROL A/C')."""
    votes: dict[int, Counter] = {}
    why: dict[int, list[str]] = defaultdict(list)
    for e in entries:
        v: Counter = Counter()
        if e.kind == OPENING:
            v[OPENING] += 10
        for rx, k in VTYPE_MAP:
            if e.vtype and rx.search(e.vtype):
                # a document type (sales, purchase, receipt, payment, CN/DN) outweighs narration keywords: a
                # purchase whose breakdown mentions "TDS 194J" is still a purchase. A journal stays weak.
                v[k] += 3 if k == JOURNAL else 5
                why[e.idx].append(f"type '{e.vtype}'")
                break
        text = " ".join([e.narration, e.ref, e.ref2, e.account])
        for rx, k, w in KEYWORDS:
            if rx.search(text):
                v[k] += w
                why[e.idx].append(f"keyword ({k})")
        for rx, k in ACCOUNT_MAP:
            if e.account and rx.search(e.account):
                v[k] += 2
                why[e.idx].append(f"account '{e.account[:25]}'")
                break
        if invoice_key_in(e.ref) and not prefix_of(e.ref):
            v[INVOICE] += 1
        if e.status.lower() in ("o/s", "os", "unpaid", "pending"):
            v[INVOICE] += 1
        p = prefix_of(e.ref2) or prefix_of(e.ref)
        if known_prefixes and p in known_prefixes:
            v[known_prefixes[p]] += 2
            why[e.idx].append(f"prefix {p} known for this counterparty")
        votes[e.idx] = v

    # invoice side: debit or credit, from entries that already look like invoices
    side_votes = Counter()
    for e in entries:
        if votes[e.idx][INVOICE] >= 3 or (votes[e.idx][INVOICE] and not votes[e.idx].most_common(1)[0][0] != INVOICE):
            side_votes["dr" if e.dr >= e.cr else "cr"] += 1
    if not side_votes:
        # registers / sheets without types: the side carrying invoice-key refs
        for e in entries:
            if invoice_key_in(e.ref):
                side_votes["dr" if e.dr >= e.cr else "cr"] += 1
    inv_side = side_votes.most_common(1)[0][0] if side_votes else "dr"
    top = side_votes.most_common(2)
    if account_hint and (len(top) < 2 or top[0][1] <= 1.2 * top[1][1]):
        if PAYABLE_RE.search(account_hint):
            inv_side = "cr"
        elif RECEIVABLE_RE.search(account_hint):
            inv_side = "dr"

    # first pass decision
    for e in entries:
        v = votes[e.idx]
        on_inv_side = (e.dr >= e.cr) == (inv_side == "dr")
        if not v:
            if on_inv_side:
                v[INVOICE] += 1
                why[e.idx].append("on the invoice side")
            else:
                v[PAYMENT] += 1
                why[e.idx].append("on the payment side")
        k = v.most_common(1)[0][0]
        # direction sanity: an 'invoice' on the payment side is a payment against that invoice (Oracle AP)
        if k == INVOICE and not on_inv_side and (e.dr or e.cr):
            k = PAYMENT
            why[e.idx].append("invoice-type line on the payment side")
        if k == PAYMENT and on_inv_side and v[REFUND] == 0 and v[PAYMENT] < 3 and v[INVOICE]:
            k = INVOICE
        if k == PAYMENT and on_inv_side and (e.dr or e.cr) and v[INVOICE] == 0:
            # money going the other way = refund / reversal of a payment
            k = REFUND if v[REFUND] else PAYMENT
        e.kind = k if e.kind != OPENING else OPENING
        e.kind_why = ", ".join(dict.fromkeys(why[e.idx])) or "default"

    # learned prefixes: majority kind per document prefix, applied to weakly classified members
    groups: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        p = prefix_of(e.ref2) or prefix_of(e.ref)
        if p:
            groups[p].append(e)
    learned = {}
    for p, es in groups.items():
        c = Counter(e.kind for e in es)
        k, n = c.most_common(1)[0]
        if n / len(es) >= 0.8 and len(es) >= 3:
            learned[p] = k
            for e in es:
                if sum(votes[e.idx].values()) <= 1 and e.kind != k:
                    e.kind = k
                    e.kind_why += f"; prefix {p} is usually {k}"

    # keys
    for e in entries:
        e.inv_key = invoice_key_in(e.ref) or invoice_key_in(e.ref2) or \
            (invoice_key_in(e.narration) if e.kind in (TDS, JOURNAL) else "")
        if not e.inv_key and e.kind in (INVOICE, CN, DN):
            # Tally bill-wise detail: "New Ref 250405-146" / "Agst Ref 250405-146" names the supplier's invoice
            for m in BILL_REF_RE.finditer(e.narration or ""):
                k = invoice_key_in(m.group(2))
                if k:
                    e.inv_key = k
                    break
        keys = [e.inv_key, norm_key(e.ref), norm_key(e.ref2)]      # in order of preference
        e.keys = [k for k in dict.fromkeys(keys) if k and len(k) >= 4]
        e.utrs = utrs_in(e.narration, e.ref, e.ref2, e.account)
        if e.kind in (INVOICE, CN, DN):
            found = []
            for m in DATE_KEY_RE.finditer(e.narration or ""):
                yy, mm, dd, n = m.groups()
                k = f"{yy}{mm}{dd}-{int(n)}"
                if 1 <= int(mm) <= 12 and k != e.inv_key:
                    found.append(k)
            e.refs_to = list(dict.fromkeys(found))
    return {"invoice_side": inv_side, "learned_prefixes": learned,
            "kinds": dict(Counter(e.kind for e in entries))}
