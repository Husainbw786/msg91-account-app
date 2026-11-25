"""Synthetic ledgers (built in the test, not real data) that exercise one matching tier each."""
import csv
import warnings

from recon.bridge import build_bridge
from recon.classify import classify
from recon.match import reconcile
from recon.parser import parse_file
from recon.profiles import ProfileStore

warnings.filterwarnings("ignore")


def walkover_csv(path, rows):
    """rows: (date dd-mm-yyyy, vtype, voucher no, particulars, debit, credit)"""
    bal = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Vch Type", "Voucher Number", "Particulars", "Debit", "Credit", "Balance"])
        for d, vt, no, part, dr, cr in rows:
            bal += dr - cr
            w.writerow([d, vt, no, part, dr or "", cr or "", f"{abs(bal):.2f} {'Dr' if bal >= 0 else 'Cr'}"])
    return str(path)


def counterparty_csv(path, rows):
    """rows: (date, doc type, external doc no, narration, debit, credit). Their books: credit = Walkover invoice."""
    bal = 0.0
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Posting Date", "Document Type", "External Doc No", "Narration", "Debit Amount", "Credit Amount",
                    "Running Balance"])
        for d, vt, no, narr, dr, cr in rows:
            bal += dr - cr
            w.writerow([d, vt, no, narr, dr or 0, cr or 0, f"{bal:.2f}"])
    return str(path)


def run(wp, cp, store=None):
    W, C = parse_file(wp, "W", store=store), parse_file(cp, "C", store=store)
    classify(W.entries)
    classify(C.entries)
    res = reconcile(W, C)
    return W, C, res, build_bridge(res)


def tiers(res):
    return sorted(m.tier for m in res.matches)


def test_t5_one_receipt_two_payments(tmp_path):
    wp = walkover_csv(tmp_path / "w.csv", [
        ("01-05-2024", "sales", "240501-1", "SMS credits", 100000, 0),
        ("10-05-2024", "receipt", "RCPT-240510-1", "NEFT from customer", 0, 100000),
    ])
    cp = counterparty_csv(tmp_path / "c.csv", [
        ("02-05-2024", "Invoice", "240501-1", "SMS credits", 0, 100000),
        ("09-05-2024", "Payment", "", "Bank payment part 1", 60000, 0),
        ("11-05-2024", "Payment", "", "Bank payment part 2", 40000, 0),
    ])
    W, C, res, br = run(wp, cp)
    assert W.confidence == "green" and C.confidence == "green"
    assert tiers(res) == ["T1 reference", "T5 one-to-many"]
    assert br.residual == 0 and not res.unmatched_w and not res.unmatched_c


def test_t7_revision_chain(tmp_path):
    wp = walkover_csv(tmp_path / "w.csv", [
        ("01-05-2024", "sales", "240501-1", "SMS May", 59000, 0),
        ("02-05-2024", "sales", "240502-2", "SMS May extra", 59000, 0),
        ("01-06-2024", "credit note", "CR-240601-1", "invoice revision 240501-1 & 240502-2", 0, 118000),
        ("01-06-2024", "sales", "240601-3", "invoice revision 240501-1 & 240502-2", 118000, 0),
    ])
    cp = counterparty_csv(tmp_path / "c.csv", [
        ("03-06-2024", "Invoice", "240601-3", "SMS May revised", 0, 118000),
    ])
    W, C, res, br = run(wp, cp)
    assert "T7 revision chain" in tiers(res)
    assert len(res.chains) == 1 and res.chains[0].net_nil
    assert [x.inv_key for x in res.chains[0].rebill] == ["240601-3"]
    assert br.residual == 0 and not res.unmatched_w and not res.unmatched_c


def test_t9_period_totals_without_references(tmp_path):
    wp = walkover_csv(tmp_path / "w.csv", [
        ("05-05-2024", "sales", "240505-1", "SMS pack", 10000, 0),
        ("12-05-2024", "sales", "240512-2", "SMS pack", 20000, 0),
        ("20-05-2024", "sales", "240520-3", "SMS pack", 30000, 0),
    ])
    cp = counterparty_csv(tmp_path / "c.csv", [
        ("15-05-2024", "Invoice", "", "sms purchase", 0, 25000),
        ("28-05-2024", "Invoice", "", "sms purchase", 0, 35000),
    ])
    W, C, res, br = run(wp, cp)
    assert tiers(res) == ["T9 period total"]
    assert br.residual == 0


