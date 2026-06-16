# run_query.py version 1
from __future__ import annotations
import argparse
import json
import yaml
from dotenv import load_dotenv
from analyzer import parse, llm_parse
from analyzer_strategy_1 import parse as parse_v1, llm_parse as llm_parse_v1
from decompose import to_sql, run as run_plan
from db import DBAdapter
from LLM.llm_router import generate_with_fallback, llm_enabled
import sys
sys.stdout.reconfigure(encoding='utf-8')

print("DEBUG: run_query.py starting")

# Load .env (e.g., GEMINI_API_KEY) once on startup
load_dotenv()

# ---------- Gemini advisor (not a coach) ----------
def llm_structured_summary(
    cfg: dict,
    parsed,
    structured_result,
    *,
    question: str = "",
    session_id: str = "",
) -> tuple[str | None, str | None]:
    """
    LLM-powered narrative that *only* looks at the structured_result
    coming back from the databases.

    Used for the === Summary === section.
    """
    if not structured_result:
        return None, None

    llm_cfg = (cfg or {}).get("llm", {})
    if not llm_enabled(cfg):
        return None, None
    if llm_cfg.get("use_for_summary") is False:
        return None, None

    task = getattr(parsed, "task", None) or ""
    role = getattr(parsed, "role", None)
    loc  = getattr(parsed, "location", None)
    cand_id = getattr(parsed, "candidate_id", None)
    cand_name = getattr(parsed, "candidate_name", None)

    system = (
        "You are a hiring analytics summarizer. "
        "You receive *only* structured JSON that came from SQL queries "
        "(jobs, candidates, skills, overlaps, counts). "
        "You must write a short, concrete summary for a hiring manager.\n\n"
        "Style:\n"
        "- 3 to 6 bullet points OR 1 short paragraph + 2–3 bullets\n"
        "- Max 150 words\n"
        "- No markdown headings, no numbered lists, no code fences\n"
        "- Do NOT invent companies, skills, or numbers that are not in the JSON.\n"
        "- Do NOT give generic career advice; stick to what the data shows.\n\n"
        "Special handling when task == 'candidate_profile_and_eligible_jobs':\n"
        "- Mention the candidate name / ID.\n"
        "- Mention their key skills.\n"
        "- Mention roughly how many jobs matched.\n"
        "- Highlight 2–3 representative matches (title + company, and optionally location).\n"
        "- Comment briefly on seniority/fit based on job titles.\n"
        "If there are zero jobs, say that clearly and suggest looking at adjacent roles "
        "purely based on the skills present.\n"
    )

    payload = {
        "task": task,
        "role": role,
        "location": loc,
        "candidate_id": cand_id,
        "candidate_name": cand_name,
        "structured_result": structured_result,
    }

    user = json.dumps(payload, ensure_ascii=False)
    text, _provider = generate_with_fallback(
        cfg,
        system,
        user,
        json_mode=False,
        capture_context={
            "kind": "llm_summary_failed",
            "question": question,
            "session_id": session_id,
            "stage": "summary",
            "fallback_used": "sql",
            "extra": {"task": task},
        },
    )
    if not text:
        return None, None

    return text.strip(), _provider

def maybe_llm_pointer(cfg: dict, parsed, location_hint: str | None = None) -> str | None:
    if not llm_enabled(cfg):
        return None

    system = (
        "You are a hiring analytics advisor. Be crisp and actionable. "
        "Max 140 words. Use exactly three titled sections with bullets:\n"
        "1) WHAT TO SCREEN FOR — 3–5 concrete skills/experiences/tools for this role/location.\n"
        "2) RESULTS HIGHLIGHTS — how to read the results shown; patterns, immediate next steps.\n"
        "3) BONUS POINTS — 2–3 extras (certs, domain tools, measurable impact).\n"
        "No pep-talk or fluff."
    )

    role = getattr(parsed, "role", "") or ""
    loc  = location_hint or getattr(parsed, "location", "") or ""
    skill = getattr(parsed, "skill", "") or ""
    task = getattr(parsed, "task", "") or ""
    user = (getattr(parsed, "llm_subquery", "") or "").strip()

    if not user:
        parts = []
        if role:  parts.append(f"role={role}")
        if skill: parts.append(f"focus_skill={skill}")
        if loc:   parts.append(f"location={loc}")
        if task:  parts.append(f"task={task}")
        user = " | ".join(parts) if parts else "general hiring analytics"

    if loc and "location=" not in user.lower():
        user += f" | location={loc}"

    text, _provider = generate_with_fallback(cfg, system, user, json_mode=False)
    return text


