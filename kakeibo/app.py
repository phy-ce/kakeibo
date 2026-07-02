import os
import io
import json
import uuid
import shutil
from datetime import date, timedelta
from itertools import groupby

from .config import load_env, user_config_dir
load_env()

from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file

from .db import get_db, init_db, get_setting, set_setting
from .exchange import fetch_rate
from .ocr import gemini as gemini_ocr
from .sync import generic as generic_sync
from . import auto_rules

from PIL import Image
import openpyxl
import pandas as pd

# Personal data such as receipt images is kept in a user folder outside the project (~/.kakeibo).
DATA_DIR   = user_config_dir()
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads', 'receipts')
TEMP_DIR   = os.path.join(DATA_DIR, 'temp')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
app.jinja_env.globals['get_setting'] = get_setting

from . import i18n
from .i18n import t
app.jinja_env.globals['t'] = i18n.t
app.jinja_env.globals['cat_label'] = i18n.cat_label
app.jinja_env.globals['current_lang'] = i18n.current_lang
app.jinja_env.globals['category_map'] = i18n.category_map


# ── helpers ──────────────────────────────────────────────────────────────────

def compress_image(src: str, dst: str, max_dim=1080, quality=65):
    img = Image.open(src)
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    img.save(dst, 'JPEG', quality=quality)


def rate_for_date(date_str: str, conn):
    row = conn.execute("SELECT rate FROM exchange_rates WHERE date=?", (date_str,)).fetchone()
    return row['rate'] if row else None


def calc_krw(jpy, rate):
    if jpy is not None and rate:
        return round(jpy * rate / 100, 0)
    return None


def get_transactions_list(filters=None):
    conn = get_db()
    where, params = [], []

    if filters:
        if filters.get('year') and filters.get('month'):
            where.append("date BETWEEN ? AND ?")
            params += [f"{filters['year']}-{int(filters['month']):02d}-01",
                       f"{filters['year']}-{int(filters['month']):02d}-31"]
        elif filters.get('year'):
            where.append("date LIKE ?")
            params.append(f"{filters['year']}-%")
        if filters.get('type'):
            where.append("type=?"); params.append(filters['type'])
        if filters.get('major'):
            where.append("major=?"); params.append(filters['major'])
        if filters.get('source'):
            where.append("source=?"); params.append(filters['source'])
        if filters.get('uncategorized'):
            where.append("(major IS NULL OR major='')")

    where.append("(deleted_at IS NULL)")
    sql = "SELECT * FROM transactions WHERE " + " AND ".join(where)
    sql += " ORDER BY date ASC, id ASC"

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()

    jpy_bal = float(get_setting('initial_jpy_balance', '0') or 0)
    krw_bal = float(get_setting('initial_krw_balance', '0') or 0)
    for t in rows:
        jpy_bal += (t['jpy_amount'] or 0)
        krw_bal += (t['krw_amount'] or 0)
        t['jpy_balance'] = jpy_bal
        t['krw_balance'] = krw_bal

    return rows


