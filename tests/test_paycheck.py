"""Payment check: one Walkover ledger, each invoice against its payment (recon/paycheck.py and the /api/checks routes)."""
import importlib
import sys

import pytest
from openpyxl import Workbook, load_workbook

from recon.paycheck import Row, analyse, as_dict, check_row, role_of, tds_for, write_output

# made-up invoices in the layout of a typical payment sheet: header in row 2 from column C, Total row at the end
INVOICES = [("03-04-2026", "INV-001", 118000, "27-05-26", 116000), ("05-05-2026", "INV-002", 236000, "04-06-26", 232000),
            ("04-06-2026", "INV-003", 590000, None, None), ("07-07-2026", "INV-004", 354000, "21-08-2026", 348000),
            ("03-08-2026", "INV-005", 472000, "21-08-2026", 464000), ("04-09-2026", "INV-006", 59000, "28-10-2026", 58000),
            ("05-10-2026", "INV-007", 177000, "28-10-2026", 174000)]


@pytest.fixture()
def sample(tmp_path):
    wb = Workbook(); ws = wb.active
    for col, h in zip("CDEFGHIJ", ["Date", "Invoice No", "Invoice Amount", "TDS 2%", "Amount after", "Date", "Payment",
                                   "Remaining"]):
        ws[f"{col}2"] = h
    for i, (d, n, a, rd, p) in enumerate(INVOICES, start=3):
        tds = a / 1.18 * 0.02
        for col, v in zip("CDEFGHIJ", [d, n, a, tds, a - tds, rd, p, a - tds - (p or 0)]):
            ws[f"{col}{i}"] = v
    t = 3 + len(INVOICES)
    ws[f"C{t}"] = "Total"
    for col in "EFGIJ":
        ws[f"{col}{t}"] = sum((ws[f"{col}{r}"].value or 0) for r in range(3, t))
    p = tmp_path / "payments.xlsx"; wb.save(p)
    return p


def r(invoice, payment=None, tds_sheet=None):
    return check_row(Row(row=1, invoice=invoice * 100, payment=payment and payment * 100,
                         tds_sheet=tds_sheet and tds_sheet * 100))


def test_tds_is_on_the_amount_before_gst_rounded_to_the_rupee():
    assert tds_for(11800000, 2) == 200000           # 118000 / 1.18 * 2% = 2000
    assert tds_for(12345600, 2) == 209200           # 2092.47 -> 2092
    assert tds_for(11800000, 10) == 1000000


def test_each_outcome():
    x = r(118000, 116000)
    assert (x.status, x.rate, x.remaining) == ("matched", "2%", 0)
    x = r(118000, 108000)                             # 118000 - 10% of 100000
    assert (x.status, x.rate, x.remaining) == ("matched", "10%", 0)
    x = r(118000, 118000)
    assert (x.status, x.remaining) == ("paid_full", 0)
    assert x.note == "Customer paid the full amount, no TDS deducted"
    x = r(118000, 115001)                             # Rs 1 inside the tolerance of 116000? no: 999 short
    assert (x.status, x.rate, x.remaining) == ("short", "2%", 99900)
    x = r(118000, 115999)                             # within Rs 1 of 116000
    assert (x.status, x.remaining) == ("matched", 0)
    x = r(118000, 117000)
    assert (x.status, x.remaining) == ("excess", -100000)
    x = r(590000)
    assert (x.status, x.remaining) == ("unpaid", 58000000)
    x = r(118000, 100000, tds_sheet=10000)            # the sheet shows 10% TDS: the shortfall is against 10%
    assert (x.status, x.rate, x.remaining) == ("short", "10%", 800000)


def test_headers():
    assert role_of("Invoice Amount") == "invoice_amount"
    assert role_of("TDS 2%") == "tds"
    assert role_of("Amount after") == "net"
    assert role_of("Payment") == "payment"
    assert role_of("Remaining") == "remaining"
    assert role_of("Invoice No") == "invoice_no"
    assert role_of("Date") == "date"
    assert role_of("Payment Date") == "received_date"
    assert role_of("Amount Received") == "payment"


def test_sample_sheet_is_read_by_rules_and_proven(sample):
    d = as_dict(analyse(str(sample)))
    assert d["confidence"] == "green" and d["mapped_by"] == "rules"
    assert {k: chr(65 + v) for k, v in d["cols"].items()} == {
        "invoice_date": "C", "invoice_no": "D", "invoice_amount": "E", "tds": "F", "net": "G",
        "received_date": "H", "payment": "I", "remaining": "J"}
    s = d["summary"]
    assert (s["invoices"], s["matched_2"], s["unpaid"], s["open"], s["remaining_total"]) == (7, 6, 1, 1, 58000000)
    unpaid = [x for x in d["rows"] if x["status"] == "unpaid"]
    assert [x["invoice_no"] for x in unpaid] == ["INV-003"]
    assert d["total_row"] == 10


