"""Receipt photo OCR with Gemini Flash."""
import json
import os
import re
import time

from google import genai
from google.genai import types
from PIL import Image

from ..i18n import t

# Default model (env override, else built-in). Used when no GUI choice is stored.
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# Models selectable from the GUI (settings page). id -> human label.
# Curated from the account's live model list (generateContent + vision capable);
# image/TTS/robotics/deep-research/gemma models are intentionally excluded.
AVAILABLE_MODELS = [
    ("gemini-flash-latest", "Gemini Flash (latest)"),
    ("gemini-flash-lite-latest", "Gemini Flash-Lite (latest, cheapest)"),
    ("gemini-pro-latest", "Gemini Pro (latest, most accurate)"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite"),
    ("gemini-2.0-flash", "Gemini 2.0 Flash"),
]

_client = None


def get_model_id() -> str:
    """Model id to use, read at call time.

    Priority: GUI setting (`gemini_model` in the settings table) -> DEFAULT_MODEL.
    Read lazily so a change on the settings page takes effect without a restart.
    """
    try:
        from ..db import get_setting
        val = get_setting("gemini_model", "")
        if val:
            return val
    except Exception:
        pass
    return DEFAULT_MODEL


class QuotaError(Exception):
    """Gemini free quota exceeded (429). If per_day=True, it is the daily limit (retrying same day is pointless)."""
    def __init__(self, message="", per_day=False, retry_after=0):
        super().__init__(message)
        self.per_day = per_day
        self.retry_after = retry_after


def _is_quota_error(msg: str) -> bool:
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _parse_quota(msg: str):
    """Extract (whether it is the daily limit, retry wait seconds) from a 429 message."""
    per_day = "PerDay" in msg or "per day" in msg.lower()
    m = re.search(r"[Rr]etry(?:Delay|.{0,4}in)['\"]?\s*[:=]?\s*['\"]?(\d+)", msg)
    return per_day, (int(m.group(1)) if m else 30)


def _get_client():
    global _client
    if _client is None:
        # Read the key at call time (so it is not affected by import order).
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(t('msg.no_api_key'))
        _client = genai.Client(api_key=api_key)
    return _client


def generate_json(contents, max_retries=2):
    """Call the model with arbitrary contents (text/[image, prompt]) -> return parsed JSON. None if the response is empty.

    429 (quota exceeded) handling:
    - For the per-minute limit (RPM), retry after a short wait.
    - For the daily limit (RPD), or if retries do not resolve it, raise QuotaError (the caller decides to stop).
    """
    client = _get_client()
    if isinstance(contents, str):
        contents = [contents]
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=get_model_id(),
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(response.text) if response.text else None
        except QuotaError:
            raise
        except Exception as e:
            msg = str(e)
            if not _is_quota_error(msg):
                raise
            per_day, retry_after = _parse_quota(msg)
            # If it is the per-minute limit and the wait is short, retry once or twice; otherwise give up
            if (not per_day) and attempt < max_retries and retry_after <= 8:
                time.sleep(retry_after + 1)
                continue
            raise QuotaError(msg, per_day=per_day, retry_after=retry_after)
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


def analyze_receipt(image_path: str) -> list:
    try:
        client = _get_client()
        image = Image.open(image_path)
        prompt = f"""
이 영수증에 있는 모든 구매 품목을 JSON 배열로 추출해줘.

출력 필드:
- date (YYYY-MM-DD)
- type (지출/수입)
- major (대분류)
- minor (소분류)
- sub (세부 카테고리)
- shop (사용처, 원어 그대로)
- item (품목명, 원어 그대로)
- amount (지출은 음수 정수)

사용 가능한 분류 체계 (이 중에서만 골라):
{_categories_text()}

위 분류 중 적절한 게 없으면 minor/sub 는 빈 문자열로 둬.
결과는 JSON 배열만 출력해.
"""
        response = client.models.generate_content(
            model=get_model_id(),
            contents=[image, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        if response.text:
            data = json.loads(response.text)
            return data if isinstance(data, list) else [data]
    except QuotaError:
        raise  # Propagate quota-exceeded up so the caller catches it and does not misread it as "0 items"
    except Exception as e:
        msg = str(e)
        if _is_quota_error(msg):
            per_day, retry_after = _parse_quota(msg)
            raise QuotaError(msg, per_day=per_day, retry_after=retry_after)
        print(f"OCR error: {e}")
    return []
