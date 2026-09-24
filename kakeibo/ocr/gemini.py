"""Receipt photo OCR with Gemini Flash."""
import datetime
import json
import os
import random
import re
import time

from google import genai
from google.genai import types
from PIL import Image, ImageOps

from ..dates import MIN_YEAR
from ..i18n import t

# Default model (env override, else built-in). Used when no GUI choice is stored.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# Models selectable from the GUI (settings page). id -> human label.
# Curated from the account's live model list (generateContent + vision capable);
# image/TTS/robotics/deep-research/gemma models are intentionally excluded.
AVAILABLE_MODELS = [
    ("gemini-3.7-flash", "Gemini 3.7 Flash (fast, default)"),
    ("gemini-3.6-flash", "Gemini 3.6 Flash"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite (cheapest)"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite"),
    ("gemini-flash-latest", "Gemini Flash (latest alias)"),
    ("gemini-flash-lite-latest", "Gemini Flash-Lite (latest alias)"),
    ("gemini-pro-latest", "Gemini Pro (latest, most accurate)"),
]

# Ids accepted by get_model_id(); a stored setting outside this set is stale
# (Google retires models, and a retired id answers 404 on every call).
_MODEL_IDS = {m[0] for m in AVAILABLE_MODELS}

# Model that answered the most recent successful call (auto mode switches it around,
# so callers can show which one actually did the work).
LAST_MODEL = ""

# Waits between retries when the model itself is busy (503 UNAVAILABLE / overloaded).
# Interactive default: someone is watching a spinner, so give up after ~23s.
OVERLOAD_BACKOFF = (2, 6, 15)

# Background default (mail sync runs on a worker thread, nobody is blocked). Capacity
# spikes routinely last longer than the interactive budget, so wait them out.
OVERLOAD_BACKOFF_BG = (5, 15, 40, 90)


def get_model_id() -> str:
    """Model id to use, read at call time.

    Priority: GUI setting (`gemini_model` in the settings table) -> DEFAULT_MODEL.
    Read lazily so a change on the settings page takes effect without a restart.
    """
    try:
        from ..db import get_setting
        val = get_setting("gemini_model", "")
        # Ignore an id that is no longer offered — an old setting pointing at a
        # retired model would otherwise 404 on every call.
        if val and val in _MODEL_IDS:
            return val
    except Exception:
        pass
    return DEFAULT_MODEL


def get_fallback_model_id() -> str:
    """Model to try when the chosen one stays overloaded ('' = don't fall back).

    A 503 is per-model capacity, so a second model often answers straight away.
    """
    try:
        from ..db import get_setting
        val = get_setting("gemini_fallback_model", "") or ""
        return val if val in _MODEL_IDS else ""
    except Exception:
        return ""


# Models known to be unusable right now -> {model: unix time it may be tried again}.
# Without this, every call in a batch would re-hit the same exhausted model first.
_cooldown = {}

COOLDOWN_RPM = 70          # per-minute quota: usable again after the minute rolls over
COOLDOWN_PER_DAY = 6 * 3600
COOLDOWN_BUSY = 120
COOLDOWN_GONE = 24 * 3600  # 404: the id is retired, do not probe it again this session


def _available(models: list, tier: str) -> list:
    """Drop models still cooling down on this key — unless that would leave nothing to call."""
    now = time.time()
    live = [m for m in models if _cooldown.get(f"{tier}:{m}", 0) <= now]
    return live or models[:1]


def auto_model_enabled() -> bool:
    """'Auto' mode: on failure, walk down AVAILABLE_MODELS instead of giving up.

    Free-tier quota is counted per model, so when one model is exhausted (429) or
    retired (404) or busy (503), the next one down the list often answers right away.
    """
    try:
        from ..db import get_setting
        return str(get_setting("gemini_auto_model", "0")) in ("1", "true", "True")
    except Exception:
        return False


def _model_chain(tier: str = "free") -> list:
    """Models to try, in order: the chosen one, its fallback, then (auto only) the rest."""
    chain = [get_model_id()]
    fb = get_fallback_model_id()
    if fb and fb not in chain:
        chain.append(fb)
    if auto_model_enabled():
        chain += [m for m, _ in AVAILABLE_MODELS if m not in chain]
        return _available(chain, tier)
    return chain


