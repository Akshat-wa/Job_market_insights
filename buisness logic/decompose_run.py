# decompose_run.py — executor (run only)

from typing import Dict, Any, List
import regex as _re
from copy import deepcopy

from session_sql import session_id_from_cfg
from session_plan import _inject_jobs_session


def _run_jobs_sql(adapter, cfg, sql: str, params, source: str = "jobs_db"):
    sid = session_id_from_cfg(cfg)
    sql2, params2 = _inject_jobs_session(sql, tuple(params or ()), sid)
    return adapter.run_sql(source, sql2, params2)


#helpers
def safe_lc(x):
    # safely convert to lowercased stripped string
    try:
        return str(x).strip().lower() if x is not None else ""
    except Exception:
        return ""


def _split_skills_text(s: str | None):
    # split a skills text into a set of lowercased tokens
    if not s:
        return set()
    toks = [t.strip().lower() for t in _re.split(r"[,\|;/]", s) if t.strip()]
    return set(toks)


def _like(s: str) -> str:
    # prepare a case-insensitive LIKE pattern
    return f"%{(s or '').lower()}%"


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _word_regex(term: str) -> str:
    t = (term or "").strip().lower()
    return rf"(?i)(^|[^a-z]){_re.escape(t)}([^a-z]|$)"


def _fetch_skill_names_for_ids(adapter, skill_ids):
    """
    given skill_ids from candidates_db, fetch their names
    from jobs_db.skills and return {skill_id -> lowercased skill name}.
    """
    ids = sorted({int(s) for s in skill_ids if s is not None})
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    sql = f"SELECT skill_id, name FROM skills WHERE skill_id IN ({placeholders})"
    rows = adapter.run_sql("jobs_db", sql, tuple(ids))
    return {row[0]: safe_lc(row[1]) for row in rows if row[1]}


# ---------------------- legitimacy + derived skills helpers ----------------------
def _fetch_legitimity_for_cids(adapter, cand_ids, cfg):
    """
    Fetch legitimacy scores for the given candidate IDs from
    candidates_db.candidate_skill_legitimacy.

    Returns:
        {cand_id: {
            "score": float | None,
            "num_skills_total": int | None,
            "has_claimed_skills": bool,
            "has_text_skills": bool,
            "declared_total_evidence": int | None,
        }}
    """
    leg_cfg = (cfg or {}).get("candidate_legitimacy", {})
    if not leg_cfg.get("enabled", True):
        return {}

    ids = sorted({int(cid) for cid in cand_ids if cid is not None})
    if not ids:
        return {}

    placeholders = ", ".join(["%s"] * len(ids))
    sql = (
        "SELECT cand_id, legitimacy_score, num_skills_total, "
        "       has_claimed_skills, has_text_skills, declared_total_evidence "
        "FROM candidate_skill_legitimacy "
        f"WHERE cand_id IN ({placeholders})"
    )

    try:
        rows = adapter.run_sql("candidates_db", sql, tuple(ids))
    except Exception as e:
        # Fallback: don't break the pipeline just because this table is missing
        print(f"[legitimacy] Warning while fetching scores: {e}")
        return {}

    mp = {}
    for cand_id, score, n_skills, has_claimed, has_text, evidence in rows:
        mp[cand_id] = {
            "score": float(score) if score is not None else None,
            "num_skills_total": n_skills,
            "has_claimed_skills": bool(has_claimed),
            "has_text_skills": bool(has_text),
            "declared_total_evidence": evidence,
        }
    return mp


def _should_drop_by_legitimacy(score, cfg) -> bool:
    """
    Decide whether to hard-filter a candidate based on legitimacy_score.
    If no filter_min_score is configured or score is None, never drop.
    """
    leg_cfg = (cfg or {}).get("candidate_legitimacy", {})
    thr = leg_cfg.get("filter_min_score")
    if thr is None or score is None:
        return False
    try:
        return float(score) < float(thr)
    except Exception:
        return False


# -------- derived skills + legitimacy helpers ----------


def _fetch_derived_skills(adapter, cand_ids):
    """
    Load auto-extracted / transformer-derived skills for a set of candidate IDs.
    Returns: {cand_id: {skill_name_lc, ...}, ...}
    """
    ids = sorted({int(cid) for cid in cand_ids if cid is not None})
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    sql = (
        f"SELECT cand_id, skill_name "
        f"FROM candidate_derived_skills "
        f"WHERE cand_id IN ({placeholders})"
    )
    rows = adapter.run_sql("candidates_db", sql, tuple(ids))
    out = {}
    for cid, sname in rows:
        s = safe_lc(sname)
        if not s:
            continue
        out.setdefault(cid, set()).add(s)
    return out


