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
├── dates.py                 # normalize_date(): fixes missing/misread years in AI-extracted dates
├── ai_items.py              # clean_items() + Refiner: shared post-processing of every AI extraction
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
- Balances (jpy_balance/krw_balance) are computed at query time by `get_transactions_list()` from initial balance + running sum (not columns). The sum always runs over the **whole** ledger; list filters (`_tx_matches`, incl. the `q` search) only pick rows afterwards — filtering first made the expense-only view show wrong balances.

## Receipt input flow (3-step API)

1. `POST /api/receipts/stage` — save photos (`~/.kakeibo/temp/{sid}/`), **flag duplicates by filename**. No OCR.
2. On the confirm screen (`receipt_stage.html`) pick which photos to analyze → `POST /api/receipts/analyze` — only the selected ones are run through `compress_image` (longest side 1080/q65) then Gemini OCR. Stops on quota exceeded.
3. Review (`receipt_review.html`) and edit → `POST /api/receipts/confirm` — INSERT one row per item, assign `receipt_id` (per photo) + `receipt_img` (whole group), and move the image to `uploads/receipts/`.

Both review pages (`receipt_review.html`, `email_review.html`) share one row layout and `static/js/review.js`:
- Each `.review-item` is two aligned lines — line 1 date/type/shop/item/amount, line 2 the category cascade behind a `분류` tag — styled by the `.review-*` / `.field-amount` / `.is-unset` rules in `static/css/main.css`. The action bar is `.review-head` (sticky).
- The major/minor/sub selects are built **client-side**; the AI suggestion rides on the row as `data-major/minor/sub` and is preselected (an off-scheme value is added as an extra option instead of being dropped). Never render the suggested value as the only `<option>` — that is what previously made the category uneditable.
- An empty select shows its `data-placeholder` and gets `.is-unset` (highlighted as a to-do); amounts are colored by sign.

The transaction list (`transactions.html`) groups rows by `receipt_id` (fallback: date+shop) via `_receipt_units()` into **collapsible groups**. Images zoom on click via the lightbox (`showReceipt` in base.html).

## Generic mail sync (sync/generic.py)

Fetch with a broad Gmail query, then **pre-filter before calling the AI** (`_looks_like_purchase`) to select only purchase emails and parse them with Gemini. Dedup via `gmail_id`.

Flow is **3-step with a review page** (mirrors receipts), so the user confirms which items to add:
1. `POST /api/email/sync` → starts a **background job** (`_run_mail_sync` thread in `app.py`) and returns `{job_id}`. The job runs `collect_generic()` (fetch + pre-filter + AI-parse, **without inserting**) and stages the per-email items to `~/.kakeibo/temp/{sid}.json`. The page polls `GET /api/email/sync/status/<job_id>` every 0.8s and shows a live progress modal (stage message, progress bar, checked/analyzed/skipped/found counters); when `status=done` it follows `review_url`. (No new items → `message`, no page.) Only one sync runs at a time — a second POST returns the running `job_id` with `already_running`. Finished jobs are dropped after 15 min.
   - `collect_generic(..., on_progress=cb)` emits `{stage: connect|list|read|skip|analyze|parsed|quota|overload|model_error|done, index, total, found, scanned, skipped, known, subject}`. The counters mean different things and the UI must not blur them: `scanned` = mails actually sent to the AI (the only ones costing quota), `skipped` = rejected by the pre-filter before any AI call, `known` = already imported or already handled in an earlier sync (`mail_seen`), `found` = mails that yielded items; `app._progress_message()` turns an event into the localized line (`sync.*` keys). A raising callback never breaks the sync.
2. `/email-review/<sid>` (`email_review.html`) — items grouped per email, each with an **include checkbox** (default on) + editable fields; select-all / per-email toggle.
3. `POST /api/email/confirm` → `insert_email_items()` inserts only the checked items (email-level `gmail_id` dedup guards double-confirm), deletes the session file.

