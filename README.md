# Ledger reconciliation engine and web app

Two features for Walkover's finance team (live at https://ledger-recon.embarko.app):

- **Two-party reconciliation**: Walkover's ledger against any counterparty's ledger (Excel, CSV, PDF or Google Sheets).
- **Payment check**: Walkover's ledger only; each invoice against the payment received, Remaining filled in.

AI reads structure only when needed; every amount is copied by code and every result is maths.
**How every step works: [docs/APPROACH.md](docs/APPROACH.md).**

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m recon parse "path/to/ledger.xlsx"
.venv/bin/python -m recon reconcile walkover.xlsx counterparty.xlsx --name "Acme" -o out/report.xlsx
.venv/bin/python -m pytest -q
```

A sheet can be picked with `file.xlsx#SheetName`. Without it, the engine picks the sheet whose numbers it can prove.

## Web app

```bash
AUTH_DISABLED=1 .venv/bin/uvicorn app.main:app --port 8765          # local, sign-in switched off
```

Sign-in is by login ID and password. The first administrator comes from `ADMIN_USERNAME` / `ADMIN_PASSWORD` (used only while no user exists); administrators add users on the Users page, which shows a temporary password once; users must replace it at first sign-in. Passwords are PBKDF2-SHA256 (600,000 iterations); five wrong passwords lock an account for 15 minutes; deactivating a user or resetting a password signs them out everywhere.

Ledgers come in as Excel/CSV uploads or Google Sheets links (the sheet must be shared "Anyone with the link can view"; the whole workbook is fetched, so tab selection works). Any column layout is handled by the engine; layouts it cannot prove go to the AI layout assist.

### Settings (environment variables)

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Signs the session cookie. Required in production: a long random value |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | First administrator (only while the users table is empty) |
| `VIASOCKET_ORG_ID`, `VIASOCKET_PROJECT_ID`, `VIASOCKET_EMBED_SECRET` | viaSocket AI for extraction (Claude Sonnet 5.5 for structure, GPT-6 Luna for row types; override with `RECON_AI_STRUCTURE_MODEL` / `RECON_AI_TYPES_MODEL` as `service:model`). `ANTHROPIC_API_KEY` works instead when viaSocket is not set |
| `STORAGE`, `DBDASH_DB_ID`, `DBDASH_AUTH_KEY`, `DBDASH_TABLE_USERS/RUNS/FILES/PROFILES/CHECKS`, `DBDASH_TABLE_RECONCILIATIONS`, `DBDASH_TABLE_PAYMENT_CHECKS` | `dbdash` keeps users, runs, files and learned formats in viaSocket Table (DB Dash) database Reconcile_app; `sqlite` keeps them in a local file |
| `EXTRACTION` | `ai` (default) or `rules` |
| `RECON_AI_MODEL` | AI model, default `claude-opus-5-5` |
| `RECON_AI=off` | Switch the AI fallback off even when a key is set |
| `DATA_DIR` / `RECON_DATA_DIR` | Where the SQLite database, uploads, reports and learned profiles live |
| `HTTPS_ONLY=0` | Allow the session cookie over plain HTTP (local testing only) |

### Hosting on Embarko

Embarko runs `python main.py` (binds `0.0.0.0:$PORT`) with dependencies from `requirements.txt`. With `STORAGE=dbdash` all data lives in viaSocket Table (DB Dash), not on Embarko; Embarko's `DATA_DIR` holds only a re-creatable file cache. Memory is capped at 256 MB; the two largest sample reconciliations peak at about 146 MB.

1. Get a deploy token: `POST https://ship.embarko.ai/api/public/deploy-tokens/request` with your email; paste the key from the email into `DEPLOY_TOKEN`.
2. Set the secrets once in Embarko → App setup: `SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`.
3. `DEPLOY_TOKEN=... deploy/embarko_deploy.sh` (uploads only `main.py`, `requirements.txt`, `app/`, `recon/`; about 60 KB).

### Extraction with AI, reconciliation with maths

