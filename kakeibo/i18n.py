"""Lightweight i18n for KR/EN separate deployment.

Language is chosen at deploy time via the KAKEIBO_LANG env var (default 'ko').
The engine keeps Korean canonical values (DB categories, SQL, prompts); only the
frontend display is localized via t() (UI strings) and cat_label() (category values).

TRANSLATIONS maps a key -> (ko, en) tuple. Add page-specific keys with the
TRANSLATIONS.update({...}) blocks below (one block per template group).
"""
import os

_LANG = None


def current_lang() -> str:
    global _LANG
    if _LANG is None:
        _LANG = 'en' if os.environ.get('KAKEIBO_LANG', 'ko').lower().startswith('en') else 'ko'
    return _LANG


_IDX = {'ko': 0, 'en': 1}


def t(key: str, **kw) -> str:
    """Localized UI string for `key`. Falls back to ko, then the key itself.
    Supports str.format params, e.g. t('msg.added_n', n=3)."""
    pair = TRANSLATIONS.get(key)
    if not pair:
        return key
    s = pair[_IDX[current_lang()]] or pair[0]
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError):
        return s


# ── Category display labels (fixed taxonomy). DB value stays Korean; only display maps. ──
# Covers db.py CATEGORIES_DEFAULT (type/major/minor/sub). User-added categories show as typed.
_CATEGORY_EN = {
    # type
    '지출': 'Expense', '수입': 'Income',
    # major
    '고정지출': 'Fixed', '변동지출': 'Variable', '저축_투자': 'Savings/Invest',
    # minor
    '주거': 'Housing', '공과금': 'Utilities', '통신비': 'Telecom', '구독': 'Subscriptions',
    '보험': 'Insurance', '식비': 'Food', '교통': 'Transport', '의료': 'Medical',
    '의류': 'Clothing', '여가': 'Leisure', '생활용품': 'Household', '기타': 'Other',
    '저축': 'Savings', '투자': 'Investment', '급여': 'Salary', '부수입': 'Side income',
    # sub
    '그 외': 'Other', '외식': 'Dining out', '식료품': 'Groceries',
}


def cat_label(value):
    """Localized display label for a category value (value itself if unknown/ko)."""
    if value and current_lang() == 'en':
        return _CATEGORY_EN.get(value, value)
    return value


def category_map() -> dict:
    """Category value -> label map for the current language (empty for ko).
    Exposed to client JS so dynamically-built category dropdowns can localize too."""
    return dict(_CATEGORY_EN) if current_lang() == 'en' else {}


# key -> (ko, en)
TRANSLATIONS = {
    # ── common ──
    'common.save':      ('저장', 'Save'),
    'common.add':       ('추가', 'Add'),
    'common.delete':    ('삭제', 'Delete'),
    'common.cancel':    ('취소', 'Cancel'),
    'common.edit':      ('수정', 'Edit'),
    'common.all':       ('전체', 'All'),
    'common.expense':   ('지출', 'Expense'),
    'common.income':    ('수입', 'Income'),
    'common.date':      ('날짜', 'Date'),
    'common.type':      ('구분', 'Type'),
    'common.shop':      ('구입처', 'Shop'),
    'common.item':      ('품목', 'Item'),
    'common.amount':    ('금액', 'Amount'),
    'common.category':  ('카테고리', 'Category'),
    'common.note':      ('비고', 'Note'),
    'common.loading':   ('로딩 중...', 'Loading...'),

    # ── nav (base.html) ──
    'nav.transactions': ('내역', 'Transactions'),
    'nav.uncategorized':('미분류', 'Uncategorized'),
    'nav.stats':        ('통계', 'Stats'),
    'nav.budget':       ('예산', 'Budget'),
    'nav.settings':     ('설정', 'Settings'),
}