`sync_generic()` (collect + insert in one shot, no review) is kept for programmatic callers.

- **Dedup / no re-scanning** — a `gmail_id` reaches `transactions` only when an item is actually saved, so the `mail_seen` table (`gmail_id, reason, seen_at`) records every mail already handled: `filtered` (pre-filter, no AI call), `no_items` (AI found no purchase), `reviewed` (shown on the review page — marked at `/api/email/confirm`, including items the user left unchecked). `collect_generic()` skips `transactions ∪ mail_seen`. Changing `email_keywords`/`email_sender_allowlist` drops the `filtered` rows (`_drop_stale_filtered()` compares a fingerprint stored in `settings.mail_filter_fingerprint`) so those mails get another look; `no_items`/`reviewed` survive.
- Filter: skip if no money pattern → **skip pure order-placement mails** (`_ORDERED_RE` and no `_SHIPPED_RE` signal, so an order is counted once at the shipped stage, not duplicated) → pass if whitelisted sender or purchase keyword present.
- User settings (settings page): `email_sender_allowlist`, `email_keywords` (comma-separated, **fill = replace defaults, empty = defaults**), `mail_sync_days`, `mail_sync_max` (Gmail fetch count, default 200, clamped 1..500). Defaults are `generic._DEFAULT_SENDERS/_DEFAULT_KEYWORDS`.
- Free-quota handling (gemini-3.5-flash: 5/min, 20/day): on 429, `gemini.QuotaError` → **stop the sync immediately** (avoids a flood), save what was processed, continue on the next run.
- **Free key first, paid key second.** Free/paid is per Google Cloud project, so a paid key (`GEMINI_API_KEY_PAID` in `.env`, optional) must come from a separate project with billing. `generate_json()` walks `gemini._tiers()`: the free key's whole model chain runs first; only a 429 moves the call to the paid key, and `_tier_cooldown` then skips the free key (RPM 70s / per-day 6h). 503/404 never switch keys. Model cooldowns are keyed `"{tier}:{model}"`. `gemini.LAST_TIER` feeds the `tier_switch` progress event; the settings page shows which keys are set.
- **503 UNAVAILABLE ("high demand") is model load, not quota.** All model calls go through `gemini.generate_json()`, which retries with jittered waits, then retries once on the `gemini_fallback_model` setting if one is chosen (settings page), then raises `gemini.OverloadError`. Two wait schedules: `OVERLOAD_BACKOFF` (2/6/15s) for interactive calls, `OVERLOAD_BACKOFF_BG` (5/15/40/90s) passed by the mail sync, which runs on a worker thread and can afford to wait a spike out.
- **A single 503 does not abort the batch.** Mail sync skips the busy mail (it stays out of `mail_seen`, so the next sync retries it) and continues; only `generic.MAX_BUSY_STREAK` (3) consecutive failures stop the run (`sync.overload_skip` vs `sync.overload_hit`). Receipt analyze does the same with `app.MAX_BUSY_PHOTOS` (2) — a skipped photo keeps its original, so it stays selectable for another analyze run. Only when the run actually stops is `overloaded` reported (`msg.*_overload_stopped`, `email.overload_partial`).
- **404 NOT_FOUND = the model id is retired**, not a transient failure (Google drops models; `models.list()` may still list one that 404s on every call). `generate_json()` skips retries, tries the fallback model once, then raises `gemini.ModelError`; callers stop immediately and show `msg.model_unavailable` pointing at the settings page. `get_model_id()`/`get_fallback_model_id()` ignore a stored id that is not in `AVAILABLE_MODELS`, so a stale setting self-heals to `DEFAULT_MODEL`. **When a model 404s, re-probe with a real `generate_content` call and refresh `AVAILABLE_MODELS` — the list endpoint is not trustworthy.**
- Gemini model is user-selectable in settings (`gemini_model`, `gemini.get_model_id()` read at call time); default `gemini.DEFAULT_MODEL`.
- **Auto model mode** (`gemini_auto_model`, asked as a checkbox in the mail-sync modal before the run starts and persisted by `POST /api/email/sync`): `_model_chain()` becomes chosen -> fallback -> the rest of `AVAILABLE_MODELS` in order, and quota/busy/retired failures move to the next model instead of stopping. `_cooldown` parks a failed model (RPM 70s, per-day 6h, busy 120s, 404 24h) so later calls in the same batch do not re-probe it; a successful call clears it. `gemini.LAST_MODEL` holds the model that answered, and the sync emits a `model_switch` progress event when it changes.
- The review page shows each mail's **original text** (`email.body`, trimmed to `generic.BODY_PREVIEW_CHARS`, staged in the session JSON) behind an "원본 메일" button, so the AI's reading can be checked before adding — the mail-side equivalent of opening the receipt photo.
- When a run stops early, `collect_generic()` reports `total` / `examined` / `remaining` plus `remaining_samples` (subjects of up to `REMAINING_PREVIEW` unprocessed mails, read with `format="metadata"` so no AI quota is used). The review-page banners and the stop messages state these numbers instead of a vague "partly analyzed".

