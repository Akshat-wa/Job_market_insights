"""
Fast candidate ingest for portfolio demos — NO transformer model required.

Uses declared resume skills + experience/project technologies.
Maps explicit skills to global jobs_db.skills by name.

Usage:
  python seed_candidates_light.py --config config.yaml
  python seed_candidates_light.py --max-mb 16
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from typing import Any, Dict, Iterable, List, Tuple

import psycopg2
import psycopg2.extras as pgx
import pycountry
import yaml
from tqdm import tqdm

UNKY = {"unknown", "not provided", ""}


def norm(x: Any) -> str:
    return "" if x is None else str(x).strip()


def clean_val(x: Any) -> str:
    s = norm(x)
    return "" if s.lower() in UNKY else s


def to_lc(s: str) -> str:
    return clean_val(s).lower()


def as_list(x) -> List:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def iso(x: Any) -> str:
    s = norm(x)
    m = re.search(r"\b(\d{4})(?:[-/ ](\d{1,2}))?(?:[-/ ](\d{1,2}))?\b", s)
    if not m:
        return ""
    y = m.group(1)
    mo = m.group(2).zfill(2) if m.group(2) else ""
    d = m.group(3).zfill(2) if m.group(3) else ""
    if y and mo and d:
        return f"{y}-{mo}-{d}"
    if y and mo:
        return f"{y}-{mo}"
    return y


def join_if_any(items: Iterable[str], sep=", "):
    vals = [clean_val(t) for t in items if clean_val(t)]
    return sep.join(vals)


def canonicalize_skill(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = name.strip().lower()
    s = s.replace("c plus plus", "c++").replace("c sharp", "c#")
    s = s.replace("node.js", "nodejs").replace("react.js", "react").replace("reactjs", "react")
    if s in ("javascript", "java script"):
        s = "js"
    return re.sub(r"\s+", " ", s).strip()


COUNTRY_MAP = {c.name.lower(): c.name for c in pycountry.countries}
COUNTRY_MAP.update({"usa": "United States", "us": "United States", "uk": "United Kingdom"})


def canonical_country(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = loc.split(",")
    candidate = parts[-1].strip().lower() if parts else ""
    return COUNTRY_MAP.get(candidate, None)


DDL = """
DROP TABLE IF EXISTS candidate_derived_skills;
DROP TABLE IF EXISTS candidate_skill_legitimacy;
DROP TABLE IF EXISTS languages;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS education;
DROP TABLE IF EXISTS experience;
DROP TABLE IF EXISTS candidate_skills;
DROP TABLE IF EXISTS candidates;