class QuotaError(Exception):
    """Gemini free quota exceeded (429). If per_day=True, it is the daily limit (retrying same day is pointless)."""
    def __init__(self, message="", per_day=False, retry_after=0):
        super().__init__(message)
        self.per_day = per_day
        self.retry_after = retry_after


class OverloadError(Exception):
    """Gemini is busy (503 UNAVAILABLE) and stayed busy after the retries + fallback model.

    Not the user's fault and not a quota problem: retrying later usually just works.
    """


class ModelError(Exception):
    """The requested model does not exist / is no longer served (404 NOT_FOUND).

    A configuration problem, not a transient one: retrying never helps, so the caller
    must stop and tell the user to pick another model on the settings page.
    """


def _is_model_error(msg: str) -> bool:
    """Model id rejected outright (retired or unavailable to this key)."""
    up = msg.upper()
    return "404" in up and "NOT_FOUND" in up


def _is_quota_error(msg: str) -> bool:
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _is_overload_error(msg: str) -> bool:
    """Transient server-side capacity error (503/502/504), worth retrying."""
    up = msg.upper()
    return any(tok in up for tok in
               ("UNAVAILABLE", "OVERLOADED", "HIGH DEMAND", "503", "502", "504"))


def _parse_quota(msg: str):
    """Extract (whether it is the daily limit, retry wait seconds) from a 429 message."""
    per_day = "PerDay" in msg or "per day" in msg.lower()
    m = re.search(r"[Rr]etry(?:Delay|.{0,4}in)['\"]?\s*[:=]?\s*['\"]?(\d+)", msg)
    return per_day, (int(m.group(1)) if m else 30)


# Two API keys = two billing tiers. Free/paid is a property of the Google Cloud
# project, not of the key, so the paid key must come from a separate project with
# billing enabled. The free key is always tried first; the paid one only takes over
# once the free quota is spent (429), so money is spent only past the free limit.
TIER_KEYS = (("free", "GEMINI_API_KEY"), ("paid", "GEMINI_API_KEY_PAID"))

_clients = {}          # tier -> genai.Client

# Tier that answered the most recent successful call ('free' / 'paid').
LAST_TIER = ""

# Free tier known to be exhausted -> unix time it may be tried again. While it
# cools down, calls go straight to the paid key instead of burning a 429 first.
_tier_cooldown = {}


def paid_key_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY_PAID", "").strip())


def _tiers() -> list:
    """Tiers to try, in order (keys read at call time, so import order does not matter)."""
    have = [tier for tier, env in TIER_KEYS if os.environ.get(env, "").strip()]
    if not have:
        raise RuntimeError(t('msg.no_api_key'))
    now = time.time()
    live = [tier for tier in have if _tier_cooldown.get(tier, 0) <= now]
    return live or have[:1]


def _get_client(tier: str = "free"):
    if tier not in _clients:
        env = dict(TIER_KEYS)[tier]
        _clients[tier] = genai.Client(api_key=os.environ[env].strip())
    return _clients[tier]


def generate_json(contents, max_retries=3, backoff=None, schema=None):
    """Call the model -> parsed JSON (None if the response is empty).

    Tries the free key first and moves to the paid key (GEMINI_API_KEY_PAID, if
    set) only when the free tier answers 429. Busy (503) and retired-model (404)
    errors are not a quota problem, so they never move the call onto the paid key.
    See _generate_on() for the per-key retry policy.
    """
    global LAST_TIER
    tiers = _tiers()
    for i, tier in enumerate(tiers):
        try:
            data = _generate_on(tier, contents, max_retries, backoff, schema)
            LAST_TIER = tier
            return data
        except QuotaError as e:
            if i == len(tiers) - 1:
                raise
            _tier_cooldown[tier] = time.time() + (COOLDOWN_PER_DAY if e.per_day else COOLDOWN_RPM)
            print(f"[gemini] {tier} quota used up, switching to the {tiers[i + 1]} key")
    return None


