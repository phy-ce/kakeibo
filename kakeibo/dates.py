"""Date normalization for AI-extracted dates (receipt OCR / mail parsing).

Receipts and order mails older than MIN_YEAR are never entered by hand, so a
year that is missing, unparsable or older than MIN_YEAR is a misread (blurred
receipt, cropped photo, hallucinated year) -> fall back to the current year.
"""
import datetime
import re

# Oldest year we accept from the AI. Anything older is treated as a misread.
MIN_YEAR = 2025
# A date this far ahead is a misread day, not a receipt from the previous year.
_FUTURE_GRACE_DAYS = 15

_FULL_RE = re.compile(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})")   # 2026-08-14, 2026/8/14, 2026年8月14日
_MD_RE   = re.compile(r"(?<!\d)(\d{1,2})\D{1,3}(\d{1,2})(?!\d)")     # 08-14 (year missing)


def _valid(year: int, month: int, day: int):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def normalize_date(raw, fix_year: bool = True, fallback: str = "", today=None) -> str:
    """Return a YYYY-MM-DD string with a plausible year.

    - `raw` may be any of 2026-08-14 / 2026.8.14 / 2026年8月14日 / 08-14 / ''.
    - Unparsable month+day -> `fallback` (normalized too), else today.
    - `fix_year=True` replaces a missing / out-of-range year with the current
      one; if that lands in the future the previous year is used instead
      (still >= MIN_YEAR). Pass False to keep a user-typed year as-is.
    """
    today = today or datetime.date.today()
    s = str(raw or "").strip()

    m = _FULL_RE.search(s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _MD_RE.search(s)
        if not m:
            if fallback:
                return normalize_date(fallback, fix_year=fix_year, today=today)
            return today.isoformat()
        year, month, day = 0, int(m.group(1)), int(m.group(2))

    if not fix_year and year and _valid(year, month, day):
        return _valid(year, month, day).isoformat()

    if year and MIN_YEAR <= year <= today.year and _valid(year, month, day):
        return _valid(year, month, day).isoformat()

    # Year missing or implausible -> this year (previous year only if clearly in the future).
    d = _valid(today.year, month, day)
    if d is None:
        if fallback:
            return normalize_date(fallback, fix_year=fix_year, today=today)
        return today.isoformat()
    if (d - today).days > _FUTURE_GRACE_DAYS:
        prev = _valid(today.year - 1, month, day)
        if prev and prev.year >= MIN_YEAR:
            return prev.isoformat()
    return d.isoformat()
