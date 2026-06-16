"""Inject session_id filters into jobs_db SQL plans automatically."""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, Tuple


def _inject_jobs_session(sql: str, params: tuple, session_id: str) -> Tuple[str, tuple]:
    if not sql or not session_id or "session_id" in sql.lower():
        return sql, params

    # skills table is global; only scope queries that touch jobs / job_skills
    if not re.search(r"\b(?:jobs|job_skills)\b", sql, flags=re.I):
        return sql, params

    params = list(params or ())

    alias_match = re.search(r"\bFROM\s+jobs\s+(\w+)", sql, flags=re.I)
    alias = alias_match.group(1) if alias_match else "j"

    js_alias = "js"
    js_match = re.search(r"\bJOIN\s+job_skills\s+(\w+)", sql, flags=re.I)
    if js_match:
        js_alias = js_match.group(1)

    if re.search(r"\bjob_skills\b", sql, flags=re.I):
        clause = f"{alias}.session_id = %s AND {js_alias}.session_id = %s"
        params = [session_id, session_id] + params
    else:
        clause = f"{alias}.session_id = %s"
        params = [session_id] + params

    if re.search(r"\bWHERE\b", sql, flags=re.I):
        sql = re.sub(r"\bWHERE\b", f"WHERE {clause} AND", sql, count=1, flags=re.I)
    else:
        inserted = False
        for kw in ("GROUP BY", "ORDER BY", "HAVING", "LIMIT"):
            pat = rf"\b{kw}\b"
            if re.search(pat, sql, flags=re.I):
                sql = re.sub(pat, f"WHERE {clause} {kw}", sql, count=1, flags=re.I)
                inserted = True
                break
        if not inserted:
            sql = sql.rstrip().rstrip(";") + f" WHERE {clause}"

    return sql, tuple(params)


def scope_plan(plan: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    if not plan or not session_id:
        return plan

    plan = copy.deepcopy(plan)
    source = plan.get("source")
    sql = plan.get("sql")
    params = plan.get("params")

    if isinstance(source, tuple) and isinstance(sql, tuple):
        new_sql = []
        new_params = []
        for s, src, p in zip(sql, source, params):
            if src == "jobs_db" and s:
                s2, p2 = _inject_jobs_session(s, p, session_id)
                new_sql.append(s2)
                new_params.append(p2)
            else:
                new_sql.append(s)
                new_params.append(p)
        plan["sql"] = tuple(new_sql)
        plan["params"] = tuple(new_params)
        return plan

    if source == "jobs_db" and isinstance(sql, str) and sql:
        s2, p2 = _inject_jobs_session(sql, params or (), session_id)
        plan["sql"] = s2
        plan["params"] = p2

    return plan
