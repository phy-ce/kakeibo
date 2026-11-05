# Kakeibo

**English** | [한국어](README.ko.md)

A local household-budget web app for people living in Japan, built with Flask and SQLite. It tracks JPY and KRW balances separately and uses Gemini to turn receipts and order emails into transactions you can review before saving.

## Features

- **Receipt reading:** upload a photo and Gemini extracts items, amounts, and dates.
- **Gmail sync:** reads order, shipping, and receipt emails without vendor-specific code, and separates shipping, discounts, and tax so the total reconciles.
- **Review before saving:** check AI results on a review page, with the original email available for comparison.
- **PayPay and PASMO:** import PayPay CSV files, or upload a mobile PASMO history screenshot.
- **Automation:** classification rules for PayPay imports and monthly recurring transactions.
- **Budgets and stats:** per-item budgets, monthly and 12-month charts, and Excel export.
- **Local data:** the ledger and secrets stay in your user folder, not in the project folder.

## Quick start

Requires Python 3.10+ on Windows. A [Gemini API key](https://aistudio.google.com/app/apikey) is needed for receipt and email reading.

```bash
git clone https://github.com/phy-ce/kakeibo.git
cd kakeibo
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
run_en.bat
```

Open http://localhost:5001. On first run, the console asks for `GEMINI_API_KEY` and saves it to `~/.kakeibo-en/.env`.

| | Korean | English |
|---|---|---|
| Launcher | `run.bat` | `run_en.bat` |
| Port | 5000 | 5001 |
| Data folder | `~/.kakeibo` | `~/.kakeibo-en` |

The two instances can run at the same time and do not share data. Set `KAKEIBO_HOME` to use another data folder.

## Optional setup

Paths below use the Korean instance's folder. For the English instance, use `~/.kakeibo-en`.

- **Gmail sync:** in [Google Cloud Console](https://console.cloud.google.com/), enable the Gmail API and create a Desktop OAuth client. Upload `credentials.json` on the Settings page, then approve access in the browser on the first sync.
- **Classification rules:** copy `auto_rules.json.example` to `~/.kakeibo/auto_rules.json` and edit it.
- **Initial categories:** copy `categories.json.example` to `~/.kakeibo/categories.json` before the first run. After that, manage categories on the Settings page.

## Project layout

```text
kakeibo/
├── app.py          Flask routes
├── db.py           SQLite schema and migrations
├── ai_items.py     Cleanup of AI-extracted items
├── auto_rules.py   Classification rules
├── recurring.py    Monthly recurring transactions
├── exchange.py     Exchange rates
├── i18n.py         Korean and English UI text
├── ocr/            Receipt reading with Gemini
├── sync/           Gmail authentication and email parsing
├── templates/      Jinja2 HTML
└── static/         CSS and JavaScript
```

## License

MIT. See [LICENSE](LICENSE).
