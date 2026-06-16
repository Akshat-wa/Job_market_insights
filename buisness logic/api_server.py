# api_server.py — Flask API for portfolio deployment
from __future__ import annotations

import os
import traceback
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

from analyzer_strategy_1 import llm_parse as llm_parse_v1, parse as parse_v1
from config_loader import DEFAULT_SESSION, load_config
from db import DBAdapter
from decompose_plan import to_sql
from decompose_run import run as run_plan
from cleanup_sessions import cleanup_sessions
from ingest_jobs_flexible import ensure_session_schema, ingest_upload
from storage_stats import get_storage_stats
from run_query import (
    format_narrative,
    llm_enabled,
    llm_structured_summary,
    maybe_enrich_with_llm_integration,
    maybe_llm_freeform,
)
from feedback.capture import capture_failure, feedback_enabled, list_failures, mark_failure

load_dotenv()

API_BUILD_ID = "2026-06-16-llm-env-v4"
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")

CFG = load_config(CONFIG_PATH)


def _runtime_cfg() -> dict:
    """Reload YAML + env each request so config edits apply without restart."""
    return load_config(CONFIG_PATH)


def _llm_live_mode() -> bool:
    """Portfolio default: SQL-only. Set LLM_LIVE=1 in .env to enable Gemini/Grok."""
    return os.getenv("LLM_LIVE", "0").strip().lower() in ("1", "true", "yes")

try:
    ensure_session_schema(CFG)
    print("[api_server] schema_deploy.sql applied (or already up to date)")
except Exception as e:
    print(f"[api_server] schema deploy warning: {e}")

try:
    portfolio = CFG.get("portfolio", {}) or {}
    cleanup_result = cleanup_sessions(
        CFG,
        ttl_hours=int(portfolio.get("session_ttl_hours", 24)),
        keep_demo=portfolio.get("demo_session_id", DEFAULT_SESSION),
    )
    if cleanup_result.get("deleted_sessions"):
        print(f"[api_server] cleaned sessions: {cleanup_result['deleted_sessions']}")
except Exception as e:
    print(f"[api_server] session cleanup warning: {e}")

adapter = DBAdapter(CFG)

app = Flask(__name__)
CORS(app)

MAX_FILE_BYTES = int((CFG.get("upload") or {}).get("max_file_bytes", 10 * 1024 * 1024))
PORTFOLIO_MIN_JOBS = 10_000  # below this, demo session likely overwritten


def _demo_session_id() -> str:
    return (CFG.get("portfolio") or {}).get("demo_session_id", DEFAULT_SESSION)


def _reject_demo_overwrite(session_id: str, mode: str = "replace"):
    if session_id == _demo_session_id() and mode == "replace":
        return (
            jsonify({
                "error": (
                    "Cannot overwrite the portfolio demo session. "
                    "Use New session for uploads/samples, or run: python seed_portfolio.py to restore."
                )
            }),
            403,
        )
    return None


def _parse_with_preference(question: str, cfg: dict):
    parser_cfg = (cfg or {}).get("parser", {}) or {}
    llm_on = llm_enabled(cfg)
    prefer_llm = bool(parser_cfg.get("prefer_llm", True)) and llm_on

    parsed = None
    used_llm_parser = False

    if prefer_llm:
        parsed = llm_parse_v1(question, cfg)
        used_llm_parser = parsed is not None
        if not parsed:
            parsed = parse_v1(question)
    else:
        parsed = parse_v1(question)
        if not parsed and llm_on:
            parsed = llm_parse_v1(question, cfg)
            used_llm_parser = parsed is not None

    return parsed, used_llm_parser


def _touch_session(session_id: str) -> None:
    try:
        adapter.run_sql(
            "jobs_db",
            """
            INSERT INTO upload_sessions (session_id, created_at, last_used_at)
            VALUES (%s, NOW(), NOW())
            ON CONFLICT (session_id) DO UPDATE SET last_used_at = NOW()
            """,
            (session_id,),
        )
    except Exception:
        pass


