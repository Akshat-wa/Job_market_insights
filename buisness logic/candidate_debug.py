# candidate_skills_debug.py
from __future__ import annotations

import argparse
import yaml

from db import DBAdapter


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Debug helper – show skills for a single candidate (by ID or name)"
    )
    ap.add_argument(
        "candidate",
        type=str,
        help="Candidate identifier (numeric cand_id like '61' or full name like 'John Doe')",
    )
    ap.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config (default: config.yaml)",
    )
    args = ap.parse_args()

    # Load config and build adapter
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    adapter = DBAdapter(cfg)

    cand_arg = args.candidate.strip()

    # -------------------------------
    # 1) Resolve candidate (id or name) in candidates_db
    # -------------------------------
    if cand_arg.isdigit():
        cid = int(cand_arg)
        cand_sql = (
            "SELECT cand_id, name, location_lc, country "
            "FROM candidates "
            "WHERE cand_id = %s"
        )
        cand_params = (cid,)
        id_mode = True
    else:
        cand_sql = (
            "SELECT cand_id, name, location_lc, country "
            "FROM candidates "
            "WHERE LOWER(name) = %s"
        )
        cand_params = (cand_arg.lower(),)
        id_mode = False

    rows = adapter.run_sql("candidates_db", cand_sql, cand_params)

    # If name lookup failed, try fuzzy match as a fallback
    if not rows and not id_mode:
        fuzzy_sql = (
            "SELECT cand_id, name, location_lc, country "
            "FROM candidates "
            "WHERE name ILIKE %s "
            "ORDER BY name LIMIT 5"
        )
        rows = adapter.run_sql("candidates_db", fuzzy_sql, (f"%{cand_arg}%",))

    if not rows:
        print("No candidate found for:", cand_arg)
        return

    cand_id, cand_name, cand_loc, cand_country = rows[0]

    print("=== Candidate ===")
    print(f"ID       : {cand_id}")
    print(f"Name     : {cand_name or '(empty)'}")
    print(f"Location : {cand_loc or '(null)'}")
    print(f"Country  : {cand_country or '(null)'}")
    print()

    # -------------------------------
    # 2) Fetch skill_ids from candidates_db.candidate_skills
    # -------------------------------
    cs_sql = (
        "SELECT skill_id, source "
        "FROM candidate_skills "
        "WHERE cand_id = %s "
        "ORDER BY skill_id"
    )
    cs_rows = adapter.run_sql("candidates_db", cs_sql, (cand_id,))

    if not cs_rows:
        print("No entries in candidate_skills for this candidate.")
        return

    skill_ids = [r[0] for r in cs_rows]
    sources = {r[0]: r[1] for r in cs_rows}

    print("Raw skill_ids from candidate_skills:", skill_ids)
    print()

    # -------------------------------
    # 3) Look up skill names, preferably in jobs_db.skills
    # -------------------------------
    placeholders = ", ".join(["%s"] * len(skill_ids))
    skills_sql = (
        f"SELECT skill_id, name "
        f"FROM skills "
        f"WHERE skill_id IN ({placeholders}) "
        f"ORDER BY name"
    )

    # try candidates_db first (in case you cloned skills there)
    name_rows = adapter.run_sql("candidates_db", skills_sql, tuple(skill_ids))

    if not name_rows:
        # fall back to jobs_db if candidates_db has no skills table / rows
        name_rows = adapter.run_sql("jobs_db", skills_sql, tuple(skill_ids))

    if not name_rows:
        print("Could not find matching rows in any skills table.")
        return

    id_to_name = {sid: sname for sid, sname in name_rows}

    print("=== Skills ===")
    for sid in skill_ids:
        sname = id_to_name.get(sid, "(name not found)")
        src = sources.get(sid, "")
        print(f"- [{sid}] {sname}  (source={src})")

    print()
    print(f"Total skills: {len(skill_ids)}")


if __name__ == "__main__":
    main()
