"""
Monthly recurring transactions (allowance, rent, subscriptions, ...).

Rules are personal data, so they live in ~/.kakeibo/recurring.json (never in the repo):

    [
      {"id": "rent", "type": "지출", "day": 27, "amount": 50000,
       "major": "주거", "minor": "월세", "sub": "그 외",
       "shop": "관리회사", "item": "월세", "start": "2026-01"}
    ]

- id      unique, never rename it: it is the dedup key in `recurring_log`
- type    '수입' or '지출'; amount is written positive, the sign follows the type
- day     day of month (clamped to the month's last day, so 31 = month end)
- start   first month 'YYYY-MM' (default: the current month); end: last month (optional)
- enabled false pauses a rule; keys starting with '_' are ignored (use them as comments)

`post_due()` inserts every occurrence whose date has arrived and that is not yet in
`recurring_log`. The log, not the ledger, decides: a posted row the user deletes
stays deleted. Runs at startup and on every transaction-list load.
"""
import calendar
import json
import os
import uuid
from datetime import date

from .config import user_path
from .db import get_db

RULE_FILE = user_path("recurring.json")
TYPES = ('수입', '지출')


def load_rules():
    if not os.path.exists(RULE_FILE):
        return []
    try:
        with open(RULE_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"[recurring] load error: {e}")
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _ym(s):
    """'2026-11' -> (2026, 11), else None."""
    try:
        y, m = str(s or '').split('-')[:2]
        y, m = int(y), int(m)
        return (y, m) if 1 <= m <= 12 else None
    except ValueError:
        return None


def _valid(rule):
    """Normalized rule or None when it cannot be posted."""
    rid = str(rule.get('id') or '').strip()
    typ = str(rule.get('type') or '').strip()
    try:
        amount = abs(float(rule.get('amount')))
        day = min(max(int(rule.get('day') or 1), 1), 31)
    except (TypeError, ValueError):
        return None
    if not rid or typ not in TYPES or not amount or rule.get('enabled') is False:
        return None
    return {**rule, 'id': rid, 'type': typ, 'day': day,
            'jpy': -amount if typ == '지출' else amount}


def _occurrences(rule, today):
    """(ym, date) of every month from start to today whose posting day has come."""
    y, m = _ym(rule.get('start')) or (today.year, today.month)
    end = _ym(rule.get('end'))
    while (y, m) <= (today.year, today.month) and (end is None or (y, m) <= end):
        d = date(y, m, min(rule['day'], calendar.monthrange(y, m)[1]))
        if d <= today:
            yield f"{y}-{m:02d}", d.isoformat()
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def post_due(today=None) -> int:
    """Insert the occurrences that are due and not yet posted. Returns how many."""
    rules = [r for r in (_valid(x) for x in load_rules()) if r]
    if not rules:
        return 0
    today = today or date.today()
    conn = get_db()
    posted = 0
    try:
        for r in rules:
            for ym, dt in _occurrences(r, today):
                if conn.execute("SELECT 1 FROM recurring_log WHERE rule_id=? AND ym=?",
                                (r['id'], ym)).fetchone():
                    continue
                rate = conn.execute("SELECT rate FROM exchange_rates WHERE date=?", (dt,)).fetchone()
                cur = conn.execute("""
                    INSERT INTO transactions
                    (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,'recurring')
                """, (dt, r['type'], r.get('major', ''), r.get('minor', ''), r.get('sub', ''),
                      r.get('shop', ''), r.get('item', '') or r['id'], r['jpy'], None,
                      r.get('note', ''), rate['rate'] if rate else None))
                conn.execute("INSERT INTO recurring_log(rule_id, ym, tx_id, posted_at) "
                             "VALUES (?,?,?,datetime('now','localtime'))", (r['id'], ym, cur.lastrowid))
                posted += 1
        conn.commit()
    finally:
        conn.close()
    return posted


def save_rules(rules):
    """Write the whole file (temp file + replace, so a crash never leaves half a JSON)."""
    tmp = RULE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RULE_FILE)


# Fields the settings dialog may write; anything else in a rule (e.g. "_comment") is kept.
_FIELDS = ('type', 'day', 'amount', 'major', 'minor', 'sub', 'shop', 'item', 'start', 'end', 'enabled')


def upsert(data):
    """Add a rule (no id) or update the rule with that id. Returns (rule, error)."""
    rule = {k: data.get(k) for k in _FIELDS if k in data}
    for k in ('major', 'minor', 'sub', 'shop', 'item', 'start', 'end'):
        if k in rule:
            rule[k] = str(rule[k] or '').strip()
    for k in ('start', 'end'):
        if rule.get(k) and not _ym(rule[k]):
            return None, k
    rules = load_rules()
    rid = str(data.get('id') or '').strip()
    if rid:
        for i, r in enumerate(rules):
            if r.get('id') == rid:
                merged = {**r, **rule}
                if not _valid({**merged, 'enabled': True}):
                    return None, 'invalid'
                rules[i] = merged
                save_rules(rules)
                return merged, None
        return None, 'not_found'
    rule['id'] = 'r' + uuid.uuid4().hex[:8]
    if not _valid({**rule, 'enabled': True}):
        return None, 'invalid'
    rules.append(rule)
    save_rules(rules)
    return rule, None


def delete(rid):
    """Drop a rule. Rows it already posted stay in the ledger (and in recurring_log)."""
    rules = load_rules()
    keep = [r for r in rules if r.get('id') != rid]
    if len(keep) != len(rules):
        save_rules(keep)
    return len(keep) != len(rules)


def describe():
    """Rules for the settings page, with whether each one is usable."""
    out = []
    for raw in load_rules():
        r = _valid(raw)
        out.append({
            'raw': {k: raw.get(k) for k in ('id',) + _FIELDS},
            'id': raw.get('id', ''), 'ok': r is not None,
            'paused': raw.get('enabled') is False,
            'type': raw.get('type', ''), 'day': raw.get('day', ''),
            'amount': r['jpy'] if r else raw.get('amount'),
            'label': ' / '.join(dict.fromkeys(
                x for x in (raw.get('minor'), raw.get('item') or raw.get('shop')) if x)),
            'start': raw.get('start', ''), 'end': raw.get('end', ''),
        })
    return out