def maybe_llm_freeform(cfg: dict, user_query: str) -> str | None:
    """
    Final fallback for out-of-database or totally random questions.
    """
    if not llm_enabled(cfg):
        return None

    system = (
        "You are a concise hiring-analytics assistant. "
        "If the question is not about our jobs/candidates data, answer helpfully in 120 words or less. "
        "If it IS about hiring, give focused, practical advice."
    )
    user = user_query.strip()
    text, _provider = generate_with_fallback(cfg, system, user, json_mode=False)
    return text

# ---------- NEW: LLM query re-writing + integration ----------

def build_llm_integration_request(parsed, structured_result):
    """
    Build a JSON-style payload + instructions for Gemini, based on the
    SQL result. This is the 'rewritten prompt' for Task (b).

    Returns (system_prompt, user_prompt) or (None, None) if not applicable.
    """
    task = getattr(parsed, "task", None)

    # 1) Enrich "top_skills_for_role" with market intelligence
    if task == "top_skills_for_role" and isinstance(structured_result, list):
        role = (parsed.role or "").strip()
        loc  = (parsed.location or "").strip()

        # keep only first N to keep prompt small
        skills = structured_result[:30]

        system = (
            "You are a hiring market analyst with access to up-to-date internet/job-market data. "
            "You receive structured SQL results about skills for a role, and you enrich them using "
            "broader knowledge (job postings, salary trends, etc.). "
            "You MUST respond with pure JSON, no comments or extra text.\n\n"
            "Output format:\n"
            "{\n"
            '  "skills": [\n'
            '     {"name": str, "frequency": int, '
            '"market_trend": "rising|stable|declining", '
            '"importance_note": str}\n'
            "  ]\n"
            "}"
        )

        payload = {
            "role": role,
            "location": loc or None,
            "skills": skills,
        }

        user = json.dumps(payload, ensure_ascii=False)
        return system, user

    # 2) Enrich "candidate_readiness" with readiness labels + notes
    if task == "candidate_readiness" and isinstance(structured_result, dict):
        role = (parsed.role or "").strip()
        loc  = (parsed.location or "").strip()
        cands = structured_result.get("candidates") or structured_result.get("matching_candidates", [])

        if not cands:
            return None, None

        # trim to first N candidates to keep token usage reasonable
        c_small = []
        for c in cands[:30]:
            c_small.append(
                {
                    "candidate_id": c.get("candidate_id"),
                    "name": c.get("name"),
                    "location": c.get("location"),
                    "skills": c.get("skills", []),
                    "overlap_skills": c.get("overlap", []),
                    "coverage": c.get("coverage"),
                    "jaccard": c.get("jaccard"),
                }
            )

        system = (
            "You are a technical recruiter using both structured candidate data and broader web knowledge "
            "about job requirements and technologies.\n"
            "Given a role and list of candidates (with skills/overlap metrics), "
            "rate each candidate's readiness for the role.\n"
            "Use your internet-scale knowledge of typical requirements for the role and location.\n\n"
            "You MUST respond with valid JSON only, using this format:\n"
            "{\n"
            '  "candidates": [\n'
            '    {"candidate_id": int, '
            '"readiness": "high|medium|low", '
            '"summary": str}\n'
            "  ]\n"
            "}"
        )

        payload = {
            "role": role,
            "location": loc or None,
            "candidates": c_small,
        }
        user = json.dumps(payload, ensure_ascii=False)
        return system, user
    
    # 3) Enrich "similar_roles_for_role" with transition metadata
    if task == "similar_roles_for_role" and isinstance(structured_result, list):
        base_role = (parsed.role or "").strip()
        loc = (parsed.location or "").strip()

        # Normalize roles list into a small, clean payload
        related_roles = []
        for row in structured_result[:50]:  # cap to keep prompt small
            if isinstance(row, dict):
                name = (row.get("similar_role")
                        or row.get("role")
                        or row.get("title"))
                if not name:
                    continue
                related_roles.append({
                    "name": str(name),
                    "similarity": row.get("similarity")  # may be None
                })
            else:
                # fallback if rows are plain strings
                name = str(row).strip()
                if name:
                    related_roles.append({"name": name, "similarity": None})

        if not related_roles:
            return None, None

        system = (
            "You are a hiring market analyst. "
            "Given a base role and a list of related roles (with optional similarity scores), "
            "you will annotate each related role with transition metadata.\n\n"
            "Respond with JSON ONLY, no comments or extra text.\n\n"
            "Output format:\n"
            "{\n"
            '  \"roles\": [\n'
            '    {\n'
            '      \"name\": str,\n'
            '      \"transition_difficulty\": \"easy\"|\"moderate\"|\"hard\",\n'
            '      \"common_transition_paths\": [str, ...],\n'
            '      \"typical_salary_band_hint\": str\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        payload = {
            "base_role": base_role,
            "location": loc or None,
            "related_roles": related_roles,
        }
        user = json.dumps(payload, ensure_ascii=False)
        return system, user

    # 4) Enrich "similar_skills_for_skill" with use-case / seniority metadata
    if task == "similar_skills_for_skill" and isinstance(structured_result, list):
        base_skill = (parsed.skill or "").strip()
        loc = (parsed.location or "").strip()

        related_skills = []
        for row in structured_result[:50]:
            if isinstance(row, dict):
                name = (row.get("similar_skill")
                        or row.get("skill")
                        or row.get("name"))
                if not name:
                    continue
                related_skills.append({
                    "name": str(name),
                    "similarity": row.get("similarity")
                })
            else:
                name = str(row).strip()
                if name:
                    related_skills.append({"name": name, "similarity": None})

        if not related_skills:
            return None, None

        system = (
            "You are a technical hiring specialist. "
            "Given a base skill and a list of related skills (with optional similarity scores), "
            "annotate each related skill with usage and seniority metadata.\n\n"
            "Respond with JSON ONLY, no comments or extra text.\n\n"
            "Output format:\n"
            "{\n"
            '  \"skills\": [\n'
            '    {\n'
            '      \"name\": str,\n'
            '      \"use_case_cluster\": str,\n'
            '      \"seniority_level_hint\": \"junior\"|\"mid\"|\"senior\"|\"mixed\",\n'
            '      \"complementary_to_base\": [str, ...]\n'
            "    }\n"
            "  ]\n"
            "}"
        )

        payload = {
            "base_skill": base_skill,
            "location": loc or None,
            "related_skills": related_skills,
        }
        user = json.dumps(payload, ensure_ascii=False)
        return system, user
    # other tasks not yet integrated
    return None, None


