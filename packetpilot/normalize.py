"""Value normalisation and comparison.

All extractors (deterministic and LLM) return raw strings; they are converted
to canonical values here so that every path is held to exactly the same
parsing rules. Normalisers return ``None`` when the raw text cannot be parsed
- callers treat that as "not extracted" rather than guessing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

from .schema import FIELD_SPECS, Line

_WS = re.compile(r"\s+")


def clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = _WS.sub(" ", str(raw)).strip()
    # scanner / form-rule debris at the edges ("___ Grace Moreau", "N/A —_")
    s = s.lstrip(" |_—–:;.,'‘’`~").rstrip(" |_—–:;,~")
    return s or None


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #

_MONEY = re.compile(
    r"(?P<neg>\()?\s*(?:USD|US\$|\$)?\s*(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<suffix>million|mil|mm|m|thousand|k|bn|billion|b)?\b",
    re.IGNORECASE,
)
_SUFFIX = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mil": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def parse_money(raw: str | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(float(raw), 2)
    s = str(raw).replace("O", "0") if re.fullmatch(r"[\s$\d,.Oo]+", str(raw)) else str(raw)
    m = _MONEY.search(s)
    if not m:
        return None
    try:
        val = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group("suffix") or "").lower()
    val *= _SUFFIX.get(suffix, 1.0)
    if m.group("neg") and ")" in s[m.end() - 1 : m.end() + 2]:
        val = -val
    return round(val, 2)


# --------------------------------------------------------------------------- #
# Integers
# --------------------------------------------------------------------------- #

_NUM_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen twenty".split()
    )
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().lower()
    m = re.search(r"-?\d[\d,]*", s)
    if m:
        try:
            return int(m.group(0).replace(",", ""))
        except ValueError:
            return None
    m = re.search(r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-](one|two|three|four|five|six|seven|eight|nine))?\b", s)
    if m:
        return _TENS[m.group(1)] + (_NUM_WORDS[m.group(2)] if m.group(2) else 0)
    for w, i in _NUM_WORDS.items():
        if re.search(rf"\b{w}\b", s):
            return i
    return None


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_DATE_FORMATS = (
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%b. %d, %Y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
)
_DATE_FIND = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})",
    re.IGNORECASE,
)


def parse_date(raw: str | date | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    m = _DATE_FIND.search(str(raw))
    if not m:
        return None
    s = m.group(1).replace("Sept", "Sep").replace(",", ", ").replace(" ,", ",")
    s = _WS.sub(" ", s).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Identifiers and enums
# --------------------------------------------------------------------------- #

FEIN_RE = re.compile(r"^\d{2}-\d{7}$")


def parse_fein(raw: str | None) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 9:
        return f"{digits[:2]}-{digits[2:]}"
    # Return what was written so the validity rule can flag it; don't guess.
    return s if re.search(r"\d", s) else None


def parse_entity(raw: str | None) -> str | None:
    s = (clean_text(raw) or "").lower()
    if not s:
        return None
    checked = _checked_options(s)
    if checked:
        s = " ".join(checked)
    if "llc" in s or "limited liability" in s:
        return "LLC"
    if "partnership" in s or "llp" in s:
        return "Partnership"
    if "sole" in s or "individual" in s:
        return "Sole Proprietor"
    if "corp" in s or "inc" in s or "s-corp" in s:
        return "Corporation"
    if "non-profit" in s or "nonprofit" in s:
        return "Non-profit"
    return None


_LINE_SYNONYMS = [
    (Line.GL.value, r"general\s+liability|\bcgl\b|\bgl\b"),
    (Line.AUTO.value, r"commercial\s+auto|business\s+auto|\bauto\b|\bautomobile\b"),
    (Line.WC.value, r"workers'?\s*comp(?:ensation)?|work\s*comp|\bwc\b"),
    (Line.PROPERTY.value, r"commercial\s+property|\bproperty\b|\bbuilding\b"),
]

_CHECKBOX = re.compile(r"\[\s*([xX✓✔■☒])?\s*\]\s*([^\[\]]+)")


def _checked_options(s: str) -> list[str] | None:
    """For '[X] A  [ ] B' style text return the checked option labels, else None."""
    boxes = _CHECKBOX.findall(s)
    if len(boxes) < 2 and not (boxes and boxes[0][0]):
        return None
    return [label.strip() for mark, label in boxes if mark]


def parse_lines(raw: str | list | None) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = ", ".join(str(x) for x in raw)
    s = str(raw)
    checked = _checked_options(s)
    if checked is not None:
        s = ", ".join(checked)
    found = []
    for code, pat in _LINE_SYNONYMS:
        if re.search(pat, s, re.IGNORECASE):
            found.append(code)
    return sorted(found) or None


def parse_bool(raw: str | bool | None) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = (clean_text(raw) or "").lower()
    checked = _checked_options(s)
    if checked:
        s = checked[0].lower()
    if re.match(r"^(yes|y|true|x)\b", s):
        return True
    if re.match(r"^(no|n|false|none)\b", s):
        return False
    return None


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def parse_email(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _EMAIL.search(str(raw))
    return m.group(0).lower() if m else None


def parse_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def parse_naics(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(r"\b(\d{6})\b", str(raw))
    return m.group(1) if m else None


def normalize(kind: str, raw: Any) -> Any:
    """Convert a raw extracted string into the canonical value for ``kind``."""
    if raw is None:
        return None
    if kind == "money":
        return parse_money(raw)
    if kind == "int":
        return parse_int(raw)
    if kind == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if kind == "fein":
        return parse_fein(raw)
    if kind == "entity":
        return parse_entity(raw)
    if kind == "lines":
        return parse_lines(raw)
    if kind == "bool":
        return parse_bool(raw)
    if kind == "email":
        return parse_email(raw)
    if kind == "phone":
        return parse_phone(raw)
    if kind == "naics":
        return parse_naics(raw)
    return clean_text(raw)


def normalize_field(path: str, raw: Any) -> Any:
    return normalize(FIELD_SPECS[path].kind, raw)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

_ORG_SUFFIX = re.compile(
    r"\b(l\.?l\.?c\.?|inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|l\.?p\.?)\b"
)


def canonical_text(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return _WS.sub(" ", s).strip()


def canonical_org(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = _ORG_SUFFIX.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return _WS.sub(" ", s).strip()


def values_equal(kind: str, a: Any, b: Any, money_tolerance: float = 0.02, path: str = "") -> bool:
    if a is None or b is None:
        return a is b
    if kind == "money":
        hi = max(abs(a), abs(b))
        return hi == 0 or abs(a - b) / hi <= money_tolerance
    if kind == "lines":
        return set(a) == set(b)
    if kind == "text":
        if path == "applicant.legal_name":
            return canonical_org(a) == canonical_org(b)
        ca, cb = canonical_text(a), canonical_text(b)
        if ca == cb:
            return True
        # long free text (operations descriptions) - tolerate light rewording
        if min(len(ca), len(cb)) > 40:
            return SequenceMatcher(None, ca, cb).ratio() >= 0.85
        return False
    return a == b


def fuzzy_contains(haystack: str, needle: str, threshold: float = 0.9) -> bool:
    """True if ``needle`` occurs in ``haystack`` exactly (whitespace/case-insensitive)
    or approximately (for OCR text)."""
    h = _WS.sub(" ", haystack).lower()
    n = _WS.sub(" ", needle).strip().lower()
    if not n:
        return False
    if n in h:
        return True
    if len(n) < 6:
        return False
    sm = SequenceMatcher(None, h, n, autojunk=False)
    m = sm.find_longest_match(0, len(h), 0, len(n))
    if m.size == 0:
        return False
    start = max(0, m.a - m.b)
    window = h[start : start + len(n) + 4]
    return SequenceMatcher(None, window, n, autojunk=False).ratio() >= threshold