def test_output_fills_remaining_and_adds_the_check_column(tmp_path, sample):
    src = sample
    d = as_dict(analyse(str(src)))
    out = tmp_path / "out.xlsx"
    write_output(str(src), ".xlsx", d, str(out))
    ws = load_workbook(out).active
    assert [ws[f"J{i}"].value for i in range(3, 10)] == [0, 0, 580000.0, 0, 0, 0, 0]
    assert ws["K2"].value == "Payment check"
    assert ws["K5"].value.startswith("Not paid") and ws["K3"].value.startswith("Matched")
    assert ws["J10"].value == 580000.0


def test_no_remaining_column_and_plain_headers(tmp_path):
    wb = Workbook(); ws = wb.active
    ws.append(["Bill No", "Bill Date", "Gross Amount", "Amount Received", "Received On"])
    ws.append(["A-1", "01-04-2026", 118000, 116000, "10-05-2026"])
    ws.append(["A-2", "02-04-2026", 236000, 216000, "10-05-2026"])     # 10% TDS
    ws.append(["A-3", "03-04-2026", 59000, 50000, "10-05-2026"])       # short against 2%: 58000 - 50000
    p = tmp_path / "plain.xlsx"; wb.save(p)
    d = as_dict(analyse(str(p)))
    assert d["cols"]["invoice_amount"] == 2 and d["cols"]["payment"] == 3 and d["cols"]["received_date"] == 4
    assert d["confidence"] == "amber"                 # nothing in the sheet to prove the reading against
    assert [x["status"] for x in d["rows"]] == ["matched", "matched", "short"]
    assert d["rows"][2]["remaining"] == 800000
    out = tmp_path / "out.xlsx"
    write_output(str(p), ".xlsx", d, str(out))
    ws = load_workbook(out).active
    assert ws["F1"].value == "Remaining" and ws["G1"].value == "Payment check"
    assert ws["F4"].value == 8000.0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    monkeypatch.setenv("RECON_DATA_DIR", str(tmp_path))
    for m in [m for m in sys.modules if m.startswith("app")]:
        del sys.modules[m]
    main = importlib.import_module("app.main")
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_api_flow(client, sample):
    with open(sample, "rb") as f:
        r = client.post("/api/checks", data={"name": "Acme payments"}, files={"file": ("payments.xlsx", f)})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    c = client.get(f"/api/checks/{cid}").json()
    assert c["status"] == "done", c["error"]
    assert c["result"]["summary"]["open"] == 1 and c["result"]["confidence"] == "green"
    assert any(x["id"] == cid for x in client.get("/api/checks").json())
    assert all(x["id"] != cid for x in client.get("/api/runs").json())     # kept apart from two-party runs
    out = client.get(f"/api/checks/{cid}/output.xlsx")
    assert out.status_code == 200 and out.content[:2] == b"PK"
    # the user picks the columns
    r = client.post(f"/api/checks/{cid}/mapping", json={"header_row": 2, "cols": {"invoice_amount": 4, "payment": 8}})
    assert r.status_code == 200
    c = client.get(f"/api/checks/{cid}").json()
    assert c["result"]["mapped_by"] == "you" and c["result"]["summary"]["open"] == 1
    r = client.post(f"/api/checks/{cid}/mapping", json={"header_row": 2, "cols": {"invoice_amount": 4}})
    assert r.status_code == 400


class FakeAI:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def json(self, task, system, user, schema, effort, max_tokens):
        self.calls += 1
        return self.answer


def test_ai_only_when_the_rules_cannot_read_the_columns(tmp_path, sample):
    from recon.ai import AIStats
    # a proven sheet: no AI call at all
    ai = FakeAI({})
    analyse(str(sample), ai=ai, stats=AIStats())
    assert ai.calls == 0
    # headers the rules cannot name: one AI call maps them; code copies the numbers
    wb = Workbook(); ws = wb.active
    ws.append(["Ref", "Dt", "Value A", "Value B", "Value C"])
    ws.append(["A-1", "01-04-2026", 118000, 2000, 116000])
    ws.append(["A-2", "02-04-2026", 59000, 1000, 50000])
    p = tmp_path / "odd.xlsx"; wb.save(p)
    ai = FakeAI({"header_row": 1, "invoice_date": "B", "invoice_no": "A", "invoice_amount": "C", "tds": "D",
                 "net": "", "received_date": "", "payment": "E", "remaining": ""})
    d = as_dict(analyse(str(p), ai=ai, stats=AIStats()))
    assert ai.calls == 1 and d["mapped_by"] == "ai"
    assert [x["status"] for x in d["rows"]] == ["matched", "short"] and d["rows"][1]["remaining"] == 800000


def test_home_page_names_the_script_by_version(client):
    r = client.get("/")
    assert r.status_code == 200 and "no-cache" in r.headers["cache-control"]
    assert "/static/app.js?v=" in r.text and "/static/styles.css?v=" in r.text