# ── backend messages (app.py / sync / ocr) ──
TRANSLATIONS.update({
    'msg.no_supported_images':  ('지원하는 이미지가 없습니다', 'No supported images'),
    'msg.quota.per_day':        ('하루 20건', '20/day'),
    'msg.quota.per_min':        ('분당 5건', '5/min'),
    'msg.cont.tomorrow':        ('내일', 'tomorrow'),
    'msg.cont.later':           ('잠시 후', 'in a moment'),
    'msg.receipt_quota_stopped':('{n}개 분석 후 중단 — Gemini 무료 할당량({limit}) 초과. 나머지 사진은 분석하지 않았으니 {cont} 다시 업로드하세요. (가계부엔 추가 안 됨)',
                                 'Stopped after analyzing {n} — Gemini free quota ({limit}) exceeded. The remaining photos were not analyzed, so please re-upload {cont}. (Nothing was added to the ledger)'),
    'msg.csv_format_error':     ('CSV 형식 오류', 'Invalid CSV format'),
    'msg.missing_columns':      ('컬럼 누락: {cols}', 'Missing columns: {cols}'),
    'msg.no_withdrawals':       ('출금 거래 없음', 'No withdrawal transactions'),
    'msg.added_n':              ('{n}건 추가', '{n} added'),
    'msg.no_file':              ('파일이 없습니다', 'No file provided'),
    'msg.not_json':             ('JSON 파일이 아닙니다', 'Not a JSON file'),
    'msg.not_oauth_client':     ('OAuth 클라이언트(데스크톱) credentials.json 이 아닙니다', 'Not an OAuth client (desktop) credentials.json'),
    'msg.gmail_creds_saved':    ('Gmail 자격증명 저장 완료. 첫 동기화 때 브라우저 인증이 열립니다.', 'Gmail credentials saved. Browser authentication will open on the first sync.'),
    'msg.rates_updated':        ('{n}개 날짜 환율 업데이트', 'Updated exchange rates for {n} dates'),
    'msg.gmail_auth_failed':    ('Gmail 인증 실패: {e}', 'Gmail authentication failed: {e}'),
    'msg.no_new_mail':          ('새 메일 없음', 'No new mail'),
    'msg.cont.tomorrow_sync':   ('내일 다시', 'tomorrow'),
    'msg.cont.later_sync':      ('잠시 후 다시', 'in a moment'),
    'msg.mail_quota_stopped':   ('{n}개 추가 후 중단 — Gemini 무료 할당량({limit}) 초과. {cont} 동기화하면 남은 메일부터 이어서 처리됩니다. (사전필터로 {skipped}통은 AI 없이 걸러 호출 절약)',
                                 'Stopped after adding {n} — Gemini free quota ({limit}) exceeded. Sync again {cont} to resume from the remaining mail. (Pre-filter skipped {skipped} without AI to save calls)'),
    'msg.mail_synced':          ('AI 분석 {scanned}통(사전필터로 {skipped}통 스킵), {n}개 항목 추가',
                                 'AI analyzed {scanned} mail(s) (pre-filter skipped {skipped}), {n} items added'),
    'msg.gmail_not_configured': ('Gmail 동기화 설정이 없습니다. 앱을 콘솔에서 재시작하면 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 입력을 안내합니다. 또는 {env} 에 두 값을 직접 넣으세요 (Google Cloud Console -> OAuth 클라이언트(데스크톱)).',
                                 'Gmail sync is not configured. Restart the app from the console to be guided through entering GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET. Or add both values directly to {env} (Google Cloud Console -> OAuth client (Desktop)).'),
    'msg.no_api_key':           ('환경변수 GEMINI_API_KEY 가 설정되지 않았습니다. 첫 실행 시 콘솔에서 키를 입력하거나 ~/.kakeibo/.env 에 GEMINI_API_KEY=... 를 추가해 주세요. (키 발급: https://aistudio.google.com/app/apikey)',
                                 'The GEMINI_API_KEY environment variable is not set. Enter the key in the console on first run, or add GEMINI_API_KEY=... to ~/.kakeibo/.env. (Get a key: https://aistudio.google.com/app/apikey)'),
    # Excel export
    'excel.sheet':       ('시트', 'Sheet'),
    'excel.date':        ('날짜', 'Date'),
    'excel.type':        ('구분', 'Type'),
    'excel.major':       ('대분류', 'Category'),
    'excel.minor':       ('소분류', 'Subcategory'),
    'excel.category':    ('카테고리', 'Detail'),
    'excel.shop':        ('구입처', 'Shop'),
    'excel.item':        ('품목명', 'Item'),
    'excel.jpy_price':   ('엔화가격', 'JPY Price'),
    'excel.krw_price':   ('원화가격', 'KRW Price'),
    'excel.jpy_balance': ('엔화잔고', 'JPY Balance'),
    'excel.krw_balance': ('원화잔고', 'KRW Balance'),
    'excel.note':        ('비고', 'Note'),
    'excel.rate':        ('환율', 'Rate'),
})

