"""Post-processing shared by every AI extraction (receipt OCR and mail parsing).

The model's JSON is a suggestion, not data: amounts arrive as strings, types are
misspelled, categories drift outside the user's scheme. Everything goes through
`clean_items()` and then a `Refiner`, so the review pages always start from rows
that can be saved as-is.

Category suggestions are improved without spending any AI quota:
  1. history  - the category the user gave the same shop+item (or an item that was
                always filed the same way) wins over the model's guess;
  2. scheme   - the model's guess is snapped onto the categories table: a wrong
                major is repaired from a known minor, an unknown value is cleared
                (so the review page flags it) and a lone sub is filled in.
"""
import re
import unicodedata

from .dates import normalize_date

EXPENSE, INCOME = '지출', '수입'

# Item names too generic to carry a category on their own (the same '送料' belongs
# to clothing on one order and to food on the next) - never matched by item alone.
_GENERIC_ITEMS = {'送料', '配送料', '手数料', '消費税', '値引', '値引き', '割引', 'クーポン',
                  'ポイント', '小計', '合計', 'shipping', 'tax', 'discount', 'fee',
                  '(세부내역 없음)'}

# IC card / wallet top-ups (PASMO history shows them as "+3,000 チャージ"). Money only
# moves into the card; the rides paid from it are the expenses, so a top-up is
# neither income nor spend. Matched on the NFKC key, so half-width ﾁｬｰｼﾞ is caught.
_TOPUP_RE = re.compile(r'チャージ|充電|충전|topup|top-up|recharge|autocharge')


def _to_int(v):
    """'¥1,200' / '-1200.0' / 1200 -> int, else None."""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    for ch in ',¥￥円 ':
        s = s.replace(ch, '')
    s = s.replace('−', '-').replace('－', '-')
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def clean_items(data, *, fallback_date='', default_shop='', fix_year=True) -> list:
    """Normalize raw model output into review-ready item dicts.

    Drops non-objects, rows without a name and zero/unreadable amounts. Returns
    [{date, type, major, minor, sub, shop, item, amount}].
    """
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    # A positive line (discount/coupon) among expense lines is part of that purchase,
    # not income - used when the model's own type is missing or garbled.
    has_expense = any(isinstance(d, dict) and d.get('type') == EXPENSE for d in data)
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        amount = _to_int(d.get('amount'))
        if not amount:
            continue
        name = str(d.get('item') or '').strip()
        if not name:
            continue
        typ = str(d.get('type') or '').strip()
        if (amount > 0 or typ == INCOME) and _is_topup(name, d.get('shop')):
            continue
        if typ not in (EXPENSE, INCOME):
            typ = INCOME if amount > 0 and not has_expense else EXPENSE
        note = str(d.get('note') or '').strip()
        if len(name) > LONG_NAME:
            # The model ignored the "short name" rule: keep the full title in the
            # note and cut the row label, so the list stays readable.
            note = note or name
            name = name[:SHORT_NAME].rstrip() + '...'
        out.append({
            'date':   normalize_date(d.get('date'), fix_year=fix_year, fallback=fallback_date),
            'type':   typ,
            'major':  str(d.get('major') or '').strip(),
            'minor':  str(d.get('minor') or '').strip(),
            'sub':    str(d.get('sub') or '').strip(),
            'shop':   str(d.get('shop') or '').strip() or default_shop,
            'item':   name,
            'amount': amount,
            'note':   note[:500],
        })
    return out


# Item names longer than LONG_NAME are cut to SHORT_NAME (full title -> note).
LONG_NAME = 60
SHORT_NAME = 40


def _key(s) -> str:
    """Match key: NFKC (full-width -> half-width), case- and space-insensitive."""
    s = unicodedata.normalize('NFKC', str(s or '')).lower()
    return re.sub(r'\s+', '', s)


def _is_topup(name, shop) -> bool:
    return bool(_TOPUP_RE.search(_key(name)) or _TOPUP_RE.search(_key(shop)))


_GENERIC_KEYS = {_key(g) for g in _GENERIC_ITEMS}


