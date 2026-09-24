import os
import io
import re
import json
import time
import uuid
import shutil
import threading
from datetime import date, timedelta
from itertools import groupby

from .config import load_env, user_config_dir
load_env()

from flask import (Flask, render_template, request, jsonify, redirect, url_for,
                   send_file, send_from_directory)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import NotFound

from .db import get_db, init_db, get_setting, set_setting
from .dates import normalize_date
from .exchange import fetch_rate
from .ocr import gemini as gemini_ocr
from .sync import generic as generic_sync
from . import auto_rules
from .ai_items import clean_items, Refiner

from PIL import Image
import openpyxl
import pandas as pd

# Personal data such as receipt images is kept in a user folder outside the project (~/.kakeibo).
DATA_DIR   = user_config_dir()
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads', 'receipts')
TEMP_DIR   = os.path.join(DATA_DIR, 'temp')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# How many receipt photos in a row may fail with a busy model (503) before the
# analyze run gives up. Below this, a busy photo is just skipped and retried later.
MAX_BUSY_PHOTOS = 2

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
app.jinja_env.globals['get_setting'] = get_setting

from . import i18n
from .i18n import t
app.jinja_env.globals['t'] = i18n.t
app.jinja_env.globals['cat_label'] = i18n.cat_label
app.jinja_env.globals['current_lang'] = i18n.current_lang
app.jinja_env.globals['category_map'] = i18n.category_map


@app.context_processor
def _nav_counts():
    # Nav badge. Counted here (one indexed COUNT) instead of the old main.js, which
    # downloaded the whole /uncategorized page on every page load to scrape a number.
    try:
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) FROM transactions WHERE deleted_at IS NULL "
                         "AND (major IS NULL OR major='')").fetchone()[0]
        conn.close()
    except Exception:
        n = 0
    return {'uncategorized_count': n}


# ── helpers ──────────────────────────────────────────────────────────────────

# Staging session ids are always uuid4 (hex or dashed). Anything else would be joined
# into a filesystem path, so it is rejected — '..' would otherwise reach the data dir.
_SESSION_RE = re.compile(r'^[0-9a-fA-F-]{32,36}$')


def _valid_session(session_id) -> bool:
    return bool(session_id) and bool(_SESSION_RE.match(str(session_id)))


def _session_file(session_id):
    """Path of a staging session's JSON file, or None for a malformed id."""
    if not _valid_session(session_id):
        return None
    return os.path.join(TEMP_DIR, f"{session_id}.json")


def compress_image(src: str, dst: str, max_dim=1080, quality=65):
    # Same upright/shrink step the OCR uses, so the stored copy is not sideways either
    gemini_ocr.load_photo(src, max_dim).save(dst, 'JPEG', quality=quality)


def rate_for_date(date_str: str, conn):
    row = conn.execute("SELECT rate FROM exchange_rates WHERE date=?", (date_str,)).fetchone()
    return row['rate'] if row else None


def calc_krw(jpy, rate):
    if jpy is not None and rate:
        return round(jpy * rate / 100, 0)
    return None


def _tx_matches(tx, f) -> bool:
    """Whether a transaction passes the list filters (year/month/type/major/source/...)."""
    d = tx['date'] or ''
    if f.get('year') and f.get('month'):
        try:
            if d[:7] != f"{f['year']}-{int(f['month']):02d}":
                return False
        except ValueError:
            return False
    elif f.get('year') and not d.startswith(f"{f['year']}-"):
        return False
    if f.get('type') and tx['type'] != f['type']:
        return False
    if f.get('major') and tx['major'] != f['major']:
        return False
    if f.get('source') and tx['source'] != f['source']:
        return False
    if f.get('uncategorized') and tx['major']:
        return False
    q = (f.get('q') or '').strip().lower()
    if q and q not in f"{tx['shop'] or ''} {tx['item'] or ''} {tx['note'] or ''}".lower():
        return False
    return True


def get_transactions_list(filters=None):
    """Live rows with running balances, oldest first.

    Balances are always accumulated over the WHOLE ledger and the filters only pick
    which rows are returned - filtering in SQL first made e.g. the expense-only view
    show balances that ignored every income row.
    """
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE deleted_at IS NULL ORDER BY date ASC, id ASC"
    ).fetchall()]
    conn.close()

    jpy_bal = float(get_setting('initial_jpy_balance', '0') or 0)
    krw_bal = float(get_setting('initial_krw_balance', '0') or 0)
    for tx in rows:
        jpy_bal += (tx['jpy_amount'] or 0)
        krw_bal += (tx['krw_amount'] or 0)
        tx['jpy_balance'] = jpy_bal
        tx['krw_balance'] = krw_bal

    if filters:
        rows = [tx for tx in rows if _tx_matches(tx, filters)]
    return rows


def _ledger_summary():
    """Headline numbers for the top of the list: balances now + this month's flow."""
    ym = date.today().strftime('%Y-%m')
    conn = get_db()
    row = conn.execute("""
        SELECT COALESCE(SUM(jpy_amount), 0) AS jpy, COALESCE(SUM(krw_amount), 0) AS krw,
               COALESCE(SUM(CASE WHEN type='지출' AND substr(date,1,7)=? THEN jpy_amount END), 0) AS expense,
               COALESCE(SUM(CASE WHEN type='수입' AND substr(date,1,7)=? THEN jpy_amount END), 0) AS income,
               COALESCE(SUM(CASE WHEN major IS NULL OR major='' THEN 1 END), 0) AS uncategorized
        FROM transactions WHERE deleted_at IS NULL
    """, (ym, ym)).fetchone()
    conn.close()
    return {
        'ym': ym,
        'jpy': float(get_setting('initial_jpy_balance', '0') or 0) + row['jpy'],
        'krw': float(get_setting('initial_krw_balance', '0') or 0) + row['krw'],
        'expense': row['expense'], 'income': row['income'],
        'net': row['income'] + row['expense'],
        'uncategorized': row['uncategorized'],
    }


def _receipt_units(rows):
    """Convert rows within a month into display units. Receipts (source=receipt) are grouped by (date, shop).

    - Two or more receipt items with the same (date, shop) → {'type':'group', rows, total, thumb}
    - If there is only one, it is demoted to an ordinary single row → {'type':'single', tx}
    - Non-receipt transactions → {'type':'single', tx}
    Even when grouped, the original rows (items) are kept intact, so editing/deleting/balance still work per item.
    """
    units, groups = [], {}
    for tx in rows:
        if tx.get('source') == 'receipt':
            # Group by receipt_id (fall back to date+shop if absent)
            key = tx.get('receipt_id') or ('ds', tx['date'], tx.get('shop') or '')
            u = groups.get(key)
            if u is None:
                u = {'type': 'group', 'date': tx['date'], 'shop': tx.get('shop') or '',
                     'rows': [], 'total': 0.0, 'thumb': ''}
                groups[key] = u
                units.append(u)
            u['rows'].append(tx)
            u['total'] += tx['jpy_amount'] or 0
            if not u['thumb'] and tx.get('receipt_img'):
                u['thumb'] = tx['receipt_img']
        else:
            units.append({'type': 'single', 'tx': tx})

    # Single-item receipts are also kept as groups (receipt header style) — unified, just without the arrow.
    return units


# ── pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('transactions'))


def _transport_reminder():
    """Show a reminder if it is the weekend (Sat/Sun) but no transport transaction has been entered this week. From Saturday through that week's Sunday.

    Matches when the category name starts with '교통' (e.g. 교통 / 교통비 / 교통/이동).
    """
    today = date.today()
    if today.weekday() < 5:  # 0=Mon ... 4=Fri → no reminder on weekdays
        return None
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    conn = get_db()
    row = conn.execute("""
        SELECT COUNT(*) AS c FROM transactions
        WHERE minor LIKE '교통%' AND deleted_at IS NULL
              AND date BETWEEN ? AND ?
    """, (monday.isoformat(), sunday.isoformat())).fetchone()
    conn.close()
    if row['c'] > 0:
        return None
    return {'monday': monday.isoformat(), 'sunday': sunday.isoformat()}


@app.route('/transactions')
def transactions():
    f = {k: v for k, v in request.args.items() if v}
    txs = get_transactions_list(f or None)

    grouped = {}
    for k, g in groupby(txs, key=lambda tx: tx['date'][:7]):
        items = list(g)
        expense = sum(tx['jpy_amount'] or 0 for tx in items if tx['type'] == '지출')
        income = sum(tx['jpy_amount'] or 0 for tx in items if tx['type'] == '수입')
        grouped[k] = {
            'units': _receipt_units(items),
            'expense': expense, 'income': income, 'net': income + expense,
            'count': len(items),
        }
    # Assign a page-wide unique id to each receipt group (for collapse/expand toggling)
    gid = 0
    for k in grouped:
        for u in grouped[k]['units']:
            if u['type'] == 'group':
                gid += 1
                u['gid'] = gid

    conn = get_db()
    ym_rows = conn.execute(
        "SELECT DISTINCT substr(date,1,7) as ym FROM transactions "
        "WHERE deleted_at IS NULL ORDER BY ym DESC"
    ).fetchall()
    majors = conn.execute(
        "SELECT DISTINCT major FROM categories WHERE type='지출' ORDER BY major"
    ).fetchall()
    conn.close()

    from . import config
    return render_template('transactions.html',
        grouped=grouped,
        year_months=[r['ym'] for r in ym_rows],
        majors=[r['major'] for r in majors],
        filters=f,
        transport_reminder=_transport_reminder(),
        summary=_ledger_summary(),
        first_run=(get_setting('setup_done', '') != '1'),
        initial_jpy=get_setting('initial_jpy_balance', '0'),
        initial_krw=get_setting('initial_krw_balance', '0'),
        gmail=config.gmail_status(),
        gmail_reminder_dismissed=(get_setting('gmail_reminder_dismissed', '') == '1'),
    )


@app.route('/uncategorized')
def uncategorized():
    conn = get_db()
    txs = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE (major IS NULL OR major='') AND deleted_at IS NULL ORDER BY date DESC"
    ).fetchall()]
    cats = [dict(c) for c in conn.execute(
        "SELECT * FROM categories ORDER BY type, major, minor, sub"
    ).fetchall()]
    conn.close()
    return render_template('uncategorized.html', txs=txs, categories=cats)


@app.route('/trash')
def trash():
    conn = get_db()
    txs = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()]
    conn.close()
    return render_template('trash.html', txs=txs)


def _drop_unused_images(conn, images) -> int:
    """Delete receipt files no surviving row points at. Returns how many were removed.

    A whole receipt group shares one `receipt_img`, so a file may only go once its
    last row is gone — hence the re-check against the table after the DELETE.
    """
    removed = 0
    for img in {i for i in images if i}:
        still = conn.execute(
            "SELECT 1 FROM transactions WHERE receipt_img=? LIMIT 1", (img,)).fetchone()
        if still:
            continue
        path = os.path.join(DATA_DIR, img.replace('/', os.sep))
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _images_of(conn, where: str, params=()) -> list:
    """receipt_img values of the rows a DELETE is about to remove."""
    return [r[0] for r in conn.execute(
        f"SELECT DISTINCT receipt_img FROM transactions WHERE {where}", params).fetchall() if r[0]]


def purge_orphan_images() -> int:
    """Startup sweep: drop receipt files no row references at all (older versions of
    this app deleted rows without their images). Returns how many were removed."""
    conn = get_db()
    try:
        used = {r[0] for r in conn.execute(
            "SELECT DISTINCT receipt_img FROM transactions "
            "WHERE receipt_img != '' AND receipt_img IS NOT NULL").fetchall() if r[0]}
    finally:
        conn.close()
    used_names = {u.split('/')[-1] for u in used}
    removed = 0
    try:
        for name in os.listdir(UPLOAD_DIR):
            if name in used_names:
                continue
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