def maybe_enrich_with_llm_integration(cfg: dict, parsed, structured_result):
    """
    Implements Task (b) + (c):
      - rewrite NL+SQL results into a JSON prompt
      - call Gemini in JSON mode
      - join LLM outputs back into structured_result.
    Returns (enriched_result, raw_llm_json_or_text_or_None).
    """
    if structured_result is None:
        return structured_result, None

    llm_cfg = (cfg or {}).get("llm", {})
    if not llm_enabled(cfg):
        return structured_result, None
    if llm_cfg.get("use_for_enrichment") is False:
        return structured_result, None

    system, user = build_llm_integration_request(parsed, structured_result)
    if not system or not user:
        return structured_result, None

    raw, _provider = generate_with_fallback(cfg, system, user, json_mode=True)
    if not raw:
        return structured_result, None
    try:
        obj = json.loads(raw)
    except Exception:
        # Some models may wrap JSON in markdown; try to salvage
        try:
            raw2 = raw.strip().strip("`").strip()
            if "{" in raw2 and "}" in raw2:
                raw2 = raw2[raw2.find("{"): raw2.rfind("}") + 1]
            obj = json.loads(raw2)
        except Exception:
            print("[LLM integration] Failed to parse JSON from Gemini")
            return structured_result, raw

    task = getattr(parsed, "task", None)

    # Join for top_skills_for_role 
    if task == "top_skills_for_role" and isinstance(structured_result, list):
        enrich_map = {}
        for s in obj.get("skills", []):
            name = (s.get("name") or "").strip().lower()
            if not name:
                continue
            enrich_map[name] = {
                "llm_market_trend": s.get("market_trend"),
                "llm_importance_note": s.get("importance_note"),
            }
        for row in structured_result:
            key = (row.get("skill") or "").strip().lower()
            extra = enrich_map.get(key)
            if extra:
                row.update(extra)
        return structured_result, obj

    # Join for candidate_readiness
    if task == "candidate_readiness" and isinstance(structured_result, dict):
        cands = structured_result.get("candidates") or structured_result.get("matching_candidates", [])
        enrich_map = {}
        for c in obj.get("candidates", []):
            cid = c.get("candidate_id")
            if cid is None:
                continue
            enrich_map[cid] = {
                "llm_readiness": c.get("readiness"),
                "llm_note": c.get("summary"),
            }
        for c in cands:
            cid = c.get("candidate_id")
            extra = enrich_map.get(cid)
            if extra:
                c.update(extra)
        return structured_result, obj
    
    # Join for similar_roles_for_role
    if task == "similar_roles_for_role" and isinstance(structured_result, list):
        enrich_map = {}
        for r in obj.get("roles", []):
            name = (r.get("name") or "").strip().lower()
            if not name:
                continue
            enrich_map[name] = {
                "llm_transition_difficulty": r.get("transition_difficulty"),
                "llm_common_transition_paths": r.get("common_transition_paths"),
                "llm_salary_band_hint": r.get("typical_salary_band_hint"),
            }

        for row in structured_result:
            if isinstance(row, dict):
                key = (
                    (row.get("similar_role")
                     or row.get("role")
                     or row.get("title")
                     or "")
                    .strip()
                    .lower()
                )
                if not key:
                    continue
                extra = enrich_map.get(key)
                if extra:
                    row.update(extra)
        return structured_result, obj

    # Join for similar_skills_for_skill
    if task == "similar_skills_for_skill" and isinstance(structured_result, list):
        enrich_map = {}
        for s in obj.get("skills", []):
            name = (s.get("name") or "").strip().lower()
            if not name:
                continue
            enrich_map[name] = {
                "llm_use_case_cluster": s.get("use_case_cluster"),
                "llm_seniority_level_hint": s.get("seniority_level_hint"),
                "llm_complementary_to_base": s.get("complementary_to_base"),
            }

        for row in structured_result:
            if isinstance(row, dict):
                key = (
                    (row.get("similar_skill")
                     or row.get("skill")
                     or row.get("name")
                     or "")
                    .strip()
                    .lower()
                )
                if not key:
                    continue
                extra = enrich_map.get(key)
                if extra:
                    row.update(extra)
        return structured_result, obj

    return structured_result, obj


