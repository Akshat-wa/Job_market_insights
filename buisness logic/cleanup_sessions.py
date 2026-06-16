"""Remove expired upload sessions to preserve Neon free-tier headroom."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import yaml

from config_loader import load_config
from db import DBAdapter


def cleanup_sessions(cfg: dict, ttl_hours: int = 24, keep_demo: str = "demo") -> dict:
    adapter = DBAdapter(cfg)
    deleted_sessions = []

    try:
        rows = adapter.run_sql(
            "jobs_db",
            """
            SELECT session_id FROM upload_sessions
            WHERE last_used_at < NOW() - (%s || ' hours')::INTERVAL
              AND session_id <> %s
            """,
            (str(ttl_hours), keep_demo),
        )
        for (sid,) in rows:
            adapter.run_sql("jobs_db", "DELETE FROM job_skills WHERE session_id = %s", (sid,))
            adapter.run_sql("jobs_db", "DELETE FROM jobs WHERE session_id = %s", (sid,))
            adapter.run_sql("jobs_db", "DELETE FROM upload_sessions WHERE session_id = %s", (sid,))
            deleted_sessions.append(sid)
    except Exception as e:
        return {"ok": False, "error": str(e), "deleted": deleted_sessions}

    return {
        "ok": True,
        "deleted_sessions": deleted_sessions,
        "ttl_hours": ttl_hours,
        "kept_demo": keep_demo,
        "at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--ttl-hours", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    portfolio = cfg.get("portfolio", {}) or {}
    ttl = args.ttl_hours or int(portfolio.get("session_ttl_hours", 24))
    demo = portfolio.get("demo_session_id", "demo")

    result = cleanup_sessions(cfg, ttl_hours=ttl, keep_demo=demo)
    print(result)


if __name__ == "__main__":
    main()
