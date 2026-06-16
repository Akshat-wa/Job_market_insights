# skills_common.py
"""
Shared normalization and location parsing helpers.

- preserve special tokens like C++, C#, Node.js by allowing +, #, . in the normalizer.
- provides parse_location + country inference helpers (explicit + heuristic).
"""

import re
from typing import Optional, Tuple, Dict
import pycountry

# Build a simple country name map (lowercase -> canonical name)
COUNTRY_MAP: Dict[str, str] = {c.name.lower(): c.name for c in pycountry.countries}
COUNTRY_MAP.update({
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "south korea": "Korea, Republic of",
    "north korea": "Korea, Democratic People's Republic of",
    "russia": "Russian Federation",
    "vietnam": "Viet Nam",
    "iran": "Iran, Islamic Republic of",
    "bolivia": "Bolivia, Plurinational State of",
    "venezuela": "Venezuela, Bolivarian Republic of",
    "tanzania": "Tanzania, United Republic of",
    "moldova": "Moldova, Republic of",
    "syria": "Syrian Arab Republic",
    "laos": "Lao People's Democratic Republic",
    "micronesia": "Micronesia, Federated States of",
    "palestine": "Palestine, State of",
    "cape verde": "Cabo Verde",
    "east timor": "Timor-Leste",
    "brunei": "Brunei Darussalam",
    "czech republic": "Czechia",
    "remote": None,
})

# Normalizer: lowercase, remove unwanted punctuation, collapse spaces.
# Allow +, #, . to preserve tokens like 'C++', 'C#', 'Node.js'
_normalize_re = re.compile(r'[^a-z0-9\s\+\#\.]')

def normalize_skill(s: Optional[str]) -> str:
    """Return a canonical lowercase, punctuation-trimmed skill string but preserve +,#,. tokens."""
    if s is None:
        return ""
    s2 = str(s).strip().lower()
    # replace disallowed characters with space, keep + # .
    s2 = _normalize_re.sub(' ', s2)
    s2 = re.sub(r'\s+', ' ', s2).strip()
    return s2

def normalize_text_lc(s: Optional[str]) -> str:
    """General lowercase normalizer for titles/locations to produce *_lc."""
    if s is None:
        return ""
    s2 = str(s).strip().lower()
    s2 = re.sub(r'\s+', ' ', s2).strip()
    return s2

def canonical_country_from_string(loc: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Try to extract/resolve a country name from a location string.
    Returns (country_name_or_none, method) where method is 'explicit', 'heuristic', or None.
    - explicit: last comma token matches COUNTRY_MAP
    - heuristic: last token is an alpha-2/3 code or common short form
    """
    if not loc:
        return None, None
    s = str(loc).strip()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    candidate = parts[-1].lower() if parts else s.lower()

    # direct map
    mapped = COUNTRY_MAP.get(candidate)
    if mapped is not None:
        return mapped, "explicit" if mapped else (None, "explicit")

    # heuristic: last token may be a 2-letter / 3-letter code or uppercase short token
    last = candidate.split()[-1].strip().upper()
    if len(last) in (2, 3):
        try:
            country = pycountry.countries.get(alpha_2=last)
            if country:
                return country.name, "heuristic"
        except Exception:
            pass
        try:
            country = pycountry.countries.get(alpha_3=last)
            if country:
                return country.name, "heuristic"
        except Exception:
            pass

    return None, None

def parse_location(raw: Optional[str]) -> dict:
    """
    Parse a raw location string into components.
    Returns dict: {
      location_raw, city, state_region, country, country_source, location_norm, location_lc
    }
    location_norm is a canonical 'city, country' if both known, else raw fallback.
    location_lc is normalize_text_lc(location_norm)
    """
    out = {
        "location_raw": None,
        "city": None,
        "state_region": None,
        "country": None,
        "country_source": None,
        "location_norm": None,
        "location_lc": None
    }
    if not raw:
        return out

    s = str(raw).strip()
    out["location_raw"] = s

    parts = [p.strip() for p in s.split(",") if p.strip()]
    if parts:
        out["city"] = parts[0] if len(parts) >= 1 else None
        out["state_region"] = parts[1] if len(parts) >= 2 else None

    country, method = canonical_country_from_string(s)
    out["country"] = country
    out["country_source"] = method

    if out["city"] and out["country"]:
        out["location_norm"] = f"{out['city']}, {out['country']}"
    elif out["country"]:
        out["location_norm"] = out["country"]
    else:
        out["location_norm"] = s

    out["location_lc"] = normalize_text_lc(out["location_norm"])
    return out
