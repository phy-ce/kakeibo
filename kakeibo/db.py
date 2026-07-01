import os
import json
import uuid
import sqlite3

from .config import user_config_dir, user_path, migrate_project_data

# Data files are kept in the user folder (~/.kakeibo), not the project folder.
DB_PATH        = user_path("kakeibo.db")
CATEGORY_FILE  = user_path("categories.json")

# Generic default categories. Users can freely add/remove them from the settings page.
CATEGORIES_DEFAULT = [
    # Expense — fixed expenses
    ('지출', '고정지출', '주거',    '그 외'),
    ('지출', '고정지출', '공과금',  '그 외'),
    ('지출', '고정지출', '통신비',  '그 외'),
    ('지출', '고정지출', '구독',    '그 외'),
    ('지출', '고정지출', '보험',    '그 외'),

    # Expense — variable expenses
    ('지출', '변동지출', '식비',    '그 외'),
    ('지출', '변동지출', '식비',    '외식'),
    ('지출', '변동지출', '식비',    '식료품'),
    ('지출', '변동지출', '교통',    '그 외'),
    ('지출', '변동지출', '의료',    '그 외'),
    ('지출', '변동지출', '의류',    '그 외'),
    ('지출', '변동지출', '여가',    '그 외'),
    ('지출', '변동지출', '생활용품', '그 외'),
    ('지출', '변동지출', '기타',    '그 외'),

    # Expense — savings/investment
    ('지출', '저축_투자', '저축',   '그 외'),
    ('지출', '저축_투자', '투자',   '그 외'),

    # Income
    ('수입', '수입', '급여',       '그 외'),
    ('수입', '수입', '부수입',     '그 외'),
    ('수입', '수입', '기타',       '그 외'),
]