1. **Extract (AI).** AI reads each workbook and fills a fixed extraction format: which sheet holds the ledger, its table(s), which column is date / reference / narration / debit / credit / TDS / balance, the party account in GL dumps, and each row's type and the invoice number it refers to. Code then copies every amount and date from the cells with that format; AI never types a number. An invoice number is kept only if it occurs in that row's own text. The file's own balances and totals are cross-checked and shown in step 1, and every extracted row can be reviewed there.
2. **Reconcile (maths only).** Runs on the saved extraction (exactly what was reviewed): matching tiers T1–T9, the bridge and the TDS analysis. No AI is called.

`EXTRACTION=rules` switches to the rule-based reader (for development without an API key). With `EXTRACTION=ai` (default) and no `ANTHROPIC_API_KEY`, the app says so instead of falling back silently.

| Module | Role |
| --- | --- |
| `app/main.py` | FastAPI routes: sign-in, users, runs, uploads / Google Sheets links, column mapping, reconcile, download, frontend |
| `app/auth.py` | Login ID + password, lockout, admin bootstrap |
| `app/gsheets.py` | Google Sheets link → workbook (docs.google.com only) |
| `app/store.py`, `app/runs.py` | Document store (DB Dash REST API or SQLite); runs and their files |
| `app/jobs.py` | Background parse / reconcile |
| `app/serialize.py` | Engine results to JSON |
| `app/static/` | Single-page frontend (HTML, CSS, vanilla JS; no build step) |
| `recon/extract.py`, `recon/ai.py`, `recon/pipeline.py`, `recon/ledger_io.py` | AI extraction, the AI backend, extract-then-reconcile pipeline, saved extractions |
| `main.py`, `deploy/embarko_deploy.sh` | Hosting entry point and deploy script |

## Pipeline (one module per stage)

| Stage | Module | What it does |
| --- | --- | --- |
| 1.1–1.2 Read | `grid.py` | Loads xlsx / xls / csv with values and formulas, handles merged cells, types every cell (date, amount, ref, text, status). Amounts are integer paise. |
| 1.3–1.5 Understand layout | `detect.py` | Finds header rows and tables (several per sheet), aligns shifted headers to data columns using merged-cell spans, assigns column roles from header name + content + formulas with the Hungarian algorithm, ignores helper/VLOOKUP columns, infers unlabelled columns. |
| 1.6–1.7 Entries | `rows.py` | Groups multi-line vouchers, narration rows, round-off journals; skips totals/opening/closing rows (labelled or not); keeps only the party account in GL dumps. |
| 2 Prove | `validate.py`, `parser.py` | Tests readings (balance sign, TDS-reduces-balance, which column is the balance) against running balances, printed totals, closing balance, base + GST = gross; repairs dd/mm swaps; carries balances between tables; rates the read green / amber / red. |
| 3 Classify | `classify.py` | Votes between voucher type, keywords, account, direction and learned document prefixes; normalises invoice keys (YYYYMMDD-N and YYMMDD-N), links TDS memos to invoices, extracts UTRs. |
| 4 Match | `match.py` | T1 exact reference, T2 reference + TDS/GST arithmetic and partial postings, T3 UTR, T4 amount + date (Hungarian), T5 one-to-many (bounded subset-sum), T7 revision chains (credit note cancels invoices, re-bill), self-cancelling sets, T9 month/quarter totals for ledgers without references. Direction and net/gross TDS basis are chosen from the data. |
| 5 Bridge | `bridge.py` | Every difference goes into a named bucket; Walkover closing + buckets must equal the counterparty closing. |
| 5 TDS | `tds.py` | Infers the TDS rate/base from the data, expected vs actual per invoice, excess withholding, other deductions; FY comparison with Walkover's TDS entries and a timing test that moves a deduction to its provision/usage year only when that makes the years agree. Re-billed invoices split back into the invoices they replaced. |
| Memory | `profiles.py` | Format fingerprints (saved column roles, re-proven on every upload, re-detected if they fail) and counterparty profiles (orientation, TDS basis/rate, document prefixes, party account) in `profiles/profiles.json`. |
| Output | `report.py` | Excel: Summary bridge (live formulas), TDS by year, Matches (with revision chains), Unmatched, Parse report, both ledgers' normalised entries. |

## Next

26AS import; more counterparty formats.
