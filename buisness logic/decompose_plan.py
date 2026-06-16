# decompose_plan.py  — query planner (to_sql only)
from dataclasses import replace
from typing import Dict, Any, List, Tuple

import pycountry
from analyzer import ParsedQuery
import regex as _re


# ---------------------- helpers (planner) ----------------------
def safe_lc(x):
    try:
        return str(x).strip().lower() if x is not None else ""
    except Exception:
        return ""


def _like(s: str) -> str:
    return f"%{(s or '').lower()}%"


def _word_regex(term):
    """
    Whole-word, case-insensitive.

    - If `term` is a string, match that word.
    - If `term` is a list/tuple/set of strings, match ANY of them
      (e.g., union of many country names for a continent).
    """
    # Multiple terms: build (a|b|c) union
    if isinstance(term, (list, tuple, set)):
        parts = [safe_lc(t) for t in term if safe_lc(t)]
        if not parts:
            return r"(?!)"  # matches nothing
        inner = "|".join(_re.escape(p) for p in parts)
        return rf"(?i)(^|[^a-z])({inner})([^a-z]|$)"

    # Single term
    t = safe_lc(term)
    if not t:
        return r"(?!)"
    return rf"(?i)(^|[^a-z]){_re.escape(t)}([^a-z]|$)"

def _clone_for_multi(parsed: ParsedQuery, task_name: str, **overrides) -> ParsedQuery:
    """
    Make a ParsedQuery for a subplan inside the multi-candidate planner.

    Crucial: set is_multi_subplan=True so to_sql() does NOT try to
    re-enter the multi-planner for this ParsedQuery.
    """
    return replace(
        parsed,
        task=task_name,
        is_multi_subplan=True,
        **overrides,
    )