## Internationalization (i18n) — `kakeibo/i18n.py`

Single codebase; **language is chosen at deploy time** by env `KAKEIBO_LANG` (`ko` default / `en`). Deploy the same code twice (KR and EN), each with its own `KAKEIBO_HOME`/DB. No runtime toggle, no template forking.

Principle: **the engine stays Korean; only the frontend display is localized.** DB category values (`지출`/`수입`/`변동지출`/…), SQL filters (`type='지출'`, `LIKE '교통%'`), Python type defaults, and Gemini prompts all stay Korean regardless of UI language.

- `t(key, **kw)` — UI string from `TRANSLATIONS` (`key -> (ko, en)` tuple), falls back ko then key. Registered as a Jinja global (like `get_setting`); also imported in `app.py`/`sync`/`ocr` to wrap user-facing `jsonify`/error/Excel messages.
- `cat_label(value)` — localized display label for a category value (returns the value itself if unknown or in ko). `_CATEGORY_EN` covers the default seed (`db.py CATEGORIES_DEFAULT`); user-added categories display as typed.
- `category_map()` — the label map exposed to client JS via `window.CAT_LABELS` + `catLabel()` (in `base.html`) so JS-built category dropdowns localize too.
- Category `<option>`: `value` = Korean canonical, display = `cat_label`/`catLabel` — so submitted/stored values stay Korean and engine logic is untouched.

**Gotcha**: templates use `t` as the translation global, so **never name a Jinja loop/macro variable `t`** (it shadows the function → `'dict' object is not callable`). Transaction loops/macros use `tx`.

Add a language: extend the tuples / add a lang branch in `i18n.py`; templates need no change.

## AI output post-processing (`kakeibo/ai_items.py`)

Receipt OCR and mail parsing both call Gemini with a **response schema** (`gemini.ITEMS_SCHEMA` / `RECEIPT_SCHEMA`; a model that rejects it is retried once without) and then run the result through:
- `clean_items()` — amounts to int (`'¥1,200'` ok), drops nameless/zero rows, normalizes dates, fixes a garbled `type` (a positive line among expenses stays `지출`, it is a discount).
- `Refiner(conn).refine(items)` — built once per batch. **History first**: the category the user last gave the same shop+item (or an item always filed one way; generic names like `送料`/`消費税` never match alone) wins and sets `cat_from='history'` (review pages show a badge). Then **snap to the scheme**: repair the major from a known minor, clear unknown values (so the review page flags them), fill a lone sub.
- Receipts also return the printed `total`; the review page compares it with the (edited) row sum live and warns on a mismatch.
- Photos go through `gemini.load_photo()` (EXIF rotation applied, longest side `OCR_MAX_DIM` 2048); the stored thumbnail uses the same helper.
- Mail HTML: `<style>/<script>/<head>` are dropped and whitespace squeezed before the 8000-char AI budget is applied.
- Mail prompt asks for a short `item` (<= `generic.ITEM_NAME_MAX` 40 chars; the full title goes to `note`, stored on insert) and passes `known_shops()` so one shop keeps one name. `clean_items()` cuts anything over `LONG_NAME` itself if the model ignores that.
- `DupFinder` (mail sync only) flags rows matching a **mail import** (ledger `source='email'` or an earlier mail in the same batch): same amount, dates <= 7 days apart, shop or item prefix match. Flagged rows carry `dup`, start **unchecked** on the review page with a badge + what they matched. Other sources are deliberately not compared (user decision: real duplicates were all mail-vs-mail).

