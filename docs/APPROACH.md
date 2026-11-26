# Ledger Reconciliation: how every step works

This document explains the logic behind every step of the Ledger Reconciliation app
(https://ledger-recon.embarko.app), in the order the work happens. It is written for the finance team and for
any developer who maintains the code. Module names are given in `code` so each rule can be traced to the file
that implements it.

The app has two features:

1. **Two-party reconciliation**: Walkover's ledger against the counterparty's ledger.
2. **Payment check**: Walkover's ledger only; each invoice against the payment received on its row.

---

## 1. Guiding principles

These rules apply everywhere and explain most design choices.

| Principle | What it means in practice |
| --- | --- |
| **AI reads structure, code copies numbers** | AI may say *which* sheet, row or column holds something. It never types an amount or a date. Every number is copied from the cell by code. |
| **The maths decides** | Matching, bridging, TDS and the payment check are pure arithmetic. No AI is called during reconciliation. |
| **Every reading is proven** | A reading of a file is accepted only when the file's own numbers confirm it (running balance, column totals, opening + movements = closing). The result is shown as **Proven / Partly proven / Not proven**. |
| **Nothing is hidden** | Every difference sits in a named line of the bridge. Open items are listed, never netted away silently. |
| **Amounts in paise** | All amounts are whole numbers of paise internally, so no rounding error can creep in. Matching tolerance is ₹1 unless stated. |
| **AI only when needed** | Rules are tried first where they are reliable; AI is called once, with only the rows it needs, when the rules cannot prove a reading. |

---

## 2. Reading a file (both features)

### 2.1 Loading (`recon/grid.py`)

- **Excel (.xlsx, .xlsm, .xls) and CSV**: every cell is read with its value and, for Excel, its formula.
  Merged cells are handled: a vertical merge copies its value down; a horizontal merge is treated as layout
  padding (copying it would create duplicate columns), but its width is remembered for header alignment.
- **Google Sheets**: the link is fetched as an Excel workbook (`app/gsheets.py`). The sheet must be shared as
  "Anyone with the link can view". All tabs are fetched, so any tab can be picked.
- **Every cell is typed**: date, amount, reference, text, status word ("paid", "pending") or empty.
  Amounts accept Indian and Western grouping, brackets for negatives, ₹/Rs/INR, and Dr/Cr suffixes.

### 2.2 Reading dates correctly (`recon/grid.py`)

Dates like `2/5/2026` can mean 2 May or 5 February. For every date column, the reading is decided in this
order; a lower step is used only if the ones above cannot decide:

1. **Proof in the column**: a value that only one order can read (e.g. `1/15/2026` must be month-first).
2. **Date sequence**: only one reading puts the dates in order.
3. **AI's opinion**: when the AI mapped the file, it also states how dates are written.
4. **Default**: Indian day-first.

If the AI disagrees with the evidence, the evidence wins and the notes say so.

### 2.3 PDF ledgers (`recon/pdf.py`, `recon/glyphs.py`, `recon/vision.py`)

PDFs must be exported from accounting software (Tally, SAP, a portal). Scans are refused with a clear message.

1. **Text reading (first)**: words and their positions are read page by page. Words on one baseline form a
   line; the columns are the vertical gaps that run down the whole table (ruling lines are not needed).
   Repeated page headers, page numbers and "continued" lines are removed; wrapped narration is joined back.
2. **Fonts without a character map** (common in Tally prints): such text comes out as symbols. The glyphs are
   recognised by their shapes against readable fonts in the same PDF and two bundled fonts. No AI, no OCR.
   Cells built this way are flagged and must be confirmed by the ledger's own arithmetic.
3. **Page images (only if the text reading cannot be proven)**: each page is drawn as an image and read by a
   vision model, which copies the rows exactly as printed. Every amount it returns must also exist in the
   PDF's own text; otherwise the ledger cannot show as proven. The better-proven reading is kept.

---

## 3. Two-party reconciliation

### 3.1 Step 1: Extraction (AI reads structure; code copies data)

`recon/extract.py`, `recon/ai.py`, `recon/pipeline.py`

**Step A: structure (one AI request per file).** The AI sees each sheet's top and bottom rows and returns a
fixed extraction format: which sheet is the ledger, its table(s) (header row, first and last data row), which
column holds date, reference, narration, debit, credit, TDS and balance, and the party account for GL dumps.

**Step B: row meaning (AI, in batches).** For each extracted row, the AI returns its type (invoice, payment,
TDS, credit note, debit note, journal, opening...) and the invoice number it refers to. An invoice number is
kept **only if it appears in that row's own text**, so nothing can be invented.

**Code then:**
- copies every amount and date from the cells using that format;
- merges multi-line vouchers, skips total/opening/closing rows (labelled or not), keeps only the party account
  in GL dumps (`recon/rows.py`);
- applies guards: an "invoice" on the payment side becomes a payment; a header repeated mid-table starts a new
  table; tables run from their header to the next table or the sheet end.

Models (via viaSocket AI): Claude Sonnet 5.5 for structure, GPT-6 Luna for row types (cheaper, high volume).

**Rule-based reader (fallback / development)**: when AI is switched off, `recon/detect.py` finds header rows and
tables, aligns shifted headers using merged-cell spans, and assigns column roles from header name, content and
formulas with the Hungarian algorithm. Helper and VLOOKUP columns are ignored.

### 3.2 Step 2: Proving the reading (`recon/validate.py`, `recon/parser.py`)

Each ledger is checked against its own numbers:

| Check | Meaning |
| --- | --- |
| Running balance | Previous balance ± this row = this row's balance, on every row |
| Column totals | Sum of debits / credits = the printed total row |
| Closing balance | Opening + movements = printed closing balance |
| Register relations | Base + GST = gross, where such columns exist |

The reading that makes these agree is chosen (which sign convention, whether TDS reduces the balance, which
column is the balance). Result:

- **Proven** (green): all checks that the file allows pass.
- **Partly proven** (amber): some pass, or the file has nothing to check against.
- **Not proven** (red): the checks fail.

**What the user sees**: a proven ledger shows only its badge, period, entry count and opening/closing balance.
Only a ledger that is not proven shows the column picker so someone can correct it before reconciling.

**Memory** (`recon/profiles.py`): each table's layout is fingerprinted. When the same format is uploaded again,
the saved column roles are reused, and still re-proven. What was learned about a counterparty (direction,
TDS booked net or gross, TDS rate, document prefixes) is remembered too.

### 3.3 Step 3: Classifying entries (`recon/classify.py`)

Each entry's type is decided by votes from voucher type, keywords, account, direction and learned document
prefixes. Invoice numbers are normalised (e.g. `260403-148`, `20260403-148`) so the two sides compare equal.
UTR / bank references are extracted from narrations.

### 3.4 Step 4: Matching (maths only, `recon/match.py`)

First, the **direction** (which side is debit for the other) and the **TDS basis** (does the counterparty book
invoices net or gross of TDS) are decided from the data. Then entries are paired in tiers. Each entry can be
used once; a tier works only on what earlier tiers left.

**How the direction is decided.** Normally the counterparty's ledger mirrors ours: our invoice is a debit in our
books and a credit in theirs. The app checks this from entries that carry one of Walkover's invoice numbers:

- a counterparty **invoice / credit note / debit note** with our number votes that it moves the same way as our
  document (after mirroring);
- a counterparty **payment** that quotes our invoice number ("Agst Ref 250606-125") settles that invoice, so it
  moves the **opposite** way, and votes accordingly;
- TDS entries and journals do not vote. With no votes, the normal mirror is used.

If payments were counted like invoices, a ledger whose payments quote our invoice numbers would be read the wrong
way round, and every payment would look "not accounted" on both sides. That is why payments vote inverted.

**Safeguard: the direction is double-checked.** Whenever anything is left unpaired, the two ledgers are also
reconciled the other way round. The app measures, for each direction, how much is left unpaired or matched with
an unexplained amount difference, and keeps the direction that leaves clearly less (at most half). A wrong
direction leaves nearly every payment and invoice unpaired, so it cannot win even if the votes above were fooled.
When the double-check overrides the vote, the reconciliation notes say so.

| Tier | Pairs when | Typical case |
| --- | --- | --- |
| **T1 Reference** | Same invoice number on both sides, same amount (₹1) | Counterparty quotes our invoice number |
| **T2 Reference + arithmetic** | Same invoice number, amount differs by an explainable amount: TDS deducted (rates 0.1%, 1%, 2%, 5%, 10% on gross or on taxable value at 5/12/18/28% GST), or the voucher partly posted to another Walkover ledger | Invoice booked net of TDS |
| **T3 Bank reference** | Payments with the same UTR and amount | NEFT/RTGS receipts |
| **T4 Amount + date** | Same amount within 7 days, closest dates paired first (Hungarian algorithm) | Payments without references |
| **T5 One-to-many** | One entry equals the sum of several on the other side, within 30 days (bounded subset search, up to 6 items) | One receipt for two invoices |
| **T6 Booked under counterparty's number** | See below | Counterparty uses its own voucher numbers |
| **T7 Revision chains** | A credit note cancels invoices within one ledger (and re-bills), netting to nil | Cancel and re-invoice |
| **Self-cancelling sets** | Entries on one side, same date, net to zero | Entry and reversal |
| **T9 Period totals** | Month or quarter totals agree when one side has no references | Summary-level ledgers |

#### T6: invoices booked under the counterparty's own number

Some counterparties book our invoice:
- under **their own voucher number** (our 250405-11 becomes their PB/25-26/7),
- **weeks later** (outside T4/T5's windows),
- at a **different amount** (net of the TDS they deduct).

No earlier tier can link these. T6 does not assume any percentage. It **learns the counterparty's booking
pattern from the two ledgers**:

1. Take the invoices still unpaired on both sides. Walkover lines with the same voucher number and date count
   as one invoice.
2. For every Walkover invoice and every counterparty invoice dated from 7 days before to 120 days after it,
   compute *counterparty amount ÷ Walkover amount*.
3. Find the ratio that repeats. It must hold on **at least 3 different invoices** on each side. For example, every pair gives 0.983: the counterparty books our invoices 1.7% lower.
4. Pair the invoices whose amounts fit that ratio within ₹2, nearest dates first, each invoice once.
5. Name the difference: if it equals a recognised TDS deduction (as in T2), it is labelled
   "TDS deducted by counterparty on invoice (…)"; otherwise "Counterparty books our invoices x% lower".

If no ratio repeats on 3 invoices, nothing is paired and the entries stay open as "not yet accounted".

### 3.5 Step 5: The bridge (`recon/bridge.py`)

Every difference goes into one named line. By construction:

> Walkover closing balance + all bridge lines = counterparty closing balance (in Walkover's direction)

The remainder is the **Unexplained difference**, which must be 0. A non-zero remainder points to a reading
problem, never to a silent reconciliation error.

| Category | Bridge lines (accounting terms) |
| --- | --- |
| Opening | Opening balance difference; counterparty invoice dated before Walkover's ledger period (sits in Walkover's opening balance) |
| Posting | Invoice partly posted to another Walkover ledger |
| Timing | Walkover invoice / receipt after the counterparty's last entry; counterparty entry after Walkover's last entry |
| Not accounted | Invoice booked by Walkover, not yet accounted by counterparty · Credit note issued by Walkover, not yet accounted by counterparty · Receipt booked by Walkover, not shown in counterparty's ledger · Entry booked by counterparty, not yet accounted in Walkover's books · Payment made by counterparty, not yet accounted in Walkover's books · Credit/debit note raised by counterparty, not yet accounted in Walkover's books |
| TDS | TDS deducted by counterparty on invoice (rate, base) · TDS deducted by counterparty (invoice booked net) · TDS as per 26AS booked by Walkover · Counterparty deduction / adjustment journal against an invoice |
| Amount / rounding | Amount differs (with reference match); rounding |

### 3.6 Step 6: TDS analysis (`recon/tds.py`)

- The **rate and base** (e.g. 2% on taxable value = gross ÷ 1.18) are inferred from the data, not configured.
- **Per invoice**: expected TDS, actual deduction, excess withholding; other deductions (e.g. GST input
  reversal) listed separately.
- **Per financial year**: deductions by the counterparty vs Walkover's TDS entries (26AS). A deduction is moved
  to the year of its provision month only when that makes the years agree better.

### 3.7 What the user sees

- **Unexplained difference = 0**: one green **"Reconciled"** box with the Excel download. If any entry is still
  not accounted by one side, it is listed inside the box under **"Open reconciling items"**; it is never hidden.
- **Otherwise**: closing balances, the bridge, TDS by year, and tabs for matched, unmatched and chains.

### 3.8 The Excel report (`recon/report.py`)

Rebuilt from the saved extractions on every download (never stored, always consistent with the current logic):
summary bridge with live formulas, TDS by year, matches (with tier and explanation), unmatched entries, parse
report, and both ledgers' normalised entries.

---

## 4. Payment check (single ledger)

`recon/paycheck.py`, `app/checks.py`

### 4.1 Reading the columns (rules first, AI only if needed)

1. **Header rules**: the header row is found in the first 40 rows, and each header is named:

   | Header examples | Field |
   | --- | --- |
   | Invoice Amount, Bill Amount, Gross Amount | Invoice amount (**required**) |
   | Payment, Amount Received, Receipt | Payment received (**required**) |
   | TDS, TDS 2% | TDS in the sheet |
   | Amount after, Net, Receivable | Amount after TDS in the sheet |
   | Remaining, Balance, Outstanding | Remaining |
   | Invoice No, Bill No | Invoice number |
   | First "Date"; second "Date", Received On, Payment Date | Invoice date; payment date |

2. **Proof against the sheet's own numbers**:
   - Amount after = Invoice − TDS on every row;
   - TDS is 2% or 10% of the amount before GST on every row;
   - each column adds up to the Total row.

   If all hold: **Proven**, and **no AI is used**.
3. **AI fallback (one request per file)**: only when the headers cannot be named or the proof fails. The AI sees
   the header and about 25 rows and answers which column letter is which field. Its reading must pass the same
   proof; the rules' reading is kept unless the AI's is better.
4. **Manual**: if neither reading is proven, the column picker is shown.

Data rows run from the header down to the Total row (or five blank rows). Rows without an invoice amount are
skipped.

### 4.2 The check on each invoice (maths only)

- **TDS** = invoice ÷ 1.18 (amount before 18% GST) × rate, rounded to the rupee.
  Example: 1,18,000 ÷ 1.18 × 2% = 2,000, so the expected payment is 1,16,000.
- **Expected payment** = invoice − TDS.

The payment is compared, within ₹1, in this order:

| Order | Payment equals | Result |
| --- | --- | --- |
| 1 | Invoice − 2% TDS | Remaining **0**, "matched at 2%" |
| 2 | Invoice − 10% TDS | Remaining **0**, "matched at 10%" |
| 3 | The full invoice | Remaining **0**, marked **"Customer paid the full amount, no TDS deducted"** |
| – | No payment | Remaining = invoice − TDS, "Not paid" |
| – | None of the above | Remaining = invoice − TDS − payment: "Short paid", or "Excess paid" if negative |

When nothing matches, the rate used for Remaining is the one the sheet's own TDS column shows for that row, or
2% if it shows none.

The 2% / 10% rule is used **only** in the payment check, never in the two-party reconciliation.

### 4.3 What the user sees

- **All matched**: one green **"All payments reconciled"** box with the download.
- **Otherwise**: tiles (invoices, matched at 2% / 10%, remaining total) and a table of only the rows with an
  amount remaining, each with its reason.
- Full-amount payments are listed in a note: "Customer paid the full amount, no TDS deducted".

### 4.4 The Excel output

The user's own sheet with formulas and formatting kept, the **Remaining** column filled in (added if missing),
and a **Payment check** column explaining each row. Open rows are highlighted yellow; full-amount-no-TDS rows
light blue. Rebuilt on every download.

---

## 5. Storage (viaSocket Table / DB Dash, database "Reconcile_app")

| Table | Holds |
| --- | --- |
| `users` | Login IDs, password hashes, roles |
| `runs` | Two-party reconciliations (small documents) |
| `files` | Uploads, extractions and results, stored in chunks |
| `profiles` | Learned formats and counterparty profiles |
| `reconciliations` | One readable row per reconciliation: Google Sheet links, report download link, app link (no reports stored) |
| `checks` | Payment checks (small documents) |
| `payment_checks` | One readable row per payment check: sheet link, result, invoices, matched, remaining total, links |

Embarko holds no data, only a cache that can be rebuilt.

## 6. Security

- Sign-in with login ID and password. Passwords are PBKDF2-SHA256 with 600,000 iterations; five wrong attempts
  lock an account for 15 minutes; a reset or deactivation signs the user out everywhere.
- Administrators add users; the first administrator comes from settings.
- Secrets (database key, AI keys, session key) live only in the server settings, never in the code or browser.
- Page images for the PDF vision reader are published at random links that expire in 15 minutes.

## 7. Hosting and updates

- Hosted on Embarko as `ledger-recon` (`python main.py`). Deployed with `deploy/embarko_deploy.sh`.
- The home page tells browsers not to cache it and names the script by version, so every deploy reaches every
  browser without a hard refresh.
- Settings are environment variables; see the README.

## 8. Tests

`tests/` covers every matching tier with synthetic ledgers, the payment check (every outcome, the AI fallback,
the Excel output), PDF reading, date handling, sign-in and the web API end to end.

Run: `.venv/bin/python -m pytest -q`. Some tests use real counterparty ledgers in `tests/samples/`. Those files
are confidential customer data and are kept out of the public repository, so those tests need a local copy.