def test_unmatched_items_land_in_buckets(tmp_path):
    wp = walkover_csv(tmp_path / "w.csv", [
        ("01-05-2024", "sales", "240501-1", "SMS", 50000, 0),
        ("02-06-2024", "credit note", "CR-240602-1", "goodwill credit", 0, 5000),
    ])
    cp = counterparty_csv(tmp_path / "c.csv", [
        ("02-05-2024", "Invoice", "240501-1", "SMS", 0, 50000),
    ])
    W, C, res, br = run(wp, cp)
    labels = {bl.label: bl.amount for bl in br.lines}
    assert labels["Credit note issued by Walkover, not yet accounted by counterparty"] == 500000
    assert br.residual == 0


def test_profile_reuse_and_fallback(tmp_path):
    wp = walkover_csv(tmp_path / "w.csv", [("01-05-2024", "sales", "240501-1", "SMS", 1000, 0),
                                           ("03-05-2024", "receipt", "RCPT-1", "NEFT", 0, 1000),
                                           ("04-05-2024", "sales", "240504-2", "SMS", 2000, 0)])
    store = ProfileStore(tmp_path / "p.json")
    led1 = parse_file(wp, "W", store=store)
    store.save()
    led2 = parse_file(wp, "W", store=ProfileStore(tmp_path / "p.json"))
    assert any("Known format" in n for t in led2.tables for n in t.notes)
    assert len(led2.entries) == len(led1.entries) == 3
    # a saved format whose labels no longer exist must not be applied
    st = ProfileStore(tmp_path / "p.json")
    fp = next(iter(st.data["formats"]))
    st.data["formats"][fp]["roles"]["debit"] = "No Such Column"
    led3 = parse_file(wp, "W", store=st)
    assert led3.confidence == "green" and len(led3.entries) == 3


def test_t6_invoices_booked_under_counterparty_numbers_net_and_late(tmp_path):
    # the counterparty books each Walkover invoice under its own voucher number, weeks later, net of 2% TDS on
    # the taxable value: no shared number, no equal amount, outside the date windows
    inv = [("05-04-2025", "250405-11", 1200000), ("08-05-2025", "250508-12", 1350000),
           ("06-06-2025", "250606-13", 1475000), ("07-07-2025", "250707-14", 1610000)]
    net = lambda g: g - round(g / 1.18 * 0.02)
    wp = walkover_csv(tmp_path / "w.csv", [(d, "sales", n, "SMS services", g, 0) for d, n, g in inv])
    cp = counterparty_csv(tmp_path / "c.csv", [
        ("19-05-2025", "Invoice", "PB/25-26/7", "Purchase invoice", 0, net(1200000)),
        ("19-05-2025", "Invoice", "PB/25-26/8", "Purchase invoice", 0, net(1350000)),
        ("17-06-2025", "Invoice", "PB/25-26/9", "Purchase invoice", 0, net(1475000)),
        ("21-07-2025", "Invoice", "PB/25-26/10", "Purchase invoice", 0, net(1610000)),
    ])
    W, C, res, br = run(wp, cp)
    assert tiers(res) == ["T6 booked under counterparty's number"] * 4
    assert not res.unmatched_w and not res.unmatched_c
    assert br.residual == 0
    assert all(l.category != "not accounted" for l in br.lines)
    pairs = {m.w[0].ref: m.c[0].ref for m in res.matches}
    assert pairs["250405-11"] == "PB/25-26/7" and pairs["250508-12"] == "PB/25-26/8"


def test_t6_needs_a_consistent_pattern(tmp_path):
    # unrelated amounts: no ratio repeats, so nothing is paired and the entries stay open
    wp = walkover_csv(tmp_path / "w.csv", [("05-04-2025", "sales", "250405-1", "x", 100000, 0),
                                          ("05-05-2025", "sales", "250505-1", "x", 230000, 0)])
    cp = counterparty_csv(tmp_path / "c.csv", [("20-04-2025", "Invoice", "A-1", "y", 0, 91000),
                                              ("20-05-2025", "Invoice", "A-2", "y", 0, 170000)])
    W, C, res, br = run(wp, cp)
    assert not any(m.tier.startswith("T6") for m in res.matches)
    assert any(l.category == "not accounted" for l in br.lines)