# ── transactions.html ──
TRANSLATIONS.update({
    'tx.gmail_not_connected':  ('Gmail 연동이 안 돼 있어요', 'Gmail is not connected'),
    'tx.gmail_connect_prompt': ('주문·영수증 메일 자동 가져오기를 쓰려면 연동하세요.', 'Connect it to auto-import order/receipt emails.'),
    'tx.go_to_settings':       ('설정으로', 'Go to settings'),
    'tx.dont_show_again':      ('다음부터 표시 안 함', "Don't show again"),
    'tx.no_transport_this_week':('이번 주 교통비 미입력', 'No transport expense logged this week'),
    'tx.add_now':              ('지금 추가', 'Add now'),
    'tx.all_categories':       ('전체 카테고리', 'All categories'),
    'tx.all_sources':          ('전체 소스', 'All sources'),
    'tx.receipt':              ('영수증', 'Receipt'),
    'tx.mail_ai':              ('메일(AI)', 'Mail (AI)'),
    'tx.manual':               ('수동', 'Manual'),
    'tx.mail_sync':            ('메일동기화', 'Mail sync'),
    'tx.exchange_rate':        ('환율', 'Exchange rate'),
    'tx.delete_selected':      ('선택삭제', 'Delete selected'),
    'tx.export':               ('내보내기', 'Export'),
    'tx.view_receipt_large':   ('영수증 크게 보기', 'View receipt larger'),
    'tx.minor':                ('소분류', 'Minor'),
    'tx.shop_item':            ('구입처/품목', 'Shop/Item'),
    'tx.jpy':                  ('엔화', 'JPY'),
    'tx.balance_jpy':          ('잔고(¥)', 'Balance (¥)'),
    'tx.balance_krw':          ('잔고(₩)', 'Balance (₩)'),
    'tx.click_to_enlarge':     ('클릭하면 크게 보기', 'Click to enlarge'),
    'tx.no_shop':              ('(가게 없음)', '(No shop)'),
    'tx.items_suffix':         ('개 품목', ' items'),
    'tx.no_transactions':      ('내역이 없습니다.', 'No transactions.'),
    'tx.set_starting_balance': ('시작 잔고 설정', 'Set starting balance'),
    'tx.balance_hint_before':  ('현재 가진 잔고를 입력하세요. 나중에 ', 'Enter your current balance. You can change it later in '),
    'tx.balance_hint_after':   ('에서 바꿀 수 있어요.', '.'),
    'tx.initial_balance_jpy':  ('엔화 초기 잔고 (¥)', 'Initial JPY balance (¥)'),
    'tx.initial_balance_krw':  ('원화 초기 잔고 (₩)', 'Initial KRW balance (₩)'),
    'tx.later':                ('나중에', 'Later'),
    'tx.add_transaction':      ('거래 추가', 'Add transaction'),
    'tx.major':                ('대분류', 'Major'),
    'tx.item_name':            ('품목명', 'Item'),
    'tx.jpy_price':            ('엔화가격', 'JPY price'),
    'tx.krw_price':            ('원화가격', 'KRW price'),
    'tx.rate_ref':             ('환율 (100엔, 참고용)', 'Exchange rate (per ¥100, ref.)'),
    'tx.processing':           ('처리 중...', 'Processing...'),
    'tx.gmail_hidden_notice':  ('숨겼습니다. 나중에 설정 → Gmail 연동에서 언제든 켤 수 있어요.', 'Hidden. You can re-enable it anytime in Settings → Gmail sync.'),
    'tx.confirm_delete_prefix':('', 'Delete '),
    'tx.confirm_delete_suffix':('건 삭제하시겠습니까?', ' items?'),
    'tx.confirm_delete_one':   ('삭제하시겠습니까?', 'Delete this item?'),
    'tx.edit_transaction':     ('거래 수정', 'Edit transaction'),
    'tx.checking_photos':      ('사진 확인 중...', 'Checking photos...'),
    'tx.importing_paypay':     ('PayPay 임포트 중...', 'Importing PayPay...'),
    'tx.syncing_mail':         ('메일 동기화 중... (AI가 주문/영수증 메일을 분석합니다. 시간이 걸릴 수 있어요)', 'Syncing mail... (AI is analyzing order/receipt emails. This may take a while)'),
    'tx.done':                 ('완료', 'Done'),
    'tx.mail_sync_error':      ('메일 동기화 오류', 'Mail sync error'),
    'tx.updating_rates':       ('환율 업데이트 중...', 'Updating exchange rates...'),
})

