"""Command line.

  python -m recon parse FILE[#Sheet]
  python -m recon reconcile WALKOVER_FILE[#Sheet] COUNTERPARTY_FILE[#Sheet] -o out.xlsx
"""
from __future__ import annotations

import argparse
import sys
import warnings
from collections import Counter

from .ai import AIStats, default_backend
from .classify import classify
from .parser import parse_file
from .pipeline import run as run_pipeline
from .profiles import ProfileStore
from .report import write_report


def _print_ledger(led, label: str) -> None:
    print(f"\n{label}: {led.path} / sheet '{led.sheet}'  ->  {len(led.entries)} entries, "
          f"{led.first_date} to {led.last_date}, confidence {led.confidence.upper()}")
    for t in led.tables:
        print(f"  table at row {t.header_row + 1} ({t.layout}): "
              + ", ".join(f"{k}={t.labels.get(v, '') or 'col ' + str(v + 1)}" for k, v in t.roles.items()))
    for c in led.checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    for n in led.notes[:12]:
        print(f"  note: {n}")
    kinds = Counter(e.kind for e in led.entries)
    if kinds:
        print("  kinds: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
    print(f"  opening {led.opening / 100:,.2f}  closing {led.closing / 100:,.2f}"
          + (f"  (printed {led.closing_printed / 100:,.2f})" if led.closing_printed is not None else ""))


def main(argv=None) -> int:
    from .viasocket import load_env
    load_env()
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(prog="recon", description="Ledger reconciliation engine (phase 1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("parse", help="read one ledger and show how it was understood")
    p1.add_argument("file")
    p2 = sub.add_parser("reconcile", help="reconcile Walkover's ledger with a counterparty ledger")
    p2.add_argument("walkover")
    p2.add_argument("counterparty")
    p2.add_argument("-o", "--out", default="reconciliation.xlsx")
    p2.add_argument("--name", default="Counterparty", help="counterparty name for the report")
    for p in (p1, p2):
        p.add_argument("--profiles", default="profiles/profiles.json", help="profile store (JSON)")
        p.add_argument("--no-profiles", action="store_true", help="do not read or write profiles")
        p.add_argument("--no-ai", action="store_true", help="no AI (needs EXTRACTION=rules)")
    a = ap.parse_args(argv)

    store = None if a.no_profiles else ProfileStore(a.profiles)
    if a.cmd == "parse":
        stats = AIStats()
        from .pipeline import extract
        ai = None if a.no_ai else default_backend(stats)
        led = extract(a.file, "C", "Counterparty", store, ai, stats)
        for line in stats.log:
            print(f"AI: {line}")
        _print_ledger(led, "Ledger")
        if store:
            store.save()
        return 0

    stats = AIStats()
    ai = None if a.no_ai else default_backend(stats)
    from .pipeline import extraction_mode
    print(f"Extraction: {extraction_mode()}" + (f" ({ai.model})" if ai else "") + "; reconciliation: maths only")
    out = run_pipeline(a.walkover, a.counterparty, a.name, store, ai)
    if store:
        store.save()
    W, C, res, br, tds = out.W, out.C, out.res, out.bridge, out.tds
    _print_ledger(W, "Walkover")
    _print_ledger(C, a.name)
    print(f"\nMatches: " + ", ".join(f"{t} {n}" for t, n in Counter(m.tier for m in res.matches).most_common())
          + f"; self-cancelling sets {len(res.self_cancel)}; unmatched W {len(res.unmatched_w)}, "
            f"C {len(res.unmatched_c)}")
    print(f"\nBridge\n  {br.w_closing / 100:>16,.2f}  Walkover closing")
    for bl in br.lines:
        print(f"  {bl.amount / 100:>16,.2f}  {bl.label} (x{bl.count})")
    print(f"  {br.derived / 100:>16,.2f}  = derived {a.name} closing")
    print(f"  {br.c_closing_w / 100:>16,.2f}  {a.name} closing per its ledger")
    print(f"  {br.residual / 100:>16,.2f}  unexplained")
    print(f"\nTDS ({tds.rate_label})")
    for r in tds.fy_rows:
        print(f"  {r['fy']}: deducted {r['counterparty'] / 100:>12,.2f}  booked by Walkover {r['walkover'] / 100:>12,.2f}"
              f"  difference {r['diff'] / 100:>10,.2f}" + (f"  (moved: {r['moved']})" if r['moved'] else ""))
    for d in tds.deductions:
        if d.excess:
            print(f"  excess withheld on {d.key}: {d.excess / 100:,.2f}")
    for k, amt, narr in tds.other_deductions:
        print(f"  other deduction on {k}: {amt / 100:,.2f} ({narr})")
    if ai:
        print(f"\nAI extraction: {stats.calls} call(s), {stats.rejected} reference(s) rejected, {stats.failures} failed")
        for line in stats.log:
            print(f"  {line}")
    write_report(res, br, a.out, "Walkover", a.name, tds, stats)
    print(f"\nReport written to {a.out}")
    return 0 if br.residual == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