def _fetch_legitimacy(adapter, cand_ids):
    """
    Load candidate_skill_legitimacy rows for a set of candidate IDs.
    Returns: {cand_id: {...columns...}}
    """
    ids = sorted({int(cid) for cid in cand_ids if cid is not None})
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    sql = (
        "SELECT cand_id, legitimacy_score, num_skills_total, "
        "       has_claimed_skills, has_text_skills, "
        "       declared_total_evidence, reason "
        "FROM candidate_skill_legitimacy "
        f"WHERE cand_id IN ({placeholders})"
    )
    rows = adapter.run_sql("candidates_db", sql, tuple(ids))
    out = {}
    for (
        cid,
        leg_score,
        num_skills_total,
        has_claimed_skills,
        has_text_skills,
        declared_total_evidence,
        reason,
    ) in rows:
        out[cid] = {
            "legitimacy_score": float(leg_score) if leg_score is not None else None,
            "num_skills_total": num_skills_total,
            "has_claimed_skills": bool(has_claimed_skills),
            "has_text_skills": bool(has_text_skills),
            "declared_total_evidence": declared_total_evidence,
            "reason": reason,
        }
    return out


def _legitimacy_bucket(score):
    """
    Simple bucketing so the LLM / UI can talk about 'high / medium / low'
    confidence instead of just a raw float.
    """
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s >= 0.8:
        return "high"
    if s >= 0.6:
        return "medium"
    if s >= 0.4:
        return "low"
    return "very_low"


def _run_candidate_multi(adapter, plan, cfg):
    """
    Federated candidate filtering: run a set of atomic candidate plans
    (each returning structured_result.candidates) and intersect on candidate_id.
    """
    from collections import defaultdict  # noqa: F401 (kept for future extensions)

    subplans = plan.get("subplans") or []
    if not subplans:
        return {"structured_result": {"candidates": []}}

    # Execute each sub-plan via the normal dispatcher so we get
    # fully shaped objects (including legitimacy, overlaps, etc.)
    sub_results: List[List[Dict[str, Any]]] = []
    for sp in subplans:
        res = run(adapter, deepcopy(sp), cfg)
        cand_list = (res or {}).get("structured_result", {}).get("candidates", [])
        sub_results.append(cand_list)

    # Per-plan candidate_id sets
    per_plan_ids: List[set] = []
    for cand_list in sub_results:
        ids = {c.get("candidate_id") for c in cand_list if c.get("candidate_id") is not None}
        per_plan_ids.append(ids)

    if not per_plan_ids:
        return {"structured_result": {"candidates": []}}

    # Intersection: candidate must satisfy *all* atomic filters
    common_ids = set.intersection(*per_plan_ids) if len(per_plan_ids) > 1 else per_plan_ids[0]
    if not common_ids:
        return {"structured_result": {"candidates": []}}

    # Merge candidate objects from all sub-plans.
    # Shallow union of keys, preferring non-empty values.
    merged: Dict[int, Dict[str, Any]] = {}
    for cand_list in sub_results:
        for c in cand_list:
            cid = c.get("candidate_id")
            if cid not in common_ids:
                continue
            slot = merged.setdefault(cid, {})
            for k, v in c.items():
                if k == "candidate_id":
                    continue
                if slot.get(k) in (None, "", [], {}):
                    slot[k] = v
    if plan.get("topk"):
        merged = dict(list(merged.items())[: plan["topk"]])
    # Turn into a list and sort by a sensible default:
    #   1) higher legitimacy_score
    #   2) then higher skills_count
    #   3) then higher years if present
    #   4) then name for stability
    out = list(merged.values())

    def _key(o):
        leg = o.get("legitimacy_score")
        skills = o.get("skills_count") or 0
        years = o.get("years") or 0.0
        name = o.get("name") or ""
        return (leg if leg is not None else 0.0, skills, years, name)

    out.sort(key=_key, reverse=True)

    return {"structured_result": {"candidates": out}}


def _run_jobs_multi(adapter, plan, cfg):
    """
    Federated jobs filtering: run a set of atomic jobs plans
    (each returning a list of job dicts) and intersect on job_id.

    Supports sub-handlers that either:
      - return {"structured_result": {"jobs": [...]}}
      - or return {"structured_result": [...]} (a bare list of job dicts).
    """
    from collections import defaultdict

    subplans = plan.get("subplans") or []
    if not subplans:
        return {"structured_result": {"jobs": []}}

    sub_results: List[List[Dict[str, Any]]] = []

    for sp in subplans:
        res = run(adapter, sp, cfg) or {}
        # Prefer structured_result, but fall back gracefully
        sr = res.get("structured_result", res)
        if isinstance(sr, list):
            job_list = sr
        elif isinstance(sr, dict):
            job_list = sr.get("jobs", [])
        else:
            job_list = []
        sub_results.append(job_list)

    if not sub_results:
        return {"structured_result": {"jobs": []}}

    per_plan_ids: List[set] = []
    job_meta: Dict[Any, Dict[str, Any]] = {}

    # Collect ids + a representative object per job
    for job_list in sub_results:
        ids = set()
        for obj in job_list:
            jid = obj.get("job_id")
            if jid is None:
                continue
            ids.add(jid)
            if jid not in job_meta:
                job_meta[jid] = dict(obj)
            else:
                # shallow merge: fill missing / empty fields
                for k, v in obj.items():
                    if job_meta[jid].get(k) in (None, "", [], {}):
                        job_meta[jid][k] = v
        per_plan_ids.append(ids)

    if not per_plan_ids:
        return {"structured_result": {"jobs": []}}

    # Intersection: job must satisfy *all* filters
    common_ids = set.intersection(*per_plan_ids) if len(per_plan_ids) > 1 else per_plan_ids[0]
    jobs = [job_meta[jid] for jid in common_ids]

    # Simple deterministic sort: by title, then company, then id
    jobs.sort(key=lambda j: (j.get("title") or "", j.get("company") or "", j.get("job_id")))

    return {"structured_result": {"jobs": jobs}}

