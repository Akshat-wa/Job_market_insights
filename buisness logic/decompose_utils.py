# decompose_utils.py
from typing import Optional, Any, Set
import regex as _re


def safe_lc(x: Any) -> str:
    
    # Safely convert to lowercase string, handling None / weird types.
    
    try:
        return str(x).strip().lower() if x is not None else ""
    except Exception:
        return ""


def _split_skills_text(s: Optional[str]) -> Set[str]:
    
    # Split a 'Python, SQL | ML' style string into a lowercase set of tokens.
    
    if not s:
        return set()
    toks = [t.strip().lower() for t in _re.split(r"[,\|;/]", s) if t.strip()]
    return set(toks)


def _like(s: str) -> str:
    
    # Wrap a string for SQL LIKE queries, lower-cased.
    
    return f"%{(s or '').lower()}%"


def _jaccard(a: Set[Any], b: Set[Any]) -> float:
    
    # Jaccard similarity between two sets.
    
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return float(len(a & b)) / float(len(a | b))


def _word_regex(term: str) -> str:
    
    # Build a whole-word, case-insensitive regex for Postgres (~*),
    # avoiding 'india' matching 'indiana' etc.
    
    t = (term or "").strip().lower()
    return r"(?i)(^|[^a-z])" + _re.escape(t) + r"([^a-z]|$)"
