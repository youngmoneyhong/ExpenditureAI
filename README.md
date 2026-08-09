# ExpenditureAI
ExpenditureAI is a local Streamlit app that turns UOB TMRW, DBS Bank, and DBS PayLah transaction screenshots into a reviewed personal-expense ledger in Google Sheets.

It is designed for a practical monthly workflow: upload screenshots, review extracted transactions, resolve duplicates and categorisation, then append checked rows to the correct year and month worksheet.

## Features

- Extracts transactions from one or many screenshots with OpenAI Vision.
- Processes several screenshots concurrently (configurable from one to four workers) to reduce waiting time.
- Uses a strict structured extraction schema for dates, merchant descriptions, amounts, source, money flow, references, and confidence.
- Runs one combined AI enrichment request for category suggestions, anomaly flags, and a short spending insight.
- Detects exact duplicates, previously appended transactions, and likely repeated rows caused by overlapping screenshots.
- Shows duplicate comparisons side by side, including matched fields and archived screenshots where available.
- Learns deliberate merchant-category decisions locally; the Merchant rules screen lets you inspect, edit, or forget these decisions.
- Ignores DBS Bank and DBS PayLah PayLah wallet top-ups by default, so internal wallet movements do not become expenses.
- Ignores UOB TMRW rows starting with `PAYMT THRU E-BANK` by default.
- Enforces normal transaction signs in the app: outflows are negative and inflows are positive.
- Creates year workbooks and month tabs automatically, based on each transaction date.
- Writes a concise, user-facing A-L Google Sheets ledger and hides non-user-facing processing data.
- Creates and refreshes a single annual Summary matrix with category rows, month-year columns, year totals, net spend, gross spend, and offsets.

## Categories And Money Flow

### Outflow categories

`Food`, `Public Transport`, `Taxi`, `Shopping`, `Gifts`, `Entertainment`, `Travel`, `Health`, `Personal Care`, `Education`, `Bills`, `Admin & Fees`, `Others`, `Insurance`, `Subscriptions`, and `Income Tax`.

### Inflow categories

`Carousell Sales`, `Cashbacks & Refunds`, `Reimbursement`, and `GVs & Prize Award`.

All four inflow categories are shown in the Summary as offsets. They reduce `Net Spend` and roll up into `Total Offset`.

`Transfer` is reserved for neutral internal transfers, including ignored PayLah top-ups.

## How It Works

1. Upload transaction screenshots in the Streamlit app.
2. Vision extraction reads each screenshot and produces transaction candidates.
3. The app normalizes dates, sources, descriptions, categories, and signed amounts.
4. Duplicate logic compares rows within the upload and against the target Google Sheet period.
5. Review the transaction table, correct category, flow, amount, or duplicate decisions, and choose which dates to append.
6. Confirmed rows are appended to the matching `YYYY` Google Sheets workbook and month tab.
7. The annual `Summary` tab refreshes automatically.

## Google Sheets Layout

Each month tab keeps the user-facing ledger in columns A-L:

| Column | Field |
| --- | --- |
| A | `check` |
| B | `date` |
| C | `source` |
| D | `category` |
| E | `description` |
| F | `amount_original` |
| G | `amount_parse_error` |
| H | `amount` |
| I | `currency` |
| J | `transaction_reference` |
| K | `money_flow` |
| L | `transaction_type` |

Column D is widened for categories, column E is widened for merchant descriptions, and the header row is frozen. The `check` and `category` fields have dropdowns for manual review.

The `Summary` tab is a single matrix instead of separate monthly blocks:

- Rows: Net Spend, spending categories, Gross Spend, the four inflow offsets, and Total Offset.
- Columns: calendar-ordered `Month Year` values plus `Year Total`.
- Net Spend, Gross Spend, and Total Offset are visually emphasized.
- Insurance, Subscriptions, and Income Tax are lightly shaded as recurring or fixed-cost areas.
- Summary column A is widened for readable category names.

Only month-tab rows with `check = Yes` are included in Summary totals.

## Setup

### 1. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

### 2. Configure environment variables

Edit `.env` and provide:

```text
OPENAI_API_KEY=...
GOOGLE_DRIVE_FOLDER_ID=...
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
VISION_CONCURRENCY=3
```

Optional settings:

- `OPENAI_MODEL`: model used for the enrichment request.
- `GOOGLE_SHEET_ID`: use one fixed spreadsheet instead of year/month routing.
- `GOOGLE_WORKSHEET_NAME`: worksheet name for fixed-spreadsheet mode.
- `SCREENSHOT_ARCHIVE_DIR`: archive folder; defaults to `screenshots`.
- `REVIEW_MEMORY_FILE`: path for local merchant-rule memory; defaults to `review_memory.json`.

### 3. Configure Google access

1. Create a Google Cloud service account.
2. Enable the Google Sheets API and Google Drive API.
3. Download its JSON key as `service_account.json`.
4. Share the target Google Drive folder with the service-account email as an Editor.
5. Place the folder ID in `GOOGLE_DRIVE_FOLDER_ID`.

Detailed instructions are in [GOOGLE_SETUP.md](GOOGLE_SETUP.md).

Your Drive folder will be organised automatically like this:

```text
Expenditure folder
  2026 Google Sheet
    May tab
    June tab
    July tab
  2027 Google Sheet
    January tab
```

## Run The App

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

Or run `launch.bat` on Windows.

## Review And Append Workflow

The review table is the decision point before anything reaches Google Sheets.

- `ready`: can be appended.
- `needs_review`: check the row before appending.
- `duplicate`: skipped by default; use `separate duplicate?` only for a genuine repeat transaction.
- `ignored`: intentionally excluded, such as a PayLah wallet top-up.

The `Dates to append` selector defaults to every extracted valid date. Remove a date to skip it for the current append only.

Before appending, the app checks for date/source/description/amount/currency duplicates in the matching month sheet. It also compares overlapping screenshot candidates conservatively using date, source, amount, flow, currency, transaction reference, and merchant-description similarity.

## AI Techniques Used

- **Multimodal Vision extraction**: reads bank-app screenshots into typed transaction records.
- **Pydantic structured output**: validates AI extraction and enrichment responses against a predictable schema.
- **Concurrent processing**: uses bounded parallel Vision requests for faster multi-screenshot batches.
- **Combined enrichment**: category, anomaly, and insight analysis share one model request to reduce latency and cost.
- **Rule-based normalization**: standardizes category names, dates, currency, signed amounts, and flow constraints.
- **Hybrid duplicate detection**: combines hashes, transaction keys, exact references, amounts, and conservative string similarity.
- **Human-in-the-loop review**: every extraction can be inspected and edited before append.
- **Local learning**: reviewed merchant decisions are stored in `review_memory.json`, with controls to edit or forget them.
- **Formula-driven summaries**: Google Sheets formulas keep the annual Summary current when checked monthly rows change.

## Tests

Run the regression suite with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests cover Google Sheets output, summary formulas and layout, category migrations, reimbursement/offset rules, duplicate detection, merchant rules, and performance behaviour.

## Privacy And Security

This project is intended to run locally. Bank screenshots are sensitive:

- Screenshots are sent to OpenAI only for the requested extraction.
- Confirmed transactions are sent to Google Sheets only when you append them.
- `.env`, `service_account.json`, screenshots, virtual environments, and `review_memory.json` are ignored by Git.
- Never commit service-account keys or real OpenAI API keys.