def _run_eligible_companies_for_candidate(adapter, plan, cfg):
    """
    Rank companies / job postings by how well their text overlaps with:
      - the candidate's skills text (if present), AND
      - the skill set of the dominant role cluster (if role_like is present).

    We build:
      base_set = candidate_skills_text ∪ role_cluster_skills

    If clusters are unavailable or fail, we fall back to the simple LIKE-based
    role-skill aggregation as before.
    """
    rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])

    # 1) Candidate skill tokens from NL / planner
    cskills = _split_skills_text(plan.get("candidate_skills_text", ""))  # already lowercased tokens

    # 2) Role skill set from jobs_db via role clusters (if possible)
    role_set = set()
    role_like = plan.get("role_like")
    if role_like:
        limit = int(plan.get("role_skills_limit", 30) or 30)

        cluster_cfg = (cfg or {}).get("cluster_expansion", {})
        use_role_clusters = bool(
            cluster_cfg.get("use_role_clusters_for_eligible_companies", True)
        )

        role_rows = None
        if use_role_clusters:
            # Cluster-based role skill set using job_role_clusters
            cluster_sql = (
                "WITH target_cluster AS ("
                "  SELECT jr.role_cluster_id "
                "  FROM jobs j "
                "  JOIN job_role_clusters jr ON j.job_id = jr.job_id "
                "  WHERE j.title_lc LIKE %s "
                "  GROUP BY jr.role_cluster_id "
                "  ORDER BY COUNT(*) DESC "
                "  LIMIT 1"
                ") "
                "SELECT s.name_lc, COUNT(*) AS freq "
                "FROM jobs j "
                "JOIN job_role_clusters jr ON j.job_id = jr.job_id "
                "JOIN job_skills js ON j.job_id = js.job_id "
                "JOIN skills s ON s.skill_id = js.skill_id "
                "JOIN target_cluster tc ON jr.role_cluster_id = tc.role_cluster_id "
                "GROUP BY s.name_lc "
                "ORDER BY freq DESC "
                "LIMIT %s"
            )
            try:
                role_rows = _run_jobs_sql(adapter, cfg, cluster_sql, (role_like, limit))
            except Exception as e:
                # Fail soft: log and fall back to non-cluster query
                print(
                    f"[eligible_companies] cluster-based role skills failed, falling back: {e}"
                )
                role_rows = None

        if not role_rows:
            # Fallback: original LIKE-based role skills
            fallback_sql = (
                "SELECT s.name_lc, COUNT(*) AS freq "
                "FROM jobs j JOIN job_skills js ON j.job_id = js.job_id "
                "JOIN skills s ON s.skill_id = js.skill_id "
                "WHERE j.title_lc LIKE %s "
                "GROUP BY s.name_lc ORDER BY freq DESC LIMIT %s"
            )
            role_rows = _run_jobs_sql(adapter, cfg, fallback_sql, (role_like, limit))

        role_set = {safe_lc(r[0]) for r in (role_rows or []) if r and r[0]}

    # 3) Use BOTH candidate skills and role-cluster skills
    #    - if candidate skills exist, we keep them
    #    - if role cluster exists, we add its skills
    base_set = set(cskills) | role_set

    scored = []
    for company, title, location, job_id in rows:
        text = " ".join(
            [str(company or ""), str(title or ""), str(location or "")]
        ).lower()
        toks = set(_re.findall(r"[a-zA-Z0-9+#\.]+", text))
        hits = len(base_set & toks) if base_set else 0
        scored.append(
            {
                "company": company,
                "title": title,
                "location": location,
                "job_id": job_id,
                "overlap_hits": hits,
            }
        )

    scored.sort(
        key=lambda x: (x["overlap_hits"], (x["company"] or "")), reverse=True
    )
    return {"structured_result": {"eligible_companies": scored[:100]}}

