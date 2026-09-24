"""Generic email -> transaction import.

Instead of per-vendor hardcoding, it fetches mail broadly, uses Gemini to pick out
only purchase emails, and extracts item/amount/date in a structured way.
(New shops like BIRKENSTOCK are handled without code changes.)

- Deduplication: Gmail message ID (`gmail_id`) — stable regardless of vendor.
- Amounts: broken down per item, but shipping/discounts/consumption tax are each
  extracted as separate entries so the total matches the order total.
"""
import re
import time
import base64
import datetime

from bs4 import BeautifulSoup

from .gmail_auth import get_gmail_service
from ..dates import normalize_date
from ..ai_items import clean_items, Refiner, DupFinder
from ..ocr import gemini
from ..i18n import t


# ── Filter defaults (fully changeable by the user in settings, and can be reverted to these anytime) ──
# Purchase keywords — shared by Gmail search + purchase detection
_DEFAULT_KEYWORDS = [
    "注文", "ご注文", "発送", "出荷", "領収書", "レシート", "ご購入", "購入",
    "決済", "お支払い", "ご請求", "明細", "order", "receipt", "invoice", "payment", "shipped",
]
# Sender allowlist (address partial match) — these senders pass even without a keyword
_DEFAULT_SENDERS = [
    "amazon.co.jp", "amazon.com", "rakuten", "paypay", "mdj.jp", "apple.com",
    "birkenstock", "uniqlo", "gu-global", "mercari", "order@", "receipt@", "billing@",
]
# Amount pattern (whether there is an amount to record) — without this, calling the AI yields nothing
_MONEY_RE = re.compile(r"[¥￥₩]\s?[\d,]{2,}|[\d,]{2,}\s?円|合計|税込|小計|請求金額|ご請求")

# ── Order-lifecycle dedup ──
# A single order sends several emails (ordered -> shipped -> out-for-delivery -> delivered),
# and 2+ of them carry the full amount (e.g. Amazon "注文済み" AND "発送済み"), so parsing
# every one double-counts the order. Count each order once at the SHIPPED/receipt stage:
# skip a pure order-PLACEMENT email that shows no shipped/receipt signal — the later
# shipment mail covers it. (The delivered-status "配達済み/配達中" mails carry no amount, so
# they are already dropped by _MONEY_RE.)
# Pure order-placement acknowledgements (skip when no shipped/receipt marker present).
_ORDERED_RE = re.compile(
    "注文済み|ご注文の確認|ご注文内容|ご注文を?(受け?付け|承り|うけたまわ)"
    "|注文受付|ご注文ありがとう|order confirmation|order received|we received your order")
# Shipped / receipt signals — if the subject has any of these it is a real purchase to keep.
_SHIPPED_RE = re.compile(
    "発送|出荷|配達|お届け|届け|領収|レシート|receipt|invoice|shipped|dispatched|delivered"
    "|ご購入|購入完了|決済完了|ご請求|請求金額|明細")


def _list_setting(conn, key: str, default: list) -> list:
    """Turn a comma-separated setting into a list. If empty, use the default (= clearing it restores the default anytime)."""
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] and row["value"].strip():
        return [x.strip() for x in row["value"].split(",") if x.strip()]
    return list(default)


def _looks_like_purchase(sender: str, subject: str, body: str,
                         allow: list, keywords: list) -> bool:
    """Cheaply decide whether this is a 'purchase email' worth calling the AI on. (Pre-Gemini filter)"""
    head = body[:4000]
    if not _MONEY_RE.search(head):
        return False                      # No trace of an amount -> skip (nothing to record)
    # Order-lifecycle dedup: skip a pure order-placement mail (no shipped/receipt signal
    # in the subject); the later shipment mail carries the same amount and is kept instead.
    subj = subject or ""
    if _ORDERED_RE.search(subj) and not _SHIPPED_RE.search(subj):
        return False
    s = (sender or "").lower()
    if any(a in s for a in allow):
        return True                       # Allowlisted sender + amount -> pass
    low = f"{subject}\n{head[:2000]}".lower()
    return any(k.lower() in low for k in keywords)     # Purchase keyword match