def _load_categories_json():
    """Read the category seed JSON file and return a list of [(type,major,minor,sub), ...] tuples.

    File format: [{"type":"지출","major":"...","minor":"...","sub":"..."}, ...]
    If the file is missing or malformed, returns None -> the caller uses the generic default.
    """
    if not os.path.exists(CATEGORY_FILE):
        return None
    try:
        with open(CATEGORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [
            (r.get("type", ""), r.get("major", ""), r.get("minor", ""), r.get("sub", ""))
            for r in data
            if r.get("type") and r.get("major")
        ]
    except Exception as e:
        print(f"[db] categories.json 로드 실패: {e}")
        return None


def get_db():
    os.makedirs(user_config_dir(), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    # Before opening the DB, if data exists in the old project folder, migrate it to ~/.kakeibo
    migrate_project_data()
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            date          TEXT    NOT NULL,
            type          TEXT,
            major         TEXT    DEFAULT '',
            minor         TEXT    DEFAULT '',
            category      TEXT    DEFAULT '',
            shop          TEXT    DEFAULT '',
            item          TEXT    DEFAULT '',
            jpy_amount    REAL,
            krw_amount    REAL,
            note          TEXT    DEFAULT '',
            exchange_rate REAL,
            source        TEXT    DEFAULT 'manual',
            paypay_id     TEXT    DEFAULT '',
            amazon_id     TEXT    DEFAULT '',
            mcdonald_id   TEXT    DEFAULT '',
            gmail_id      TEXT    DEFAULT '',
            receipt_img   TEXT    DEFAULT '',
            receipt_name  TEXT    DEFAULT '',
            receipt_id    TEXT    DEFAULT '',
            created_at    TEXT    DEFAULT (datetime('now','localtime')),
            deleted_at    TEXT    DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS exchange_rates (
            date       TEXT PRIMARY KEY,
            rate       REAL NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS categories (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            type  TEXT NOT NULL,
            major TEXT NOT NULL,
            minor TEXT DEFAULT '',
            sub   TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS budget (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            match_shop  TEXT    DEFAULT '',
            match_item  TEXT    DEFAULT '',
            match_major TEXT    DEFAULT '',
            match_minor TEXT    DEFAULT '',
            match_sub   TEXT    DEFAULT '',
            amount      REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tx_date   ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_tx_source ON transactions(source);
    """)

    # Existing DB migration
    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    if 'deleted_at' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN deleted_at TEXT DEFAULT NULL")
    if 'mcdonald_id' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN mcdonald_id TEXT DEFAULT ''")
    if 'gmail_id' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN gmail_id TEXT DEFAULT ''")
    if 'receipt_name' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN receipt_name TEXT DEFAULT ''")
    if 'receipt_id' not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN receipt_id TEXT DEFAULT ''")

    # Group receipt transactions by receipt_id (backfill existing data + propagate images). No-op if already filled.
    _backfill_receipt_ids(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_deleted ON transactions(deleted_at)")

    # budget table migration: category-based -> individual-item-based
    bcols = [r[1] for r in conn.execute("PRAGMA table_info(budget)").fetchall()]
    if 'name' not in bcols:
        # If old category-based data exists, convert it to name and preserve it
        old_rows = []
        if 'major' in bcols:
            try:
                old_rows = conn.execute(
                    "SELECT year, month, major, minor, "
                    "COALESCE(sub,'') AS sub, amount FROM budget"
                ).fetchall()
            except Exception:
                old_rows = []
        conn.executescript("""
            DROP TABLE IF EXISTS budget;
            CREATE TABLE budget (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                year       INTEGER NOT NULL,
                month      INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                match_shop TEXT    DEFAULT '',
                match_item TEXT    DEFAULT '',
                amount     REAL    NOT NULL
            );
        """)
        for r in old_rows:
            label = ' / '.join(x for x in (r['major'], r['minor'], r['sub']) if x) or '예산'
            conn.execute(
                "INSERT INTO budget(year,month,name,match_shop,match_item,"
                "match_major,match_minor,match_sub,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (r['year'], r['month'], label, '', '',
                 r['major'] or '', r['minor'] or '', r['sub'] or '',
                 r['amount'])
            )

    # Add match_major/minor/sub columns to an existing budget table (if already migrated to individual-item form)
    bcols = [r[1] for r in conn.execute("PRAGMA table_info(budget)").fetchall()]
    for col in ('match_major', 'match_minor', 'match_sub'):
        if col not in bcols:
            conn.execute(f"ALTER TABLE budget ADD COLUMN {col} TEXT DEFAULT ''")

    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        # If a user-defined categories.json exists, seed from it (otherwise use the generic default)
        seed = _load_categories_json() or CATEGORIES_DEFAULT
        conn.executemany(
            "INSERT INTO categories(type,major,minor,sub) VALUES(?,?,?,?)",
            seed
        )

    for k, v in [('initial_jpy_balance', '0'), ('initial_krw_balance', '0')]:
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))

    conn.commit()
    conn.close()


def _backfill_receipt_ids(conn):
    """Group source='receipt' rows by (date, shop), assign a shared receipt_id,
    and propagate the group's representative image to rows where receipt_img is empty.

    (Historically: due to an image-move bug, only the first item row had a receipt_img)
    If a receipt_id already exists, reuse it -> safe to run multiple times (idempotent).
    """
    need = conn.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE source='receipt' AND (receipt_id='' OR receipt_id IS NULL)"
    ).fetchone()[0]
    if not need:
        return

    rows = conn.execute(
        "SELECT id, date, shop, receipt_img, receipt_id "
        "FROM transactions WHERE source='receipt'"
    ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r['date'], r['shop'] or ''), []).append(r)

    for rs in groups.values():
        rid = next((r['receipt_id'] for r in rs if r['receipt_id']), '') or uuid.uuid4().hex
        primary_img = next((r['receipt_img'] for r in rs if r['receipt_img']), '')
        for r in rs:
            new_img = r['receipt_img'] or primary_img
            if r['receipt_id'] != rid or r['receipt_img'] != new_img:
                conn.execute(
                    "UPDATE transactions SET receipt_id=?, receipt_img=? WHERE id=?",
                    (rid, new_img, r['id'])
                )


def get_setting(key, default='0'):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    conn.commit()
    conn.close()