@app.route('/api/transactions/<int:tx_id>/restore', methods=['POST'])
def api_restore_tx(tx_id):
    conn = get_db()
    conn.execute("UPDATE transactions SET deleted_at=NULL WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tx_id>/destroy', methods=['DELETE'])
def api_destroy_tx(tx_id):
    conn = get_db()
    imgs = _images_of(conn, "id=?", (tx_id,))
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    _drop_unused_images(conn, imgs)     # only files no remaining row points at
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/trash/empty', methods=['DELETE'])
def api_empty_trash():
    conn = get_db()
    imgs = _images_of(conn, "deleted_at IS NOT NULL")
    conn.execute("DELETE FROM transactions WHERE deleted_at IS NOT NULL")
    conn.commit()
    _drop_unused_images(conn, imgs)
    conn.close()
    return jsonify({'ok': True})


@app.route('/receipt-review/<session_id>')
def receipt_review(session_id):
    sf = _session_file(session_id)
    if not sf or not os.path.exists(sf):
        return redirect(url_for('transactions'))
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    conn = get_db()
    cats = [dict(c) for c in conn.execute(
        "SELECT * FROM categories ORDER BY type, major, minor, sub"
    ).fetchall()]
    conn.close()
    return render_template('receipt_review.html',
        session_id=session_id, results=data['results'], categories=cats)


@app.route('/temp/<session_id>/<filename>')
def temp_image(session_id, filename):
    # send_from_directory refuses a filename that resolves outside the session folder;
    # the folder itself comes from the URL, so the session id must be checked first.
    if not _valid_session(session_id):
        return '', 404
    try:
        return send_from_directory(os.path.join(TEMP_DIR, session_id), filename,
                                   mimetype='image/jpeg')
    except NotFound:
        return '', 404


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve stored receipt images (relative to BASE_DIR/uploads/).

    The receipt_img column is stored as 'uploads/receipts/{uuid}.jpg' → referenced in templates as /{{ t.receipt_img }}.
    """
    try:
        return send_from_directory(os.path.join(DATA_DIR, 'uploads'), filename)
    except NotFound:
        return '', 404


@app.route('/stats')
def stats():
    y = request.args.get('year',  date.today().year,  type=int)
    m = request.args.get('month', date.today().month, type=int)
    return render_template('stats.html', year=y, month=m)


@app.route('/budget')
def budget():
    y = request.args.get('year',  date.today().year,  type=int)
    m = request.args.get('month', date.today().month, type=int)

    conn = get_db()
    budgets = [dict(b) for b in conn.execute(
        "SELECT * FROM budget WHERE year=? AND month=? ORDER BY name", (y, m)
    ).fetchall()]
    cats = [dict(r) for r in conn.execute(
        "SELECT DISTINCT major, minor, sub FROM categories WHERE type='지출' ORDER BY major, minor, sub"
    ).fetchall()]
    # All expense transactions for the given month
    txs = [dict(r) for r in conn.execute("""
        SELECT shop, item, major, minor, category, jpy_amount FROM transactions
        WHERE type='지출' AND deleted_at IS NULL
              AND date BETWEEN ? AND ?
    """, (f"{y}-{m:02d}-01", f"{y}-{m:02d}-31")).fetchall()]
    conn.close()

    def _match(b, t):
        ms  = (b.get('match_shop')  or '').strip().lower()
        mi  = (b.get('match_item')  or '').strip().lower()
        mmj = (b.get('match_major') or '').strip()
        mmn = (b.get('match_minor') or '').strip()
        msb = (b.get('match_sub')   or '').strip()
        if not (ms or mi or mmj or mmn or msb):
            return False
        if ms  and ms  not in (t.get('shop')  or '').lower(): return False
        if mi  and mi  not in (t.get('item')  or '').lower(): return False
        if mmj and mmj != (t.get('major')    or ''):          return False
        if mmn and mmn != (t.get('minor')    or ''):          return False
        if msb and msb != (t.get('category') or ''):          return False
        return True

    budget_data = []
    total_budget = 0.0
    total_actual = 0.0
    for b in budgets:
        actual = sum((t['jpy_amount'] or 0) for t in txs if _match(b, t))
        budget_data.append({**b,
            'actual': actual,
            'exceeded': abs(actual) > b['amount'] if b['amount'] else False,
        })
        total_budget += (b['amount'] or 0)
        total_actual += abs(actual)

    totals = {
        'budget':   total_budget,
        'actual':   total_actual,
        'remain':   total_budget - total_actual,
        'exceeded': total_actual > total_budget and total_budget > 0,
    }

    return render_template('budget.html', year=y, month=m,
        budget_data=budget_data, totals=totals, categories=cats)


@app.route('/settings')
def settings():
    conn = get_db()
    cats = [dict(c) for c in conn.execute(
        "SELECT * FROM categories ORDER BY type, major, minor, sub"
    ).fetchall()]
    conn.close()
    from . import config
    from .ocr import gemini
    return render_template('settings.html',
        categories=cats,
        initial_jpy=get_setting('initial_jpy_balance', '0'),
        initial_krw=get_setting('initial_krw_balance', '0'),
        gmail=config.gmail_status(),
        default_senders=", ".join(generic_sync._DEFAULT_SENDERS),
        default_keywords=", ".join(generic_sync._DEFAULT_KEYWORDS),
        ai_models=gemini.AVAILABLE_MODELS,
        current_model=gemini.get_model_id(),
        current_fallback=gemini.get_fallback_model_id(),
        key_free=bool(os.environ.get('GEMINI_API_KEY', '').strip()),
        key_paid=gemini.paid_key_configured(),
    )


# ── api: transactions ─────────────────────────────────────────────────────────

@app.route('/api/transactions/<int:tx_id>', methods=['GET'])
def api_get_tx(tx_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))


@app.route('/api/transactions', methods=['POST'])
def api_add_tx():
    d = request.json
    conn = get_db()
    rate = d.get('exchange_rate') or rate_for_date(d.get('date', ''), conn)
    jpy  = d.get('jpy_amount')
    krw  = d.get('krw_amount')
    cur  = conn.execute("""
        INSERT INTO transactions
        (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source)
        VALUES (:date,:type,:major,:minor,:category,:shop,:item,:jpy,:krw,:note,:rate,'manual')
    """, {**d, 'jpy': jpy, 'krw': krw, 'rate': rate})
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'ok': True})


@app.route('/api/transactions/<int:tx_id>', methods=['PUT'])
def api_update_tx(tx_id):
    d = request.json
    conn = get_db()
    rate = d.get('exchange_rate') or rate_for_date(d.get('date', ''), conn)
    jpy  = d.get('jpy_amount')
    krw  = d.get('krw_amount')
    conn.execute("""
        UPDATE transactions SET
            date=:date, type=:type, major=:major, minor=:minor, category=:category,
            shop=:shop, item=:item, jpy_amount=:jpy, krw_amount=:krw,
            note=:note, exchange_rate=:rate
        WHERE id=:id
    """, {**d, 'id': tx_id, 'jpy': jpy, 'krw': krw, 'rate': rate})
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tx_id>', methods=['DELETE'])
def api_delete_tx(tx_id):
    conn = get_db()
    conn.execute("UPDATE transactions SET deleted_at=datetime('now','localtime') WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/transactions/bulk-delete', methods=['POST'])
def api_bulk_delete():
    ids = request.json.get('ids', [])
    if not ids:
        return jsonify({'ok': False})
    conn = get_db()
    conn.execute(
        f"UPDATE transactions SET deleted_at=datetime('now','localtime') WHERE id IN ({','.join('?'*len(ids))})",
        ids
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted': len(ids)})


# ── api: receipts ─────────────────────────────────────────────────────────────

def _added_receipt_names(conn) -> set:
    """Set of original filenames of receipts already added to the ledger (not deleted) — used for duplicate detection."""
    return set(
        r[0] for r in conn.execute(
            "SELECT DISTINCT receipt_name FROM transactions "
            "WHERE receipt_name != '' AND receipt_name IS NOT NULL AND deleted_at IS NULL"
        ).fetchall() if r[0]
    )


@app.route('/api/receipts/stage', methods=['POST'])
def api_stage():
    """Step 1: Just receive and save the files, and mark those already added (duplicates) by filename.
    OCR (AI) is not called yet — only the ones the user selects on the confirmation screen are analyzed."""
    files = request.files.getlist('images')
    if not files:
        return jsonify({'error': 'No files'}), 400

    session_id  = str(uuid.uuid4())
    session_dir = os.path.join(TEMP_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    conn = get_db()
    added = _added_receipt_names(conn)
    conn.close()

    staged = []
    for idx, f in enumerate(files):
        name = f.filename or ''
        ext = os.path.splitext(name)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
            continue
        # Save with an index prefix to avoid filename collisions within the batch
        # The original name is kept for display/dup-check, but what hits the disk is
        # sanitized — a filename may contain path separators or reserved words.
        safe = secure_filename(name) or f"image{ext}"
        saved = f"{idx}_{safe}"
        f.save(os.path.join(session_dir, saved))
        staged.append({'name': name, 'saved': saved, 'dup': name in added})

    if not staged:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({'error': t('msg.no_supported_images')}), 400

    sf = _session_file(session_id)
    with open(sf, 'w', encoding='utf-8') as fp:
        json.dump({'staged': staged, 'session_dir': session_dir}, fp, ensure_ascii=False)

    return jsonify({'session_id': session_id, 'staged': staged})


@app.route('/receipt-stage/<session_id>')
def receipt_stage(session_id):
    sf = _session_file(session_id)
    if not sf or not os.path.exists(sf):
        return redirect(url_for('transactions'))
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    return render_template('receipt_stage.html',
        session_id=session_id, staged=data.get('staged', []))


@app.route('/api/receipts/analyze', methods=['POST'])
def api_analyze():
    """Step 2: start OCR (AI) on the photos selected on the confirmation screen.

    Runs as a background job (one AI call per photo, plus retry waits when the model
    is busy, easily a minute or more); the page polls
    /api/receipts/analyze/status/<job_id> for per-photo progress.
    """
    d          = request.json or {}
    session_id = d.get('session_id')
    selected   = [s for s in d.get('selected', []) if s]   # 'saved' tokens

    sf = _session_file(session_id)
    if not sf or not os.path.exists(sf):
        return jsonify({'error': 'Session expired'}), 400
    if not selected:
        return jsonify({'error': t('receipt.select_one')}), 400

    now = time.time()
    with _mail_jobs_lock:
        # One run per upload batch: a double click must not analyze (and pay for) twice
        for jid, job in _mail_jobs.items():
            if (job.get('kind') == 'receipt' and job.get('status') == 'running'
                    and job.get('session_id') == session_id):
                return jsonify({'job_id': jid, 'already_running': True})
        _drop_old_jobs(now)
        job_id = uuid.uuid4().hex
        _mail_jobs[job_id] = {'kind': 'receipt', 'status': 'running', 'session_id': session_id,
                              'index': 0, 'total': len(selected), 'current': '',
                              'message': '', 'photos': {s: {'state': 'wait'} for s in selected},
                              'started_at': now}

    threading.Thread(target=_run_receipt_job, args=(job_id, session_id, selected),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/receipts/analyze/status/<job_id>')
def api_analyze_status(job_id):
    """Live progress of a receipt-analyze job (polled by the stage page)."""
    with _mail_jobs_lock:
        job = _mail_jobs.get(job_id)
        job = dict(job, photos=dict(job['photos'])) if job else None
    if not job or job.get('kind') != 'receipt':
        return jsonify({'error': t('msg.sync_job_gone')}), 404
    out = {k: job.get(k) for k in ('status', 'index', 'total', 'current', 'message', 'photos')}
    if job['status'] == 'done':
        out.update(job.get('result') or {})
        if (job.get('result') or {}).get('count'):
            out['review_url'] = url_for('receipt_review', session_id=job['session_id'])
    elif job['status'] == 'error':
        out['error'] = job.get('error')
    return jsonify(out)


def _run_receipt_job(job_id, session_id, selected):
    """Worker entry: the job must always leave 'running' (see _run_mail_sync)."""
    try:
        result = _analyze_receipts(job_id, session_id, set(selected))
        _mail_job_update(job_id, status='done', result=result, finished_at=time.time())
    except Exception as e:
        _mail_job_update(job_id, status='error', error=str(e), finished_at=time.time())


def _photo_state(job_id, saved, **state):
    with _mail_jobs_lock:
        job = _mail_jobs.get(job_id)
        if job is not None:
            job['photos'][saved] = state


def _analyze_receipts(job_id, session_id, selected) -> dict:
    """OCR the selected photos of a staged batch, reporting each one to the job."""
    sf = _session_file(session_id)
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    session_dir = data['session_dir']

    todo = [s for s in data.get('staged', []) if s['saved'] in selected]
    results = []
    quota = None
    overloaded = False
    model_error = False
    busy_streak = 0
    conn = get_db()
    refiner = Refiner(conn)     # history + scheme, loaded once per run
    conn.close()
    for i, s in enumerate(todo, 1):
        saved = s['saved']
        _mail_job_update(job_id, index=i, current=saved,
                         message=t('receipt.prog_analyzing', i=i, n=len(todo), name=s['name']))
        _photo_state(job_id, saved, state='run')
        orig = os.path.join(session_dir, saved)
        if not os.path.exists(orig):
            _photo_state(job_id, saved, state='error')
            continue
        compressed_name = f"{uuid.uuid4()}.jpg"
        try:
            compress_image(orig, os.path.join(session_dir, compressed_name))
        except (OSError, ValueError) as e:
            # Corrupt/unreadable photo: skip it rather than fail the whole batch
            # (and lose the photos already analyzed in this run).
            print(f"[receipt] cannot open image: {type(e).__name__}")  # ASCII only (cp949)
            _photo_state(job_id, saved, state='error')
            continue

        def on_retry(seconds, i=i):
            _mail_job_update(job_id, message=t('receipt.prog_retry', i=i, n=len(todo),
                                               s=int(round(seconds))))
        try:
            ocr = gemini_ocr.analyze_receipt(orig, on_retry=on_retry)
        except gemini_ocr.QuotaError as e:
            # Quota exceeded → skip this photo and the rest. Keep the originals (retry possible).
            quota = e
            os.remove(os.path.join(session_dir, compressed_name))
            _photo_state(job_id, saved, state='stopped')
            break
        except gemini_ocr.OverloadError:
            # Gemini is busy (503) even after retries. Skip just this photo — its
            # original is kept, so it stays selectable for another analyze run — and
            # try the next one. Only a run of busy photos means the model is down.
            busy_streak += 1
            os.remove(os.path.join(session_dir, compressed_name))
            _photo_state(job_id, saved, state='busy')
            if busy_streak >= MAX_BUSY_PHOTOS:
                overloaded = True
                break
            continue
        except gemini_ocr.ModelError:
            # The configured model no longer exists (404) → every photo would fail
            # the same way. Stop and point the user at the settings page.
            model_error = True
            os.remove(os.path.join(session_dir, compressed_name))
            _photo_state(job_id, saved, state='stopped')
            break
        os.remove(orig)
        busy_streak = 0
        # Sanitize (a blurred receipt often yields a wrong/missing year), then fix the
        # categories from the user's history and the category scheme.
        items = refiner.refine(clean_items(ocr['items']))
        items_sum = sum(it['amount'] for it in items)
        _photo_state(job_id, saved, state='ok' if items else 'empty', items=len(items))
        # status: ok (has items) / empty (OCR succeeded but 0 items -> make it explicit to the user)
        results.append({
            'image': compressed_name, 'name': s['name'], 'items': items,
            'status': 'ok' if items else 'empty',
            # Printed total vs. extracted lines: a mismatch means a missed/doubled line
            'total': ocr.get('total'),
            'mismatch': bool(ocr.get('total') and items and abs(items_sum) != ocr['total']),
        })

    # Photos left untouched by an early stop stay selectable for the next run
    with _mail_jobs_lock:
        job = _mail_jobs.get(job_id)
        if job is not None:
            for saved, st in job['photos'].items():
                if st.get('state') == 'wait':
                    st['state'] = 'stopped'

    # Append: a second run (after a quota stop) must not drop the first run's results.
    # Re-read the file so nothing written meanwhile is lost.
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    data['results'] = data.get('results', []) + results
    with open(sf, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)

    resp = {'session_id': session_id, 'count': len(data['results']), 'new': len(results)}
    if quota:
        limit = t('msg.quota.per_day') if quota.per_day else t('msg.quota.per_min')
        cont = t('msg.cont.tomorrow') if quota.per_day else t('msg.cont.later')
        resp['stopped'] = True
        resp['message'] = t('msg.receipt_quota_stopped', n=len(results), limit=limit, cont=cont)
    elif overloaded:
        resp['stopped'] = True
        resp['message'] = t('msg.receipt_overload_stopped', n=len(results))
    elif model_error:
        resp['stopped'] = True
        resp['message'] = t('msg.model_unavailable', model=gemini_ocr.get_model_id())
    return resp


@app.route('/api/receipts/confirm', methods=['POST'])
def api_confirm():
    d          = request.json
    session_id = d.get('session_id')
    items      = d.get('items', [])

    sf = _session_file(session_id)
    if not sf or not os.path.exists(sf):
        return jsonify({'error': 'Session expired'}), 400
    with open(sf, encoding='utf-8') as f:
        session_data = json.load(f)
    session_dir = session_data['session_dir']
    # One receipt (image) = one receipt_id. All items on that receipt share it.
    results = session_data.get('results', [])
    name_by_img = {r['image']: r.get('name', '') for r in results}       # original filename for duplicate detection
    rid_by_img  = {r['image']: uuid.uuid4().hex for r in results}         # receipt group id
    moved = set()

    conn = get_db()
    for item in items:
        img_temp     = item.get('image', '')
        receipt_name = name_by_img.get(img_temp, '')
        receipt_id   = rid_by_img.get(img_temp, '')
        receipt_img  = ''
        if img_temp:
            dst = os.path.join(UPLOAD_DIR, img_temp)
            src = os.path.join(session_dir, img_temp)
            if img_temp not in moved and os.path.exists(src):
                os.rename(src, dst)          # move the image only once per receipt
                moved.add(img_temp)
            if os.path.exists(dst):          # link the same path to every item on the same receipt
                receipt_img = f"uploads/receipts/{img_temp}"

        # Keep a date the user typed on the review page; only fill in a blank/broken one.
        dt   = normalize_date(item.get('date', ''), fix_year=False)
        rate = rate_for_date(dt, conn)
        jpy  = item.get('amount')
        conn.execute("""
            INSERT INTO transactions
            (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source,receipt_img,receipt_name,receipt_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (dt, item.get('type','지출'),
              item.get('major',''), item.get('minor',''), item.get('sub',''),
              item.get('shop',''), item.get('item',''),
              jpy, None, '', rate, 'receipt', receipt_img, receipt_name, receipt_id))
    conn.commit()
    conn.close()

    shutil.rmtree(session_dir, ignore_errors=True)
    os.remove(sf)
    return jsonify({'ok': True, 'saved': len(items)})


# ── api: paypay ───────────────────────────────────────────────────────────────

@app.route('/api/paypay/import', methods=['POST'])
def api_paypay():
    f = request.files.get('csv')
    if not f:
        return jsonify({'error': 'No file'}), 400
    try:
        df = pd.read_csv(io.BytesIO(f.read()), encoding='utf-8-sig')
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if '取引内容' not in df.columns:
        return jsonify({'error': t('msg.csv_format_error')}), 400

    # Column name compatibility (handling PayPay CSV format changes)
    col_date = '取引日時' if '取引日時' in df.columns else '取引日'
    col_shop = '店舗名／送金先' if '店舗名／送金先' in df.columns else '取引先'
    col_amt  = '金額（円）' if '金額（円）' in df.columns else '出金金額（円）'

    required = {col_date, col_shop, col_amt, '取引番号'}
    missing = required - set(df.columns)
    if missing:
        return jsonify({'error': t('msg.missing_columns', cols=missing)}), 400

    # Process only rows that have an outflow amount (includes 支払い, 請求書払い, etc.; チャージ is auto-excluded)
    def _has_outflow(v):
        s = str(v).replace(',', '').replace('¥', '').strip()
        if s in ('', '-', 'nan', 'None'):
            return False
        try:
            return float(s) > 0
        except ValueError:
            return False

    payments = df[df[col_amt].apply(_has_outflow)].copy()
    if payments.empty:
        return jsonify({'count': 0, 'message': t('msg.no_withdrawals')})

    conn = get_db()
    existing = set(r[0] for r in conn.execute(
        "SELECT paypay_id FROM transactions WHERE paypay_id != '' AND deleted_at IS NULL"
    ).fetchall())

    new_rows = []
    for _, pay in payments.iterrows():
        pid = str(pay['取引番号']).strip()
        if pid in existing:
            continue
        dt = str(pay[col_date]).strip()[:10].replace('/', '-')
        shop = str(pay[col_shop]).strip()
        raw_amt = str(pay[col_amt]).replace(',', '').replace('¥', '').strip()
        try:
            amount = -abs(float(raw_amt))
        except ValueError:
            amount = 0.0
        rate = rate_for_date(dt, conn)
        row = {
            'date': dt, 'type': '지출', 'major': '', 'minor': '', 'category': '',
            'shop': shop, 'item': '(세부내역 없음)',
            'jpy_amount': amount, 'krw_amount': None,
            'note': '', 'exchange_rate': rate, 'source': 'paypay', 'paypay_id': pid,
        }
        auto_rules.apply(row, source='paypay')
        new_rows.append(row)
        existing.add(pid)

    if new_rows:
        conn.executemany("""
            INSERT INTO transactions
            (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source,paypay_id)
            VALUES (:date,:type,:major,:minor,:category,:shop,:item,:jpy_amount,:krw_amount,:note,:exchange_rate,:source,:paypay_id)
        """, new_rows)
        conn.commit()
    conn.close()
    return jsonify({'count': len(new_rows), 'message': t('msg.added_n', n=len(new_rows))})


# ── api: email sync (AI, vendor-agnostic) ─────────────────────────────────────

# ── mail sync jobs ────────────────────────────────────────────────────────────
# The sync runs for minutes (one AI call per purchase mail), so it runs in a
# background thread and the page polls the job for a live progress readout.
_mail_jobs = {}                  # job_id -> {status, stage, message, index, total, ...}
                                 # (receipt-analyze jobs live here too, with kind='receipt')
_mail_jobs_lock = threading.Lock()
_MAIL_JOB_TTL = 900              # forget a finished job after 15 min


def _drop_old_jobs(now):
    """Forget finished jobs nobody polled for a while. Call with _mail_jobs_lock held."""
    for jid in [k for k, v in _mail_jobs.items()
                if v.get('finished_at') and now - v['finished_at'] > _MAIL_JOB_TTL]:
        del _mail_jobs[jid]


def _mail_job_update(job_id, **fields):
    with _mail_jobs_lock:
        job = _mail_jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _short(text, n=45):
    text = (text or '').strip()
    return text if len(text) <= n else text[:n] + '...'


def _progress_message(ev):
    """Localized one-liner describing what the sync is doing right now."""
    stage = ev.get('stage')
    if stage == 'connect':
        return t('sync.connecting')
    if stage == 'list':
        return t('sync.listing', days=ev.get('days', ''))
    if stage == 'read':
        return t('sync.reading', i=ev.get('index', 0), n=ev.get('total', 0))
    if stage == 'skip':
        key = 'sync.skipping_known' if ev.get('reason') == 'already' else 'sync.skipping'
        return t(key, i=ev.get('index', 0), n=ev.get('total', 0))
    if stage == 'analyze':
        return t('sync.analyzing', i=ev.get('index', 0), n=ev.get('total', 0),
                 subject=_short(ev.get('subject')))
    if stage == 'parsed':
        return t('sync.parsed', k=ev.get('items', 0), subject=_short(ev.get('subject')))
    if stage == 'quota':
        return t('sync.quota_hit')
    if stage == 'overload':
        # A single busy mail is skipped; only a streak actually stops the sync
        k = ev.get('busy_streak', 0)
        if k and k < generic_sync.MAX_BUSY_STREAK:
            return t('sync.overload_skip', k=k)
        return t('sync.overload_hit')
    if stage == 'model_error':
        return t('sync.model_error_hit')
    if stage == 'model_switch':
        return t('sync.model_switch', model=ev.get('model', ''))
    if stage == 'tier_switch':
        return t('sync.tier_paid')
    if stage == 'done':
        return t('sync.finishing')
    return ''


def _run_mail_sync(job_id, query):
    """Background worker entry. Whatever goes wrong, the job must leave 'running' —
    otherwise api_email() keeps handing back this dead job and no sync can start
    again until the app restarts."""
    try:
        _mail_sync_job(job_id, query)
    except Exception as e:
        _mail_job_update(job_id, status='error', error=str(e), finished_at=time.time())


def _mail_sync_job(job_id, query):
    """Collect mails and stage them for the review page."""
    def on_progress(ev):
        fields = {'stage': ev.get('stage'), 'message': _progress_message(ev)}
        for k in ('index', 'total', 'found', 'scanned', 'skipped', 'known'):
            if k in ev:
                fields[k] = ev[k]
        _mail_job_update(job_id, **fields)

    conn = get_db()        # own connection: this runs off the request thread
    try:
        res = generic_sync.collect_generic(conn, query=query, on_progress=on_progress)
    finally:
        conn.close()

    if res.get('error'):
        _mail_job_update(job_id, status='error', error=res['error'], finished_at=time.time())
        return

    emails = res.get('emails', [])
    result = {
        'count': sum(len(e['items']) for e in emails),
        'scanned': res.get('scanned', 0),
        'skipped': res.get('skipped', 0),
        'known': res.get('known', 0),
    }
    session_id = None

    if not emails:
        # Nothing to review -> report a message, no review page
        if res.get('quota_exceeded'):
            quota = res['quota']
            limit = t('msg.quota.per_day') if quota.per_day else t('msg.quota.per_min')
            cont = t('msg.cont.tomorrow_sync') if quota.per_day else t('msg.cont.later_sync')
            result['message'] = t('msg.mail_quota_stopped', n=0, limit=limit, cont=cont,
                                  skipped=res.get('skipped', 0),
                                  total=res.get('total', 0), done=res.get('examined', 0),
                                  remaining=res.get('remaining', 0))
        elif res.get('overloaded'):
            result['message'] = t('msg.mail_overload_stopped', n=0,
                                  total=res.get('total', 0), done=res.get('examined', 0),
                                  remaining=res.get('remaining', 0))
        elif res.get('model_error'):
            result['message'] = t('msg.model_unavailable', model=gemini_ocr.get_model_id())
        else:
            result['message'] = t('msg.no_new_mail')
    else:
        # Stage the parsed items for the review page (same temp-session pattern as receipts)
        session_id = uuid.uuid4().hex
        os.makedirs(TEMP_DIR, exist_ok=True)
        payload = {
            'emails': emails,
            'scanned': res.get('scanned', 0),
            'skipped': res.get('skipped', 0),
            'quota_exceeded': res.get('quota_exceeded', False),
            'overloaded': res.get('overloaded', False),
            'model_error': bool(res.get('model_error')),
            'total': res.get('total', 0),
            'examined': res.get('examined', 0),
            'remaining': res.get('remaining', 0),
            'remaining_samples': res.get('remaining_samples', []),
        }
        with open(os.path.join(TEMP_DIR, f"{session_id}.json"), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        result['session_id'] = session_id

    _mail_job_update(job_id, status='done', result=result, session_id=session_id,
                     message=result.get('message', ''), finished_at=time.time())


@app.route('/api/email/sync', methods=['POST'])
def api_email():
    """Step 1: start the fetch + pre-filter + AI-parse run. Returns a job id to poll.

    The actual work happens in a thread (see _run_mail_sync); the client polls
    /api/email/sync/status/<job_id> to show progress and then follows review_url.
    """
    data = request.get_json(silent=True) or {}
    # Auto model mode is asked for right before the sync starts; remember the answer so
    # the setting and the settings page stay in sync (receipt OCR reads it too).
    if 'auto' in data:
        set_setting('gemini_auto_model', '1' if data.get('auto') else '0')
    now = time.time()
    with _mail_jobs_lock:
        # Only one sync at a time (it burns Gemini quota) — hand back the running one
        for jid, job in _mail_jobs.items():
            if job.get('kind', 'mail') == 'mail' and job.get('status') == 'running':
                return jsonify({'job_id': jid, 'already_running': True})
        _drop_old_jobs(now)
        job_id = uuid.uuid4().hex
        _mail_jobs[job_id] = {'status': 'running', 'stage': 'connect',
                              'message': t('sync.connecting'),
                              'index': 0, 'total': 0, 'found': 0,
                              'scanned': 0, 'skipped': 0, 'known': 0, 'started_at': now}

    threading.Thread(target=_run_mail_sync, args=(job_id, data.get('query') or None),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/email/sync/status/<job_id>')
def api_email_status(job_id):
    """Live progress of a mail-sync job (polled by the transactions page)."""
    with _mail_jobs_lock:
        job = dict(_mail_jobs.get(job_id) or {})
    if not job:
        return jsonify({'error': t('msg.sync_job_gone')}), 404

    out = {k: job.get(k) for k in
           ('status', 'stage', 'message', 'index', 'total', 'found', 'scanned',
            'skipped', 'known')}
    if job.get('status') == 'done':
        out.update(job.get('result') or {})
        if job.get('session_id'):
            out['review_url'] = url_for('email_review', session_id=job['session_id'])
    elif job.get('status') == 'error':
        out['error'] = job.get('error')
    return jsonify(out)


@app.route('/email-review/<session_id>')
def email_review(session_id):
    """Step 2: review the AI-parsed mail items and pick which to add."""
    sf = _session_file(session_id)
    if not sf or not os.path.exists(sf):
        return redirect(url_for('transactions'))
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    conn = get_db()
    cats = [dict(c) for c in conn.execute(
        "SELECT * FROM categories ORDER BY type, major, minor, sub"
    ).fetchall()]
    conn.close()
    return render_template('email_review.html',
        session_id=session_id, emails=data.get('emails', []),
        scanned=data.get('scanned', 0), skipped=data.get('skipped', 0),
        quota_exceeded=data.get('quota_exceeded', False),
        overloaded=data.get('overloaded', False),
        model_error=data.get('model_error', False),
        total=data.get('total', 0), examined=data.get('examined', 0),
        remaining=data.get('remaining', 0),
        remaining_samples=data.get('remaining_samples', []), categories=cats)


@app.route('/api/email/confirm', methods=['POST'])
def api_email_confirm():
    """Step 3: insert only the items the user selected on the review page."""
    d = request.json or {}
    session_id = d.get('session_id')
    items = d.get('items', [])
    conn = get_db()
    n = generic_sync.insert_email_items(conn, items)
    sf = _session_file(session_id)
    # Every mail on the review page counts as handled — including the ones whose items
    # the user left unchecked — so the next sync does not analyze them all over again.
    if sf and os.path.exists(sf):
        try:
            with open(sf, encoding='utf-8') as f:
                staged = json.load(f)
            generic_sync.mark_seen(
                conn, [e.get('gmail_id') for e in staged.get('emails', [])], 'reviewed')
        except (OSError, ValueError):
            pass
    conn.close()
    if sf and os.path.exists(sf):
        try:
            os.remove(sf)
        except OSError:
            pass
    return jsonify({'saved': n, 'message': t('msg.added_n', n=n)})


# ── api: Gmail credentials upload ─────────────────────────────────────────────

@app.route('/api/gmail/credentials', methods=['POST'])
def api_gmail_credentials():
    from . import config
    f = request.files.get('credentials')
    if not f:
        return jsonify({'error': t('msg.no_file')}), 400
    try:
        raw = f.read()
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        return jsonify({'error': t('msg.not_json')}), 400

    node = data.get('installed') or data.get('web')
    if not node or not node.get('client_id') or not node.get('client_secret'):
        return jsonify({'error': t('msg.not_oauth_client')}), 400

    os.makedirs(config.user_config_dir(), exist_ok=True)
    dest = config.user_path('credentials.json')
    with open(dest, 'wb') as out:
        out.write(raw)
    # If the client changes, the existing token is invalid → force re-authentication
    old_token = config.user_path('token.json')
    if os.path.exists(old_token):
        os.remove(old_token)
    return jsonify({'ok': True, 'message': t('msg.gmail_creds_saved')})


# ── api: exchange rates ───────────────────────────────────────────────────────

@app.route('/api/exchange-rates/fetch', methods=['POST'])
def api_fetch_rates():
    d     = request.json or {}
    try:
        year  = int(d.get('year')  or date.today().year)
        month = int(d.get('month') or date.today().month)
    except (TypeError, ValueError):
        return jsonify({'error': 'Bad year/month'}), 400

    conn = get_db()
    dates = conn.execute("""
        SELECT DISTINCT date FROM transactions
        WHERE date LIKE ? AND date NOT IN (SELECT date FROM exchange_rates)
        ORDER BY date
    """, (f"{year}-{month:02d}-%",)).fetchall()

    fetched = 0
    for row in dates:
        dt   = row['date']
        rate = fetch_rate(dt)
        if not rate:
            continue
        conn.execute("INSERT OR REPLACE INTO exchange_rates(date,rate) VALUES(?,?)", (dt, rate))
        # The rate is for reference only — only update the transaction's exchange_rate column, leave krw_amount untouched
        conn.execute(
            "UPDATE transactions SET exchange_rate=? WHERE date=?",
            (rate, dt)
        )
        fetched += 1

    conn.commit()
    conn.close()
    return jsonify({'fetched': fetched, 'message': t('msg.rates_updated', n=fetched)})


@app.route('/api/exchange-rates/<date_str>', methods=['PUT'])
def api_set_rate(date_str):
    rate = request.json.get('rate')
    if not rate:
        return jsonify({'error': 'No rate'}), 400
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO exchange_rates(date,rate) VALUES(?,?)", (date_str, float(rate)))
    # The rate is for reference only — only update the transaction's exchange_rate column, leave krw_amount untouched
    conn.execute("UPDATE transactions SET exchange_rate=? WHERE date=?", (float(rate), date_str))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── api: stats ────────────────────────────────────────────────────────────────

@app.route('/api/stats')
def api_stats():
    y = request.args.get('year',  date.today().year,  type=int)
    m = request.args.get('month', date.today().month, type=int)
    conn = get_db()

    monthly = conn.execute("""
        SELECT substr(date,1,7) as ym,
               SUM(CASE WHEN type='지출' THEN jpy_amount ELSE 0 END) as expense,
               SUM(CASE WHEN type='수입' THEN jpy_amount ELSE 0 END) as income
        FROM transactions WHERE date LIKE ? AND deleted_at IS NULL
        GROUP BY ym ORDER BY ym
    """, (f"{y}-%",)).fetchall()

    cat_expense = conn.execute("""
        SELECT minor, SUM(jpy_amount) as total
        FROM transactions
        WHERE type='지출' AND deleted_at IS NULL AND date BETWEEN ? AND ?
        GROUP BY minor ORDER BY total ASC
    """, (f"{y}-{m:02d}-01", f"{y}-{m:02d}-31")).fetchall()

    budgets = conn.execute(
        "SELECT name, amount FROM budget WHERE year=? AND month=?", (y, m)
    ).fetchall()
    conn.close()

    return jsonify({
        'monthly':     [dict(r) for r in monthly],
        'cat_expense': [dict(r) for r in cat_expense],
        'budgets':     [dict(r) for r in budgets],
    })


# ── api: budget ───────────────────────────────────────────────────────────────

@app.route('/api/budget', methods=['POST'])
def api_save_budget():
    d = request.json
    conn = get_db()
    for item in d['items']:
        conn.execute(
            "INSERT INTO budget(year,month,name,match_shop,match_item,"
            "match_major,match_minor,match_sub,amount) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (d['year'], d['month'], item.get('name','').strip(),
             item.get('match_shop','').strip(), item.get('match_item','').strip(),
             item.get('match_major','').strip(), item.get('match_minor','').strip(),
             item.get('match_sub','').strip(),
             item.get('amount', 0))
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/budget/<int:bid>', methods=['GET'])
def api_get_budget(bid):
    conn = get_db()
    row = conn.execute("SELECT * FROM budget WHERE id=?", (bid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))


@app.route('/api/budget/<int:bid>', methods=['PUT'])
def api_update_budget(bid):
    d = request.json
    conn = get_db()
    conn.execute(
        "UPDATE budget SET name=?, match_shop=?, match_item=?, "
        "match_major=?, match_minor=?, match_sub=?, amount=? WHERE id=?",
        (d.get('name','').strip(),
         d.get('match_shop','').strip(),
         d.get('match_item','').strip(),
         d.get('match_major','').strip(),
         d.get('match_minor','').strip(),
         d.get('match_sub','').strip(),
         d.get('amount', 0),
         bid)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/budget/<int:bid>', methods=['DELETE'])
def api_delete_budget(bid):
    conn = get_db()
    conn.execute("DELETE FROM budget WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/budget/copy-prev', methods=['POST'])
def api_copy_prev_budget():
    d = request.json
    y, m = int(d['year']), int(d['month'])
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    conn = get_db()
    rows = conn.execute(
        "SELECT name, match_shop, match_item, match_major, match_minor, match_sub, amount "
        "FROM budget WHERE year=? AND month=?",
        (py, pm)
    ).fetchall()
    copied = 0
    for r in rows:
        dup = conn.execute(
            "SELECT 1 FROM budget WHERE year=? AND month=? AND name=?",
            (y, m, r['name'])
        ).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO budget(year,month,name,match_shop,match_item,"
            "match_major,match_minor,match_sub,amount) VALUES(?,?,?,?,?,?,?,?,?)",
            (y, m, r['name'], r['match_shop'], r['match_item'],
             r['match_major'], r['match_minor'], r['match_sub'], r['amount'])
        )
        copied += 1
    conn.commit()
    conn.close()
    return jsonify({'copied': copied})


# ── api: categories ───────────────────────────────────────────────────────────

@app.route('/api/categories', methods=['GET'])
def api_get_cats():
    conn = get_db()
    cats = [dict(c) for c in conn.execute(
        "SELECT * FROM categories ORDER BY type, major, minor, sub"
    ).fetchall()]
    conn.close()
    return jsonify(cats)


@app.route('/api/categories', methods=['POST'])
def api_add_cat():
    d = request.json
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO categories(type,major,minor,sub) VALUES(?,?,?,?)",
        (d['type'], d['major'], d.get('minor',''), d.get('sub',''))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'ok': True})


@app.route('/api/categories/<int:cid>', methods=['DELETE'])
def api_del_cat(cid):
    conn = get_db()
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── api: settings ─────────────────────────────────────────────────────────────

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    for k, v in request.json.items():
        set_setting(k, v)
    return jsonify({'ok': True})


# ── api: export ───────────────────────────────────────────────────────────────

@app.route('/api/export/excel')
def api_export():
    start = request.args.get('start', '')
    end   = request.args.get('end', '')
    conn  = get_db()
    if start and end:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE deleted_at IS NULL AND date BETWEEN ? AND ? "
            "ORDER BY date, id",
            (start, end)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE deleted_at IS NULL ORDER BY date, id").fetchall()
    conn.close()

    txs = [dict(r) for r in rows]
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = t('excel.sheet')
    ws.append([t('excel.date'), t('excel.type'), t('excel.major'), t('excel.minor'),
                t('excel.category'), t('excel.shop'), t('excel.item'),
                t('excel.jpy_price'), t('excel.krw_price'), t('excel.jpy_balance'),
                t('excel.krw_balance'), t('excel.note'), t('excel.rate')])

    # The first balance row starts from the initial balance, like the transaction list.
    # (Loop variable is `tx`, not `t` — `t` is the translation function above.)
    init_jpy = float(get_setting('initial_jpy_balance', '0') or 0)
    init_krw = float(get_setting('initial_krw_balance', '0') or 0)
    for i, tx in enumerate(txs, start=2):
        ws.append([
            tx['date'], tx['type'], tx['major'], tx['minor'], tx['category'],
            tx['shop'], tx['item'], tx['jpy_amount'], tx['krw_amount'],
            f"=J{i-1}+H{i}" if i > 2 else init_jpy + (tx['jpy_amount'] or 0),
            f"=K{i-1}+I{i}" if i > 2 else init_krw + (tx['krw_amount'] or 0),
            tx['note'], tx['exchange_rate']
        ])
        ws.cell(i, 1).number_format = 'YYYY/MM/DD'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"kakeibo_{start or 'all'}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