def extract_text(payload) -> str:
    """Extract the mail body text (text/plain preferred, otherwise HTML -> text)."""
    def _decode(part):
        data = part["body"].get("data", "")
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore") if data else ""

    def _collect(p, plain, html):
        if "parts" in p:
            for sub in p["parts"]:
                _collect(sub, plain, html)
        else:
            mime = p.get("mimeType", "")
            if mime == "text/plain":
                plain.append(_decode(p))
            elif mime == "text/html":
                html.append(_decode(p))

    plain, html = [], []
    _collect(payload, plain, html)
    text = "".join(plain).strip()
    if not text and html:
        soup = BeautifulSoup("".join(html), "html.parser")
        # <style>/<script> text is not mail content, yet get_text() keeps it - on a
        # styled shop mail the CSS alone used to fill most of the 8000-char AI budget.
        for tag in soup(["style", "script", "head", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    return _squeeze(text)


def _squeeze(text: str) -> str:
    """Collapse layout whitespace (HTML tables leave long runs of blank lines/spaces)."""
    text = re.sub(r"[ \t　\xa0]+", " ", text or "")
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _header(payload, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# Longest item name kept on the row; a longer shop title (Amazon's SEO listings run to
# 150+ chars) is shortened by the model and the full title goes to the note.
ITEM_NAME_MAX = 40


def known_shops(conn, limit: int = 40) -> list:
    """Shop names the user already has for mail imports, most used first.

    Handed to the model so one shop keeps one name ('Octopus Energy' and
    'オクトパスエナジー' used to arrive as two shops, splitting stats and history).
    """
    return [r[0] for r in conn.execute(
        "SELECT shop FROM transactions WHERE source='email' AND deleted_at IS NULL "
        "AND shop != '' GROUP BY shop ORDER BY COUNT(*) DESC LIMIT ?", (limit,)) if r[0]]


def _build_prompt(sender: str, subject: str, body: str, order_date: str,
                  shops: list = None) -> str:
    cats = gemini.categories_text()
    # If the body is too long, truncate it to save tokens
    body = body[:8000]
    shop_rule = ""
    if shops:
        shop_rule = ("- 아래 [기존 가게 이름] 에 같은 가게(표기만 다른 것 포함: 가타카나/영문, "
                     "'オンラインストア' 유무 등)가 있으면 그 이름을 글자 그대로 써라.\n")
    shop_list = f"\n[기존 가게 이름]\n{', '.join(shops)}\n" if shops else ""
    return f"""너는 가계부 앱의 메일 파서다. 아래는 수신한 이메일 한 통이다.
이게 '실제 구매/결제 내역'(주문확인, 발송알림, 영수증, 결제완료 등)이면 구매 품목을 JSON 배열로 추출해라.
광고/뉴스레터/배송상태만 알리는 등 금액이 없는 메일이면 빈 배열 [] 을 반환해라.

[규칙]
- 각 품목을 한 원소로: date, type, major, minor, sub, shop, item, amount, note
- amount 는 엔(¥) 정수. 지출은 음수, 환불/적립/수입은 양수.
- 배송비(送料), 할인(割引/ディスカウント), 소비税(消費税)도 각각 별도 항목으로 넣어
  모든 amount 의 합이 '주문합계/ご請求金額/注文合計' 과 일치하게 해라.
  (할인은 지출을 줄이므로 양수 amount)
- item 은 {ITEM_NAME_MAX}자 이내의 짧은 상품명(원어 그대로, 브랜드 + 무엇인지 + 핵심 규격).
  검색용 수식어(対応/おすすめ/送料無料 같은 문구, 사이즈 나열 등)는 빼라.
  줄였다면 원래 상품명 전체를 note 에 넣고, 안 줄였으면 note 는 "".
- shop 은 판매처명(원어 그대로). 알 수 없으면 발신 도메인 사용.
{shop_rule}- date 는 구매/주문일(YYYY-MM-DD). 본문에 없거나 연도가 불확실하면 {order_date} 사용.
- type 은 지출 또는 수입.
- major/minor/sub 는 아래 분류 체계에서 골라라. 적절한 게 없으면 minor/sub 는 "".
{shop_list}
[사용 가능한 분류 체계]
{cats}

[이메일]
보낸사람: {sender}
제목: {subject}
본문:
{body}

JSON 배열만 출력해라."""


def parse_email_ai(sender: str, subject: str, body: str, msg_date_ms: int,
                   shops: list = None) -> list:
    """Use the AI to extract a list of transaction items from the mail. Returns [] if it is not a purchase email."""
    dt = datetime.datetime.fromtimestamp(msg_date_ms / 1000)
    order_date = f"{dt.year}-{dt.month:02d}-{dt.day:02d}"

    try:
        # Mail sync runs on a background thread -> wait out capacity spikes properly
        data = gemini.generate_json(_build_prompt(sender, subject, body, order_date, shops),
                                    backoff=gemini.OVERLOAD_BACKOFF_BG,
                                    schema=gemini.ITEMS_SCHEMA)
    except (gemini.QuotaError, gemini.OverloadError, gemini.ModelError):
        raise  # Quota / model-busy / bad model is caught by the caller, which stops the sync
    except Exception as e:
        print(f"[generic] AI parse error: {type(e).__name__}")   # ASCII only (cp949)
        return []
    return clean_items(data, fallback_date=order_date, default_shop=sender)


def collect_generic(conn, days: int = None, query: str = None, max_results: int = None,
                    on_progress=None) -> dict:
    """Fetch + pre-filter + AI-parse purchase mails, WITHOUT inserting.

    Returns parsed items grouped per email so the caller can render a review page
    and let the user pick which to add. Already-imported gmail_ids and the cheap
    pre-filter are dropped before any AI call, exactly as before.

    `on_progress(event)` is called as the sync advances so a UI can show what is
    happening (this can take minutes: one AI call per purchase mail). Events are
    plain dicts with a `stage` key: connect / list / read / skip / analyze / parsed
    / quota / done. Never raises out of the callback.

    Returns: {emails, scanned, skipped, quota, quota_exceeded} or {error, ...}.
    Each email = {gmail_id, sender, subject, date, items:[{date,type,major,minor,sub,shop,item,amount}]}.
    """
    def emit(stage, **info):
        if not on_progress:
            return
        try:
            on_progress(dict(stage=stage, **info))
        except Exception:
            pass   # progress reporting must never break the sync

    emit('connect')
    try:
        service = get_gmail_service()
    except Exception as e:
        return {"error": t('msg.gmail_auth_failed', e=e), "emails": [], "scanned": 0, "skipped": 0}

    if days is None:
        row = conn.execute("SELECT value FROM settings WHERE key='mail_sync_days'").fetchone()
        try:
            days = int(row["value"]) if row and str(row["value"]).strip() else 30
        except (ValueError, TypeError):
            days = 30
        days = max(1, days)

    # How many mails to fetch from Gmail per sync (setting, default 200). The cheap
    # pre-filter runs before any AI call, so a larger fetch mostly costs a Gmail list
    # call. Gmail caps a single list page at 500.
    if max_results is None:
        row = conn.execute("SELECT value FROM settings WHERE key='mail_sync_max'").fetchone()
        try:
            max_results = int(row["value"]) if row and str(row["value"]).strip() else 200
        except (ValueError, TypeError):
            max_results = 200
        max_results = max(1, min(max_results, 500))

    # Senders/keywords: use the setting if present (replacing), otherwise the default. (Clearing the setting restores the default anytime)
    keywords = _list_setting(conn, 'email_keywords', _DEFAULT_KEYWORDS)
    allow    = [s.lower() for s in _list_setting(conn, 'email_sender_allowlist', _DEFAULT_SENDERS)]

    if not query:
        row = conn.execute("SELECT value FROM settings WHERE key='email_sync_query'").fetchone()
        query = row["value"] if row and row["value"].strip() else None
    if not query:
        query = f"newer_than:{days}d ({' OR '.join(keywords)})"

    emit('list', days=days)
    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results).execute()
    except Exception as e:
        return {"error": str(e), "emails": [], "scanned": 0, "skipped": 0}

    messages = results.get("messages", [])
    if not messages:
        emit('done', found=0, scanned=0, skipped=0, total=0)
        return {"emails": [], "scanned": 0, "skipped": 0}

    # Skip mails already handled, so nothing is downloaded or AI-analyzed twice:
    # imported ones (present in the ledger) plus the mail_seen table (filtered /
    # parsed-to-nothing / already reviewed).
    _drop_stale_filtered(conn, keywords, allow)
    existing = set(
        r[0] for r in conn.execute(
            "SELECT gmail_id FROM transactions "
            "WHERE gmail_id != '' AND gmail_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchall() if r[0]
    )
    existing |= set(r[0] for r in conn.execute("SELECT gmail_id FROM mail_seen").fetchall() if r[0])

    emails = []
    scanned = 0
    skipped = 0        # pre-filter said "not a purchase mail" (no AI call spent)
    known = 0          # already imported or already handled in an earlier sync
    quota = None
    overload = None
    model_error = None
    busy_streak = 0        # consecutive mails lost to 503; only a run of them stops the sync
    active_model = gemini.get_model_id()
    refiner = Refiner(conn)     # category history + scheme, loaded once per sync
    on_paid = False             # whether the paid API key has taken over this run
    shops = known_shops(conn)   # keep one name per shop across mails
    since = (datetime.date.today()
             - datetime.timedelta(days=days + DupFinder.WINDOW_DAYS)).isoformat()
    dups = DupFinder(conn, since)   # same purchase already in the ledger / this batch
    total = len(messages)
    idx = 0
    stopped_at = 0      # 1-based index of the mail the run stopped on (0 = ran to the end)

    def prog():
        """Counters as of right now (recomputed per emit, not snapshotted)."""
        return dict(index=idx, total=total, found=len(emails), scanned=scanned,
                    skipped=skipped, known=known)

    for idx, msg in enumerate(messages, 1):
        mid = msg["id"]
        if mid in existing:
            known += 1
            emit('skip', reason='already', **prog())
            continue
        emit('read', **prog())
        try:
            full = service.users().messages().get(
                userId="me", id=mid, format="full").execute()
        except Exception:
            continue
        payload = full["payload"]
        body = extract_text(payload)
        if not body:
            # Nothing readable (image-only / attachment-only mail): same verdict as the
            # pre-filter, so remember it instead of downloading it again every sync.
            skipped += 1
            mark_seen(conn, [mid], 'filtered')
            existing.add(mid)
            continue
        sender = _header(payload, "From")
        subject = _header(payload, "Subject")
        ms = int(full.get("internalDate", time.time() * 1000))

        # Pre-filter before calling the AI — skip if it does not look like a purchase email (saves quota)
        if not _looks_like_purchase(sender, subject, body, allow, keywords):
            skipped += 1
            mark_seen(conn, [mid], 'filtered')
            existing.add(mid)
            emit('skip', reason='filtered', subject=subject, sender=sender, **prog())
            continue

        scanned += 1
        emit('analyze', subject=subject, sender=sender, **prog())
        try:
            items = parse_email_ai(sender, subject, body, ms, shops=shops)
        except gemini.QuotaError as e:
            quota = e           # Quota exceeded -> stop without calling more (already-processed items are saved)
            stopped_at = idx
            emit('quota', per_day=e.per_day, **prog())
            break
        except gemini.OverloadError as e:
            # Gemini itself is busy (503). One busy mail is not a reason to abandon the
            # whole inbox: skip it (it stays unseen, so the next sync retries it) and
            # carry on. Only a run of MAX_BUSY_STREAK failures means the model is truly
            # down, and then we stop and keep whatever was parsed.
            overload = e
            busy_streak += 1
            emit('overload', busy_streak=busy_streak, **prog())
            if busy_streak >= MAX_BUSY_STREAK:
                stopped_at = idx
                break
            continue
        except gemini.ModelError as e:
            # The configured model is gone (404). Every further mail would fail the
            # same way, so stop at once instead of silently skipping the whole inbox.
            model_error = e
            stopped_at = idx
            emit('model_error', **prog())
            break
        busy_streak = 0
        items = dups.check(refiner.refine(items), mid)
        if gemini.LAST_TIER == 'paid' and not on_paid:
            on_paid = True                        # free quota spent, paid key took over
            emit('tier_switch', **prog())
        if gemini.LAST_MODEL and gemini.LAST_MODEL != active_model:
            active_model = gemini.LAST_MODEL      # auto mode moved to another model
            emit('model_switch', model=active_model, **prog())
        emit('parsed', subject=subject, sender=sender, items=len(items), **prog())
        if not items:
            # The AI read it and found no purchase — never spend a call on it again.
            mark_seen(conn, [mid], 'no_items')
            existing.add(mid)
            continue
        dt = datetime.datetime.fromtimestamp(ms / 1000)
        emails.append({
            "gmail_id": mid,
            "sender": sender,
            "subject": subject,
            "date": f"{dt.year}-{dt.month:02d}-{dt.day:02d}",
            "items": items,
            # Kept so the user can read the source mail before adding anything,
            # the way a receipt photo can be opened on its review page.
            "body": body[:BODY_PREVIEW_CHARS],
            "body_truncated": len(body) > BODY_PREVIEW_CHARS,
        })
        existing.add(mid)

    # Mails the run never got to. `existing` grew as we went, so anything still unknown
    # there is genuinely unprocessed — that is the "rest" the user is told to sync later.
    # `stopped_at` is 1-based and the mail it names was NOT processed (quota/model
    # error hit on it), so the slice starts at that mail itself.
    left = ([m for m in messages[stopped_at - 1:] if m["id"] not in existing]
            if stopped_at else [])
    remaining = len(left)
    # Subjects of the first few, so "some of them" is not a mystery. Metadata-only Gmail
    # reads: no AI call, no quota cost — that is why it is safe after a quota stop.
    remaining_samples = []
    for m in left[:REMAINING_PREVIEW]:
        try:
            meta = service.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From"]).execute()
            remaining_samples.append({
                "subject": _header(meta["payload"], "Subject") or "(no subject)",
                "sender": _header(meta["payload"], "From"),
            })
        except Exception:
            break        # previewing must never break the sync result
    emit('done', found=len(emails), scanned=scanned, skipped=skipped, known=known, total=total)
    return {"emails": emails, "scanned": scanned, "skipped": skipped, "known": known,
            "quota": quota, "quota_exceeded": bool(quota),
            "overloaded": bool(overload) and busy_streak >= MAX_BUSY_STREAK,
            "busy_skipped": bool(overload),
            "model_error": str(model_error) if model_error else "",
            "total": total, "examined": (stopped_at - 1) if stopped_at else total,
            "remaining": remaining, "remaining_samples": remaining_samples}


# How many mails in a row may fail with 503 before the sync gives up. Below this the
# sync just skips the busy ones and keeps going.
MAX_BUSY_STREAK = 3

# How many of the not-yet-processed mails are previewed by subject when a run stops
# early, so the user can see what "the rest" actually is.
REMAINING_PREVIEW = 10

# How much of the mail text is kept for the review page's "original mail" view. The
# staged session is a JSON file, so the whole inbox must not end up in it.
BODY_PREVIEW_CHARS = 4000


def mark_seen(conn, gmail_ids, reason: str) -> None:
    """Remember mails that must not be fetched/analyzed again.

    A gmail_id lands in `transactions` only when an item is actually saved, so
    without this table every sync re-downloads and re-analyzes the same mails that
    were filtered out, parsed to nothing, or left unchecked on the review page.

    reason: 'filtered' (pre-filter, no AI call) | 'no_items' (AI found nothing) |
            'reviewed' (shown on the review page, saved or not).
    """
    ids = [str(g) for g in gmail_ids if g]
    if not ids:
        return
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO mail_seen (gmail_id, reason, seen_at) VALUES (?,?,?) "
        "ON CONFLICT(gmail_id) DO UPDATE SET reason=excluded.reason, seen_at=excluded.seen_at",
        [(g, reason, now) for g in ids])
    conn.commit()


def _filter_fingerprint(keywords, allow) -> str:
    """Identity of the current pre-filter config, so 'filtered' can be re-evaluated."""
    return repr((sorted(keywords), sorted(allow)))


def _drop_stale_filtered(conn, keywords, allow) -> None:
    """Forget 'filtered' mails when the keyword/sender settings changed.

    Those were skipped by the old rules; under new rules they deserve another look.
    'no_items' / 'reviewed' entries are unaffected — the AI already saw them.
    """
    fp = _filter_fingerprint(keywords, allow)
    row = conn.execute("SELECT value FROM settings WHERE key='mail_filter_fingerprint'").fetchone()
    if row and row["value"] == fp:
        return
    conn.execute("DELETE FROM mail_seen WHERE reason='filtered'")
    conn.execute("INSERT INTO settings (key,value) VALUES ('mail_filter_fingerprint',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fp,))
    conn.commit()


def insert_email_items(conn, items: list) -> int:
    """Insert user-selected mail items (each carrying its own gmail_id). Returns inserted count.

    Email-level dedup: skip any gmail_id already present in the ledger (guards a
    double confirm / re-sync). Siblings of the same email are unaffected because the
    'existing' set is snapshotted once up front, not updated per row.
    """
    existing = set(
        r[0] for r in conn.execute(
            "SELECT gmail_id FROM transactions "
            "WHERE gmail_id != '' AND gmail_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchall() if r[0]
    )
    rows = []
    for it in items:
        gid = str(it.get("gmail_id") or "")
        if gid and gid in existing:
            continue
        try:
            amount = int(round(float(it.get("amount"))))
        except (ValueError, TypeError):
            continue
        if amount == 0:
            continue
        item_name = str(it.get("item", "")).strip()
        if not item_name:
            continue
        rows.append({
            # Keep a date the user typed on the review page; only fill in a blank/broken one.
            "date": normalize_date(it.get("date"), fix_year=False),
            "type": str(it.get("type") or ("수입" if amount > 0 else "지출")),
            "major": str(it.get("major", "")).strip(),
            "minor": str(it.get("minor", "")).strip(),
            "category": str(it.get("sub", "")).strip(),
            "shop": str(it.get("shop", "")).strip(),
            "item": item_name,
            "jpy_amount": amount, "krw_amount": None,
            "note": str(it.get("note") or "").strip(), "exchange_rate": None,
            "source": "email", "gmail_id": gid,
        })
    if rows:
        conn.executemany("""
            INSERT INTO transactions
            (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source,gmail_id)
            VALUES (:date,:type,:major,:minor,:category,:shop,:item,:jpy_amount,:krw_amount,:note,:exchange_rate,:source,:gmail_id)
        """, rows)
        conn.commit()
    return len(rows)


def sync_generic(conn, days: int = None, query: str = None, max_results: int = None) -> dict:
    """One-shot fetch + parse + INSERT (no review). Kept for programmatic callers."""
    res = collect_generic(conn, days=days, query=query, max_results=max_results)
    if res.get("error"):
        return {"error": res["error"], "new_count": 0}

    items = [{**it, "gmail_id": em["gmail_id"]}
             for em in res["emails"] for it in em["items"]]
    n = insert_email_items(conn, items)
    scanned, skipped = res["scanned"], res["skipped"]

    if res.get("quota"):
        quota = res["quota"]
        limit = t('msg.quota.per_day') if quota.per_day else t('msg.quota.per_min')
        cont = t('msg.cont.tomorrow_sync') if quota.per_day else t('msg.cont.later_sync')
        return {
            "new_count": n, "quota_exceeded": True,
            "message": t('msg.mail_quota_stopped', n=n, limit=limit, cont=cont, skipped=skipped,
                         total=res.get("total", 0), done=res.get("examined", 0),
                         remaining=res.get("remaining", 0)),
        }
    if res.get("overloaded"):
        return {
            "new_count": n, "overloaded": True,
            "message": t('msg.mail_overload_stopped', n=n,
                         total=res.get("total", 0), done=res.get("examined", 0),
                         remaining=res.get("remaining", 0)),
        }
    if res.get("model_error"):
        return {
            "new_count": n, "model_error": True,
            "message": t('msg.model_unavailable', model=gemini.get_model_id()),
        }
    if not res["emails"] and scanned == 0 and skipped == 0:
        return {"new_count": 0, "message": t('msg.no_new_mail')}
    return {
        "new_count": n,
        "message": t('msg.mail_synced', scanned=scanned, skipped=skipped, n=n),
    }