def _run_filter_candidates_by_experience_role(adapter, plan, cfg):
    """
    Filter candidates by:
      - minimum years of experience
      - overlap with a role's skill cluster (optional)

    Explicit skills (candidate_skills -> jobs_db.skills) are primary.
    Derived skills are secondary and shown separately.
    """
    from collections import defaultdict

    if isinstance(plan["sql"], tuple):
        stu_sql, role_sql = plan["sql"]
        stu_params, role_params = plan["params"]
        rows = adapter.run_sql("candidates_db", stu_sql, stu_params)
        role_rows = adapter.run_sql("jobs_db", role_sql, role_params)
        role_set = {safe_lc(r[0]) for r in role_rows if r and r[0]}
        filter_role = True
    else:
        rows = adapter.run_sql("candidates_db", plan["sql"], plan["params"])
        role_set = set()
        filter_role = False

    cand_skill_ids = defaultdict(set)
    cand_years = {}
    cand_loc = {}
    cand_name = {}

    # Expect: cand_id, years, skill_id, location, name
    for row in rows:
        cid = row[0]
        yrs = float(row[1] or 0.0)
        sid = row[2]
        loc = row[3] if len(row) > 3 else None
        name = row[4] if len(row) > 4 else None

        cand_years[cid] = yrs
        if sid is not None:
            cand_skill_ids[cid].add(sid)
        if loc and cid not in cand_loc:
            cand_loc[cid] = loc
        if name and cid not in cand_name:
            cand_name[cid] = name

    # Map skill_ids -> names -> lc
    all_ids = set()
    for sids in cand_skill_ids.values():
        all_ids.update(sids)
    id2name = _fetch_skill_names_for_ids(adapter, all_ids)

    cand_sk = {}
    for cid, sids in cand_skill_ids.items():
        names = {id2name[sid] for sid in sids if sid in id2name}
        cand_sk[cid] = {safe_lc(n) for n in names if n}

    # Derived skills + legitimacy
    derived_sk = _fetch_derived_skills(adapter, cand_years.keys())
    legit_map = _fetch_legitimacy(adapter, cand_years.keys())

    min_y = float(plan.get("min_years") or 0.0)
    out = []
    for cid, yrs in cand_years.items():
        if yrs + 1e-9 < min_y:
            continue

        core = cand_sk.get(cid, set())
        derived = derived_sk.get(cid, set())
        all_sk = core | derived

        core_inter = sorted(core & role_set) if role_set else []
        derived_inter = sorted((derived & role_set) - set(core_inter)) if role_set else []

        # FIRST: we require overlap in explicit skills to pass a role filter.
        if filter_role and not core_inter:
            continue

        leg = legit_map.get(cid, {}) or {}
        score = leg.get("legitimacy_score")

        out.append(
            {
                "candidate_id": cid,
                "name": cand_name.get(cid) or f"Candidate {cid}",
                "years": round(yrs, 2),
                "location": cand_loc.get(cid),
                "skills_count": len(all_sk),
                "role_overlap": core_inter + derived_inter,
                "role_overlap_core": core_inter,
                "role_overlap_derived": derived_inter,
                "legitimacy_score": score,
                "legitimacy_bucket": _legitimacy_bucket(score),
                "legitimacy_reason": leg.get("reason"),
            }
        )

    out.sort(
        key=lambda x: (
            x["years"],
            len(x.get("role_overlap_core", [])),
            len(x.get("role_overlap_derived", [])),
            x["skills_count"],
            x.get("legitimacy_score") or 0.0,
        ),
        reverse=True,
    )

    return {"structured_result": {"candidates": out[:200]}}


