"""Deterministic clean-up of model output so numbers become comparable across documents."""
from __future__ import annotations

import re
from datetime import date

SCALE = {"none": 1.0, "thousand": 1e3, "lakh": 1e5, "million": 1e6, "crore": 1e7, "billion": 1e9, "trillion": 1e12}

_UNIT_ALIASES = {
    "rs": "INR", "rs.": "INR", "₹": "INR", "inr": "INR", "rupees": "INR", "indian rupees": "INR", "rupee": "INR",
    "$": "USD", "us$": "USD", "usd": "USD", "us dollars": "USD", "dollars": "USD", "dollar": "USD",
    "percent": "%", "per cent": "%", "percentage": "%", "pct": "%", "%": "%",
    "percentage points": "pp", "pp": "pp", "bps": "bps", "basis points": "bps",
    "number": "count", "nos": "count", "nos.": "count", "units": "count", "no.": "count",
    "people": "persons", "employees": "persons", "persons": "persons",
    "x": "x", "times": "x",
}


def unit_norm(unit: str | None) -> str:
    u = (unit or "").strip()
    return _UNIT_ALIASES.get(u.lower(), u)


def scale_norm(scale: str | None) -> str:
    s = (scale or "none").strip().lower()
    if s in ("mn", "mln", "millions"):
        s = "million"
    if s in ("bn", "billions"):
        s = "billion"
    if s in ("cr", "crores"):
        s = "crore"
    if s in ("lakhs", "lacs", "lac"):
        s = "lakh"
    if s in ("k", "thousands", "000s"):
        s = "thousand"
    return s if s in SCALE else "none"


def value_norm(value_num: float | None, scale: str) -> float | None:
    if value_num is None:
        return None
    return float(value_num) * SCALE[scale_norm(scale)]


_slug_re = re.compile(r"[^a-z0-9]+")
_STOP = {"the", "of", "for", "in", "as", "at", "on", "and", "to", "a", "an", "total"}


def attribute_key(attribute: str) -> str:
    words = [w for w in _slug_re.sub(" ", attribute.lower()).split() if w not in _STOP]
    return "_".join(words) or "unknown"


# --- periods -------------------------------------------------------------------------------------------------------
_FY = re.compile(r"\bFY\s*'?(\d{2,4})(?:\s*[-/]\s*(\d{2,4}))?\b", re.I)
_YY = re.compile(r"\b(20\d{2})\s*[-/]\s*(\d{2,4})\b")
_Q = re.compile(r"\bQ([1-4])\b", re.I)
_H = re.compile(r"\bH([12])\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_DATE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(20\d{2})\b|\b([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})\b")


def _fy_end_year(a: str, b: str | None) -> int:
    """'FY24' -> 2024, 'FY 2023-24' -> 2024, '2024-25' -> 2025."""
    if b:
        end = int(b)
        return end if end > 100 else 2000 + end
    y = int(a)
    return y if y > 100 else 2000 + y


def parse_period(label: str, fy_start_month: int = 4) -> tuple[str, str, str] | None:
    """Best-effort fallback when the model left ISO dates empty. Returns (start, end, type) or None."""
    if not label:
        return None
    text = label.strip()
    m = _DATE.search(text)
    if m:
        try:
            if m.group(1):
                d = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
            else:
                d = date(int(m.group(6)), _MONTHS[m.group(4).lower()], int(m.group(5)))
            return d.isoformat(), d.isoformat(), "point_in_time"
        except (KeyError, ValueError):
            pass
    fy = _FY.search(text) or _YY.search(text)
    if fy:
        end_year = _fy_end_year(fy.group(1), fy.group(2))
        if fy_start_month == 1:
            start, end = date(end_year, 1, 1), date(end_year, 12, 31)
        else:
            start = date(end_year - 1, fy_start_month, 1)
            end = _last_day_before(end_year, fy_start_month)
        q = _Q.search(text)
        h = _H.search(text)
        if q:
            qi = int(q.group(1)) - 1
            qs_month = (fy_start_month - 1 + 3 * qi) % 12 + 1
            qs_year = start.year + ((fy_start_month - 1 + 3 * qi) // 12)
            qe_month = (qs_month + 2 - 1) % 12 + 1
            qe_year = qs_year + (1 if qs_month + 2 > 12 else 0)
            return date(qs_year, qs_month, 1).isoformat(), _month_end(qe_year, qe_month).isoformat(), "quarter"
        if h:
            hi = int(h.group(1)) - 1
            hs_month = (fy_start_month - 1 + 6 * hi) % 12 + 1
            hs_year = start.year + ((fy_start_month - 1 + 6 * hi) // 12)
            he_month = (hs_month + 5 - 1) % 12 + 1
            he_year = hs_year + (1 if hs_month + 5 > 12 else 0)
            return date(hs_year, hs_month, 1).isoformat(), _month_end(he_year, he_month).isoformat(), "half_year"
        return start.isoformat(), end.isoformat(), "fiscal_year" if fy_start_month != 1 else "calendar_year"
    y = re.fullmatch(r"\s*(20\d{2})\s*", text)
    if y:
        yr = int(y.group(1))
        return date(yr, 1, 1).isoformat(), date(yr, 12, 31).isoformat(), "calendar_year"
    return None


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1).fromordinal(date(year, month + 1, 1).toordinal() - 1)


def _last_day_before(end_year: int, fy_start_month: int) -> date:
    first = date(end_year, fy_start_month, 1)
    return date.fromordinal(first.toordinal() - 1)


def fy_start_month_from_convention(convention: str) -> int:
    c = (convention or "").lower()
    if "calendar" in c and "april" not in c:
        return 1
    if "july" in c and "june" in c:
        return 7
    if "october" in c and "september" in c:
        return 10
    return 4  # Indian default


# --- numbers in quotes -----------------------------------------------------------------------------------------------
def number_in_text(value: float, text: str) -> bool:
    """Does the number (as written with commas / decimals / Indian grouping) appear in the quote?"""
    digits = re.sub(r"[,\s]", "", text)
    cands = set()
    for fmt in ("{:.0f}", "{:.1f}", "{:.2f}", "{:.3f}", "{:g}"):
        try:
            cands.add(fmt.format(value))
        except (ValueError, OverflowError):
            pass
    cands.add(str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value))
    return any(c and c in digits for c in cands)