# ---------- Narrative formatter ----------

def _loc_suffix(parsed) -> str:
    loc = (getattr(parsed, "location", None) or "").strip()
    return f" in {loc}" if loc else ""


def _job_lines(jobs: list, limit: int = 10) -> list[str]:
    lines = []
    for r in jobs[:limit]:
        title = r.get("title") or r.get("job_title") or "—"
        company = r.get("company") or r.get("company_name") or "—"
        where = r.get("location") or ""
        lines.append(f"• {title} — {company}" + (f" ({where})" if where else ""))
    return lines


def _candidate_lines(cands: list, limit: int = 10) -> list[str]:
    lines = []
    for c in cands[:limit]:
        name = c.get("name") or f"Candidate {c.get('candidate_id')}"
        where = f" — {c['location']}" if c.get("location") else ""
        overlap = c.get("overlap") or c.get("skills") or []
        if isinstance(overlap, list) and overlap:
            extra = f": {', '.join(str(x) for x in overlap[:6])}"
        else:
            yrs = c.get("years")
            extra = f": {yrs} yrs" if yrs is not None else ""
        lines.append(f"• {name}{where}{extra}")
    return lines


def _generic_summary(parsed, structured_result) -> str:
    """Last-resort summary from whatever structured payload we got."""
    if isinstance(structured_result, list):
        if not structured_result:
            return "No results matched that query."
        if isinstance(structured_result[0], dict):
            keys = set(structured_result[0].keys())
            if keys & {"title", "company", "job_id"}:
                head = f"Found {len(structured_result)} job(s)."
                return "\n".join([head, *_job_lines(structured_result)])
            if keys & {"skill", "frequency"}:
                top = ", ".join(
                    f"{d.get('skill', d.get('name', '?'))} ({d.get('frequency', d.get('count', '?'))})"
                    for d in structured_result[:10]
                )
                return f"Top results: {top or '—'}"
            if keys & {"candidate_id", "name"}:
                head = f"Found {len(structured_result)} candidate(s)."
                return "\n".join([head, *_candidate_lines(structured_result)])
        return f"Found {len(structured_result)} result(s)."

    if isinstance(structured_result, dict):
        parts = []
        for key, value in structured_result.items():
            if isinstance(value, list) and value:
                parts.append(f"{key}: {len(value)}")
            elif value not in (None, "", [], {}):
                parts.append(f"{key}: {value}")
        if parts:
            role = (getattr(parsed, "role", None) or "").strip()
            skill = (getattr(parsed, "skill", None) or "").strip()
            subject = role or skill or "your query"
            return f"Results for {subject}{_loc_suffix(parsed)} — " + "; ".join(parts[:6]) + "."

    return "Query completed — see structured data below."