# ── budget.html + settings.html ──
TRANSLATIONS.update({
    'budget.year_suffix':         ('년', ''),
    'budget.month_suffix':        ('월', ''),
    'budget.add_item':            ('예산 항목 추가', 'Add budget item'),
    'budget.copy_prev':           ('지난달 복사', 'Copy last month'),
    'budget.item_name':           ('항목명', 'Item name'),
    'budget.match_condition':     ('매칭조건', 'Match condition'),
    'budget.budget':              ('예산', 'Budget'),
    'budget.actual_spent':        ('실제 지출', 'Actual spent'),
    'budget.remaining':           ('잔여', 'Remaining'),
    'budget.major_short':         ('대', 'Major'),
    'budget.minor_short':         ('소', 'Minor'),
    'budget.sub':                 ('세부', 'Sub'),
    'budget.no_match':            ('매칭조건 없음', 'No match condition'),
    'budget.no_items':            ('예산 항목이 없습니다.', 'No budget items.'),
    'budget.total':               ('합계', 'Total'),
    'budget.name_placeholder':    ('예: 넷플릭스, iCloud, 도시가스', 'e.g. Netflix, iCloud, city gas'),
    'budget.shop_match':          ('구입처 매칭', 'Shop match'),
    'budget.partial_match':       ('(부분일치)', '(partial match)'),
    'budget.shop_placeholder':    ('예: 東京ガス', 'e.g. 東京ガス'),
    'budget.item_match':          ('품목 매칭', 'Item match'),
    'budget.partial_optional':    ('(부분일치, 선택)', '(partial match, optional)'),
    'budget.item_placeholder':    ('예: iCloud+ 200GB', 'e.g. iCloud+ 200GB'),
    'budget.category_match':      ('카테고리 매칭 (선택, 정확히 일치)', 'Category match (optional, exact match)'),
    'budget.major':               ('대분류', 'Major'),
    'budget.minor':               ('소분류', 'Minor'),
    'budget.budget_jpy':          ('예산 (엔화)', 'Budget (JPY)'),
    'budget.amount_placeholder':  ('예: 3000', 'e.g. 3000'),
    'budget.match_help':          ('※ 매칭조건들은 AND 로 결합돼. 하나라도 입력해야 실제지출이 집계됨.', '* Match conditions are combined with AND. Enter at least one for actual spending to be tallied.'),
    'budget.edit_item':           ('예산 항목 수정', 'Edit budget item'),
    'budget.name_amount_required':('항목명과 예산은 필수임', 'Item name and budget are required'),
    'budget.confirm_delete':      ('삭제하시겠습니까?', 'Delete this?'),
    'budget.confirm_copy':        ('지난달 예산 항목들을 이번 달로 복사할까요?', "Copy last month's budget items to this month?"),
    'budget.copied_suffix':       ('개 복사 완료', ' items copied'),
    'settings.initial_balance':       ('잔고 초기값', 'Initial balance'),
    'settings.initial_jpy':           ('엔화 초기 잔고 (¥)', 'Initial JPY balance (¥)'),
    'settings.initial_krw':           ('원화 초기 잔고 (₩)', 'Initial KRW balance (₩)'),
    'settings.mail_sync_days':        ('메일 동기화 기간 (일)', 'Mail sync period (days)'),
    'settings.mail_sync_help':        ('최근 N일 이내 메일만 가져옵니다', 'Only fetches mail from the last N days'),
    'settings.mail_sync_max':         ('메일 조회 개수', 'Mail fetch count'),
    'settings.mail_sync_max_help':    ('한 번에 Gmail에서 가져올 최대 메일 수 (기본 200, 최대 500). AI 호출 전 필터링됩니다.',
                                       'Max mails fetched from Gmail per sync (default 200, max 500). Pre-filtered before any AI call.'),
    'settings.sender_allowlist':      ('구매 발신자 화이트리스트', 'Purchase sender allowlist'),
    'settings.placeholder_default':   ('비우면 기본값 사용', 'Leave empty to use defaults'),
    'settings.sender_help':           ('쉼표 구분. 이 발신자는 AI로 바로 분석.', 'Comma-separated. These senders are analyzed by AI directly.'),
    'settings.fill_replace':          ('채우면 그 값이 그대로 사용(기본 대체), 비우면 기본값', 'If filled, that value is used as-is (replaces default); if empty, default is used'),
    'settings.purchase_keywords':     ('구매 키워드', 'Purchase keywords'),
    'settings.keyword_help':          ('쉼표 구분. 메일 검색 + 구매 판정에 사용.', 'Comma-separated. Used for mail search + purchase detection.'),
    'settings.fill_replace_short':    ('채우면 대체, 비우면 기본값', 'If filled, replaces; if empty, default'),
    'settings.reset_defaults':        ('기본값으로 되돌리기', 'Reset to defaults'),
    'settings.reset_title':           ('발신자·키워드를 기본값으로', 'Reset senders and keywords to defaults'),
    'settings.gmail_integration':     ('Gmail 연동', 'Gmail integration'),
    'settings.status':                ('상태', 'Status'),
    'settings.connected':             ('연동됨', 'Connected'),
    'settings.credentials_registered':('자격증명 등록됨 (첫 동기화 시 인증)', 'Credentials registered (authenticate on first sync)'),
    'settings.not_set':               ('미설정', 'Not set'),
    'settings.gmail_creds_1':         ('에서', ': create'),
    'settings.oauth_client':          ('OAuth 클라이언트(데스크톱)', 'OAuth client (Desktop)'),
    'settings.gmail_creds_2':         ('을 만들어 다운로드한', ', download and upload the'),
    'settings.gmail_creds_3':         ('을 그대로 올리세요.', 'as-is.'),
    'settings.upload':                ('업로드', 'Upload'),
    'settings.manage_categories':     ('카테고리 관리', 'Manage categories'),
    'settings.major':                 ('대분류', 'Major'),
    'settings.minor':                 ('소분류', 'Minor'),
    'settings.add_category':          ('카테고리 추가', 'Add category'),
    'settings.saved':                 ('저장 완료', 'Saved'),
    'settings.confirm_reset':         ('발신자·키워드를 기본값으로 되돌릴까요?', 'Reset senders and keywords to defaults?'),
    'settings.select_creds':          ('credentials.json 파일을 선택하세요', 'Please select a credentials.json file'),
    'settings.done':                  ('완료', 'Done'),
    'settings.upload_failed':         ('업로드 실패', 'Upload failed'),
    'settings.confirm_delete':        ('삭제하시겠습니까?', 'Delete this?'),
    'settings.ai_model':              ('AI 모델', 'AI model'),
    'settings.ai_model_help':         ('영수증 OCR·메일 분석에 쓰는 Gemini 모델. Pro는 더 정확하지만 무료 할당량이 적어요.',
                                       'Gemini model used for receipt OCR and mail parsing. Pro is more accurate but has a smaller free quota.'),
})