def _generate_on(tier, contents, max_retries=3, backoff=None, schema=None):
    """Call the model with one key (text/[image, prompt]) -> return parsed JSON. None if the response is empty.

    Every model call in the app goes through here, so retry policy lives in one place:

    429 (quota exceeded):
    - For the per-minute limit (RPM), retry after a short wait.
    - For the daily limit (RPD), or if retries do not resolve it, raise QuotaError (the caller decides to stop).

    503 UNAVAILABLE ("high demand" - the model is busy, nothing to do with our quota):
    - Retry with the `backoff` waits (OVERLOAD_BACKOFF by default; pass
      OVERLOAD_BACKOFF_BG from a background worker), then try the fallback model if
      one is configured, then raise OverloadError so the caller can stop cleanly.

    404 NOT_FOUND (the model id is retired/unknown):
    - No retry, try the fallback model once, then raise ModelError.

    `schema` (optional) is a response schema: the model is then held to that exact
    JSON shape (field names, integer amounts). If a model rejects the schema
    (400 INVALID_ARGUMENT) the call is repeated once without it.
    """
    global LAST_MODEL
    waits = backoff or OVERLOAD_BACKOFF
    max_retries = max(max_retries, len(waits))
    client = _get_client(tier)
    if isinstance(contents, str):
        contents = [contents]

    # Quota is per project, so model cooldowns are tracked per tier.
    def _cool(model, seconds):
        _cooldown[f"{tier}:{model}"] = time.time() + seconds

    models = _model_chain(tier)
    auto = len(models) > 1 and auto_model_enabled()

    last_overload = ""
    last_model_error = ""
    last_quota = None
    for mi, model in enumerate(models):
        more_models = mi < len(models) - 1
        for attempt in range(max_retries + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema),
                )
                _cooldown.pop(f"{tier}:{model}", None)
                LAST_MODEL = model
                return json.loads(response.text) if response.text else None
            except Exception as e:
                msg = str(e)
                if schema is not None and "INVALID_ARGUMENT" in msg.upper():
                    # Schema not supported by this model/API version: plain JSON mode
                    # still works, and clean_items() copes with the looser output.
                    print(f"[gemini] schema rejected ({model}), retrying without it")
                    schema = None
                    continue
                if _is_quota_error(msg):
                    per_day, retry_after = _parse_quota(msg)
                    last_quota = QuotaError(msg, per_day=per_day, retry_after=retry_after)
                    _cool(model, COOLDOWN_PER_DAY if per_day else COOLDOWN_RPM)
                    # Auto mode: this model's quota is spent, so hand over to the next
                    # one instead of waiting or stopping the whole run.
                    if auto and more_models:
                        print(f"[gemini] quota exhausted ({model}), switching model")
                        break
                    # Per-minute limit with a short wait -> retry; otherwise let the caller stop
                    if (not per_day) and attempt < max_retries and retry_after <= 8:
                        time.sleep(retry_after + 1)
                        continue
                    raise last_quota
                if _is_model_error(msg):
                    # Retired/unknown model: retrying is pointless, but the fallback
                    # model (if any) may still be alive, so try that before giving up.
                    last_model_error = msg
                    _cool(model, COOLDOWN_GONE)
                    print(f"[gemini] model not available ({model})")
                    break
                if _is_overload_error(msg):
                    last_overload = msg
                    # Auto mode: another model is cheaper than sitting out a spike
                    if auto and more_models:
                        _cool(model, COOLDOWN_BUSY)
                        print(f"[gemini] model busy ({model}), switching model")
                        break
                    if attempt < len(waits):
                        # Jitter so parallel/consecutive calls do not all retry in lockstep
                        wait = waits[attempt] * random.uniform(0.8, 1.3)
                        print(f"[gemini] model busy ({model}), retrying in {wait:.0f}s")
                        time.sleep(wait)
                        continue
                    break            # exhausted this model -> try the fallback, if any
                raise
    # Every model in the chain failed -> report the most actionable reason
    if last_quota:
        raise last_quota
    if last_overload:
        raise OverloadError(last_overload)
    if last_model_error:
        raise ModelError(last_model_error)
    return None


def categories_text() -> str:
    """Serialize the expense category scheme as prompt text (also used by external modules)."""
    return _categories_text()


def _categories_text() -> str:
    """Serialize the expense categories currently in the DB as text for the OCR prompt."""
    # Lazy import — avoid a DB dependency at module load time
    from ..db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT major, minor, sub FROM categories "
        "WHERE type='지출' ORDER BY major, minor, sub"
    ).fetchall()
    conn.close()
    lines = ["대분류\t소분류\t세부"]
    for r in rows:
        lines.append(f"{r['major']}\t{r['minor']}\t{r['sub']}")
    return "\n".join(lines)