def _run_candidate_profile_and_eligible_jobs(adapter, plan, cfg):
    """
    For a given candidate (by id or name/location), return:
      - candidate profile (explicit + derived skills + legitimacy)
      - list of eligible jobs ranked by overlap.

    Matching logic:
      - PRIMARY: skills from candidate_skills (mapped to jobs_db.skills).
      - SECONDARY: derived skills that map by name into jobs_db.skills.
    """
    from collections import defaultdict

    cand_source, jobs_source = plan["source"]

    # 1) Resolve candidate row from candidates_db
    where_clauses = ["1=1"]
    params: List[Any] = []

    # If we have an explicit candidate_id, use ONLY that to resolve the candidate.
    if plan.get("candidate_id") is not None:
        where_clauses = ["cand_id = %s"]
        params.append(plan["candidate_id"])
    else:
        # Fallback: use name and/or location
        where_clauses = ["1=1"]
        if plan.get("candidate_name"):
            where_clauses.append("LOWER(name) LIKE %s")
            params.append(f"%{plan['candidate_name'].lower()}%")
        if plan.get("location"):
            where_clauses.append("location_lc LIKE %s")
            params.append(f"%{plan['location'].lower()}%")


    cs_sql = (
        "SELECT cand_id, name, location, summary "
        "FROM candidates "
        f"WHERE {' AND '.join(where_clauses)} "
        "ORDER BY cand_id "
        "LIMIT 1"
    )
    rows = adapter.run_sql(cand_source, cs_sql, tuple(params))
    if not rows:
        return {"structured_result": {"candidate": None, "eligible_jobs": []}}

    cand_id, cand_name, cand_loc, cand_summary = rows[0]

    # 2) Explicit skills for candidate (skill_ids)
    rows = adapter.run_sql(
        cand_source,
        "SELECT DISTINCT skill_id FROM candidate_skills WHERE cand_id = %s",
        (cand_id,),
    )
    core_skill_ids = sorted({r[0] for r in rows if r and r[0] is not None})

    id2name_core = _fetch_skill_names_for_ids(adapter, core_skill_ids)
    core_skill_names = {safe_lc(n) for n in id2name_core.values() if n}

    # 3) Derived skills for candidate
    derived_map = _fetch_derived_skills(adapter, [cand_id])
    derived_skill_names = derived_map.get(cand_id, set())

    # Map derived names into jobs_db.skills (only ones that exist there)
    derived_skill_ids = set()
    if derived_skill_names:
        placeholders = ", ".join(["%s"] * len(derived_skill_names))
        sql = (
            "SELECT skill_id, name_lc "
            "FROM skills "
            f"WHERE name_lc IN ({placeholders})"
        )
        rows = _run_jobs_sql(adapter, cfg, sql, tuple(derived_skill_names), source=jobs_source)
        for sid, name_lc in rows:
            if sid is not None:
                derived_skill_ids.add(sid)

    # Candidate's full skill-id set and names
    all_skill_ids = sorted(set(core_skill_ids) | derived_skill_ids)
    id2name_all = _fetch_skill_names_for_ids(adapter, all_skill_ids)
    all_skill_names = {safe_lc(n) for n in id2name_all.values() if n}

    # 4) Legitimacy for candidate
    legit_map = _fetch_legitimacy(adapter, [cand_id])
    leg = legit_map.get(cand_id, {}) or {}
    leg_score = leg.get("legitimacy_score")

    candidate_obj = {
        "candidate_id": cand_id,
        "name": cand_name,
        "location": cand_loc,
        "summary": cand_summary,
        "skills_core": sorted(core_skill_names),
        "skills_derived": sorted(derived_skill_names),
        "skills": sorted(all_skill_names),
        "legitimacy_score": leg_score,
        "legitimacy_bucket": _legitimacy_bucket(leg_score),
        "legitimacy_reason": leg.get("reason"),
    }

    # If candidate has no mapped skills at all, no sensible matching
    if not all_skill_ids:
        return {"structured_result": {"candidate": candidate_obj, "eligible_jobs": []}}

    # 5) Jobs that share these skills, plus optional role/location filters
    placeholders = ", ".join(["%s"] * len(all_skill_ids))
    jobs_sql = (
        "SELECT j.job_id, j.title, j.company, j.location, js.skill_id "
        "FROM jobs j "
        "JOIN job_skills js ON j.job_id = js.job_id "
        f"WHERE js.skill_id IN ({placeholders})"
    )
    job_params: List[Any] = list(all_skill_ids)

    # Optional: restrict jobs to a particular role/title pattern
    job_role = plan.get("role")
    if job_role:
        jobs_sql += " AND j.title_lc LIKE %s"
        job_params.append(_like(job_role))

    # Optional: restrict jobs to a particular country / city
    job_loc = plan.get("location")
    if job_loc:
        jobs_sql += " AND (j.country_lc = %s OR j.location_lc ~* %s)"
        job_params.extend(
            [
                (job_loc or "").lower(),
                _word_regex(job_loc),
            ]
        )

    rows = _run_jobs_sql(adapter, cfg, jobs_sql, tuple(job_params), source=jobs_source)

    jobs_meta = {}
    job_skill_ids = defaultdict(set)

    for job_id, title, company, loc, sid in rows:
        if job_id not in jobs_meta:
            jobs_meta[job_id] = {
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": loc,
            }
        if sid is not None:
            job_skill_ids[job_id].add(sid)

    core_id_set = set(core_skill_ids)
    derived_id_set = set(all_skill_ids) - core_id_set

    cand_skill_all_set = set(all_skill_names)  # strings

    eligible_jobs = []
    for job_id, meta in jobs_meta.items():
        sids = job_skill_ids[job_id]

        core_match_ids = sids & core_id_set
        derived_match_ids = sids & derived_id_set

        core_names = sorted(
            {id2name_all[sid] for sid in core_match_ids if sid in id2name_all}
        )
        derived_names = sorted(
            {id2name_all[sid] for sid in derived_match_ids if sid in id2name_all}
        )
        all_names = sorted(set(core_names) | set(derived_names))

        overlap_count = len(all_names)
        jac = overlap_count / max(1, len(cand_skill_all_set))

        eligible_jobs.append(
            {
                "job_id": job_id,
                "title": meta["title"],
                "company": meta["company"],
                "location": meta["location"],
                "matched_skills": all_names,
                "matched_skills_core": core_names,
                "matched_skills_derived": derived_names,
                "overlap_count": overlap_count,
                "jaccard": round(jac, 3),
            }
        )

    topk = int(plan.get("topk") or 25)
    eligible_jobs.sort(
        key=lambda x: (x["overlap_count"], x["jaccard"]), reverse=True
    )
    eligible_jobs = eligible_jobs[:topk]

    return {"structured_result": {"candidate": candidate_obj, "eligible_jobs": eligible_jobs}}