# ── stats / receipt / trash / uncategorized ──
TRANSLATIONS.update({
    'stats.year_suffix':            ('년', ''),
    'stats.month_suffix':           ('월', ''),
    'stats.monthly_income_expense': ('월별 수입/지출', 'Monthly income/expense'),
    'stats.expense_by_category':    ('카테고리별 지출', 'Expense by category'),
    'stats.this_month_summary':     ('이번달 요약', 'This month summary'),
    'stats.budget_vs_actual':       ('예산 vs 실제 지출', 'Budget vs actual expense'),
    'stats.other':                  ('기타', 'Other'),
    'stats.net':                    ('순수지', 'Net'),
    'stats.no_budget':              ('예산이 설정되지 않았습니다.', 'No budget set.'),
    'receipt.review_photos':        ('분석할 사진 확인', 'Review photos to analyze'),
    'receipt.only_checked_analyzed':('체크된 사진만 AI 분석합니다.', 'Only checked photos are analyzed by AI.'),
    'receipt.duplicate':            ('중복', 'Duplicate'),
    'receipt.dup_auto_unchecked':   ('(이미 추가한 파일명)은 자동으로 체크 해제됩니다.', '(Already-added filenames) are unchecked automatically.'),
    'receipt.select_all':           ('전체선택', 'Select all'),
    'receipt.deselect_all':         ('전체해제', 'Deselect all'),
    'receipt.start_analysis':       ('분석 시작', 'Start analysis'),
    'receipt.click_to_enlarge':     ('클릭하면 크게 보기', 'Click to enlarge'),
    'receipt.already_added':        ('이미 추가됨', 'Already added'),
    'receipt.select_one':           ('분석할 사진을 하나 이상 선택하세요', 'Select at least one photo to analyze'),
    'receipt.analyzing':            ('AI 분석 중...', 'Analyzing...'),
    'receipt.review_receipt':       ('영수증 확인', 'Review receipt'),
    'receipt.save_all':             ('전체 저장', 'Save all'),
    'receipt.no_items_recognized':  ('품목 인식 안 됨 — 사진이 흐리거나 영수증이 아닐 수 있어요 (그냥 두면 추가 안 됨)', 'No items recognized — the photo may be blurry or not a receipt (left as-is, nothing is added)'),
    'receipt.items_count_suffix':   ('개 품목', ' items'),
    'receipt.item_name':            ('품목명', 'Item'),
    'receipt.saved_suffix':         ('개 저장 완료', ' saved'),
    'trash.title':                  ('휴지통', 'Trash'),
    'trash.delete_all_permanently': ('전체 영구삭제', 'Delete all permanently'),
    'trash.deleted_at':             ('삭제일시', 'Deleted at'),
    'trash.source':                 ('소스', 'Source'),
    'trash.restore':                ('복원', 'Restore'),
    'trash.empty':                  ('휴지통이 비어있습니다.', 'Trash is empty.'),
    'trash.confirm_destroy':        ('영구 삭제하시겠습니까? 복구할 수 없습니다.', 'Delete permanently? This cannot be undone.'),
    'trash.confirm_empty':          ('전체 영구 삭제하시겠습니까? 복구할 수 없습니다.', 'Delete all permanently? This cannot be undone.'),
    'uncat.title':                  ('미분류 내역', 'Uncategorized transactions'),
    'uncat.source':                 ('소스', 'Source'),
    'uncat.major':                  ('대분류', 'Major'),
    'uncat.minor':                  ('소분류', 'Minor'),
    'uncat.empty':                  ('미분류 내역이 없습니다. 👍', 'No uncategorized transactions. 👍'),
    'uncat.select_major':           ('대분류는 선택해줘', 'Please select a major category'),
    'uncat.save_failed':            ('저장 실패', 'Save failed'),
    # email review (AI mail -> confirm before add)
    'email.review_title':           ('메일에서 추가할 항목 확인', 'Review items to add from mail'),
    'email.review_help':            ('AI가 분석한 항목입니다. 추가할 것만 체크하고 저장하세요.',
                                     'Items parsed by AI. Check only the ones to add, then save.'),
    'email.add_selected':           ('선택 추가', 'Add selected'),
    'email.select_one':             ('추가할 항목을 하나 이상 선택하세요', 'Select at least one item to add'),
    'email.nothing':                ('추가할 새 항목이 없습니다.', 'No new items to add.'),
    'email.toggle_all_in_mail':     ('이 메일 항목 전체 선택/해제', 'Select/deselect all items in this mail'),
    'email.quota_partial':          ('Gemini 무료 할당량 초과로 일부만 분석됐습니다. 나머지는 나중에 다시 동기화하세요.',
                                     'Only part was analyzed due to the Gemini free-quota limit. Sync again later for the rest.'),
})