def _cfg_without_llm(cfg: dict) -> dict:
    out = dict(cfg)
    out["llm"] = {**(cfg.get("llm") or {}), "enabled": False}
    out["parser"] = {**(cfg.get("parser") or {}), "prefer_llm": False}
    return out


def _is_llm_quota_error(exc: Exception) -> bool:
    err = str(exc)
    err_l = err.lower()
    name = type(exc).__name__.lower()
    return (
        "429" in err
        or "quota" in err_l
        or "rate limit" in err_l
        or "resourceexhausted" in name
        or "too many requests" in err_l
    )


def process_question(
    question: str,
    session_id: str | None = None,
    *,
    force_sql_fallback: bool = False,
) -> dict:
    base_cfg = _runtime_cfg()
    cfg = _cfg_without_llm(base_cfg) if force_sql_fallback else dict(base_cfg)
    cfg["session_id"] = session_id or DEFAULT_SESSION
    _touch_session(cfg["session_id"])

    parsed, used_llm_parser = _parse_with_preference(question, cfg)

    if not parsed:
        freeform = None
        feedback_id = None
        if not force_sql_fallback:
            try:
                freeform = maybe_llm_freeform(cfg, question)
            except Exception as e:
                print(f"[process_question] freeform LLM failed: {e}")
        if not freeform and feedback_enabled(cfg):
            feedback_id = capture_failure(
                cfg,
                kind="parse_failed",
                question=question,
                session_id=cfg["session_id"],
                error="regex and LLM parser could not understand question",
                context={"stage": "parse", "used_llm_parser": used_llm_parser},
                fallback_used="static_message",
            )
        return {
            "mode": "llm_freeform",
            "used_llm_parser": used_llm_parser,
            "session_id": cfg["session_id"],
            "answer": freeform or (
                "Sorry, I couldn't understand that question. "
                "Try one of the example chips (e.g. Top 10 skills for data scientist in India)."
            ),
            "llm_degraded": force_sql_fallback,
            "feedback_id": feedback_id,
        }

    plan = to_sql(parsed, cfg)
    result = run_plan(adapter, plan, cfg) or {}
    structured = result.get("structured_result")

    llm_artifact = None
    if not force_sql_fallback:
        try:
            enriched, llm_artifact = maybe_enrich_with_llm_integration(cfg, parsed, structured)
            structured = enriched
        except Exception as e:
            print(f"[process_question] enrichment failed: {e}")
    result["structured_result"] = structured

    llm_summary = None
    feedback_id = None
    if not force_sql_fallback:
        try:
            llm_summary = llm_structured_summary(
                cfg, parsed, structured,
                question=question,
                session_id=cfg["session_id"],
            )
        except Exception as e:
            print(f"[process_question] summary LLM failed: {e}")

    summary = llm_summary or format_narrative(plan.get("task"), parsed, structured)

    if structured in (None, {}, []) and feedback_enabled(cfg):
        feedback_id = capture_failure(
            cfg,
            kind="empty_result",
            question=question,
            session_id=cfg["session_id"],
            error="query parsed but returned no structured data",
            context={"stage": "sql", "task": plan.get("task")},
            fallback_used="none",
        )

    return {
        "mode": "structured",
        "used_llm_parser": used_llm_parser,
        "session_id": cfg["session_id"],
        "parsed": asdict(parsed),
        "plan": {"task": plan.get("task"), "source": plan.get("source")},
        "structured_result": structured,
        "summary": summary,
        "summary_source": "llm" if llm_summary else "sql",
        "llm_degraded": force_sql_fallback,
        "feedback_id": feedback_id,
        "advisor_notes": None,
        "llm_integration_raw": llm_artifact,
    }


