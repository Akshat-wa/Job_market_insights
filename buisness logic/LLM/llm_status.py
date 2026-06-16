"""Runtime LLM provider status for health checks and user-facing notices."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_STATES: Dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_error(error: str) -> str:
    err_l = (error or "").lower()
    if "not set" in err_l or "api_key" in err_l:
        return "not_configured"
    if (
        "429" in error
        or "quota" in err_l
        or "rate limit" in err_l
        or "resourceexhausted" in err_l
        or "too many requests" in err_l
    ):
        return "quota_exhausted"
    return "unavailable"


def record_provider_outcome(provider: str, status: str, detail: str = "") -> None:
    _STATES[provider.lower()] = {
        "status": status,
        "detail": (detail or "")[:400],
        "updated_at": _now(),
    }


def record_provider_error(provider: str, error: str) -> None:
    record_provider_outcome(provider, _classify_error(error), error)


def _provider_snapshot(provider: str) -> dict:
    key_env = "GEMINI_API_KEY" if provider == "gemini" else "XAI_API_KEY"
    key_set = bool(os.getenv(key_env))
    cached = _STATES.get(provider) or {}
    status = cached.get("status")

    if not key_set:
        return {
            "provider": provider,
            "status": "not_configured",
            "key_set": False,
            "detail": f"{key_env} not set",
            "updated_at": cached.get("updated_at"),
        }

    if not status:
        return {
            "provider": provider,
            "status": "ready",
            "key_set": True,
            "detail": "Configured - no recent calls yet",
            "updated_at": None,
        }

    return {
        "provider": provider,
        "status": status,
        "key_set": True,
        "detail": cached.get("detail", ""),
        "updated_at": cached.get("updated_at"),
    }


def get_llm_status(cfg: dict | None, *, sql_only_mode: bool = False) -> dict[str, Any]:
    """Public status for /api/health and UI banner."""
    from LLM.llm_router import llm_enabled

    enabled = llm_enabled(cfg) and not sql_only_mode
    gemini = _provider_snapshot("gemini")
    grok = _provider_snapshot("grok")

    level = "off"
    user_message = "AI summaries are off in SQL-only mode."
    summary_source_hint = "sql"

    if not enabled:
        if not llm_enabled(cfg):
            user_message = "AI summaries are disabled in server config."
        elif sql_only_mode:
            user_message = "AI summaries are off (SQL-only mode)."
        level = "off"
    else:
        g_status = gemini.get("status")
        x_status = grok.get("status")

        if g_status == "ok" or (g_status == "ready" and x_status not in ("ok",)):
            level = "ok"
            user_message = "AI summaries: Gemini is active."
            summary_source_hint = "llm"
        elif g_status == "quota_exhausted":
            level = "warning"
            if x_status == "ok":
                user_message = (
                    "Gemini free daily limit reached — using Grok for summaries. "
                    "Data queries still work normally."
                )
                summary_source_hint = "llm"
            elif x_status == "quota_exhausted":
                level = "error"
                user_message = (
                    "Gemini and Grok free limits reached — summaries use SQL only. "
                    "Your data and queries still work; AI limits usually reset daily."
                )
            else:
                user_message = (
                    "Gemini free daily limit reached — summaries may use SQL or Grok fallback. "
                    "Data queries still work; limit typically resets daily."
                )
        elif g_status == "unavailable":
            level = "warning"
            if x_status == "ok":
                user_message = "Gemini temporarily unavailable — using Grok for summaries."
                summary_source_hint = "llm"
            else:
                user_message = (
                    "Gemini API unavailable — summaries use SQL fallback. "
                    "Structured results and tables are unaffected."
                )
        elif g_status == "ready":
            level = "ok"
            user_message = "Gemini configured — AI summaries available."
            summary_source_hint = "llm"
        elif g_status == "not_configured":
            level = "off"
            user_message = "Gemini API key not configured — SQL summaries only."

    return {
        "enabled": enabled,
        "level": level,
        "user_message": user_message,
        "summary_source_hint": summary_source_hint,
        "gemini": gemini,
        "grok": grok,
    }


def get_query_notice(
    cfg: dict | None,
    *,
    summary_source: str,
    summary_provider: str | None = None,
    sql_only_mode: bool = False,
) -> Optional[str]:
    """Short notice when a query used SQL instead of LLM."""
    if summary_source == "llm":
        if summary_provider:
            return f"Summary powered by {summary_provider.title()}."
        return None

    status = get_llm_status(cfg, sql_only_mode=sql_only_mode)
    if status["level"] in ("warning", "error"):
        return status["user_message"]
    if status["enabled"] and summary_source == "sql":
        return (
            "This summary was generated from SQL (no LLM). "
            "Gemini may be at its free daily limit or temporarily unavailable."
        )
    return None
