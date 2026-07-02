# CLAUDE.md

Personal household-budget app for a resident of Japan (Flask + SQLite local web app). Manages JPY (¥) / KRW (₩) balances independently; receipt OCR and mail parsing use Google Gemini.

## Run / build

```bash
run.bat                # python -m kakeibo (auto-opens browser) → http://localhost:5000
build.bat              # build dist/kakeibo.exe with PyInstaller (optional)
pip install -r requirements.txt
```

- Entry point: `kakeibo/__main__.py` → `main()`. Secrets are prepared via `config.prompt_missing_keys()` **before** importing the app (order matters — the app/ocr modules read env at import time).
- Change port: env `FLASK_PORT`. Disable browser auto-open: `KAKEIBO_NO_BROWSER=1`.
- Language: env `KAKEIBO_LANG` (`ko` default / `en`). Deploy KR/EN separately with different `KAKEIBO_LANG` + `KAKEIBO_HOME` (see i18n below).

## Core principle: code / data separation

**The project folder holds code (the engine) only.** All real data and secrets are stored in the user home **`~/.kakeibo/`** (Windows `%USERPROFILE%\.kakeibo\`). The location can be overridden with env `KAKEIBO_HOME`.

Inside `~/.kakeibo/`: `kakeibo.db`, `.env` (GEMINI_API_KEY, FLASK_SECRET_KEY, optional GOOGLE_CLIENT_ID/SECRET), `credentials.json`/`token.json` (Gmail), `uploads/receipts/` (receipt images), `temp/` (in-progress temp), `categories.json`/`auto_rules.json` (optional).

- Path helpers: `config.user_config_dir()`, `config.user_path(name)`, `config.resolve_secret_file(name)` (user home first, project root as fallback).
- `config.migrate_project_data()` — moves data an older version created in the project folder to `~/.kakeibo` once (called at the start of `init_db()`).
- `paths.app_dir()` is now used only for legacy fallback/migration (not the default data location).

## Layout

```
kakeibo/                     # Python package
├── __main__.py              # entry point
├── app.py                   # all Flask routes (pages + /api/*)
├── config.py                # secret/data paths, .env load, Gmail status, data migration
├── i18n.py                  # UI i18n: t()/cat_label()/category_map(), language from KAKEIBO_LANG
├── db.py                    # SQLite schema/migrations, data paths, backfill
├── exchange.py              # exchange-rate lookup
├── auto_rules.py            # auto-classification rules (applied to PayPay import only)
├── ocr/gemini.py            # Gemini: receipt OCR (analyze_receipt) + generic JSON (generate_json), QuotaError
├── sync/gmail_auth.py       # Gmail OAuth (token.json first, else authenticate via credentials.json)
├── sync/generic.py          # generic mail → transactions (AI parsing, vendor-agnostic)
└── templates/, static/
```

## Data model (transactions table)

One transaction (item) = one row. `source` values: `manual`/`paypay`/`receipt`/`email`. Per-source dedup columns:
- `paypay_id`, `gmail_id` (generic mail), `receipt_name` (receipt original filename), `amazon_id`/`mcdonald_id` (legacy)
- `receipt_id` — **id that groups the items belonging to a single receipt**. Assigned per photo at receipt confirm; all items share it. `receipt_img` is also linked on every row of the group.
- Balances (jpy_balance/krw_balance) are computed at query time by `get_transactions_list()` from initial balance + running sum (not columns).

## Receipt input flow (3-step API)

1. `POST /api/receipts/stage` — save photos (`~/.kakeibo/temp/{sid}/`), **flag duplicates by filename**. No OCR.
2. On the confirm screen (`receipt_stage.html`) pick which photos to analyze → `POST /api/receipts/analyze` — only the selected ones are run through `compress_image` (longest side 1080/q65) then Gemini OCR. Stops on quota exceeded.
3. Review (`receipt_review.html`) and edit → `POST /api/receipts/confirm` — INSERT one row per item, assign `receipt_id` (per photo) + `receipt_img` (whole group), and move the image to `uploads/receipts/`.

The transaction list (`transactions.html`) groups rows by `receipt_id` (fallback: date+shop) via `_receipt_units()` into **collapsible groups**. Images zoom on click via the lightbox (`showReceipt` in base.html).

## Generic mail sync (sync/generic.py)

Fetch with a broad Gmail query, then **pre-filter before calling the AI** (`_looks_like_purchase`) to select only purchase emails and parse them with Gemini. Dedup via `gmail_id`.

Flow is **3-step with a review page** (mirrors receipts), so the user confirms which items to add:
1. `POST /api/email/sync` → `collect_generic()` fetches + pre-filters + AI-parses **without inserting**, stages the per-email items to `~/.kakeibo/temp/{sid}.json`, returns `review_url`. (No new items → returns a message, no page.)
2. `/email-review/<sid>` (`email_review.html`) — items grouped per email, each with an **include checkbox** (default on) + editable fields; select-all / per-email toggle.
3. `POST /api/email/confirm` → `insert_email_items()` inserts only the checked items (email-level `gmail_id` dedup guards double-confirm), deletes the session file.

`sync_generic()` (collect + insert in one shot, no review) is kept for programmatic callers.

- Filter: skip if no money pattern → **skip pure order-placement mails** (`_ORDERED_RE` and no `_SHIPPED_RE` signal, so an order is counted once at the shipped stage, not duplicated) → pass if whitelisted sender or purchase keyword present.
- User settings (settings page): `email_sender_allowlist`, `email_keywords` (comma-separated, **fill = replace defaults, empty = defaults**), `mail_sync_days`, `mail_sync_max` (Gmail fetch count, default 200, clamped 1..500). Defaults are `generic._DEFAULT_SENDERS/_DEFAULT_KEYWORDS`.
- Free-quota handling (gemini-3.5-flash: 5/min, 20/day): on 429, `gemini.QuotaError` → **stop the sync immediately** (avoids a flood), save what was processed, continue on the next run.
- Gemini model is user-selectable in settings (`gemini_model`, `gemini.get_model_id()` read at call time); default `gemini.DEFAULT_MODEL`.

## Internationalization (i18n) — `kakeibo/i18n.py`

Single codebase; **language is chosen at deploy time** by env `KAKEIBO_LANG` (`ko` default / `en`). Deploy the same code twice (KR and EN), each with its own `KAKEIBO_HOME`/DB. No runtime toggle, no template forking.

Principle: **the engine stays Korean; only the frontend display is localized.** DB category values (`지출`/`수입`/`변동지출`/…), SQL filters (`type='지출'`, `LIKE '교통%'`), Python type defaults, and Gemini prompts all stay Korean regardless of UI language.

- `t(key, **kw)` — UI string from `TRANSLATIONS` (`key -> (ko, en)` tuple), falls back ko then key. Registered as a Jinja global (like `get_setting`); also imported in `app.py`/`sync`/`ocr` to wrap user-facing `jsonify`/error/Excel messages.
- `cat_label(value)` — localized display label for a category value (returns the value itself if unknown or in ko). `_CATEGORY_EN` covers the default seed (`db.py CATEGORIES_DEFAULT`); user-added categories display as typed.
- `category_map()` — the label map exposed to client JS via `window.CAT_LABELS` + `catLabel()` (in `base.html`) so JS-built category dropdowns localize too.
- Category `<option>`: `value` = Korean canonical, display = `cat_label`/`catLabel` — so submitted/stored values stay Korean and engine logic is untouched.

**Gotcha**: templates use `t` as the translation global, so **never name a Jinja loop/macro variable `t`** (it shadows the function → `'dict' object is not callable`). Transaction loops/macros use `tx`.

Add a language: extend the tuples / add a lang branch in `i18n.py`; templates need no change.

## Notes / gotchas

- **Windows Korean console (cp949)**: printing non-cp949 characters like `—`/`→`/emoji to the console raises `UnicodeEncodeError`. Keep console `print()` output ASCII.
- Always save files with `encoding="utf-8"`.
- `auto_rules` currently applies to PayPay import only (not receipt/mail).
- Settings save: `POST /api/settings` writes the received keys directly into the `settings` table (arbitrary keys allowed).
- First-run onboarding: if `setup_done` is unset, show the balance-input modal; if Gmail is not connected, show a reminder banner (hidden via `gmail_reminder_dismissed`).

## Conventions

- **Commit messages must always be written in English.**
- **Code comments and docstrings must be written in English.**
- User-facing UI strings and messages are localized in `kakeibo/i18n.py` (ko + en) — add new ones there via `t('key')` / `cat_label()` rather than hardcoding text in templates or `jsonify`. Console `print()` prompts, **Gemini prompt strings**, and **DB category values** stay Korean (engine internals).