## AI dates (`kakeibo/dates.py`)

Receipts/mails older than `MIN_YEAR` (2025) are never entered, so a year that is missing, unparsable or older than that is treated as a misread: `normalize_date()` replaces it with the current year (previous year only if the result would be more than 15 days in the future). Applied to OCR output in `/api/receipts/analyze` and to AI mail output in `parse_email_ai()`. At confirm time it runs with `fix_year=False`, so a year the user typed on the review page is kept and only a blank/broken date is filled in. The Gemini prompts also state today's date and the `MIN_YEAR` floor.

## Housekeeping (runs once at startup, from `__main__.main()`)

- `config.backup_db()` — one dated copy of the ledger per day in `~/.kakeibo/backups/kakeibo-YYYY-MM-DD.db`, newest `BACKUP_KEEP` (7) kept. Uses the sqlite3 backup API, not a file copy, so it is consistent even with WAL pages pending.
- `config.cleanup_temp()` — drops staging sessions in `~/.kakeibo/temp` older than `TEMP_TTL_DAYS` (3). `rmtree` otherwise only runs on confirm, so every abandoned upload/review used to stay forever (24 MB of stale photos when this was added).
- `app.purge_orphan_images()` — removes files in `uploads/receipts/` that no row references. During normal use `_drop_unused_images()` handles this: destroy / empty-trash collect the rows' `receipt_img` first and delete a file only after re-checking that no surviving row points at it (a whole receipt group shares one image).

## Notes / gotchas

- **Windows Korean console (cp949)**: printing non-cp949 characters like `—`/`→`/emoji to the console raises `UnicodeEncodeError`. Keep console `print()` output ASCII.
- Always save files with `encoding="utf-8"`.
- `auto_rules` currently applies to PayPay import only (not receipt/mail).
- Settings save: `POST /api/settings` writes the received keys directly into the `settings` table (arbitrary keys allowed).
- Staging session ids come from the URL/JSON and are joined into paths, so always go through `app._session_file()` / `_valid_session()` (uuid-only); a raw `os.path.join(TEMP_DIR, session_id)` let `/temp/%2E%2E/kakeibo.db` download the ledger.
- Every aggregate/export query must filter `deleted_at IS NULL` (trash rows are soft-deleted) — stats and Excel export once counted trashed rows.
- Uploaded filenames are sanitized with `secure_filename()` before hitting disk (the original name is still kept for display and the duplicate check); `/uploads/...` and `/temp/...` are served with `send_from_directory`, which refuses paths resolving outside their root.
- First-run onboarding: if `setup_done` is unset, show the balance-input modal; if Gmail is not connected, show a reminder banner (hidden via `gmail_reminder_dismissed`).

## Conventions

- **Commit messages must always be written in English.**
- **Code comments and docstrings must be written in English.**
- User-facing UI strings and messages are localized in `kakeibo/i18n.py` (ko + en) — add new ones there via `t('key')` / `cat_label()` rather than hardcoding text in templates or `jsonify`. Console `print()` prompts, **Gemini prompt strings**, and **DB category values** stay Korean (engine internals).