CREATE TABLE candidates(
    cand_id BIGSERIAL PRIMARY KEY,
    name TEXT, email TEXT, phone TEXT,
    location TEXT, location_lc TEXT, country TEXT,
    summary TEXT, certifications TEXT, search_blob TEXT
);
CREATE TABLE candidate_skills(cand_id BIGINT, skill_id BIGINT, source TEXT);
CREATE TABLE experience(
    cand_id BIGINT, company TEXT, title TEXT, location TEXT, location_lc TEXT,
    start_date TEXT, end_date TEXT, duration TEXT, responsibilities TEXT
);
CREATE TABLE education(
    cand_id BIGINT, institution TEXT, degree TEXT, major TEXT,
    start_date TEXT, end_date TEXT, gpa TEXT, honors TEXT, accreditation TEXT
);
CREATE TABLE projects(
    cand_id BIGINT, name TEXT, role TEXT, description TEXT,
    impact TEXT, url TEXT, technologies TEXT
);
CREATE TABLE languages(cand_id BIGINT, language TEXT, level TEXT);
CREATE TABLE candidate_skill_legitimacy(
    cand_id BIGINT PRIMARY KEY,
    legitimacy_score DOUBLE PRECISION,
    num_skills_total INT,
    has_claimed_skills BOOLEAN,
    has_text_skills BOOLEAN,
    declared_total_evidence INT,
    reason TEXT
);
CREATE TABLE candidate_derived_skills(cand_id BIGINT, skill_name TEXT, source TEXT);
CREATE INDEX idx_cand_loc ON candidates(location_lc);
CREATE INDEX idx_cand_country ON candidates(country);
"""


def extract_core(rec: Dict[str, Any]) -> Dict[str, str]:
    pi = rec.get("personal_info", {}) or {}
    loc = pi.get("location") or {}
    city = clean_val(loc.get("city"))
    country_raw = clean_val(loc.get("country"))
    location = join_if_any([city, country_raw], sep=", ")
    return dict(
        name=clean_val(pi.get("name")),
        email=clean_val(pi.get("email")),
        phone=clean_val(pi.get("phone")),
        location=location,
        location_lc=to_lc(location),
        country=canonical_country(location),
        summary=clean_val(pi.get("summary")),
        certifications=clean_val(rec.get("certifications")),
    )


def extract_skills(rec: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    tech = (rec.get("skills") or {}).get("technical") or {}

    def pull(arr) -> List[str]:
        vals = []
        for it in as_list(arr):
            if isinstance(it, dict):
                vals.append(clean_val(it.get("name")))
            else:
                vals.append(clean_val(it))
        return [v for v in vals if v]

    for cat, arr in tech.items():
        if isinstance(arr, list):
            out[f"technical.{cat}"] = pull(arr)
    return out


def extract_experience(rec: Dict[str, Any]):
    rows, extra = [], {}
    for e in as_list(rec.get("experience")):
        if not isinstance(e, dict):
            continue
        te = e.get("technical_environment") or {}
        techs = te.get("technologies") or []
        names = [clean_val(t.get("name") if isinstance(t, dict) else t) for t in as_list(techs)]
        names = [n for n in names if n]
        if names:
            extra["experience.technologies"] = names
        rows.append(dict(
            company=clean_val(e.get("company")),
            title=clean_val(e.get("title")),
            location=clean_val(e.get("location")),
            location_lc=to_lc(clean_val(e.get("location"))),
            start_date=iso((e.get("dates") or {}).get("start")),
            end_date=iso((e.get("dates") or {}).get("end")),
            duration=clean_val((e.get("dates") or {}).get("duration")),
            responsibilities=join_if_any(e.get("responsibilities") or [], sep=" | "),
        ))
    return rows, extra


def extract_education(rec: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for ed in as_list(rec.get("education")):
        if not isinstance(ed, dict):
            continue
        deg = ed.get("degree") or {}
        inst = ed.get("institution") or {}
        ach = ed.get("achievements") or {}
        dates = ed.get("dates") or {}
        rows.append(dict(
            institution=clean_val(inst.get("name")),
            degree=clean_val(deg.get("level") or deg.get("field")),
            major=clean_val(deg.get("major")),
            start_date=iso(dates.get("start")),
            end_date=iso(dates.get("expected_graduation") or dates.get("end")),
            gpa=clean_val(ach.get("gpa")),
            honors=clean_val(ach.get("honors")),
            accreditation=clean_val(inst.get("accreditation")),
        ))
    return rows


def extract_projects(rec: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for p in as_list(rec.get("projects")):
        if not isinstance(p, dict):
            continue
        rows.append(dict(
            name=clean_val(p.get("name")),
            role=clean_val(p.get("role")),
            description=clean_val(p.get("description")),
            impact=clean_val(p.get("impact")),
            url=clean_val(p.get("url")),
            technologies=join_if_any(p.get("technologies") or [], sep=", "),
        ))
    return rows


def get_matching_skill_id(cur_jobs, skill_name: str):
    s = canonicalize_skill(skill_name)
    if not s:
        return None
    cur_jobs.execute("SELECT skill_id FROM skills WHERE name_lc = %s LIMIT 1", (s,))
    row = cur_jobs.fetchone()
    return row[0] if row else None


def load_skill_cache(cur_jobs) -> dict[str, int]:
    """One round-trip to jobs DB — avoids per-skill Neon lookups."""
    cur_jobs.execute("SELECT name_lc, skill_id FROM skills")
    return {row[0]: row[1] for row in cur_jobs.fetchall() if row[0]}


def resolve_skill_id(skill_cache: dict[str, int], skill_name: str) -> int | None:
    return skill_cache.get(canonicalize_skill(skill_name))


def add_skill_list_cached(cur_local, skill_cache: dict[str, int], cand_id: int, skills: Iterable[str], source: str):
    rows = []
    for s in skills:
        sid = resolve_skill_id(skill_cache, s)
        if sid is not None:
            rows.append((cand_id, sid, source))
    if rows:
        pgx.execute_values(
            cur_local,
            "INSERT INTO candidate_skills(cand_id, skill_id, source) VALUES %s",
            rows,
            page_size=2000,
        )


def add_derived(cur_local, cand_id: int, skills: Iterable[str], source: str):
    rows = [(cand_id, clean_val(s), source) for s in skills if clean_val(s)]
    if rows:
        pgx.execute_values(
            cur_local,
            "INSERT INTO candidate_derived_skills(cand_id, skill_name, source) VALUES %s",
            rows,
            page_size=2000,
        )


def simple_legitimacy(skill_count: int, derived_count: int) -> Tuple[float, str]:
    if skill_count >= 5:
        return 0.75, "portfolio_light_many_skills"
    if skill_count >= 1:
        return 0.65, "portfolio_light_some_skills"
    if derived_count >= 3:
        return 0.55, "portfolio_light_derived_only"
    if derived_count >= 1:
        return 0.45, "portfolio_light_sparse"
    return 0.25, "portfolio_light_minimal"


def _rows_from_record(rec: Dict[str, Any], skill_cache: dict[str, int]) -> dict:
    """Parse one JSONL record into row buffers (no DB calls)."""
    core = extract_core(rec)
    cand_row = (
        core["name"], core["email"], core["phone"],
        core["location"], core["location_lc"], core["country"],
        core["summary"], core["certifications"],
        f"{core['name']} {core['summary']} {core['location']}".lower(),
    )

    skill_rows: List[Tuple[int, str]] = []  # (skill_id, source) — cand_id added at flush
    derived_rows: List[Tuple[str, str]] = []  # (skill_name, source)
    exp_rows: List[Tuple] = []
    edu_rows: List[Tuple] = []
    proj_rows: List[Tuple] = []
    explicit_count = 0
    derived_count = 0

    tech_sk = extract_skills(rec)
    for cat, vals in tech_sk.items():
        if "auto_extracted" in cat:
            for s in vals:
                if clean_val(s):
                    derived_rows.append((clean_val(s), cat))
            derived_count += len(vals)
        else:
            for s in vals:
                sid = resolve_skill_id(skill_cache, s)
                if sid is not None:
                    skill_rows.append((sid, cat))
            explicit_count += len(vals)

    experience, env_sk = extract_experience(rec)
    for r in experience:
        exp_rows.append((
            r["company"], r["title"], r["location"], r["location_lc"],
            r["start_date"], r["end_date"], r["duration"], r["responsibilities"],
        ))
    for cat, vals in env_sk.items():
        for s in vals:
            if clean_val(s):
                derived_rows.append((clean_val(s), cat))
        derived_count += len(vals)

    for ed in extract_education(rec):
        edu_rows.append((
            ed["institution"], ed["degree"], ed["major"],
            ed["start_date"], ed["end_date"], ed["gpa"], ed["honors"], ed["accreditation"],
        ))

    for pr in extract_projects(rec):
        proj_rows.append((
            pr["name"], pr["role"], pr["description"], pr["impact"], pr["url"], pr["technologies"],
        ))
        if pr["technologies"]:
            techs = [t.strip() for t in pr["technologies"].split(",") if t.strip()]
            for t in techs:
                derived_rows.append((t, "projects.technologies"))
            derived_count += len(techs)

    score, reason = simple_legitimacy(explicit_count, derived_count)
    leg_row = (
        score, explicit_count + derived_count,
        explicit_count > 0, derived_count > 0, explicit_count, reason,
    )

    return {
        "cand_row": cand_row,
        "skill_rows": skill_rows,
        "derived_rows": derived_rows,
        "exp_rows": exp_rows,
        "edu_rows": edu_rows,
        "proj_rows": proj_rows,
        "leg_row": leg_row,
    }


BATCH_SIZE = 80  # fewer Neon round-trips (critical when DB is far away)


def _flush_batch(cur_cand, batch: List[dict]) -> None:
    if not batch:
        return

    pgx.execute_values(
        cur_cand,
        """INSERT INTO candidates(name,email,phone,location,location_lc,country,summary,certifications,search_blob)
           VALUES %s RETURNING cand_id""",
        [b["cand_row"] for b in batch],
        page_size=BATCH_SIZE,
    )
    cand_ids = [row[0] for row in cur_cand.fetchall()]

    all_skills, all_derived, all_exp, all_edu, all_proj, all_leg = [], [], [], [], [], []
    for cid, b in zip(cand_ids, batch):
        all_skills.extend([(cid, sid, src) for sid, src in b["skill_rows"]])
        all_derived.extend([(cid, name, src) for name, src in b["derived_rows"]])
        all_exp.extend([(cid, *row) for row in b["exp_rows"]])
        all_edu.extend([(cid, *row) for row in b["edu_rows"]])
        all_proj.extend([(cid, *row) for row in b["proj_rows"]])
        all_leg.append((cid, *b["leg_row"]))

    if all_skills:
        pgx.execute_values(
            cur_cand,
            "INSERT INTO candidate_skills(cand_id, skill_id, source) VALUES %s",
            all_skills,
            page_size=5000,
        )
    if all_derived:
        pgx.execute_values(
            cur_cand,
            "INSERT INTO candidate_derived_skills(cand_id, skill_name, source) VALUES %s",
            all_derived,
            page_size=5000,
        )
    if all_exp:
        pgx.execute_values(
            cur_cand,
            """INSERT INTO experience(cand_id,company,title,location,location_lc,start_date,end_date,duration,responsibilities)
               VALUES %s""",
            all_exp,
            page_size=5000,
        )
    if all_edu:
        pgx.execute_values(
            cur_cand,
            """INSERT INTO education(cand_id,institution,degree,major,start_date,end_date,gpa,honors,accreditation)
               VALUES %s""",
            all_edu,
            page_size=5000,
        )
    if all_proj:
        pgx.execute_values(
            cur_cand,
            """INSERT INTO projects(cand_id,name,role,description,impact,url,technologies)
               VALUES %s""",
            all_proj,
            page_size=5000,
        )
    if all_leg:
        pgx.execute_values(
            cur_cand,
            """INSERT INTO candidate_skill_legitimacy(
                   cand_id,legitimacy_score,num_skills_total,has_claimed_skills,
                   has_text_skills,declared_total_evidence,reason)
               VALUES %s""",
            all_leg,
            page_size=5000,
        )
    cur_cand.connection.commit()


def ingest_light(cfg: dict, jsonl_path: str, max_bytes: int) -> dict:
    jobs_info = cfg["jobs_db"]
    cand_info = cfg["candidates_db"]

    con_jobs = psycopg2.connect(jobs_info["dsn"])
    con_cand = psycopg2.connect(cand_info["dsn"])
    con_cand.autocommit = False
    cur_jobs = con_jobs.cursor()
    cur_cand = con_cand.cursor()

    cur_cand.execute(DDL)
    con_cand.commit()

    print("📚 Loading skill cache from jobs DB (one query) …")
    skill_cache = load_skill_cache(cur_jobs)
    print(f"   → {len(skill_cache):,} skills cached")
    cur_jobs.close()
    con_jobs.close()

    bytes_read = 0
    count = 0
    batch: List[dict] = []
    t0 = time.time()

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in tqdm(f, desc="Ingesting candidates (light)"):
            if bytes_read >= max_bytes:
                break
            bytes_read += len(line.encode("utf-8"))
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            batch.append(_rows_from_record(rec, skill_cache))
            count += 1

            if len(batch) >= BATCH_SIZE:
                _flush_batch(cur_cand, batch)
                batch.clear()

        if batch:
            _flush_batch(cur_cand, batch)
            batch.clear()

    cur_cand.close()
    con_jobs.close()
    con_cand.close()

    elapsed = time.time() - t0
    print(f"✅ Light candidate ingest: {count:,} profiles in {elapsed:.1f}s ({bytes_read/(1024*1024):.1f} MB read)")
    return {"candidates": count, "bytes_read": bytes_read, "elapsed_sec": round(elapsed, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--jsonl", default=None)
    ap.add_argument("--max-mb", type=float, default=16.0)
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    from config_loader import load_config
    cfg = {**cfg, **load_config(args.config)}

    jsonl = args.jsonl or cfg.get("candidates_csv") or "data/master_resumes.jsonl"
    if not os.path.isfile(jsonl):
        raise FileNotFoundError(f"Resume JSONL not found: {jsonl}")

    max_bytes = int(args.max_mb * 1024 * 1024)
    ingest_light(cfg, jsonl, max_bytes)


if __name__ == "__main__":
    main()
