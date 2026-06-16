"""Capture LLM/parse/query failures for second-LLM diagnosis and human review."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_FEEDBACK_DIR = Path(__file__).resolve().parent
_DEFAULT_LOG = _FEEDBACK_DIR / "failures.jsonl"


def _feedback_cfg(cfg: dict | None) -> dict:
    return (cfg or {}).get("feedback", {}) or {}


def feedback_enabled(cfg: dict | None) -> bool:
    return bool(_feedback_cfg(cfg).get("enabled", True))


def log_path(cfg: dict | None) -> Path:
    rel = _feedback_cfg(cfg).get("log_path") or "feedback/failures.jsonl"
    base = Path(__file__).resolve().parent.parent
    return base / rel if not os.path.isabs(rel) else Path(rel)


def _append_record(path: Path, record: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fid = record.get("id") or str(uuid.uuid4())
    record["id"] = fid
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return fid


def _diagnosis_provider(cfg: dict, provider_attempts: List[dict]) -> Optional[str]:
    fb = _feedback_cfg(cfg)
    explicit = (fb.get("diagnosis_provider") or "").strip().lower()
    if explicit and explicit != "auto":
        return explicit

    failed = {a.get("provider") for a in provider_attempts if a.get("provider")}
    llm_cfg = (cfg or {}).get("llm", {}) or {}
    chain = llm_cfg.get("providers") or [llm_cfg.get("provider") or "grok"]
    for p in reversed([str(x).lower() for x in chain]):
        if p not in failed:
            return p
    return str(chain[-1]).lower() if chain else "grok"


def _run_diagnosis(
    cfg: dict,
    record: dict,
    provider_attempts: List[dict],
) -> Optional[dict]:
    fb = _feedback_cfg(cfg)
    if not fb.get("auto_diagnose", True):
        return None
    if not (cfg or {}).get("llm", {}).get("enabled"):
        return None

    provider = _diagnosis_provider(cfg, provider_attempts)
    llm_cfg = (cfg or {}).get("llm", {}) or {}
    model = llm_cfg.get("grok_model") or "grok-2-1212"
    if provider == "gemini":
        model = llm_cfg.get("model") or "gemini-2.5-flash-lite"

    system = (
        "You debug a hiring-analytics app (NL questions → regex parser → SQL → optional LLM summary). "
        "Given a failure record, respond with compact JSON only:\n"
        '{"root_cause":"...","suggested_fix":"regex/parser/SQL hint for a developer",'
        '"example_working_query":"...","needs_human_review":true}\n'
        "Be specific and actionable. Do not invent data."
    )
    user = json.dumps(
        {
            "kind": record.get("kind"),
            "question": record.get("question"),
            "error": record.get("error"),
            "stage": record.get("context", {}).get("stage"),
            "provider_attempts": provider_attempts,
        },
        ensure_ascii=False,
    )

    try:
        if provider == "gemini":
            from LLM.llm_gemini import GeminiLLM
            llm = GeminiLLM(model=model, json_mode=True, safety=llm_cfg.get("safety", "default"))
        elif provider == "grok":
            from LLM.llm_grok import GrokLLM
            llm = GrokLLM(model=model, json_mode=True)
        else:
            return None
        raw = (llm.generate(system, user) or "").strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            raw2 = raw
            if "{" in raw2 and "}" in raw2:
                raw2 = raw2[raw2.find("{") : raw2.rfind("}") + 1]
            obj = json.loads(raw2)
        return {"provider": provider, "model": model, "analysis": obj, "raw": raw[:2000]}
    except Exception as e:
        print(f"[feedback] diagnosis via {provider} failed: {e}")
        return {"provider": provider, "model": model, "analysis": None, "error": str(e)[:500]}


def capture_failure(
    cfg: dict | None,
    *,
    kind: str,
    question: str = "",
    session_id: str = "",
    error: str = "",
    context: Optional[dict] = None,
    provider_attempts: Optional[List[dict]] = None,
    fallback_used: str = "",
) -> Optional[str]:
    """
    Append a failure record. Optionally run a second LLM for diagnosis.
    Returns failure id or None if disabled.
    """
    if not feedback_enabled(cfg):
        return None

    path = log_path(cfg)
    attempts = list(provider_attempts or [])
    record: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "question": (question or "")[:2000],
        "session_id": session_id or "",
        "error": (error or "")[:4000],
        "context": context or {},
        "provider_attempts": attempts,
        "fallback_used": fallback_used,
        "review_status": "open",
        "review_notes": "",
    }

    diagnosis = _run_diagnosis(cfg, record, attempts)
    if diagnosis:
        record["diagnosis"] = diagnosis

    fid = _append_record(path, record)
    print(f"[feedback] captured {kind} id={fid[:8]}... -> {path}")
    return fid


def list_failures(
    cfg: dict | None = None,
    *,
    status: str = "open",
    limit: int = 50,
) -> List[dict]:
    path = log_path(cfg)
    if not path.is_file():
        return []

    rows: List[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if status and status != "all":
        rows = [r for r in rows if r.get("review_status") == status]
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows[:limit]


def mark_failure(
    failure_id: str,
    *,
    status: str = "reviewed",
    notes: str = "",
    cfg: dict | None = None,
) -> bool:
    path = log_path(cfg)
    if not path.is_file():
        return False

    updated = False
    out_lines: List[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                out_lines.append(line.rstrip("\n"))
                continue
            if rec.get("id") == failure_id:
                rec["review_status"] = status
                if notes:
                    rec["review_notes"] = notes
                rec["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                updated = True
            out_lines.append(json.dumps(rec, ensure_ascii=False))

    if updated:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    return updated
