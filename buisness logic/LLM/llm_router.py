"""Multi-provider LLM router: Gemini → Grok → empty (caller uses SQL/regex fallback)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _provider_chain(cfg: dict) -> List[str]:
    llm_cfg = (cfg or {}).get("llm", {}) or {}
    chain = llm_cfg.get("providers")
    if isinstance(chain, list) and chain:
        return [str(p).strip().lower() for p in chain if str(p).strip()]
    legacy = (llm_cfg.get("provider") or "gemini").strip().lower()
    return [legacy]


def _model_for(provider: str, llm_cfg: dict) -> str:
    if provider == "grok":
        return llm_cfg.get("grok_model") or "grok-2-1212"
    return llm_cfg.get("model") or "gemini-2.5-flash-lite"


def _make_llm(provider: str, model: str, json_mode: bool, llm_cfg: dict):
    if provider == "gemini":
        from LLM.llm_gemini import GeminiLLM
        return GeminiLLM(
            model=model,
            json_mode=json_mode,
            safety=llm_cfg.get("safety", "default"),
        )
    if provider == "grok":
        from LLM.llm_grok import GrokLLM
        return GrokLLM(model=model, json_mode=json_mode)
    raise ValueError(f"Unknown LLM provider: {provider}")


def llm_enabled(cfg: dict) -> bool:
    if _env_truthy("LLM_ENABLED") or _env_truthy("LLM_LIVE"):
        return bool(_provider_chain(cfg or {}))
    llm_cfg = (cfg or {}).get("llm", {}) or {}
    if not llm_cfg.get("enabled"):
        return False
    return bool(_provider_chain(cfg))


def generate_with_fallback(
    cfg: dict,
    system: str,
    user: str,
    json_mode: bool = False,
    *,
    capture_context: dict | None = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Try each provider in order. Returns (text, provider_name) or (None, None).
    On total failure, optionally logs to feedback/failures.jsonl for review.
    """
    if not llm_enabled(cfg):
        return None, None

    llm_cfg = (cfg or {}).get("llm", {}) or {}
    attempts: List[dict] = []
    for provider in _provider_chain(cfg):
        model = _model_for(provider, llm_cfg)
        try:
            llm = _make_llm(provider, model, json_mode, llm_cfg)
            text = (llm.generate(system, user) or "").strip()
            if text:
                from LLM.llm_status import record_provider_outcome
                record_provider_outcome(provider, "ok")
                print(f"[LLM] OK via {provider} ({model})")
                return text, provider
            print(f"[LLM] empty response from {provider}")
            attempts.append({"provider": provider, "model": model, "error": "empty response"})
        except Exception as e:
            from LLM.llm_status import record_provider_error
            record_provider_error(provider, str(e))
            print(f"[LLM] {provider} failed: {e}")
            attempts.append({"provider": provider, "model": model, "error": str(e)[:500]})

    if capture_context:
        try:
            from feedback.capture import capture_failure
            capture_failure(
                cfg,
                kind=capture_context.get("kind", "llm_all_failed"),
                question=capture_context.get("question", ""),
                session_id=capture_context.get("session_id", ""),
                error="; ".join(
                    f"{a.get('provider')}: {a.get('error')}" for a in attempts
                )[:2000],
                context={
                    "stage": capture_context.get("stage", "llm"),
                    **(capture_context.get("extra") or {}),
                },
                provider_attempts=attempts,
                fallback_used=capture_context.get("fallback_used", "sql"),
            )
        except Exception as cap_err:
            print(f"[feedback] capture skipped: {cap_err}")

    return None, None