def _receipt_units(rows):
    """Convert rows within a month into display units. Receipts (source=receipt) are grouped by (date, shop).

    - Two or more receipt items with the same (date, shop) → {'type':'group', rows, total, thumb}
    - If there is only one, it is demoted to an ordinary single row → {'type':'single', tx}
    - Non-receipt transactions → {'type':'single', tx}
    Even when grouped, the original rows (items) are kept intact, so editing/deleting/balance still work per item.
    """
    units, groups = [], {}
    for t in rows:
        if t.get('source') == 'receipt':
            # Group by receipt_id (fall back to date+shop if absent)
            key = t.get('receipt_id') or ('ds', t['date'], t.get('shop') or '')
            u = groups.get(key)
            if u is None:
                u = {'type': 'group', 'date': t['date'], 'shop': t.get('shop') or '',
                     'rows': [], 'total': 0.0, 'thumb': ''}
                groups[key] = u
                units.append(u)
            u['rows'].append(t)
            u['total'] += t['jpy_amount'] or 0
            if not u['thumb'] and t.get('receipt_img'):
                u['thumb'] = t['receipt_img']
        else:
            units.append({'type': 'single', 'tx': t})

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
    for k, g in groupby(txs, key=lambda t: t['date'][:7]):
        items = list(g)
        grouped[k] = {
            'units': _receipt_units(items),
            'expense': sum(t['jpy_amount'] or 0 for t in items if t['type'] == '지출'),
            'income':  sum(t['jpy_amount'] or 0 for t in items if t['type'] == '수입'),
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
        "SELECT DISTINCT substr(date,1,7) as ym FROM transactions ORDER BY ym DESC"
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
    conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/trash/empty', methods=['DELETE'])
def api_empty_trash():
    conn = get_db()
    conn.execute("DELETE FROM transactions WHERE deleted_at IS NOT NULL")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/receipt-review/<session_id>')
def receipt_review(session_id):
    sf = os.path.join(TEMP_DIR, f"{session_id}.json")
    if not os.path.exists(sf):
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
    path = os.path.join(TEMP_DIR, session_id, filename)
    if not os.path.exists(path):
        return '', 404
    return send_file(path, mimetype='image/jpeg')


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve stored receipt images (relative to BASE_DIR/uploads/).

    The receipt_img column is stored as 'uploads/receipts/{uuid}.jpg' → referenced in templates as /{{ t.receipt_img }}.
    """
    full = os.path.join(DATA_DIR, 'uploads', filename)
    if not os.path.exists(full):
        return '', 404
    return send_file(full)


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
        saved = f"{idx}_{name}"
        f.save(os.path.join(session_dir, saved))
        staged.append({'name': name, 'saved': saved, 'dup': name in added})

    if not staged:
        shutil.rmtree(session_dir, ignore_errors=True)
        return jsonify({'error': t('msg.no_supported_images')}), 400

    sf = os.path.join(TEMP_DIR, f"{session_id}.json")
    with open(sf, 'w', encoding='utf-8') as fp:
        json.dump({'staged': staged, 'session_dir': session_dir}, fp, ensure_ascii=False)

    return jsonify({'session_id': session_id, 'staged': staged})


@app.route('/receipt-stage/<session_id>')
def receipt_stage(session_id):
    sf = os.path.join(TEMP_DIR, f"{session_id}.json")
    if not os.path.exists(sf):
        return redirect(url_for('transactions'))
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    return render_template('receipt_stage.html',
        session_id=session_id, staged=data.get('staged', []))


@app.route('/api/receipts/analyze', methods=['POST'])
def api_analyze():
    """Step 2: Run OCR (AI) analysis only on the photos selected on the confirmation screen."""
    d          = request.json or {}
    session_id = d.get('session_id')
    selected   = set(d.get('selected', []))   # list of 'saved' tokens

    sf = os.path.join(TEMP_DIR, f"{session_id}.json")
    if not os.path.exists(sf):
        return jsonify({'error': 'Session expired'}), 400
    with open(sf, encoding='utf-8') as f:
        data = json.load(f)
    session_dir = data['session_dir']

    results = []
    quota = None
    for s in data.get('staged', []):
        if s['saved'] not in selected:
            continue
        orig = os.path.join(session_dir, s['saved'])
        if not os.path.exists(orig):
            continue
        compressed_name = f"{uuid.uuid4()}.jpg"
        compress_image(orig, os.path.join(session_dir, compressed_name))
        try:
            items = gemini_ocr.analyze_receipt(orig)
        except gemini_ocr.QuotaError as e:
            # Quota exceeded → skip this photo and the rest. Keep the originals (retry possible).
            quota = e
            os.remove(os.path.join(session_dir, compressed_name))
            break
        os.remove(orig)
        # status: ok (has items) / empty (OCR succeeded but 0 items → make it explicit to the user)
        results.append({
            'image': compressed_name, 'name': s['name'], 'items': items,
            'status': 'ok' if items else 'empty',
        })

    data['results'] = results
    with open(sf, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)

    resp = {'session_id': session_id, 'count': len(results)}
    if quota:
        limit = t('msg.quota.per_day') if quota.per_day else t('msg.quota.per_min')
        cont = t('msg.cont.tomorrow') if quota.per_day else t('msg.cont.later')
        resp['quota_exceeded'] = True
        resp['message'] = t('msg.receipt_quota_stopped', n=len(results), limit=limit, cont=cont)
    return jsonify(resp)


@app.route('/api/receipts/confirm', methods=['POST'])
def api_confirm():
    d          = request.json
    session_id = d.get('session_id')
    items      = d.get('items', [])

    sf = os.path.join(TEMP_DIR, f"{session_id}.json")
    if not os.path.exists(sf):
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

        dt   = item.get('date', '')
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

@app.route('/api/email/sync', methods=['POST'])
def api_email():
    data = request.get_json(silent=True) or {}
    conn = get_db()
    result = generic_sync.sync_generic(conn, query=(data.get('query') or None))
    conn.close()
    return jsonify(result)


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
    year  = d.get('year',  date.today().year)
    month = d.get('month', date.today().month)

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
        FROM transactions WHERE date LIKE ? GROUP BY ym ORDER BY ym
    """, (f"{y}-%",)).fetchall()

    cat_expense = conn.execute("""
        SELECT minor, SUM(jpy_amount) as total
        FROM transactions
        WHERE type='지출' AND date BETWEEN ? AND ?
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
            "SELECT * FROM transactions WHERE date BETWEEN ? AND ? ORDER BY date, id",
            (start, end)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM transactions ORDER BY date, id").fetchall()
    conn.close()

    txs = [dict(r) for r in rows]
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = t('excel.sheet')
    ws.append([t('excel.date'), t('excel.type'), t('excel.major'), t('excel.minor'),
                t('excel.category'), t('excel.shop'), t('excel.item'),
                t('excel.jpy_price'), t('excel.krw_price'), t('excel.jpy_balance'),
                t('excel.krw_balance'), t('excel.note'), t('excel.rate')])

    for i, t in enumerate(txs, start=2):
        ws.append([
            t['date'], t['type'], t['major'], t['minor'], t['category'],
            t['shop'], t['item'], t['jpy_amount'], t['krw_amount'],
            f"=J{i-1}+H{i}" if i > 2 else (t['jpy_amount'] or 0),
            f"=K{i-1}+I{i}" if i > 2 else (t['krw_amount'] or 0),
            t['note'], t['exchange_rate']
        ])
        ws.cell(i, 1).number_format = 'YYYY/MM/DD'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"kakeibo_{start or 'all'}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
