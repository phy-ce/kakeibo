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
    if text:
        return text
    if html:
        return BeautifulSoup("".join(html), "html.parser").get_text(separator="\n")
    return ""


def _header(payload, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _build_prompt(sender: str, subject: str, body: str, order_date: str) -> str:
    cats = gemini.categories_text()
    # If the body is too long, truncate it to save tokens
    body = body[:8000]
    return f"""너는 가계부 앱의 메일 파서다. 아래는 수신한 이메일 한 통이다.
이게 '실제 구매/결제 내역'(주문확인, 발송알림, 영수증, 결제완료 등)이면 구매 품목을 JSON 배열로 추출해라.
광고/뉴스레터/배송상태만 알리는 등 금액이 없는 메일이면 빈 배열 [] 을 반환해라.

[규칙]
- 각 품목을 한 원소로: date, type, major, minor, sub, shop, item, amount
- amount 는 엔(¥) 정수. 지출은 음수, 환불/적립/수입은 양수.
- 배송비(送料), 할인(割引/ディスカウント), 소비税(消費税)도 각각 별도 항목으로 넣어
  모든 amount 의 합이 '주문합계/ご請求金額/注文合計' 과 일치하게 해라.
  (할인은 지출을 줄이므로 양수 amount)
- shop 은 판매처명(원어 그대로). 알 수 없으면 발신 도메인 사용.
- date 는 구매/주문일(YYYY-MM-DD). 본문에 없으면 {order_date} 사용.
- type 은 지출 또는 수입.
- major/minor/sub 는 아래 분류 체계에서 골라라. 적절한 게 없으면 minor/sub 는 "".

[사용 가능한 분류 체계]
{cats}

[이메일]
보낸사람: {sender}
제목: {subject}
본문:
{body}

JSON 배열만 출력해라."""


def parse_email_ai(sender: str, subject: str, body: str, msg_date_ms: int) -> list:
    """Use the AI to extract a list of transaction items from the mail. Returns [] if it is not a purchase email."""
    dt = datetime.datetime.fromtimestamp(msg_date_ms / 1000)
    order_date = f"{dt.year}-{dt.month:02d}-{dt.day:02d}"

    try:
        data = gemini.generate_json(_build_prompt(sender, subject, body, order_date))
    except gemini.QuotaError:
        raise  # Quota exceeded is caught by the caller (sync_generic), which stops the sync
    except Exception as e:
        print(f"[generic] AI 파싱 오류: {e}")
        return []
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]

    items = []
    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            amount = int(round(float(d.get("amount", 0))))
        except (ValueError, TypeError):
            continue
        if amount == 0:
            continue
        item_name = str(d.get("item", "")).strip()
        if not item_name:
            continue
        items.append({
            "date":   str(d.get("date") or order_date)[:10],
            "type":   str(d.get("type") or ("수입" if amount > 0 else "지출")),
            "major":  str(d.get("major", "")).strip(),
            "minor":  str(d.get("minor", "")).strip(),
            "sub":    str(d.get("sub", "")).strip(),
            "shop":   str(d.get("shop", "")).strip() or sender,
            "item":   item_name,
            "amount": amount,
        })
    return items


def sync_generic(conn, days: int = None, query: str = None, max_results: int = None) -> dict:
    try:
        service = get_gmail_service()
    except Exception as e:
        return {"error": t('msg.gmail_auth_failed', e=e), "new_count": 0}

    if days is None:
        row = conn.execute("SELECT value FROM settings WHERE key='mail_sync_days'").fetchone()
        days = int(row["value"]) if row else 30

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

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results).execute()
    except Exception as e:
        return {"error": str(e), "new_count": 0}

    messages = results.get("messages", [])
    if not messages:
        return {"new_count": 0, "message": t('msg.no_new_mail')}

    # Filter out already-imported gmail_ids before calling the AI to save cost
    existing = set(
        r[0] for r in conn.execute(
            "SELECT gmail_id FROM transactions "
            "WHERE gmail_id != '' AND gmail_id IS NOT NULL AND deleted_at IS NULL"
        ).fetchall() if r[0]
    )

    new_rows = []
    scanned = 0
    skipped = 0
    quota = None
    for msg in messages:
        mid = msg["id"]
        if mid in existing:
            continue
        try:
            full = service.users().messages().get(
                userId="me", id=mid, format="full").execute()
        except Exception:
            continue
        payload = full["payload"]
        body = extract_text(payload)
        if not body:
            continue
        sender = _header(payload, "From")
        subject = _header(payload, "Subject")
        ms = int(full.get("internalDate", time.time() * 1000))

        # Pre-filter before calling the AI — skip if it does not look like a purchase email (saves quota)
        if not _looks_like_purchase(sender, subject, body, allow, keywords):
            skipped += 1
            continue

        scanned += 1
        try:
            items = parse_email_ai(sender, subject, body, ms)
        except gemini.QuotaError as e:
            quota = e           # Quota exceeded -> stop without calling more (already-processed items are saved)
            break
        if not items:
            continue
        for it in items:
            new_rows.append({
                "date": it["date"], "type": it["type"],
                "major": it["major"], "minor": it["minor"], "category": it["sub"],
                "shop": it["shop"], "item": it["item"],
                "jpy_amount": it["amount"], "krw_amount": None,
                "note": "", "exchange_rate": None,
                "source": "email", "gmail_id": mid,
            })
        existing.add(mid)

    if new_rows:
        conn.executemany("""
            INSERT INTO transactions
            (date,type,major,minor,category,shop,item,jpy_amount,krw_amount,note,exchange_rate,source,gmail_id)
            VALUES (:date,:type,:major,:minor,:category,:shop,:item,:jpy_amount,:krw_amount,:note,:exchange_rate,:source,:gmail_id)
        """, new_rows)
        conn.commit()

    if quota:
        limit = t('msg.quota.per_day') if quota.per_day else t('msg.quota.per_min')
        cont = t('msg.cont.tomorrow_sync') if quota.per_day else t('msg.cont.later_sync')
        return {
            "new_count": len(new_rows),
            "quota_exceeded": True,
            "message": t('msg.mail_quota_stopped', n=len(new_rows), limit=limit,
                         cont=cont, skipped=skipped),
        }

    return {
        "new_count": len(new_rows),
        "message": t('msg.mail_synced', scanned=scanned, skipped=skipped, n=len(new_rows)),
    }
