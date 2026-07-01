# Kakeibo

A personal household-budget app for residents of Japan — a local web app built on Flask + SQLite.
Manages JPY (¥) and KRW (₩) balances **independently**; the exchange rate is shown for reference only.

> The UI ships in **Korean and English**, chosen at deploy time via the `KAKEIBO_LANG` env var (`ko` default / `en`). See [Language (KR / EN)](#language-kr--en).

## Features

- 📷 **Receipt OCR**: upload a photo and Gemini Flash auto-extracts items/amounts.
- 💳 **PayPay CSV import**: import a PayPay transaction-history CSV in one go.
- 📧 **Gmail sync (AI)**: regardless of vendor, Gemini reads order/shipping/receipt emails and auto-extracts items, amounts, and dates (Amazon, McDonald's, BIRKENSTOCK, etc. — no per-vendor code needed). Shipping fees, discounts, and consumption tax are broken out so the total reconciles.
- 🤖 **Auto-classification rules**: auto-fill category/item name by matching shop/item/category (`auto_rules.json`).
- 💰 **Per-item budgets**: budgets at the individual transaction-item level, not a category bundle (e.g. Netflix, gas bill).
- 📊 **Stats / Trash / Excel export**: Chart.js graphs, soft delete, openpyxl Excel export.

## Install (from source)

Requirements: Python 3.10+

```bash
git clone https://github.com/<YOUR_GITHUB_USERNAME>/kakeibo.git
cd kakeibo

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
```

### Secrets & configuration

Secrets (API keys) are stored in the **user home, not the project folder** (`~/.kakeibo/.env`, on Windows `%USERPROFILE%\.kakeibo\.env`). No manual setup is needed — the **console guides you automatically on first run**.

1. **API key (required for OCR/mail AI)**
   - `FLASK_SECRET_KEY` — auto-generated and saved (nothing to do).
   - `GEMINI_API_KEY` — if missing, the console prompts for it on first run and saves it to `~/.kakeibo/.env`; it is not asked again on later runs.
     - Get a key (free): [Google AI Studio](https://aistudio.google.com/app/apikey)
     - To change it later, edit `~/.kakeibo/.env` directly.
     - To change the storage location, set the `KAKEIBO_HOME` environment variable.
   - (Backward compat) A `.env` in the project root is also read, but the user-home `.env` takes precedence.

2. **(Optional) Gmail sync** — to import order/receipt emails:
   - [Google Cloud Console](https://console.cloud.google.com/) → new project → enable Gmail API → create an OAuth client (Desktop).
   - Upload the downloaded `credentials.json` via **Settings page → Gmail integration → Upload** (saved to `~/.kakeibo/credentials.json`).
   - On the first sync, consent in the browser → `~/.kakeibo/token.json` is created automatically (reused afterwards).

3. **(Optional) Auto-classification rules**
   - Copy `auto_rules.json.example` to **`~/.kakeibo/auto_rules.json`** and edit with your own rules.

4. **(Optional) Customize initial categories**
   - Copy `categories.json.example` to **`~/.kakeibo/categories.json`** and edit with your own categories.
   - **Applied only before the first run** (seeded only when the DB is empty). If a DB already exists, add/remove from the Settings page.

> **Data/code separation**: the project folder holds **code (the engine) only**. All real data — DB (`kakeibo.db`), uploaded/temporary receipts, secrets (`.env`, `credentials.json`, `token.json`), `auto_rules.json`, `categories.json` — is stored under **`~/.kakeibo/`** (Windows `%USERPROFILE%\.kakeibo\`). Override the location with `KAKEIBO_HOME`. (Data an older version created in the project folder is moved here automatically on first run.)

### Run

```bash
run.bat                          # Korean  → http://localhost:5000  (data: ~/.kakeibo)
run_en.bat                       # English → http://localhost:5001  (data: ~/.kakeibo-en)
# python -m kakeibo              # run directly
```

## Language (KR / EN)

One codebase; the language is chosen **at launch** by the `KAKEIBO_LANG` env var (`ko` default / `en`). Deploy the same code as two instances — Korean and English — each with its own data folder (`KAKEIBO_HOME`) and port. No runtime toggle, no forked templates.

| | Korean | English |
|---|---|---|
| Launcher | `run.bat` | `run_en.bat` |
| `KAKEIBO_LANG` | `ko` | `en` |
| `KAKEIBO_HOME` (data) | `~/.kakeibo` | `~/.kakeibo-en` |
| Port | 5000 | 5001 |

- Only the **frontend display** is localized (`kakeibo/i18n.py`). The engine — DB category values, SQL, and the Gemini prompts — stays in Korean internally, so both instances behave identically.
- Category names show localized labels while the stored value stays Korean; user-added categories display as typed.
- Each instance keeps its **own DB and secrets** (they don't share data) and can run at the same time.
- Add another language by extending the translation table in `kakeibo/i18n.py`; templates need no change.

## Install (Windows exe)

Download `kakeibo-*.zip` from [Releases](../../releases):

1. Unzip (anywhere).
2. Double-click `kakeibo.exe` → a console window opens and **prompts for `GEMINI_API_KEY` on first run** (saved to `~/.kakeibo/.env`; not asked again).
3. (Optional) For Gmail sync, place `credentials.json` in the same folder as the exe.
4. The browser then opens automatically.

The DB (`kakeibo.db`), uploads folder, and temp folder are created next to the exe automatically. Only the API key is kept separately in the user home (`~/.kakeibo`).

## Layout

```
kakeibo/                    # repo root
├── kakeibo/                # Python package
│   ├── app.py              # Flask routes
│   ├── i18n.py             # UI i18n (t / cat_label), language from KAKEIBO_LANG
│   ├── db.py               # SQLite schema/migrations
│   ├── exchange.py         # Yahoo Finance exchange rate
│   ├── auto_rules.py       # auto-classification rule engine
│   ├── paths.py            # source/exe compatible path helper
│   ├── sync/               # Gmail-based sync
│   │   ├── gmail_auth.py   # shared OAuth
│   │   └── generic.py      # generic mail parsing (AI, vendor-agnostic)
│   ├── ocr/
│   │   └── gemini.py       # receipt OCR
│   ├── templates/          # Jinja2 HTML
│   └── static/             # CSS/JS
├── requirements.txt
├── run.bat / run_en.bat / build.bat
├── .env.example
└── auto_rules.json.example
```

## License

MIT — see `LICENSE`.