# Response shape of one extracted row (receipt OCR and mail parsing share it).
ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "date":   {"type": "STRING"},
        "type":   {"type": "STRING", "enum": ["지출", "수입"]},
        "major":  {"type": "STRING"},
        "minor":  {"type": "STRING"},
        "sub":    {"type": "STRING"},
        "shop":   {"type": "STRING"},
        "item":   {"type": "STRING"},
        "amount": {"type": "INTEGER"},
        # Full product title when `item` was shortened (mail parsing); optional.
        "note":   {"type": "STRING"},
    },
    "required": ["date", "type", "major", "minor", "sub", "shop", "item", "amount"],
}
ITEMS_SCHEMA = {"type": "ARRAY", "items": ITEM_SCHEMA}

# A receipt also reports its printed total, so the review page can check that the
# extracted lines add up (a missed or doubled line is the typical OCR error).
RECEIPT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "total": {"type": "INTEGER", "nullable": True},
        "items": ITEMS_SCHEMA,
    },
    "required": ["items"],
}

# Longest side of the photo sent to the model. Phone photos are 4000px+; beyond
# ~2000px small receipt print gains nothing but upload time and image tokens.
OCR_MAX_DIM = 2048


def load_photo(image_path: str, max_dim: int = OCR_MAX_DIM) -> Image.Image:
    """Open a photo upright (EXIF rotation applied) and shrink it to `max_dim`.

    Phones store most shots sideways plus an EXIF 'rotate me' flag that PIL
    ignores, so without exif_transpose the model reads a receipt lying on its side.
    """
    img = ImageOps.exif_transpose(Image.open(image_path))
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def analyze_receipt(image_path: str) -> dict:
    """OCR one receipt photo -> {"items": [raw item dicts], "total": int | None}.

    Items are the model's raw output; the caller runs them through
    ai_items.clean_items() / Refiner like every other AI source.
    """
    try:
        image = load_photo(image_path)
        today = datetime.date.today()
        prompt = f"""
이 영수증을 읽어서 JSON 으로 추출해줘.

오늘은 {today.isoformat()} 이다.

[출력]
- total: 영수증에 인쇄된 최종 결제 합계(合計/お会計/ご請求額, 세금 포함)를 양의 정수로. 안 보이면 null.
- items: 구매 품목 배열. 각 원소의 필드:
  - date (YYYY-MM-DD). 영수증에 연도가 없거나 흐려서 읽을 수 없으면 올해({today.year})로 써.
    {MIN_YEAR}년보다 이전 연도는 오독이니 쓰지 마.
  - type (지출/수입)
  - major (대분류) / minor (소분류) / sub (세부 카테고리)
  - shop (사용처, 원어 그대로)
  - item (품목명, 원어 그대로)
  - amount (그 줄의 금액 정수. 지출은 음수)

[규칙]
- 수량이 2개 이상인 줄은 단가가 아니라 그 줄의 금액(수량 x 단가)을 amount 로.
- 할인(値引/割引/クーポン)은 별도 항목으로, amount 는 양수 (지출을 줄이므로).
- 소비세가 外税 로 따로 적혀 있으면 '消費税' 항목으로 추가. 内税(가격에 포함)면 추가하지 마.
- 小計/合計/お預り/お釣り/ポイント残高/支払方法 같은 줄은 품목이 아니다. 넣지 마.
- 모든 amount 의 합 = -(total) 이 되게 해라.

[사용 가능한 분류 체계] (이 중에서만 골라)
{_categories_text()}

위 분류 중 적절한 게 없으면 minor/sub 는 빈 문자열로 둬.
"""
        # Same retry/fallback path as mail parsing (429 + 503 handling lives there)
        data = generate_json([image, prompt], schema=RECEIPT_SCHEMA)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            total = data.get("total")
            return {"items": data["items"],
                    "total": abs(int(total)) if isinstance(total, (int, float)) and total else None}
        if isinstance(data, list):        # schema was dropped and the model sent a bare array
            return {"items": data, "total": None}
    except (QuotaError, OverloadError, ModelError):
        raise  # Let the caller stop cleanly instead of misreading this as "0 items"
    except Exception as e:
        print(f"OCR error: {type(e).__name__}")   # ASCII only (cp949 console)
    return {"items": [], "total": None}