@app.get("/api/health")
def health():
    try:
        adapter.run_sql("jobs_db", "SELECT 1", ())
        db_ok = True
        err = None
        storage = get_storage_stats(adapter)
    except Exception as e:
        db_ok = False
        err = str(e)
        storage = None
    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "db_error": err,
        "storage": storage,
        "api_version": API_BUILD_ID,
        "sql_only_mode": not _llm_live_mode(),
        "llm_enabled": llm_enabled(_runtime_cfg()),
        "llm_use_for_summary": (_runtime_cfg().get("llm") or {}).get("use_for_summary"),
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "xai_key_set": bool(os.getenv("XAI_API_KEY")),
        "llm_live_env": _llm_live_mode(),
        "time": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/api/session/new")
def api_session_new():
    session_id = str(uuid.uuid4())
    _touch_session(session_id)
    return jsonify({"session_id": session_id, "label": "demo"})


@app.get("/api/schema-help")
def api_schema_help():
    return jsonify({
        "postings_columns": {
            "required_one_of": ["job_link OR (title + company)"],
            "job_link_aliases": ["job_link", "url", "link", "job_url"],
            "title_aliases": ["job_title", "title", "position", "role"],
            "company_aliases": ["company", "company_name", "employer"],
            "location_aliases": ["job_location", "location", "city"],
        },
        "skills_columns": {
            "skills_aliases": ["job_skills", "skills", "required_skills", "skill_list"],
            "join_key": "job_link (or synthetic key from title+company+location)",
        },
        "modes": [
            "postings_csv only",
            "skills_csv only (needs existing jobs in session)",
            "both files",
            "single combined CSV with postings + skills columns",
        ],
        "limits": {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_rows": (CFG.get("upload") or {}).get("max_rows", 20000),
        },
        "note": "Candidate matching uses pre-loaded demo resumes; job queries use your uploaded session data.",
    })


@app.post("/api/load-demo")
def api_load_demo():
    data = request.get_json(force=True, silent=True) or {}
    portfolio = CFG.get("portfolio", {}) or {}
    session_id = (data.get("session_id") or "").strip() or portfolio.get("demo_session_id", DEFAULT_SESSION)
    use_portfolio = bool(data.get("use_portfolio", False))

    blocked = _reject_demo_overwrite(session_id, "replace")
    if blocked:
        return blocked

    candidates = []
    if use_portfolio:
        candidates.append(os.path.join(os.path.dirname(__file__), "data", "portfolio_jobs_combined.csv"))
    else:
        candidates.append(os.path.join(os.path.dirname(__file__), "..", "demo", "sample_combined.csv"))

    demo_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not demo_path:
        return jsonify({"error": "No demo CSV found. Run: python seed_portfolio.py"}), 404

    try:
        with open(demo_path, "rb") as f:
            combined_bytes = f.read()
        result = ingest_upload(
            CFG,
            session_id=session_id,
            combined_bytes=combined_bytes,
            mode="replace",
        )
        _touch_session(session_id)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.post("/api/upload")
def api_upload():
    session_id = (request.form.get("session_id") or "").strip() or DEFAULT_SESSION
    mode = (request.form.get("mode") or "replace").strip().lower()
    if mode not in ("replace", "append"):
        mode = "replace"

    blocked = _reject_demo_overwrite(session_id, mode)
    if blocked:
        return blocked

    postings_file = request.files.get("postings_file")
    skills_file = request.files.get("skills_file")
    combined_file = request.files.get("combined_file")

    postings_bytes = None
    skills_bytes = None
    combined_bytes = None

    for label, fobj, target in (
        ("postings_file", postings_file, "postings"),
        ("skills_file", skills_file, "skills"),
        ("combined_file", combined_file, "combined"),
    ):
        if fobj and fobj.filename:
            data = fobj.read()
            if len(data) > MAX_FILE_BYTES:
                return jsonify({"error": f"{label} exceeds {MAX_FILE_BYTES} bytes"}), 400
            if target == "postings":
                postings_bytes = data
            elif target == "skills":
                skills_bytes = data
            else:
                combined_bytes = data

    if not any([postings_bytes, skills_bytes, combined_bytes]):
        return jsonify({"error": "Provide postings_file, skills_file, or combined_file"}), 400

    try:
        result = ingest_upload(
            CFG,
            session_id=session_id,
            postings_bytes=postings_bytes,
            skills_bytes=skills_bytes,
            combined_bytes=combined_bytes,
            mode=mode,
        )
        _touch_session(session_id)
        return jsonify(result)
    except Exception as e:
        print("[api_server] upload error:")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.get("/api/session/<session_id>/stats")
