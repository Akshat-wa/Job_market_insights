"""
One-command portfolio seed (~100 MB total on Neon free tier).

What it does:
  1. Wipe old job data (frees disk if you hit 512 MB limit)
  2. Generate ~12 MB synthetic jobs CSV (~50–70k jobs, 4–5 skills each)
  3. Ingest into session 'demo'
  4. Light-ingest resumes (~20 MB, no transformer)

Usage:
  python seed_portfolio.py
  python seed_portfolio.py --skip-reset --skip-candidates
  python seed_portfolio.py --force-csv

Prerequisites:
  - DATABASE_URL set in .env (Neon or local Postgres)
  - CANDIDATES_DATABASE_URL (or same host, dbname=candidates)
  - data/master_resumes.jsonl present (~16 MB)
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

from cleanup_sessions import cleanup_sessions
from config_loader import DEFAULT_SESSION, load_config
from db import DBAdapter
from generate_portfolio_jobs import generate_csv
from ingest_jobs_flexible import ensure_session_schema, ingest_upload
from reset_jobs_db import reset_jobs_db
from seed_candidates_light import ingest_light
from storage_stats import get_storage_stats


def _merge_budget(cfg: dict) -> dict:
    budget_path = os.path.join(os.path.dirname(__file__), "portfolio_budget.yaml")
    if os.path.isfile(budget_path):
        with open(budget_path, "r", encoding="utf-8") as f:
            budget = yaml.safe_load(f) or {}
        for k, v in budget.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Seed portfolio demo data (~100 MB budget)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--jobs-mb", type=float, default=None, help="Synthetic CSV size on disk")
    ap.add_argument("--candidates-mb", type=float, default=None, help="Max JSONL bytes to ingest")
    ap.add_argument("--session-id", default=None, help="Demo session id (default: demo)")
    ap.add_argument("--skip-jobs", action="store_true")
    ap.add_argument("--skip-candidates", action="store_true")
    ap.add_argument("--skip-cleanup", action="store_true")
    ap.add_argument("--skip-reset", action="store_true", help="Do not wipe jobs DB first")
    ap.add_argument("--force-csv", action="store_true", help="Regenerate jobs CSV even if cached")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = _merge_budget(load_config(args.config))
    portfolio = cfg.get("portfolio", {}) or {}

    jobs_mb = args.jobs_mb or float(portfolio.get("jobs_csv_target_mb", 12))
    cand_mb = args.candidates_mb or float(portfolio.get("candidates_jsonl_max_mb", 16))
    sk_min = int(portfolio.get("skills_per_job_min", 4))
    sk_max = int(portfolio.get("skills_per_job_max", 5))
    session_id = args.session_id or portfolio.get("demo_session_id", DEFAULT_SESSION)

    jobs_csv = os.path.join(os.path.dirname(__file__), "data", "portfolio_jobs_combined.csv")
    jsonl = cfg.get("candidates_csv") or os.path.join(os.path.dirname(__file__), "data", "master_resumes.jsonl")

    print("=" * 60)
    print("Portfolio seed — Path A (no full LinkedIn dumps)")
    print("=" * 60)
    print(f"  Jobs CSV target : ~{jobs_mb} MB  →  session '{session_id}'")
    print(f"  Candidates    : ~{cand_mb} MB from {jsonl}")
    print(f"  Neon target    : ~{portfolio.get('target_demo_db_mb', 100)} MB total (jobs + candidates)")
    print()

    if args.dry_run:
        print("Dry run — no changes made.")
        return

    if not cfg.get("jobs_db", {}).get("dsn"):
        print("❌ Set DATABASE_URL in .env before seeding.")
        sys.exit(1)

    dsn = cfg["jobs_db"]["dsn"]
    if "localhost" in dsn or "127.0.0.1" in dsn:
        print("❌ Still using localhost Postgres from config.yaml.")
        print("   Fix .env — DATABASE_URL must be your Neon connection string.")
        print("   Example: DATABASE_URL=postgresql://user:pass@ep-xxx.neon.tech/job_market?sslmode=require")
        sys.exit(1)

    ensure_session_schema(cfg)

    if not args.skip_jobs and not args.skip_reset:
        print("🧨 Resetting jobs DB (free disk space) …")
        reset_jobs_db(cfg, vacuum=True)

    if not args.skip_cleanup:
        print("🧹 Cleaning expired upload sessions …")
        cleanup_sessions(
            cfg,
            ttl_hours=int(portfolio.get("session_ttl_hours", 24)),
            keep_demo=session_id,
        )

    if not args.skip_jobs:
        need_gen = args.force_csv or not os.path.isfile(jobs_csv)
        if not need_gen and os.path.isfile(jobs_csv):
            sz = os.path.getsize(jobs_csv) / (1024 * 1024)
            if sz > jobs_mb * 1.2 or sz < jobs_mb * 0.5:
                print(f"ℹ️  Cached CSV ({sz:.1f} MB) wrong size for target {jobs_mb} MB — regenerating")
                need_gen = True

        if need_gen:
            print(f"🛠  Generating synthetic jobs CSV (~{jobs_mb} MB, {sk_min}-{sk_max} skills/job) …")
            generate_csv(jobs_csv, target_mb=jobs_mb, skills_min=sk_min, skills_max=sk_max)
        else:
            sz = os.path.getsize(jobs_csv) / (1024 * 1024)
            print(f"ℹ️  Using cached jobs CSV ({sz:.1f} MB): {jobs_csv}")

        print(f"📥 Ingesting jobs into session '{session_id}' …")
        with open(jobs_csv, "rb") as f:
            result = ingest_upload(
                cfg,
                session_id=session_id,
                combined_bytes=f.read(),
                mode="replace",
                max_rows=100_000,
            )
        print(f"   → {result['stats'].get('jobs_inserted', '?')} jobs, "
              f"{result['stats'].get('skill_links_inserted', '?')} skill links")

    if not args.skip_candidates:
        if not os.path.isfile(jsonl):
            print(f"⚠️  Skipping candidates — file not found: {jsonl}")
        else:
            print("📥 Light candidate ingest (no transformer) …")
            ingest_light(cfg, jsonl, max_bytes=int(cand_mb * 1024 * 1024))

    print("\n📊 Storage snapshot:")
    adapter = DBAdapter(cfg)
    stats = get_storage_stats(adapter)
    print(f"   jobs DB      : {stats['jobs_db'].get('database_size_mb', '?')} MB")
    print(f"   candidates DB: {stats['candidates_db'].get('database_size_mb', '?')} MB")
    print(f"   total est.   : {stats.get('total_estimated_mb', '?')} MB")
    print(f"   headroom     : {stats.get('headroom_mb', '?')} MB (of 512 MB Neon free)")

    demo_sess = next(
        (s for s in stats.get("jobs_db", {}).get("sessions", []) if s["session_id"] == session_id),
        None,
    )
    if demo_sess:
        print(f"   demo session : {demo_sess['jobs']:,} jobs, {demo_sess['skill_links']:,} skill links")

    cand_count = stats.get("candidates_db", {}).get("candidates")
    if cand_count:
        print(f"   candidates   : {cand_count:,} profiles")

    print("\n✅ Portfolio seed complete.")
    print("   Next: python api_server.py  →  Load sample / run queries in UI")


if __name__ == "__main__":
    main()
