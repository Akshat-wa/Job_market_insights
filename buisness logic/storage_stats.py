"""Storage usage helpers for Neon free-tier budgeting."""
from __future__ import annotations

from typing import Any, Dict

from db import DBAdapter


def _mb(bytes_val: int | float | None) -> float:
    if not bytes_val:
        return 0.0
    return round(float(bytes_val) / (1024 * 1024), 2)


def get_storage_stats(adapter: DBAdapter) -> Dict[str, Any]:
    out: Dict[str, Any] = {"jobs_db": {}, "candidates_db": {}}

    try:
        row = adapter.run_sql("jobs_db", "SELECT pg_database_size(current_database())", ())[0]
        out["jobs_db"]["database_size_mb"] = _mb(row[0])
    except Exception as e:
        out["jobs_db"]["error"] = str(e)

    try:
        rows = adapter.run_sql(
            "jobs_db",
            """
            SELECT j.session_id, COUNT(DISTINCT j.job_id) AS jobs,
                   COUNT(js.skill_id) AS skill_links
            FROM jobs j
            LEFT JOIN job_skills js
              ON js.job_id = j.job_id AND js.session_id = j.session_id
            GROUP BY j.session_id
            ORDER BY jobs DESC
            """,
            (),
        )
        out["jobs_db"]["sessions"] = [
            {"session_id": r[0], "jobs": r[1], "skill_links": r[2]} for r in rows
        ]
    except Exception as e:
        out["jobs_db"]["sessions_error"] = str(e)

    try:
        row = adapter.run_sql("candidates_db", "SELECT pg_database_size(current_database())", ())[0]
        out["candidates_db"]["database_size_mb"] = _mb(row[0])
        cand = adapter.run_sql("candidates_db", "SELECT COUNT(*) FROM candidates", ())[0]
        out["candidates_db"]["candidates"] = cand[0]
    except Exception as e:
        out["candidates_db"]["error"] = str(e)

    jobs_mb = out["jobs_db"].get("database_size_mb", 0) or 0
    cand_mb = out["candidates_db"].get("database_size_mb", 0) or 0
    out["total_estimated_mb"] = round(jobs_mb + cand_mb, 2)
    out["neon_free_limit_mb"] = 512
    out["headroom_mb"] = round(512 - (jobs_mb + cand_mb), 2)
    return out