COUNTRY_MAP = {c.name.lower(): c.name for c in pycountry.countries}
COUNTRY_MAP.update({
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
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
CONTINENT_COUNTRIES = {
    "europe": [
        "France", "Germany", "Spain", "Italy", "Netherlands", "Belgium",
        "United Kingdom", "Ireland", "Switzerland", "Austria",
        "Sweden", "Norway", "Denmark", "Finland", "Portugal", "Greece",
        "Poland", "Czechia", "Hungary", "Romania", "Bulgaria",
        "Croatia", "Serbia", "Slovakia", "Slovenia",
    ],
    "asia": [
        "India", "China", "Japan", "Singapore", "Korea, Republic of",
        "Indonesia", "Malaysia", "Thailand", "Philippines",
        "Viet Nam", "Bangladesh", "Pakistan", "South Korea", "Republic of Korea",
        "China", "Taiwan"
    ],
    "north america": [
        "United States", "Canada", "Mexico",
    ],
    "south america": [
        "Brazil", "Argentina", "Chile", "Colombia", "Peru",
    ],
    "africa": [
        "South Africa", "Nigeria", "Kenya", "Egypt", "Morocco",
    ],
    "oceania": [
        "Australia", "New Zealand",
    ],
}
def canonical_country(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = loc.split(",")
    candidate = parts[-1].strip().lower() if parts else ""
    return COUNTRY_MAP.get(candidate, None)

def _normalize_location_for_query(loc: str | None) -> tuple[str | None, str | None]:
    """
    Take a user/location string and return:
      - country_lc: normalized lowercased country name for equality checks
      - pattern: regex pattern for matching in *_location_lc columns

    Cases:
      - Country name / 'US' / 'USA' / etc -> (country_lc, regex for that country)
      - City name (e.g., 'berlin')        -> (loc_lc, regex for that token)
      - Continent (e.g., 'europe')        -> (None, regex matching ANY country
                                              in that continent)
    """
    if not loc:
        return None, None

    loc_lc = safe_lc(loc)

    # 1) Continent names: build a regex that matches ANY country in that continent.
    #    We deliberately return country_lc=None so SQL falls back to the regex part:
    #       (... OR location_lc ~* pattern)
    continent_countries = CONTINENT_COUNTRIES.get(loc_lc)
    if continent_countries:
        pattern = _word_regex(continent_countries)
        return None, pattern

    # 2) Normal country resolution via COUNTRY_MAP (usa/us/india/etc.)
    canon = canonical_country(loc)
    if canon:
        country_lc = safe_lc(canon)
        return country_lc, _word_regex(canon)

    # 3) Fallback: treat the raw text as a token to search in location_lc.
    if not loc_lc:
        return None, None
    return loc_lc, _word_regex(loc)

def _build_candidate_multi_plan(parsed, cfg):
    """
    Build a federated candidate_multi plan by splitting one ParsedQuery
    (with multiple filters) into several subplans, each with ONE filter.
    """
    subplans = []

    # Experience filter
    if getattr(parsed, "min_years", None) is not None:
        p_exp = _clone_for_multi(
            parsed,
            task_name="filter_candidates_by_experience_role",
            skills_text=None,
            project_query=None,
        )
        subplans.append(to_sql(p_exp, cfg))

    # Skills filter
    if getattr(parsed, "skills_text", None):
        p_sk = _clone_for_multi(
            parsed,
            task_name="filter_candidates_by_skills",
            min_years=None,
            project_query=None,
        )
        subplans.append(to_sql(p_sk, cfg))

    # Projects filter
    if getattr(parsed, "project_query", None):
        p_proj = _clone_for_multi(
            parsed,
            task_name="filter_candidates_by_projects",
            min_years=None,
            skills_text=None,
        )
        subplans.append(to_sql(p_proj, cfg))

    if not subplans:
        raise ValueError("candidate_multi: no active candidate filters")

    return {
        "task": "candidate_multi",
        "kind": "multi_candidate",
        "subplans": subplans,
        "combine": "intersection",
        "topk": parsed.topk or 50,
        "llm_subquery": getattr(parsed, "llm_subquery", None),
    }

def _build_jobs_multi_plan(parsed, cfg):
    """
    Build a federated jobs_multi plan by splitting one ParsedQuery
    (role + skills + location) into several subplans, each with ONE filter.
    """
    subplans = []

    # Role filter
    if getattr(parsed, "role", None):
        p_role = _clone_for_multi(
            parsed,
            task_name="filter_jobs_by_role",
            # ensure this subplan is role-only
            skills=None,
        )
        subplans.append(to_sql(p_role, cfg))

    # Skills filter
    skills_list = getattr(parsed, "skills", None) or []
    if skills_list:
        p_sk = _clone_for_multi(
            parsed,
            task_name="filter_jobs_by_skills",
            # ensure this subplan is skills-only
            role=None,
        )
        subplans.append(to_sql(p_sk, cfg))

    # Location filter
    if getattr(parsed, "location", None):
        p_loc = _clone_for_multi(
            parsed,
            task_name="filter_jobs_by_location",
            # ensure this subplan is location-only
            role=None,
            skills=None,
        )
        subplans.append(to_sql(p_loc, cfg))

    if not subplans:
        raise ValueError("jobs_multi: no active job filters")

    return {
        "task": "jobs_multi",
        "kind": "multi_jobs",
        "subplans": subplans,
        "combine": "intersection",
        "llm_subquery": getattr(parsed, "llm_subquery", None),
    }

# ---------------------- planner ----------------------
def to_sql(parsed, cfg):
    limit = int(parsed.topk or 200)
    # --------- multi-candidate federation gate (TOP-LEVEL ONLY) ---------
    if (
        not getattr(parsed, "is_multi_subplan", False)
        and parsed.task in {
            "filter_candidates_by_experience_role",
            "filter_candidates_by_skills",
            "filter_candidates_by_projects",
            "filter_candidates_by_location",
        }
    ):
        active_filters = 0
        if getattr(parsed, "min_years", None) is not None:
            active_filters += 1
        if getattr(parsed, "skills_text", None):
            active_filters += 1
        if getattr(parsed, "project_query", None):
            active_filters += 1
        if getattr(parsed, "location", None):        
            active_filters += 1
        # Only use multi-plan when more than one filter is active
        if active_filters > 1:
            return _build_candidate_multi_plan(parsed, cfg)


# --------- multi-jobs federation gate (TOP-LEVEL ONLY) ---------
    if (
        not getattr(parsed, "is_multi_subplan", False)
        and parsed.task == "list_jobs_for_role"
    ):
        active_filters = 0
        if getattr(parsed, "role", None):
            active_filters += 1
        if getattr(parsed, "skills", None):
            active_filters += 1
        if getattr(parsed, "location", None):
            active_filters += 1

        # Only use multi-plan when more than one filter is active
        if active_filters > 1:
            return _build_jobs_multi_plan(parsed, cfg)

        # ---------------- Filter jobs by role (title pattern) ----------------
    if parsed.task == "filter_jobs_by_role":
        params: List[Any] = [_like(parsed.role or "")]
        sql = (
            "SELECT j.job_id, j.title, j.company, j.location "
            "FROM jobs j "
            "WHERE j.title_lc LIKE %s "
            f"LIMIT {limit}"
        )
        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: [
                {
                    "job_id": r[0],
                    "title": r[1],
                    "company": r[2],
                    "location": r[3],
                }
                for r in rows
            ],
            "llm_subquery": parsed.llm_subquery,
        }
    # ---------------- Filter jobs by required skills (AND across skills) ----------------
    if parsed.task == "filter_jobs_by_skills":
        # ParsedQuery already has a list of skills from analyzer
        skills_raw = getattr(parsed, "skills", None) or []
        skills = [safe_lc(s) for s in skills_raw if safe_lc(s)]
        # If somehow we got here with no skills, just bail out to a trivial plan
        if not skills:
            sql = (
                "SELECT j.job_id, j.title, j.company, j.location "
                f"FROM jobs j LIMIT {limit}"
            )
            return {
                "task": parsed.task,
                "source": "jobs_db",
                "sql": sql,
                "params": (),
                "post": lambda rows: [
                    {
                        "job_id": r[0],
                        "title": r[1],
                        "company": r[2],
                        "location": r[3],
                    }
                    for r in rows
                ],
                "llm_subquery": parsed.llm_subquery,
            }

        placeholders = ", ".join(["%s"] * len(skills))
        sql = (
            "SELECT j.job_id, j.title, j.company, j.location, "
            "       COUNT(DISTINCT s.name_lc) AS matched_skills, "
            "       string_agg(DISTINCT s.name_lc, ',') AS matched_skill_names "
            "FROM jobs j "
            "JOIN job_skills js ON j.job_id = js.job_id "
            "JOIN skills s ON s.skill_id = js.skill_id "
            f"WHERE s.name_lc IN ({placeholders}) "
            "GROUP BY j.job_id, j.title, j.company, j.location "
            "HAVING COUNT(DISTINCT s.name_lc) >= %s "
            f"LIMIT {limit}"
        )

        params: List[Any] = skills + [len(skills)]

        def post(rows):
            out = []
            for job_id, title, company, loc, matched_cnt, matched_names in rows:
                matched_list = [
                    n for n in (matched_names or "").split(",") if n
                ]
                out.append(
                    {
                        "job_id": job_id,
                        "title": title,
                        "company": company,
                        "location": loc,
                        "matched_skills": matched_list,
                        "matched_skills_count": matched_cnt,
                    }
                )
            return out

        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": post,
            "llm_subquery": parsed.llm_subquery,
        }
    # ---------------- Filter jobs by location only ----------------
    if parsed.task == "filter_jobs_by_location":
        country_lc, pattern = _normalize_location_for_query(parsed.location)
        params: List[Any] = [_like(parsed.location), _like(parsed.location), pattern]
        sql = (
            "SELECT j.job_id, j.title, j.company, j.location "
            "FROM jobs j "
            "WHERE (j.country_lc ILIKE %s OR j.location_lc ILIKE %s OR j.location_lc ~* %s) "
            f"LIMIT {limit}"
        )
        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: [
                {
                    "job_id": r[0],
                    "title": r[1],
                    "company": r[2],
                    "location": r[3],
                }
                for r in rows
            ],
            "llm_subquery": parsed.llm_subquery,
        }

    # ---------------- Count jobs by skill ----------------
    if parsed.task == "count_jobs_by_skill":
        params: List[Any] = [_like(parsed.skill)]
        sql = (
            "SELECT COUNT(DISTINCT j.job_id) "
            "FROM jobs j "
            "JOIN job_skills js ON j.job_id = js.job_id "
            "JOIN skills s ON s.skill_id = js.skill_id "
            "WHERE s.name_lc LIKE %s"
        )
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            sql += " AND (j.country_lc ILIKE %s OR j.location_lc ILIKE %s OR j.location_lc ~* %s)"
            params.extend([_like(parsed.location), _like(parsed.location), pattern])



        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: {"count": rows[0][0] if rows else 0},
            "llm_subquery": parsed.llm_subquery,
        }

    # ---------------- Top-K skills for a role ----------------
    if parsed.task == "top_skills_for_role":
        params: List[Any] = [_like(parsed.role)]
        sql = (
            "SELECT s.name, COUNT(*) AS freq "
            "FROM jobs j "
            "JOIN job_skills js ON j.job_id = js.job_id "
            "JOIN skills s ON s.skill_id = js.skill_id "
            "WHERE j.title_lc LIKE %s"
        )
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            sql += " AND (j.country_lc ILIKE %s OR j.location_lc ILIKE %s OR j.location_lc ~* %s)"
            params.extend([_like(parsed.location), _like(parsed.location), pattern])

        sql += f" GROUP BY s.name ORDER BY freq DESC LIMIT {limit}"
        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: [{"skill": r[0], "frequency": r[1]} for r in rows],
            "llm_subquery": parsed.llm_subquery,
        }

    # ---------------- List jobs for a role ----------------
    # ---------------- List jobs for a role (optionally with skills + location) ----------------
    if parsed.task == "list_jobs_for_role":
        # Role LIKE pattern
        role_like = _like(parsed.role) if getattr(parsed, "role", None) else "%%"
        params: List[Any] = [role_like]

        skills_list = getattr(parsed, "skills", None) or []
        # normalize skills to lowercase strings
        skills_lc = [safe_lc(s) for s in skills_list if safe_lc(s)]

        # CASE 1: role + optional location, but NO skills filter
        if not skills_lc:
            sql = (
                "SELECT j.job_id, j.title, j.company, j.location "
                "FROM jobs j "
                "WHERE j.title_lc LIKE %s"
            )
            if parsed.location:
                country_lc, pattern = _normalize_location_for_query(parsed.location)
                sql += " AND (j.country_lc ILIKE %s OR j.location_lc ILIKE %s OR j.location_lc ~* %s)"
                params.extend([_like(parsed.location), _like(parsed.location), pattern])




            sql += f" LIMIT {limit}"

        # CASE 2: role + skills (+ optional location): require ALL skills via HAVING
        else:
            placeholders = ", ".join(["%s"] * len(skills_lc))
            sql = (
                "SELECT j.job_id, j.title, j.company, j.location "
                "FROM jobs j "
                "JOIN job_skills js ON j.job_id = js.job_id "
                "JOIN skills s ON s.skill_id = js.skill_id "
                "WHERE j.title_lc LIKE %s "
            )
            if parsed.location:
                sql += " AND (j.country_lc = %s OR j.location_lc ~* %s)"
                params.extend(
                    [(parsed.location or "").lower(), _word_regex(parsed.location)]
                )

            # Only keep rows whose skills are in our requested set
            sql += f" AND s.name_lc IN ({placeholders}) "
            params.extend(skills_lc)

            # Group per job and require it to have ALL requested skills
            sql += (
                "GROUP BY j.job_id, j.title, j.company, j.location "
                "HAVING COUNT(DISTINCT s.name_lc) >= %s "
                "ORDER BY j.job_id "
                f"LIMIT {limit}"
            )
            params.append(len(skills_lc))

        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: [
                {
                    "job_id": r[0],
                    "title": r[1],
                    "company": r[2],
                    "location": r[3],
                }
                for r in rows
            ],
            "llm_subquery": parsed.llm_subquery,
        }
    # ---------------- Similar roles for a role (role_clusters) ----------------
    if parsed.task == "similar_roles_for_role":
        topk = int(parsed.topk or 10)
        params: List[Any] = [_like(parsed.role), topk]

        sql = """
        WITH target_cluster AS (
            SELECT jr.role_cluster_id
            FROM jobs j
            JOIN job_role_clusters jr ON j.job_id = jr.job_id
            WHERE j.title_lc LIKE %s
            GROUP BY jr.role_cluster_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
        )
        SELECT
            rc.role_cluster_id,
            rc.label,
            rc.top_terms,
            j.title_lc AS role_name,
            COUNT(*) AS freq
        FROM jobs j
        JOIN job_role_clusters jr ON j.job_id = jr.job_id
        JOIN target_cluster tc ON tc.role_cluster_id = jr.role_cluster_id
        LEFT JOIN role_clusters rc ON rc.role_cluster_id = jr.role_cluster_id
        GROUP BY rc.role_cluster_id, rc.label, rc.top_terms, j.title_lc
        ORDER BY freq DESC
        LIMIT %s
        """

        def post(rows):
            if not rows:
                return {
                    "base_role": parsed.role,
                    "cluster_id": None,
                    "cluster_label": None,
                    "cluster_top_terms": [],
                    "similar_roles": [],
                }

            cluster_id = rows[0][0]
            cluster_label = rows[0][1]
            top_terms_raw = rows[0][2] or ""
            top_terms = [t.strip() for t in top_terms_raw.split("|") if t.strip()]

            base_lc = safe_lc(parsed.role)
            sims = []
            for _, _, _, role_name, freq in rows:
                if safe_lc(role_name) == base_lc:
                    continue
                sims.append({"role": role_name, "frequency": freq})

            return {
                "base_role": parsed.role,
                "cluster_id": cluster_id,
                "cluster_label": cluster_label,
                "cluster_top_terms": top_terms,
                "similar_roles": sims,
            }

        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": post,
            "llm_subquery": parsed.llm_subquery,
        }
    # ---------------- Similar skills for a skill (skill_clusters) ----------------
    if parsed.task == "similar_skills_for_skill":
        topk = int(parsed.topk or 15)
        params: List[Any] = [_like(parsed.skill), topk]

        sql = """
        WITH base_skill AS (
            SELECT skill_id, name_lc
            FROM skills
            WHERE name_lc LIKE %s
            ORDER BY LENGTH(name_lc) ASC
            LIMIT 1
        ),
        target_cluster AS (
            SELECT scm.skill_cluster_id
            FROM skill_cluster_membership scm
            JOIN base_skill b ON b.skill_id = scm.skill_id
        )
        SELECT
            sc.skill_cluster_id,
            sc.label,
            sc.size,
            s.skill_id,
            s.name_lc,
            COUNT(js.job_id) AS job_count
        FROM target_cluster tc
        JOIN skill_cluster_membership scm ON scm.skill_cluster_id = tc.skill_cluster_id
        JOIN skills s ON s.skill_id = scm.skill_id
        LEFT JOIN skill_clusters sc ON sc.skill_cluster_id = scm.skill_cluster_id
        LEFT JOIN job_skills js ON js.skill_id = scm.skill_id
        GROUP BY sc.skill_cluster_id, sc.label, sc.size, s.skill_id, s.name_lc
        ORDER BY job_count DESC
        LIMIT %s
        """

        def post(rows):
            # no cluster membership / no such skill in clusters
            if not rows:
                return {
                    "base_skill": parsed.skill,
                    "cluster_id": None,
                    "cluster_label": None,
                    "cluster_size": None,
                    "related_skills": [],
                }

            cluster_id = rows[0][0]
            cluster_label = rows[0][1]
            cluster_size = rows[0][2]

            base_lc = safe_lc(parsed.skill)
            rel = []
            for _, _, _, sid, name_lc, job_count in rows:
                if safe_lc(name_lc) == base_lc:
                    continue
                rel.append(
                    {
                        "skill_id": sid,
                        "skill": name_lc,
                        "job_count": job_count,
                    }
                )

            return {
                "base_skill": parsed.skill,
                "cluster_id": cluster_id,
                "cluster_label": cluster_label,
                "cluster_size": cluster_size,
                "related_skills": rel,
            }

        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": post,
            "llm_subquery": parsed.llm_subquery,
        }

    # ---------------- Candidate readiness (federated) ----------------
    if parsed.task == "candidate_readiness":
        topk = int(parsed.topk or 100)

        # 1) Role skills from jobs_db
        limit = int(cfg.get("role_skill_limit", 30))
        role_skill_sql = (
            "SELECT s.name, COUNT(*) AS freq "
            "FROM jobs j "
            "JOIN job_skills js ON j.job_id = js.job_id "
            "JOIN skills s ON s.skill_id = js.skill_id "
            "WHERE j.title_lc LIKE %s "
            "GROUP BY s.name "
            "ORDER BY freq DESC "
            f"LIMIT {limit}"
        )

        # 2) Candidate experience + skills from candidates_db
        cand_sql = (
            "WITH spans AS ( "
            "  SELECT e.cand_id AS cid, "
            "         CASE "
            "           WHEN NULLIF(e.start_date,'') IS NULL THEN NULL "
            "           WHEN e.start_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN e.start_date::date "
            "           WHEN e.start_date ~ '^[0-9]{4}-[0-9]{2}$' THEN to_date(e.start_date || '-01','YYYY-MM-DD') "
            "           WHEN e.start_date ~ '^[0-9]{4}$' THEN to_date(e.start_date || '-01-01','YYYY-MM-DD') "
            "           ELSE NULL "
            "         END AS sd, "
            "         CASE "
            "           WHEN NULLIF(e.end_date,'') IS NULL THEN NULL "
            "           WHEN e.end_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN e.end_date::date "
            "           WHEN e.end_date ~ '^[0-9]{4}-[0-9]{2}$' THEN to_date(e.end_date || '-01','YYYY-MM-DD') "
            "           WHEN e.end_date ~ '^[0-9]{4}$' THEN to_date(e.end_date || '-01-01','YYYY-MM-DD') "
            "           ELSE NULL "
            "         END AS ed "
            "  FROM experience e "
            "), spans_norm AS ( "
            "  SELECT cid, sd, COALESCE(ed, CURRENT_DATE) AS ed "
            "  FROM spans "
            "), agg AS ( "
            "  SELECT cid, "
            "         SUM(GREATEST(0, (ed - sd))) / 365.25 AS years "
            "  FROM spans_norm "
            "  WHERE sd IS NOT NULL "
            "  GROUP BY cid "
            ") "
            "SELECT c.cand_id AS cid, COALESCE(MAX(a.years), 0.0) AS years, "
            "       cs.skill_id, "
            "       COALESCE(MAX(e.location_lc), MAX(c.location_lc)) AS loc, "
            "       MAX(c.name) AS cname "
            "FROM candidates c "
            "LEFT JOIN agg a ON a.cid = c.cand_id "
            "LEFT JOIN candidate_skills cs ON c.cand_id = cs.cand_id "
            "LEFT JOIN experience e ON e.cand_id = c.cand_id "
        )

        params2: List[Any] = []

        # Optional candidate location filter
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            cand_sql += (
                "WHERE (LOWER(c.country) = %s "
                "   OR e.location_lc ~* %s "
                "   OR c.location_lc ~* %s) "
            )
            params2.extend([country_lc, pattern, pattern])

        cand_sql += "GROUP BY c.cand_id, cs.skill_id "

        # Optional min_years filter via HAVING
        if parsed.min_years is not None:
            cand_sql += "HAVING COALESCE(MAX(a.years), 0.0) >= %s "
            params2.append(float(parsed.min_years))

        return {
            "task": parsed.task,
            "source": ("jobs_db", "candidates_db"),
            "sql": (role_skill_sql, cand_sql),
            "params": ((_like(parsed.role),), tuple(params2)),
            "post": None,
            "llm_subquery": parsed.llm_subquery,
            "topk": topk,
        }

    # ---------------- Filter candidates by experience (≥ years; optional role skills) ----------------
    if parsed.task == "filter_candidates_by_experience_role":
        params: List[Any] = []

        exp_sql = (
            "WITH spans AS ( "
            "  SELECT e.cand_id AS cid, "
            "         CASE "
            "           WHEN NULLIF(e.start_date,'') IS NULL THEN NULL "
            "           WHEN e.start_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN e.start_date::date "
            "           WHEN e.start_date ~ '^[0-9]{4}-[0-9]{2}$' THEN to_date(e.start_date || '-01','YYYY-MM-DD') "
            "           WHEN e.start_date ~ '^[0-9]{4}$' THEN to_date(e.start_date || '-01-01','YYYY-MM-DD') "
            "           ELSE NULL "
            "         END AS sd, "
            "         CASE "
            "           WHEN NULLIF(e.end_date,'') IS NULL THEN NULL "
            "           WHEN e.end_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN e.end_date::date "
            "           WHEN e.end_date ~ '^[0-9]{4}-[0-9]{2}$' THEN to_date(e.end_date || '-01','YYYY-MM-DD') "
            "           WHEN e.end_date ~ '^[0-9]{4}$' THEN to_date(e.end_date || '-01-01','YYYY-MM-DD') "
            "           ELSE NULL "
            "         END AS ed "
            "  FROM experience e "
            "), spans_norm AS ( "
            "  SELECT cid, sd, COALESCE(ed, CURRENT_DATE) AS ed "
            "  FROM spans "
            "), agg AS ( "
            "  SELECT cid, "
            "         SUM(GREATEST(0, (ed - sd))) / 365.25 AS years "
            "  FROM spans_norm "
            "  WHERE sd IS NOT NULL "
            "  GROUP BY cid "
            ") "
            "SELECT c.cand_id AS cid, COALESCE(MAX(a.years), 0.0) AS years, "
            "       cs.skill_id, "
            "       COALESCE(MAX(e.location_lc), MAX(c.location_lc)) AS loc, "
            "       MAX(c.name) AS cname "
            "FROM candidates c "
            "LEFT JOIN agg a ON a.cid = c.cand_id "
            "LEFT JOIN candidate_skills cs ON c.cand_id = cs.cand_id "
            "LEFT JOIN experience e ON e.cand_id = c.cand_id "
        )
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            exp_sql += (
                "WHERE (LOWER(c.country) = %s OR e.location_lc ~* %s OR c.location_lc ~* %s) "
            )
            params.extend([country_lc, pattern, pattern])

        exp_sql += "GROUP BY c.cand_id, cs.skill_id "

        role_skill_sql = None
        role_params: Tuple[Any, ...] = ()
        if parsed.role:
            role_skill_sql = (
                "SELECT s.name, COUNT(*) AS freq "
                "FROM jobs j "
                "JOIN job_skills js ON j.job_id = js.job_id "
                "JOIN skills s ON s.skill_id = js.skill_id "
                "WHERE j.title_lc LIKE %s "
                "GROUP BY s.name ORDER BY freq DESC "
                f"LIMIT {int(cfg.get('role_skills_limit', 30))}"
            )
            role_params = (_like(parsed.role),)

        if role_skill_sql:
            return {
                "task": parsed.task,
                "source": ("candidates_db", "jobs_db"),
                "sql": (exp_sql, role_skill_sql),
                "params": (tuple(params), role_params),
                "post": None,
                "llm_subquery": parsed.llm_subquery,
                "min_years": parsed.min_years,
                "filter_role": True,
            }
        else:
            return {
                "task": parsed.task,
                "source": "candidates_db",
                "sql": exp_sql,
                "params": tuple(params),
                "post": None,
                "llm_subquery": parsed.llm_subquery,
                "min_years": parsed.min_years,
                "filter_role": False,
            }

    # ---------------- Candidate: eligible companies for candidate ----------------
    if parsed.task == "eligible_companies_for_candidate":
        params: List[Any] = [_like(parsed.role)]
        sql = (
            "SELECT j.company, j.title, j.location, j.job_id "
            "FROM jobs j WHERE j.title_lc LIKE %s"
        )
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            sql += " AND (j.country_lc = %s OR j.location_lc ~* %s)"
            params.extend([country_lc, pattern])

        sql += f" LIMIT {limit}"
        return {
            "task": parsed.task,
            "source": "jobs_db",
            "sql": sql,
            "params": tuple(params),
            "post": None,
            "llm_subquery": parsed.llm_subquery,
            "candidate_skills_text": parsed.skills_text or "",
            "role_like": _like(parsed.role),
            "role_skills_limit": int(cfg.get("role_skills_limit", 30)),
        }

    # ---------------- Candidates by skills (candidates_db only) ----------------
    if parsed.task == "filter_candidates_by_skills":
        sql = (
            "SELECT c.cand_id, c.name, c.location_lc, "
            "       cs.skill_id "
            "FROM candidates c "
            "JOIN candidate_skills cs ON c.cand_id = cs.cand_id "
        )
        params: List[Any] = []
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            sql += "WHERE (LOWER(c.country) = %s OR c.location_lc ~* %s) "
            params.extend([country_lc, pattern])
        sql += f"ORDER BY c.cand_id LIMIT {limit}"

        return {
            "task": parsed.task,
            "source": "candidates_db",
            "sql": sql,
            "params": tuple(params),
            "post": None,
            "llm_subquery": parsed.llm_subquery,
            "requested_skills_text": parsed.skills_text or "",
        }

    # ---------------- Candidates by location only ----------------
    if parsed.task == "filter_candidates_by_location":
        sql = (
            "SELECT c.cand_id, c.name, c.location_lc "
            "FROM candidates c "
            "WHERE (LOWER(c.country) = %s OR c.location_lc ~* %s) "
            "ORDER BY c.name "
            f"LIMIT {limit}"
        )
        country_lc, pattern = _normalize_location_for_query(parsed.location)
        params = (country_lc, pattern)

        return {
            "task": parsed.task,
            "source": "candidates_db",
            "sql": sql,
            "params": params,
            "post": None,
            "llm_subquery": parsed.llm_subquery,
        }

    # ---------------- Candidates by projects ----------------
    if parsed.task == "filter_candidates_by_projects":
        topic = parsed.project_query or ""
        sql = (
            "SELECT c.cand_id, c.name, c.location_lc, "
            "       p.name, p.description, p.technologies "
            "FROM candidates c "
            "JOIN projects p ON p.cand_id = c.cand_id "
            "ORDER BY c.cand_id"
        )
        return {
            "task": parsed.task,
            "source": "candidates_db",
            "sql": sql,
            "params": (),
            "post": None,
            "llm_subquery": parsed.llm_subquery,
            "project_topic": topic,
        }

    # ---------------- Top candidates by skill count ----------------
    if parsed.task == "top_candidates_by_skill_count":
        params: List[Any] = []
        sql = (
            "SELECT c.cand_id, c.name, c.location_lc, "
            "       COUNT(DISTINCT cs.skill_id) AS skills_count "
            "FROM candidates c "
            "LEFT JOIN candidate_skills cs ON c.cand_id = cs.cand_id "
        )
        if parsed.location:
            country_lc, pattern = _normalize_location_for_query(parsed.location)
            sql += "WHERE (LOWER(c.country) = %s OR c.location_lc ~* %s) "
            params.extend([country_lc, pattern])

        sql += (
            "GROUP BY c.cand_id, c.name, c.location_lc "
            "ORDER BY skills_count DESC, c.name "
            "LIMIT %s"
        )
        params.append(int(parsed.topk or 10))
        return {
            "task": parsed.task,
            "source": "candidates_db",
            "sql": sql,
            "params": tuple(params),
            "post": lambda rows: [
                {
                    "candidate_id": r[0],
                    "name": r[1],
                    "location": r[2],
                    "skills_count": r[3],
                }
                for r in rows
            ],
            "llm_subquery": parsed.llm_subquery,
        }
    
    # ---------------- Candidate profile + eligible jobs (federated) ----------------
    if parsed.task == "candidate_profile_and_eligible_jobs":
        return {
            "task": parsed.task,
            "source": ("candidates_db", "jobs_db"),
            "sql": (None, None),  # dynamic queries built in run()
            "params": ((), ()),
            "post": None,
            "llm_subquery": parsed.llm_subquery,
            "candidate_name": parsed.candidate_name,
            "candidate_id": parsed.candidate_id,
            "location": parsed.location,   # used for candidate *and* job location
            "role": parsed.role,           # NEW: used to restrict job titles
            "topk": int(parsed.topk or 30),
        }



