"""Session-scoping helpers for jobs tables (skills remain global)."""
from __future__ import annotations

from typing import Any, List, Tuple

DEFAULT_SESSION = "demo"


def session_id_from_cfg(cfg: dict | None) -> str:
    return (cfg or {}).get("session_id") or DEFAULT_SESSION


def job_session_clause(alias: str = "j") -> str:
    return f" AND {alias}.session_id = %s"


def job_session_params(session_id: str) -> Tuple[str, ...]:
    return (session_id,)


def extend_params(params: List[Any] | Tuple[Any, ...], session_id: str) -> Tuple[Any, ...]:
    return tuple(params) + (session_id,)


def js_session_clause(alias: str = "js") -> str:
    return f" AND {alias}.session_id = %s"