def _run_candidate_readiness(adapter, plan, cfg):
    """
    Given a role (defined by its skill cluster), score candidates by
    (a) how many of those skills they have, and
    (b) skill-set overlap.

    We treat:
      - candidate_skills (mapped skill_ids -> jobs_db.skills) as PRIMARY evidence
      - candidate_derived_skills as SECONDARY / bonus evidence

    Thresholds (min_hits, coverage, jaccard) are computed using ONLY explicit skills.
    Derived skills can optionally help a high-legitimacy candidate pass thresholds
    (controlled by candidate_readiness_allow_derived_rescue in cfg).
    """
    from collections import defaultdict

    jobs_source, cand_source = plan["source"]

    # 1) Role skills from jobs_db (optionally via role_clusters)
    role_rows = None
    params0 = plan.get("params", ((), ()))
    params0 = params0[0] if isinstance(params0, tuple) else params0
    role_like_param = params0[0] if params0 else None

    cluster_cfg = (cfg or {}).get("cluster_expansion", {})
    use_role_clusters = bool(cluster_cfg.get("use_role_clusters_for_readiness", True)) and bool(role_like_param)

    if use_role_clusters:
        limit = int(cfg.get("role_skill_limit", 30))
        cluster_sql = """
        WITH target_cluster AS (
            SELECT jr.role_cluster_id
            FROM jobs j
            JOIN job_role_clusters jr ON j.job_id = jr.job_id
            WHERE j.title_lc LIKE %s
            GROUP BY jr.role_cluster_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
        )
        SELECT s.name, COUNT(*) AS freq
        FROM jobs j
        JOIN job_role_clusters jr ON j.job_id = jr.job_id
        JOIN job_skills js ON j.job_id = js.job_id
        JOIN skills s ON s.skill_id = js.skill_id
        JOIN target_cluster tc ON jr.role_cluster_id = tc.role_cluster_id
        GROUP BY s.name
        ORDER BY freq DESC
        LIMIT %s
        """
        try:
            role_rows = _run_jobs_sql(adapter, cfg, cluster_sql, (role_like_param, limit), source=jobs_source)
        except Exception as e:
            print(f"[candidate_readiness] cluster-based role skills failed, falling back: {e}")
            role_rows = None

    if not role_rows:
        # Fallback: original LIKE-based role skills
        role_rows = adapter.run_sql(jobs_source, plan["sql"][0], plan["params"][0])

    role_set = {safe_lc(r[0]) for r in role_rows if r and r[0]}

    # 2) Candidate skill_ids per candidate
    rows = adapter.run_sql(cand_source, plan["sql"][1], plan["params"][1])

    cand_skill_ids = defaultdict(set)
    loc_map = {}
    name_map = {}

    # rows: cand_id, years, skill_id, location, name
    for row in rows:
        cid = row[0]
        skill_id = row[2] if len(row) > 2 else None
        loc = row[3] if len(row) > 3 else None
        name = row[4] if len(row) > 4 else None

        if skill_id is not None:
            cand_skill_ids[cid].add(skill_id)
        if loc and cid not in loc_map:
            loc_map[cid] = loc
        if name and cid not in name_map:
            name_map[cid] = name

    # 3) Map skill_ids -> names
    all_ids = set()
    for sids in cand_skill_ids.values():
        all_ids.update(sids)
    id2name = _fetch_skill_names_for_ids(adapter, all_ids)

    core_sk = {}
    for cid, sids in cand_skill_ids.items():
        names = {id2name[sid] for sid in sids if sid in id2name}
        core_sk[cid] = {safe_lc(n) for n in names if n}

    # 4) Derived skills + legitimacy per candidate
    derived_sk = _fetch_derived_skills(adapter, core_sk.keys())
    legit_map = _fetch_legitimacy(adapter, core_sk.keys())

    jaccard_thr = float(cfg.get("candidate_readiness_threshold", 0.25))
    min_hits = int(cfg.get("readiness_min_hits", 2))
    cov_thr = float(cfg.get("coverage_threshold", 0.10))
    topk = int(plan.get("topk") or 100)

    leg_cfg = (cfg or {}).get("candidate_legitimacy", {})
    rescue_min_score = leg_cfg.get("rescue_min_score")
    if rescue_min_score is None:
        rescue_min_score = leg_cfg.get("filter_min_score", 0.6)

    allow_derived_rescue = bool(cfg.get("candidate_readiness_allow_derived_rescue", True))

    matches = []
    for cid, core_set in core_sk.items():
        derived_set = derived_sk.get(cid, set())
        all_sk = core_set | derived_set

        core_inter = sorted(core_set & role_set)
        derived_inter = sorted((derived_set & role_set) - set(core_inter))
        hits = len(core_inter)
        cov = hits / max(1, len(role_set)) if role_set else 0.0
        jac = _jaccard(core_set, role_set)

        leg = legit_map.get(cid, {}) or {}
        score = leg.get("legitimacy_score")

        # 1) global legitimacy-based drop (fake / low-evidence profiles)
        if _should_drop_by_legitimacy(score, cfg):
            continue

        # 2) normal explicit-skill thresholds
        passed_explicit = (hits >= min_hits or cov >= cov_thr or jac >= jaccard_thr)
        passed_derived = False

        # 3) optional "derived rescue" path:
        #    if explicit skills just miss, but derived+legitimacy are strong, still keep.
        if not passed_explicit and allow_derived_rescue and role_set:
            hits_all = len(all_sk & role_set)
            cov_all = hits_all / max(1, len(role_set))
            jac_all = _jaccard(all_sk, role_set)
            try:
                s_val = float(score) if score is not None else None
            except (TypeError, ValueError):
                s_val = None
            if (
                s_val is not None
                and s_val >= float(rescue_min_score)
                and (hits_all >= min_hits or cov_all >= cov_thr or jac_all >= jaccard_thr)
            ):
                passed_derived = True

        if not (passed_explicit or passed_derived):
            continue

        matches.append(
            {
                "candidate_id": cid,
                "name": name_map.get(cid) or f"Candidate {cid}",
                "location": loc_map.get(cid),
                # matches
                "overlap": core_inter + derived_inter,
                "overlap_core": core_inter,
                "overlap_derived": derived_inter,
                "coverage": round(cov, 3),
                "jaccard": round(jac, 3),
                # skills
                "skills": sorted(all_sk),
                "skills_core": sorted(core_set),
                "skills_derived": sorted(derived_set),
                # legitimacy
                "legitimacy_score": score,
                "legitimacy_bucket": _legitimacy_bucket(score),
                "legitimacy_reason": leg.get("reason"),
            }
        )

    matches_sorted = sorted(
        matches,
        key=lambda x: (
            len(x.get("overlap_core", [])),
            len(x.get("overlap_derived", [])),
            x["coverage"],
            x["jaccard"],
            x.get("legitimacy_score") or 0.0,
        ),
        reverse=True,
    )[:topk]

    return {
        "structured_result": {
            "role_skills": sorted(role_set),
            "candidates": matches_sorted,
        }
    }

