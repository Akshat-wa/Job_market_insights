"""
Free Neon disk space by wiping all job data (all sessions).
Run this before re-seeding with a smaller dataset.

Usage:
  python reset_jobs_db.py
  python reset_jobs_db.py --vacuum
"""
from __future__ import annotations

import argparse
import sys

import psycopg2

from config_loader import load_config
from storage_stats import get_storage_stats
from db import DBAdapter


def reset_jobs_db(cfg: dict, vacuum: bool = True) -> dict:
    info = cfg["jobs_db"]
    con = psycopg2.connect(info["dsn"])
    con.autocommit = True
    cur = con.cursor()

    before = get_storage_stats(DBAdapter(cfg))

    print("🧨 Truncating job_skills, jobs, skills …")
    cur.execute("TRUNCATE TABLE job_skills")
    cur.execute("TRUNCATE TABLE jobs RESTART IDENTITY CASCADE")
    cur.execute("TRUNCATE TABLE skills RESTART IDENTITY CASCADE")

    # Remove non-demo session metadata
    try:
        cur.execute("DELETE FROM upload_sessions WHERE session_id <> 'demo'")
    except Exception:
        pass

    if vacuum:
        print("🧹 Running VACUUM (reclaims disk on Neon) …")
        try:
            cur.execute("VACUUM FULL job_skills")
            cur.execute("VACUUM FULL jobs")
            cur.execute("VACUUM FULL skills")
        except Exception as e:
            print(f"   VACUUM FULL skipped ({e}), trying VACUUM …")
            cur.execute("VACUUM job_skills")
            cur.execute("VACUUM jobs")
            cur.execute("VACUUM skills")

    cur.close()
    con.close()

    after = get_storage_stats(DBAdapter(cfg))
    return {"before_mb": before.get("total_estimated_mb"), "after_mb": after.get("total_estimated_mb")}


def main():
    ap = argparse.ArgumentParser(description="Wipe all jobs DB data to free Neon space")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--no-vacuum", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg.get("jobs_db", {}).get("dsn"):
        print("❌ Set DATABASE_URL in .env")
        sys.exit(1)

    print("⚠️  This deletes ALL job sessions (including demo).")
    result = reset_jobs_db(cfg, vacuum=not args.no_vacuum)
    print(f"✅ Done. Storage: {result['before_mb']} MB → {result['after_mb']} MB")
    print("   Next: python seed_portfolio.py")


if __name__ == "__main__":
    main()
