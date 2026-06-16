"""Load runtime config from YAML + environment variables."""
from __future__ import annotations

import os
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SESSION = "demo"


def _postgres_dsn_from_env() -> str | None:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("PGHOST")
    if not host:
        return None
    port = os.getenv("PGPORT", "5432")
    dbname = os.getenv("PGDATABASE", "job_market")
    user = os.getenv("PGUSER", "postgres")
    password = os.getenv("PGPASSWORD", "")
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _candidates_dsn_from_env(jobs_dsn: str | None) -> str | None:
    cand = os.getenv("CANDIDATES_DATABASE_URL")
    if cand:
        return cand
    cand_db = os.getenv("CANDIDATES_PGDATABASE", "candidates")
    if jobs_dsn and jobs_dsn.startswith(("postgresql://", "postgres://")):
        base, _, tail = jobs_dsn.partition("://")
        hostpart, _, pathqs = tail.partition("/")
        if not pathqs:
            return jobs_dsn
        _, _, qs = pathqs.partition("?")
        uri = f"{base}://{hostpart}/{cand_db}"
        if qs:
            uri += f"?{qs}"
        return uri
    cand_db_env = os.getenv("CANDIDATES_PGDATABASE")
    if cand_db_env and jobs_dsn:
        return jobs_dsn.replace(
            f"dbname={os.getenv('PGDATABASE', 'job_market')}",
            f"dbname={cand_db_env}",
        )
    return jobs_dsn


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}

    jobs_dsn = _postgres_dsn_from_env()
    if jobs_dsn:
        cfg.setdefault("jobs_db", {})
        cfg["jobs_db"]["backend"] = "postgres"
        cfg["jobs_db"]["dsn"] = jobs_dsn

    cand_dsn = _candidates_dsn_from_env(jobs_dsn)
    if cand_dsn:
        cfg.setdefault("candidates_db", {})
        cfg["candidates_db"]["backend"] = "postgres"
        cfg["candidates_db"]["dsn"] = cand_dsn

    cfg.setdefault("session_id", DEFAULT_SESSION)
    cfg.setdefault("upload", {})
    cfg["upload"].setdefault("max_file_bytes", 10 * 1024 * 1024)
    cfg["upload"].setdefault("max_rows", 20_000)

    budget_path = os.path.join(os.path.dirname(__file__), "portfolio_budget.yaml")
    if os.path.isfile(budget_path):
        with open(budget_path, "r", encoding="utf-8") as f:
            budget = yaml.safe_load(f) or {}
        for key, val in budget.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key] = {**cfg[key], **val}
            else:
                cfg[key] = val

    return cfg