def format_narrative(task, parsed, structured_result) -> str:
    if structured_result is None:
        return "I didn’t find anything for that query."

    if isinstance(structured_result, list):
        if not structured_result:
            return "I didn’t find anything for that query."
        if task == "top_skills_for_role":
            top = ", ".join(
                f"{d.get('skill', d.get('name', '?'))} ({d.get('frequency', 0)})"
                for d in structured_result[:10]
            ) or "—"
            role = (getattr(parsed, "role", None) or "").strip() or "the role"
            return f"Top skills for {role}{_loc_suffix(parsed)}: {top}"
        if task in ("list_jobs_for_role", "filter_jobs_by_role"):
            head = f"Found {len(structured_result)} job(s) for {(getattr(parsed, 'role', None) or 'the role').strip()}{_loc_suffix(parsed)}."
            return "\n".join([head, *_job_lines(structured_result)])
        return _generic_summary(parsed, structured_result)

    if isinstance(structured_result, dict) and not structured_result:
        return "I didn’t find anything for that query."

    if task == "candidate_readiness":
        role_sk = structured_result.get("role_skills", [])
        cands = structured_result.get("candidates") or structured_result.get("matching_candidates", [])
        n = len(cands)
        role = (parsed.role or "").strip()
        head = f"Found {n} candidate(s) ready for {role}" if role else f"Found {n} candidate(s)"
        head += _loc_suffix(parsed) + "."
        lines = _candidate_lines(cands)
        top_sk = ", ".join(role_sk[:10]) if role_sk else "—"
        tail = f"Most frequent role skills: {top_sk}."
        return "\n".join([head, *lines, tail])

    if task in ("filter_candidates_by_experience_role", "filter_candidates_by_skills",
                "filter_candidates_by_location", "filter_candidates_by_projects",
                "top_candidates_by_skill_count", "candidate_multi"):
        cands = structured_result.get("candidates", [])
        role = (parsed.role or "").strip()
        loc = (parsed.location or "").strip()
        yrs = getattr(parsed, "min_years", None) or 0
        head = f"Found {len(cands)} candidate(s)"
        if yrs:
            head += f" with ≥{yrs} years"
        if role:
            head += f" for {role}"
        if loc:
            head += f" in {loc}"
        head += "."
        return "\n".join([head, *_candidate_lines(cands)])

    if task == "eligible_companies_for_candidate":
        rows = structured_result.get("eligible_companies", [])
        head = f"Top {min(10, len(rows))} matching companies:"
        lines = [f"• {r.get('company')} — {r.get('title')} ({r.get('location')})" for r in rows[:10]]
        return "\n".join([head, *lines]) if lines else "No matching companies found."

    if task == "count_jobs_by_skill":
        skill = (getattr(parsed, "skill", None) or "").strip() or "that skill"
        count = structured_result.get("count", 0)
        return f"Jobs requiring {skill}{_loc_suffix(parsed)}: {count}"

    if task == "top_skills_for_role":
        items = structured_result if isinstance(structured_result, list) else structured_result.get("skills", [])
        top = ", ".join(
            f"{d.get('skill', d.get('name', '?'))} ({d.get('frequency', 0)})"
            for d in (items or [])[:10]
        ) or "—"
        role = (parsed.role or "").strip() or "the role"
        return f"Top skills for {role}{_loc_suffix(parsed)}: {top}"

    if task in ("list_jobs_for_role", "jobs_multi", "filter_jobs_by_role",
                "filter_jobs_by_skills", "filter_jobs_by_location"):
        jobs = structured_result.get("jobs", structured_result if isinstance(structured_result, list) else [])
        if not isinstance(jobs, list):
            jobs = []
        role = (getattr(parsed, "role", None) or "").strip() or "the query"
        head = f"Found {len(jobs)} job(s) for {role}{_loc_suffix(parsed)}."
        lines = _job_lines(jobs)
        return "\n".join([head, *lines]) if lines else head

    if task == "similar_roles_for_role":
        base = structured_result.get("base_role") or (getattr(parsed, "role", None) or "")
        sims = structured_result.get("similar_roles", [])
        label = structured_result.get("cluster_label")
        head = f"Roles similar to {base}"
        if label:
            head += f" (cluster: {label})"
        head += f" — {len(sims)} found:"
        lines = [
            f"• {r.get('role', r.get('similar_role', '?'))} ({r.get('frequency', r.get('count', '?'))} jobs)"
            for r in sims[:10]
        ]
        return "\n".join([head, *lines]) if lines else head + " none in this dataset."

    if task == "similar_skills_for_skill":
        base = structured_result.get("base_skill") or (getattr(parsed, "skill", None) or "")
        rel = structured_result.get("related_skills", structured_result.get("skills", []))
        label = structured_result.get("cluster_label")
        head = f"Skills related to {base}"
        if label:
            head += f" (cluster: {label})"
        head += f" — {len(rel)} found:"
        lines = [
            f"• {s.get('skill', s.get('name_lc', s.get('name', '?')))} ({s.get('job_count', s.get('frequency', '?'))} jobs)"
            for s in rel[:12]
        ]
        return "\n".join([head, *lines]) if lines else head + " none in this dataset."

    if task == "candidate_profile_and_eligible_jobs":
        cand = structured_result.get("candidate") or {}
        jobs = structured_result.get("eligible_jobs", [])
        name = cand.get("name") or f"Candidate {cand.get('candidate_id', '?')}"
        skills = cand.get("skills") or cand.get("skills_core") or []
        skill_txt = ", ".join(skills[:8]) if skills else "—"
        head = f"{name}: skills include {skill_txt}."
        if not jobs:
            return head + " No eligible jobs matched in this session."
        lines = [f"Matched {len(jobs)} job(s):"] + _job_lines(jobs)
        return "\n".join([head, *lines])

    return _generic_summary(parsed, structured_result)