def _run_filter_candidates_by_skills(adapter, plan, cfg):
    """
    Filter candidates by a set of requested skills.

    Priority:
      1) Look for matches in explicit / mentioned skills (candidate_skills).
      2) Also look in candidate_derived_skills as a fallback / bonus.
    """
    from collections import defaultdict

    rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])
    requested = _split_skills_text(plan.get("requested_skills_text", ""))
    requested_lc = {safe_lc(s) for s in requested}

    cand_skill_ids = defaultdict(set)
    cand_loc = {}
    cand_name = {}

    # Expect: cand_id, name, location_lc, skill_id
    for cid, name, loc, skill_id in rows:
        cand_name[cid] = name
        if loc and cid not in cand_loc:
            cand_loc[cid] = loc
        if skill_id is not None:
            cand_skill_ids[cid].add(skill_id)

    if not requested_lc:
        # Fallback: no skill filter given, reuse generic "top candidates by skill_count".
        return _run_top_candidates_by_skill_count(adapter, plan, cfg)

    # Map skill_ids -> names
    all_ids = set()
    for sids in cand_skill_ids.values():
        all_ids.update(sids)
    id2name = _fetch_skill_names_for_ids(adapter, all_ids)

    # Core explicit skills
    cand_core_sk = {}
    for cid, sids in cand_skill_ids.items():
        names = {id2name[sid] for sid in sids if sid in id2name}
        cand_core_sk[cid] = {safe_lc(n) for n in names if n}

    # Derived skills + legitimacy
    derived_sk = _fetch_derived_skills(adapter, cand_core_sk.keys())
    legit_map = _fetch_legitimacy(adapter, cand_core_sk.keys())

    out = []
    for cid, core_set in cand_core_sk.items():
        derived_set = derived_sk.get(cid, set())
        all_sk = core_set | derived_set

        # FIRST: matches in explicit skills
        direct_hits = sorted(core_set & requested_lc) if requested_lc else []
        # THEN: matches only from derived skills, excluding ones already seen
        derived_hits = sorted((derived_set & requested_lc) - set(direct_hits)) if requested_lc else []

        # If user asked for skills, candidate must match via either direct or derived.
        if not direct_hits and not derived_hits:
            continue
        leg = legit_map.get(cid, {}) or {}
        score = leg.get("legitimacy_score")

        # Global legitimacy-based drop if configured (uses candidate_legitimacy.filter_min_score)
        if _should_drop_by_legitimacy(score, cfg):
            continue

        out.append(
            {
                "candidate_id": cid,
                "name": cand_name.get(cid),
                "location": cand_loc.get(cid),
                "skills_count": len(all_sk),
                "matched_skills_direct": direct_hits,
                "matched_skills_derived": derived_hits,
                "matched_skills": direct_hits + derived_hits,
                "legitimacy_score": score,
                "legitimacy_bucket": _legitimacy_bucket(score),
                "legitimacy_reason": leg.get("reason"),
            }
        )

    out.sort(
        key=lambda x: (
            len(x.get("matched_skills_direct", [])),
            len(x.get("matched_skills_derived", [])),
            x["skills_count"],
            x.get("legitimacy_score") or 0.0,
            x.get("name") or "",
        ),
        reverse=True,
    )

    return {"structured_result": {"candidates": out[:200]}}


def _run_filter_candidates_by_location(adapter, plan, cfg):
    rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])
    out = [
        {
            "candidate_id": r[0],
            "name": r[1],
            "location": r[2],
        }
        for r in rows
    ]

    # Attach legitimacy, but don't change ordering here
    leg_map = _fetch_legitimity_for_cids(
        adapter, [r["candidate_id"] for r in out], cfg
    )
    for obj in out:
        leg = leg_map.get(obj["candidate_id"])
        if leg:
            score = leg["score"]
            obj["legitimacy_score"] = score
            obj["legitimacy_bucket"] = _legitimacy_bucket(score)
            obj["legitimacy_meta"] = {
                "num_skills_total": leg["num_skills_total"],
                "has_claimed_skills": leg["has_claimed_skills"],
                "has_text_skills": leg["has_text_skills"],
                "declared_total_evidence": leg["declared_total_evidence"],
            }

    return {"structured_result": {"candidates": out}}