class Refiner:
    """Category fixer built once per batch (one analyze run / one mail sync)."""

    # How many recent categorized rows feed the history lookup.
    HISTORY_ROWS = 5000

    def __init__(self, conn):
        self.scheme = [(r['type'], r['major'], r['minor'] or '', r['sub'] or '')
                       for r in conn.execute("SELECT type, major, minor, sub FROM categories")]
        self._by_shop_item = {}
        by_item = {}
        rows = conn.execute(
            "SELECT type, shop, item, major, minor, category, source FROM transactions "
            "WHERE deleted_at IS NULL AND major != '' AND major IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (self.HISTORY_ROWS,)).fetchall()
        # Newest first: the first hit per key is the user's latest decision.
        for r in rows:
            cat = (r['type'], r['major'], r['minor'] or '', r['category'] or '')
            item = _key(r['item'])
            if not item:
                continue
            self._by_shop_item.setdefault((_key(r['shop']), item), cat)
            if item not in _GENERIC_KEYS and r['source'] != 'paypay':
                by_item.setdefault(item, set()).add(cat)
        # Item-only matches are trusted only when the user never filed it two ways.
        self._by_item = {k: next(iter(v)) for k, v in by_item.items() if len(v) == 1}

    def refine(self, items: list) -> list:
        """Apply history, then snap to the scheme. Mutates and returns `items`.

        Sets item['cat_from'] = 'history' when a past decision was reused, so the
        review page can say where the suggestion came from.
        """
        for it in items:
            hit = (self._by_shop_item.get((_key(it.get('shop')), _key(it.get('item'))))
                   or self._by_item.get(_key(it.get('item'))))
            if hit and hit[0] == it.get('type'):
                it['major'], it['minor'], it['sub'] = hit[1], hit[2], hit[3]
                it['cat_from'] = 'history'
            self._fit_scheme(it)
        return items

    def _fit_scheme(self, it) -> None:
        rows = [r for r in self.scheme if r[0] == it.get('type')] or self.scheme
        if not rows:
            return
        major, minor, sub = it.get('major', ''), it.get('minor', ''), it.get('sub', '')
        if any(r[1:] == (major, minor, sub) for r in rows):
            return

        majors = {r[1] for r in rows}
        if major not in majors and minor:
            # The model often gets the minor right and the major wrong (or blank):
            # repair it when the minor (+sub) points at exactly one major.
            cands = {r[1] for r in rows if r[2] == minor and (not sub or r[3] == sub)} \
                or {r[1] for r in rows if r[2] == minor}
            if len(cands) == 1:
                major = cands.pop()
        if major not in majors:
            major = minor = sub = ''
        minors = {r[2] for r in rows if r[1] == major}
        if minor not in minors:
            minor = sub = ''
        subs = sorted({r[3] for r in rows if r[1] == major and r[2] == minor} - {''})
        if sub not in subs:
            # A lone sub (the default scheme's '그 외') is not a real choice: fill it in.
            sub = subs[0] if minor and len(subs) == 1 else ''
        it['major'], it['minor'], it['sub'] = major, minor, sub


class DupFinder:
    """Flags extracted rows that look like a purchase the ledger already has.

    One order often sends several mails that each carry the full amount (order
    confirmation + shipment, or two shipments of a split order), and the shop is
    not always spelled the same ('zoff' / 'Zoffオンラインストア'). Such rows are
    flagged, not dropped: the review page leaves them unchecked and says what they
    matched, so a genuine repeat purchase is one click away.

    Match = same amount, dates at most WINDOW_DAYS apart, and the same shop or item
    (after normalization, one being a prefix of the other).

    Only mail imports are compared (the ledger's source='email' rows + this batch):
    the duplicates seen in practice were all mail-vs-mail, and receipts / PayPay /
    manual rows only produced coincidental same-amount matches.
    """

    WINDOW_DAYS = 7

    def __init__(self, conn, since: str):
        self._pool = [
            {'date': r['date'], 'shop': r['shop'] or '', 'item': r['item'] or '',
             'amount': int(round(r['jpy_amount'])), 'where': 'ledger', 'gmail_id': ''}
            for r in conn.execute(
                "SELECT date, shop, item, jpy_amount FROM transactions "
                "WHERE deleted_at IS NULL AND jpy_amount IS NOT NULL AND source='email' "
                "AND date >= ?", (since,))
        ]

    @staticmethod
    def _days(a: str, b: str) -> int:
        from datetime import date
        try:
            return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
        except ValueError:
            return 999

    @staticmethod
    def _same(a: str, b: str, n: int) -> bool:
        a, b = _key(a)[:n], _key(b)[:n]
        return bool(a and b) and (a.startswith(b) or b.startswith(a))

    def check(self, items: list, gmail_id: str = '') -> list:
        """Mark items with it['dup'] = {date, shop, item, amount, where}; then remember
        them, so a later mail in the same batch is compared against this one too."""
        for it in items:
            for p in self._pool:
                if p['gmail_id'] and p['gmail_id'] == gmail_id:
                    continue             # rows of the same mail are never duplicates of each other
                if p['amount'] != it['amount'] or self._days(p['date'], it['date']) > self.WINDOW_DAYS:
                    continue
                # Shops compare on a short prefix ('zoff' vs 'zoffオンラインストア'),
                # items on a longer one (the same product may be re-titled slightly).
                if self._same(p['shop'], it['shop'], 4) or self._same(p['item'], it['item'], 12):
                    it['dup'] = {k: p[k] for k in ('date', 'shop', 'item', 'amount', 'where')}
                    break
        self._pool += [{'date': it['date'], 'shop': it['shop'], 'item': it['item'],
                        'amount': it['amount'], 'where': 'batch', 'gmail_id': gmail_id}
                       for it in items]
        return items