def api_session_stats(session_id: str):
    cfg = dict(CFG)
    cfg["session_id"] = session_id
    try:
        jobs = adapter.run_sql(
            "jobs_db",
            "SELECT COUNT(*) FROM jobs WHERE session_id = %s",
            (session_id,),
        )[0][0]
        links = adapter.run_sql(
            "jobs_db",
            """
            SELECT COUNT(*) FROM job_skills js
            JOIN jobs j ON j.job_id = js.job_id AND j.session_id = js.session_id
            WHERE js.session_id = %s
            """,
            (session_id,),
        )[0][0]
        return jsonify({"session_id": session_id, "jobs": jobs, "skill_links": links})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/query")
def api_query():
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    session_id = (data.get("session_id") or "").strip() or DEFAULT_SESSION

    if not question:
        return jsonify({"error": "question is required"}), 400

    sql_only = not _llm_live_mode()

    try:
        payload = process_question(
            question,
            session_id=session_id,
            force_sql_fallback=sql_only,
        )
        payload["api_version"] = API_BUILD_ID
        payload["sql_only_mode"] = sql_only
        return jsonify(payload)
    except Exception as e:
        print("[api_server] ERROR while processing question:")
        traceback.print_exc()
        if feedback_enabled(_runtime_cfg()):
            capture_failure(
                _runtime_cfg(),
                kind="query_error_recovered",
                question=question,
                session_id=session_id,
                error=str(e)[:2000],
                context={"stage": "query"},
                fallback_used="sql",
            )
        try:
            payload = process_question(
                question,
                session_id=session_id,
                force_sql_fallback=True,
            )
            payload["llm_degraded"] = True
            payload["api_version"] = API_BUILD_ID
            payload["sql_only_mode"] = True
            return jsonify(payload)
        except Exception as retry_err:
            print("[api_server] SQL-only retry failed:")
            traceback.print_exc()
            fid = None
            if feedback_enabled(_runtime_cfg()):
                fid = capture_failure(
                    _runtime_cfg(),
                    kind="query_error",
                    question=question,
                    session_id=session_id,
                    error=str(retry_err)[:2000],
                    context={"stage": "query", "first_error": str(e)[:500]},
                    fallback_used="none",
                )
            return jsonify({
                "error": (
                    "Query failed. Stop api_server.py (Ctrl+C), run it again from "
                    "'buisness logic', then retry."
                ),
                "detail": str(retry_err)[:300],
                "api_version": API_BUILD_ID,
                "feedback_id": fid,
            }), 500


@app.get("/api/feedback/failures")
def api_feedback_list():
    status = (request.args.get("status") or "open").strip()
    limit = min(int(request.args.get("limit", 50)), 200)
    rows = list_failures(_runtime_cfg(), status=status, limit=limit)
    return jsonify({"failures": rows, "count": len(rows)})


@app.post("/api/feedback/failures/<failure_id>/review")
def api_feedback_review(failure_id: str):
    data = request.get_json(force=True, silent=True) or {}
    status = (data.get("status") or "reviewed").strip()
    notes = (data.get("notes") or "").strip()
    cfg = _runtime_cfg()
    rows = list_failures(cfg, status="all", limit=10_000)
    full_id = next((r["id"] for r in rows if r.get("id", "").startswith(failure_id)), failure_id)
    if not mark_failure(full_id, status=status, notes=notes, cfg=cfg):
        return jsonify({"error": "failure id not found"}), 404
    return jsonify({"ok": True, "id": full_id, "status": status})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"[api_server] build={API_BUILD_ID} sql_only={not _llm_live_mode()}")
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