def _run_filter_candidates_by_projects(adapter, plan, cfg):
    from collections import defaultdict

    rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])

    cand_projects = defaultdict(list)
    cand_loc = {}
    cand_name = {}

    for cid, name, loc, title, desc, techs in rows:
        cand_name[cid] = name
        if loc and cid not in cand_loc:
            cand_loc[cid] = loc
        cand_projects[cid].append(
            {
                "title": title,
                "description": desc,
                "technologies": techs,
            }
        )

    topic = plan.get("project_topic", "") or ""
    topic_terms = set(_re.findall(r"[a-zA-Z0-9+#\.]+", topic.lower()))

    # Legitimacy map
    leg_map = _fetch_legitimity_for_cids(adapter, cand_projects.keys(), cfg)

    out = []
    for cid, plist in cand_projects.items():
        text = " ".join(
            (p["title"] or "")
            + " "
            + (p["description"] or "")
            + " "
            + (p.get("technologies") or "")
            for p in plist
        ).lower()
        toks = set(_re.findall(r"[a-zA-Z0-9+#\.]+", text))
        hits = sorted(toks & topic_terms) if topic_terms else []
        score = len(hits)

        leg = leg_map.get(cid)
        leg_score = leg["score"] if leg else None
        if _should_drop_by_legitimacy(leg_score, cfg):
            continue

        obj = {
            "candidate_id": cid,
            "name": cand_name.get(cid),
            "location": cand_loc.get(cid),
            "projects": plist,
            "topic_hits": hits,
            "match_score": score,
        }
        if leg:
            obj["legitimacy_score"] = leg_score
            obj["legitimacy_bucket"] = _legitimacy_bucket(leg_score)
            obj["legitimacy_meta"] = {
                "num_skills_total": leg["num_skills_total"],
                "has_claimed_skills": leg["has_claimed_skills"],
                "has_text_skills": leg["has_text_skills"],
                "declared_total_evidence": leg["declared_total_evidence"],
            }
        out.append(obj)

    out.sort(
        key=lambda x: (
            x.get("legitimacy_score") if x.get("legitimacy_score") is not None else 0.0,
            x["match_score"],
            len(x["projects"]),
        ),
        reverse=True,
    )
    return {"structured_result": {"candidates": out}}


def _run_top_candidates_by_skill_count(adapter, plan, cfg):
    rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])
    out = [
        {
            "candidate_id": r[0],
            "name": r[1],
            "location": r[2],
            "skills_count": r[3],
        }
        for r in rows
    ]

    leg_map = _fetch_legitimity_for_cids(
        adapter, [o["candidate_id"] for o in out], cfg
    )

    kept = []
    for obj in out:
        leg = leg_map.get(obj["candidate_id"])
        score = leg["score"] if leg else None
        if _should_drop_by_legitimacy(score, cfg):
            continue
        if leg:
            obj["legitimacy_score"] = score
            obj["legitimacy_bucket"] = _legitimacy_bucket(score)
            obj["legitimacy_meta"] = {
                "num_skills_total": leg["num_skills_total"],
                "has_claimed_skills": leg["has_claimed_skills"],
                "has_text_skills": leg["has_text_skills"],
                "declared_total_evidence": leg["declared_total_evidence"],
            }
        kept.append(obj)

    kept.sort(
        key=lambda x: (
            x.get("legitimacy_score") if x.get("legitimacy_score") is not None else 0.0,
            x["skills_count"],
            x["name"] or "",
        ),
        reverse=True,
    )
    return {"structured_result": {"candidates": kept}}


# ---------------------- main executor (dispatcher) ----------------------
def run(adapter, plan: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    from session_plan import scope_plan

    plan = scope_plan(plan, session_id_from_cfg(cfg))
    task = plan.get("task")

    handlers = {
        "eligible_companies_for_candidate": _run_eligible_companies_for_candidate,
        "filter_candidates_by_experience_role": _run_filter_candidates_by_experience_role,
        "candidate_profile_and_eligible_jobs": _run_candidate_profile_and_eligible_jobs,
        "candidate_readiness": _run_candidate_readiness,
        "filter_candidates_by_skills": _run_filter_candidates_by_skills,
        "filter_candidates_by_location": _run_filter_candidates_by_location,
        "filter_candidates_by_projects": _run_filter_candidates_by_projects,
        "top_candidates_by_skill_count": _run_top_candidates_by_skill_count,
        "candidate_multi": _run_candidate_multi,
        "jobs_multi": _run_jobs_multi,   
    }

    handler = handlers.get(task)
    if handler is not None:
        return handler(adapter, plan, cfg)

    # ---------- Generic single-source ----------
    if not isinstance(plan.get("sql"), tuple):
        rows = adapter.run_sql(plan["source"], plan["sql"], plan["params"])
        post = plan.get("post")
        return {"structured_result": post(rows) if post else rows}

    raise RuntimeError("Unexpected plan shape in run()")
