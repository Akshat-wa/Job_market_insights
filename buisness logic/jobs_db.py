# build_jobs_db_pg.py  — no fuzzy merging, fast COPY + staging join
import os
import re
import sys
import io
import time
import csv
import yaml
import pandas as pd
from tqdm import tqdm
import psycopg2
from collections import Counter
import warnings
import pycountry
warnings.filterwarnings("ignore", category=UserWarning)

# ---------------- Utility helpers ----------------
def norm_str(x):
    if pd.isna(x): return None
    return str(x).strip()

def lower_or_empty(x):
    return (x or "").lower()

def ci_contains(hay: str | None, needles):
    if not hay: return False
    h = hay.lower()
    return any(n.lower() in h for n in needles)

def normalize_skill(s: str) -> str:
    # Basic canonicalization (no fuzzy merge): lowercase + trim + collapse spaces + strip punct
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s
# Example mapping: lowercase keys -> canonical country name
COUNTRY_MAP = {c.name.lower(): c.name for c in pycountry.countries}
COUNTRY_MAP.update({
    "usa": "United States",
    "us": "United States",
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
    "remote": None,  # special case
})
def canonical_country(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = loc.split(",")
    candidate = parts[-1].strip().lower() if parts else ""
    return COUNTRY_MAP.get(candidate, None)
# ---------------- DDL ----------------
DDL = """
DROP TABLE IF EXISTS job_role_clusters;
DROP TABLE IF EXISTS role_clusters;
DROP TABLE IF EXISTS skill_cluster_membership;
DROP TABLE IF EXISTS skill_clusters;
DROP TABLE IF EXISTS job_skills;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS jobs;

CREATE TABLE jobs(
  job_id      SERIAL PRIMARY KEY,
  job_link    TEXT UNIQUE,
  title       TEXT,
  company     TEXT,
  location    TEXT,
  title_lc    TEXT,
  location_lc TEXT,
  country     TEXT,
  country_lc  TEXT
);

CREATE TABLE skills(
  skill_id  SERIAL PRIMARY KEY,
  name      TEXT UNIQUE,
  name_lc   TEXT UNIQUE
);

CREATE TABLE job_skills(
  job_id   INTEGER NOT NULL,
  skill_id INTEGER NOT NULL,
  PRIMARY KEY (job_id, skill_id)
);
"""

INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_jobs_link         ON jobs(job_link);
CREATE INDEX IF NOT EXISTS idx_jobs_title_lc     ON jobs(title_lc);
CREATE INDEX IF NOT EXISTS idx_jobs_location_lc  ON jobs(location_lc);
CREATE INDEX IF NOT EXISTS idx_jobs_country_lc   ON jobs(country_lc);
CREATE INDEX IF NOT EXISTS idx_skills_name_lc    ON skills(name_lc);
CREATE INDEX IF NOT EXISTS idx_js_job            ON job_skills(job_id);
CREATE INDEX IF NOT EXISTS idx_js_skill          ON job_skills(skill_id);
"""

# ---------------- Main ----------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    jobs_skills_csv = cfg.get("jobs_skills_csv")
    jobs_postings_csv = cfg.get("jobs_postings_csv")
    filters = cfg.get("ingest_filters", {}) or {}
    keep_countries = filters.get("countries") or []
    title_regex = filters.get("title_regex")
    raw_max = filters.get("max_jobs")
    max_jobs = int(raw_max) if raw_max else None
    title_re = re.compile(title_regex, re.I) if title_regex else None

    # --- Connect ---
    info = cfg["jobs_db"]
    if info.get("backend") != "postgres":
        print("ERROR: jobs_db.backend must be 'postgres'")
        sys.exit(1)
    con = psycopg2.connect(info["dsn"])
    con.autocommit = False
    cur = con.cursor()

    # --- Session tuning (safe, per-session) ---
    cur.execute("SET synchronous_commit TO OFF;")
    cur.execute("SET maintenance_work_mem TO '2GB';")
    cur.execute("SET work_mem TO '256MB';")

    # --- Drop + recreate tables ---
    print("🧨 Dropping and recreating tables ...")
    cur.execute(DDL)
    con.commit()

    # ---------------- JOBS ----------------
    print("📦 Loading job postings CSV...")
    post_df = pd.read_csv(jobs_postings_csv)
    cols = {c.lower(): c for c in post_df.columns}
    job_link_col = cols.get("job_link")
    title_col = cols.get("job_title") or cols.get("title") or cols.get("position")
    company_col = cols.get("company") or cols.get("company_name") or cols.get("employer")
    job_loc_col = cols.get("job_location") or cols.get("location")
    city_col = cols.get("search_city")
    country_col = cols.get("search_country")
    if not job_link_col:
        print("❌ job_link missing in postings CSV")
        sys.exit(1)

    def derive_location(row):
        loc = norm_str(row[job_loc_col]) if job_loc_col else None
        if loc: return loc
        city = norm_str(row[city_col]) if city_col else None
        country = norm_str(row[country_col]) if country_col else None
        if city and country: return f"{city}, {country}"
        return city or country or None

    kept_rows = []
    seen_links = set()
    t0 = time.time()

    for _, r in tqdm(post_df.iterrows(), total=len(post_df), desc="Filtering job postings"):
        if max_jobs and len(kept_rows) >= max_jobs:
            break
        jlink = norm_str(r[job_link_col])
        if not jlink or jlink in seen_links:
            continue
        title = norm_str(r[title_col])
        company = norm_str(r[company_col])
        loc = derive_location(r)
        loc_val = derive_location(r)

        # 1) Try to extract country from last segment of location
        country_val = canonical_country(loc_val)

        # 2) If still None, attempt to infer by scanning entire location text
        if country_val is None and loc_val:
            loc_lower = loc_val.lower()
            for key, canon in COUNTRY_MAP.items():
                if canon and key in loc_lower:
                    country_val = canon
                    break

        # 3) Final fallback: check if location ends in common abbreviations
        if country_val is None and loc_val:
            parts = [p.strip().lower() for p in loc_val.split(",")]
            if parts:
                last = parts[-1]
                if last in COUNTRY_MAP:
                    country_val = COUNTRY_MAP[last]

        # 4) Normalized lowercase version
        country_lc = (country_val or "").lower()

        if keep_countries and not ci_contains(country_val, keep_countries):
            continue
        if title_re and not (title and title_re.search(title)):
            continue
        kept_rows.append((jlink, title, company, loc, lower_or_empty(title),
                          lower_or_empty(loc), country_val, country_lc))
        seen_links.add(jlink)

    print(f"✅ Filtered {len(kept_rows):,} / {len(post_df):,} postings in {time.time()-t0:.1f}s")

    # --- COPY jobs ---
    buf = io.StringIO(newline="")       # important on Windows for csv
    w = csv.writer(buf)
    for row in kept_rows:
        w.writerow([(x if x is not None else "") for x in row])
    buf.seek(0)

    cur.copy_expert("""
        COPY jobs (job_link, title, company, location, title_lc, location_lc, country, country_lc)
        FROM STDIN WITH (FORMAT CSV)
    """, buf)
    con.commit()

    cur.execute("SELECT job_link, job_id FROM jobs WHERE job_link = ANY(%s)", (list(seen_links),))
    job_id_map = dict(cur.fetchall())
    print(f"📄 Inserted {len(job_id_map):,} jobs")

    # ---------------- SKILLS ----------------
    print("\n📦 Loading job skills CSV...")
    skills_df = pd.read_csv(jobs_skills_csv)
    s_cols = {c.lower(): c for c in skills_df.columns}
    s_job_link_col = s_cols.get("job_link")
    s_skills_col   = s_cols.get("job_skills") or s_cols.get("skills")
    if not s_job_link_col or not s_skills_col:
        print("❌ job_skills.csv must have job_link + job_skills")
        sys.exit(1)

    all_skill_names = set()
    js_pairs = []  # (job_id, skill_name_lc)

    for _, r in tqdm(skills_df.iterrows(), total=len(skills_df), desc="Parsing job-skill pairs"):
        link = norm_str(r[s_job_link_col])
        jid = job_id_map.get(link)
        if not jid:
            continue
        raw = norm_str(r[s_skills_col]) or ""
        skills = [normalize_skill(s) for s in raw.split(",") if s.strip()]
        seen_local = set()
        for s in skills:
            if s in seen_local: continue
            seen_local.add(s)
            all_skill_names.add(s)
            js_pairs.append((jid, s))
            if len(seen_local) >= 30:
                break

    print(f"✅ Parsed {len(js_pairs):,} job-skill pairs; {len(all_skill_names):,} unique skills")
    top = Counter(s for _, s in js_pairs).most_common(1)
    print(f"ℹ️ Top skill frequency: {top[0] if top else ('—', 0)}")

    # --- COPY canonical skills (name, name_lc) ---
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    for s in sorted(all_skill_names):
        w.writerow([s, s])
    buf.seek(0)
    cur.copy_expert("COPY skills (name, name_lc) FROM STDIN WITH (FORMAT CSV)", buf)
    con.commit()

    # --- Stage (job_id, skill_name_lc) then resolve skill_id via JOIN ---
    print("\n🗃  Staging job_skills for de-dup + join ...")
    cur.execute("DROP TABLE IF EXISTS job_skills_stage;")
    cur.execute("""
        CREATE TEMP TABLE job_skills_stage(
            job_id        INTEGER,
            skill_name_lc TEXT
        ) ON COMMIT PRESERVE ROWS;
    """)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    for (jid, s) in js_pairs:
        w.writerow([jid, s])
    buf.seek(0)

    cur.copy_expert(
        "COPY job_skills_stage (job_id, skill_name_lc) FROM STDIN WITH (FORMAT CSV)",
        buf
    )
    # index & analyze before join; no commit yet (preserve rows)
    cur.execute("CREATE INDEX ON job_skills_stage(skill_name_lc);")
    cur.execute("ANALYZE job_skills_stage;")

    # Insert distinct pairs joined to skills
    cur.execute("""
        INSERT INTO job_skills(job_id, skill_id)
        SELECT DISTINCT s.job_id, sk.skill_id
        FROM job_skills_stage s
        JOIN skills sk ON sk.name_lc = s.skill_name_lc
        ON CONFLICT (job_id, skill_id) DO NOTHING;
    """)
    con.commit()
    print("✅ job_skills loaded (deduplicated via staging join).")

    # ---------------- Finalize ----------------
    print("\n🔧 Building indexes ...")
    cur.execute(INDEX_DDL)
    con.commit()

    # Re-enable durability for subsequent work
    cur.execute("SET synchronous_commit TO ON;")
    con.commit()

    # Summary
    cur.execute("SELECT COUNT(*) FROM jobs"); jobs_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM skills"); skills_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM job_skills"); js_cnt = cur.fetchone()[0]
    print(f"\n✅ [DONE] jobs={jobs_cnt:,}, skills={skills_cnt:,}, job_skills={js_cnt:,}")
    print(f"⏱ Total time: {(time.time()-t0)/60:.2f} min")

    cur.close(); con.close()

if __name__ == "__main__":
    main()