def main():
    print("DEBUG: entering main()")

    ap = argparse.ArgumentParser(description="Job Market Insights – Query Analyzer")
    ap.add_argument("question", type=str, help="Natural language query")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    print(f"DEBUG: question arg = {args.question}")
    print(f"DEBUG: using config = {args.config}")

    # Load config and build adapter
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    adapter = DBAdapter(cfg)

    # Register REGEXP for SQLite adapters (safe no-op otherwise)
    try:
        import sqlite3, re
        raw = (
            getattr(adapter, "conn", None)
            or getattr(adapter, "con", None)
            or getattr(adapter, "connection", None)
        )
        if isinstance(raw, sqlite3.Connection):
            def _regex(expr, item):
                if item is None:
                    return 0
                return 1 if re.search(expr, item) else 0
            raw.create_function("REGEXP", 2, _regex)
    except Exception:
        pass



    # --------- Analyze the NL question ---------
    parser_cfg = (cfg or {}).get("parser", {})
    prefer_llm = bool(parser_cfg.get("prefer_llm", False))

    parsed = None
    used_llm_parser = False

    if prefer_llm:
        # 1) Try LLM parser first
        parsed = llm_parse_v1(args.question, cfg)
        used_llm_parser = parsed is not None

        # 2) Fallback to manual regex parser
        if not parsed:
            parsed = parse(args.question)
    else:
        # 1) Try manual regex parser first
        parsed = parse(args.question)

        # 2) Fallback to LLM parser if manual failed
        if not parsed:
            parsed = llm_parse_v1(args.question, cfg)
            used_llm_parser = parsed is not None

    # If still nothing, final fallback → freeform answer
    if not parsed:
        freeform = maybe_llm_freeform(cfg, args.question)
        if freeform:
            print("\n=== LLM Answer (freeform) ===")
            print(freeform)
            return
        print("Sorry, I couldn't understand that question yet.")
        return

    # --------- Decompose into SQL plan ---------
    plan = to_sql(parsed, cfg)
    print("\n=== RAW ParsedQuery ===")
    try:
        # Convert dataclass to dict for clean printing
        from dataclasses import asdict
        pq = asdict(parsed)
        print(json.dumps(pq, indent=2, ensure_ascii=False))
    except Exception:
        # fallback: print raw object
        print(parsed)
    print("\n=== Parsed Query ===")
    print(f"Mode: {parsed.mode}, Task: {parsed.task}, Domain: {parsed.domain}, ")
    if getattr(parsed, "select_attributes", None):
        print("Select attributes:", parsed.select_attributes)
    if getattr(parsed, "filters", None):
        print("Filters:", parsed.filters)
    if getattr(parsed, "candidate_id", None) is not None:
        print("Candidate ID hint:", parsed.candidate_id)

    print("\n=== Decomposition ===")
    if isinstance(plan.get("sql"), tuple):
        print("Structured (multi-source):")
        (sql1, sql2) = plan["sql"]
        (p1, p2) = plan.get("params", ((), ()))
        (src1, src2) = plan.get("source", ("?", "?"))
        print(f"[{src1}] SQL-1:\n{sql1}\nParams: {p1}")
        print(f"[{src2}] SQL-2:\n{sql2}\nParams: {p2}")
    else:
        print("Structured (single-source):")
        print(f"[{plan.get('source')}] SQL:\n{plan.get('sql')}\nParams: {plan.get('params')}")

    print("\nLLM sub-query:")
    print(plan.get("llm_subquery"))

    # --------- Execute plan ---------
    result = run_plan(adapter, plan, cfg)
    structured_result = result.get("structured_result")

    print("\n=== Structured Result (before LLM integration) ===")
    try:
        print(structured_result)
    except UnicodeEncodeError:
        # Fallback for weird Unicode chars on Windows console
        print(str(structured_result).encode("utf-8", errors="replace"))
    # --------- LLM integration (Task 2b + 2c) ---------
    enriched, llm_artifact = maybe_enrich_with_llm_integration(cfg, parsed, structured_result)
    result["structured_result"] = enriched

    print("\n=== Structured Result (after LLM integration) ===")
    print(str(result.get("structured_result")).encode("utf-8", errors="ignore").decode("utf-8"))


    if llm_artifact is not None:
        print("\n=== Raw LLM integration JSON ===")
        print(llm_artifact)

    # --------- Summary (LLM if possible, else rule-based) ---------
    try:
        summary, _prov = llm_structured_summary(cfg, parsed, result.get("structured_result"))
        if not summary:
            summary = format_narrative(plan.get("task"), parsed, result.get("structured_result"))
        print("\n=== Summary ===")
        print(summary)
    except Exception as e:
        print(f"[format error] {e}")

    # --------- Optional advisor notes ---------
    # tips = maybe_llm_pointer(cfg, parsed, getattr(parsed, "location", None))
    # if tips:
    #     print("\n=== Advisor notes (Gemini) ===")
    #     print(tips)


if __name__ == "__main__":
    main()
