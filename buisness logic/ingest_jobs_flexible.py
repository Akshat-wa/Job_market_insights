# Flexible job CSV ingest for portfolio deployment (session-scoped jobs, global skills).
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
from typing import Any, BinaryIO, Dict, List, Optional, Set, Tuple

import pandas as pd
import psycopg2

from config_loader import DEFAULT_SESSION

# Column alias maps (case-insensitive header matching)
POSTINGS_ALIASES = {
    "job_link": ("job_link", "url", "link", "job_url", "posting_url"),
    "title": ("job_title", "title", "position", "role", "job_title_name"),
    "company": ("company", "company_name", "employer", "organization"),
    "location": ("job_location", "location", "city", "work_location"),
    "search_city": ("search_city", "city_name"),
    "search_country": ("search_country", "country", "nation"),
    "skills": ("job_skills", "skills", "required_skills", "skill_list", "skill"),
}

MAX_FILE_BYTES_DEFAULT = 10 * 1024 * 1024
MAX_ROWS_DEFAULT = 20_000


def _norm_str(x) -> Optional[str]:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    return s or None


def _lower_or_empty(x) -> str:
    return (x or "").lower()


def normalize_skill(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_columns(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    cols = {c.lower().strip(): c for c in df.columns}
    detected: Dict[str, Optional[str]] = {}
    for canonical, aliases in POSTINGS_ALIASES.items():
        detected[canonical] = None
        for alias in aliases:
            if alias in cols:
                detected[canonical] = cols[alias]
                break
    return detected


def synthetic_job_link(title: str | None, company: str | None, location: str | None) -> str:
    raw = "|".join([title or "", company or "", location or ""]).strip().lower()
    if not raw:
        raw = "unknown"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"synthetic://{digest}"


def derive_location(row, detected: Dict[str, Optional[str]]) -> Optional[str]:
    loc_col = detected.get("location")
    city_col = detected.get("search_city")
    country_col = detected.get("search_country")

    loc = _norm_str(row[loc_col]) if loc_col else None
    if loc:
        return loc
    city = _norm_str(row[city_col]) if city_col else None
    country = _norm_str(row[country_col]) if country_col else None
    if city and country:
        return f"{city}, {country}"
    return city or country


def _canonical_country(loc: str | None) -> Tuple[Optional[str], str]:
    """Lightweight country extraction (full map lives in jobs_db.py)."""
    if not loc:
        return None, ""
    try:
        from jobs_db import canonical_country as cc, COUNTRY_MAP
        country_val = cc(loc)
        if country_val is None:
            loc_lower = loc.lower()
            for key, canon in COUNTRY_MAP.items():
                if canon and key in loc_lower:
                    country_val = canon
                    break
        if country_val is None:
            parts = [p.strip().lower() for p in loc.split(",")]
            if parts and parts[-1] in COUNTRY_MAP:
                country_val = COUNTRY_MAP[parts[-1]]
        return country_val, (country_val or "").lower()
    except Exception:
        parts = [p.strip() for p in loc.split(",")]
        last = parts[-1] if parts else ""
        return last or None, last.lower()


def read_csv_bytes(data: bytes, max_rows: int) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(data), nrows=max_rows)


def parse_postings_df(
    df: pd.DataFrame,
    detected: Dict[str, Optional[str]],
) -> Tuple[List[Tuple], List[str], Dict[str, Any]]:
    """
    Returns (job_rows, warnings, stats).
    job_rows: (link, title, company, loc, title_lc, location_lc, country, country_lc)
    """
    warnings: List[str] = []
    link_col = detected.get("job_link")
    title_col = detected.get("title")
    company_col = detected.get("company")

    if not link_col:
        warnings.append("job_link column missing — generating synthetic keys from title+company+location")

    kept: List[Tuple] = []
    seen_links: Set[str] = set()

    for _, r in df.iterrows():
        if link_col:
            jlink = _norm_str(r[link_col])
        else:
            title = _norm_str(r[title_col]) if title_col else None
            company = _norm_str(r[company_col]) if company_col else None
            loc = derive_location(r, detected)
            jlink = synthetic_job_link(title, company, loc)

        if not jlink or jlink in seen_links:
            continue

        title = _norm_str(r[title_col]) if title_col else None
        company = _norm_str(r[company_col]) if company_col else None
        loc = derive_location(r, detected)
        country_val, country_lc = _canonical_country(loc)

        kept.append(
            (
                jlink,
                title,
                company,
                loc,
                _lower_or_empty(title),
                _lower_or_empty(loc),
                country_val,
                country_lc,
            )
        )
        seen_links.add(jlink)

    stats = {"postings_rows_read": len(df), "postings_rows_kept": len(kept)}
    return kept, warnings, stats


def parse_skills_from_df(
    df: pd.DataFrame,
    detected: Dict[str, Optional[str]],
    job_id_map: Dict[str, int],
    session_id: str,
) -> Tuple[Set[str], List[Tuple], List[str]]:
    """
    Returns (all_skill_names, js_pairs as (session_id, job_id, skill_name_lc), warnings).
    """
    warnings: List[str] = []
    link_col = detected.get("job_link")
    skills_col = detected.get("skills")

    if not skills_col:
        return set(), [], ["No skills column found in CSV"]

    if not link_col:
        warnings.append("Skills column present but job_link missing — matching via synthetic links")

    all_skill_names: Set[str] = set()
    js_pairs: List[Tuple] = []

    for _, r in df.iterrows():
        if link_col:
            link = _norm_str(r[link_col])
        else:
            title_col = detected.get("title")
            company_col = detected.get("company")
            title = _norm_str(r[title_col]) if title_col else None
            company = _norm_str(r[company_col]) if company_col else None
            loc = derive_location(r, detected)
            link = synthetic_job_link(title, company, loc)

        jid = job_id_map.get(link or "")
        if not jid:
            continue

        raw = _norm_str(r[skills_col]) or ""
        skills = [normalize_skill(s) for s in raw.split(",") if s.strip()]
        seen_local: Set[str] = set()
        for s in skills:
            if not s or s in seen_local:
                continue
            seen_local.add(s)
            all_skill_names.add(s)
            js_pairs.append((session_id, jid, s))
            if len(seen_local) >= 5:
                break

    return all_skill_names, js_pairs, warnings


def ingest_upload(
    cfg: dict,
    session_id: str,
    postings_bytes: Optional[bytes] = None,
    skills_bytes: Optional[bytes] = None,
    combined_bytes: Optional[bytes] = None,
    mode: str = "replace",
    max_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Ingest CSV upload for a session.
    mode: 'replace' clears existing session job rows first.
    """
    upload_cfg = (cfg or {}).get("upload", {})
    if max_rows is None:
        max_rows = int(upload_cfg.get("max_rows", MAX_ROWS_DEFAULT))

    info = cfg["jobs_db"]
    if info.get("backend") != "postgres":
        raise RuntimeError("Upload ingest requires jobs_db.backend=postgres")

    con = psycopg2.connect(info["dsn"])
    con.autocommit = False
    cur = con.cursor()

    warnings: List[str] = []
    stats: Dict[str, Any] = {"session_id": session_id}

    try:
        if mode == "replace":
            cur.execute("DELETE FROM job_skills WHERE session_id = %s", (session_id,))
            cur.execute("DELETE FROM jobs WHERE session_id = %s", (session_id,))

        postings_df = None
        skills_df = None
        combined_detected: Dict[str, Optional[str]] = {}

        if combined_bytes:
            combined_df = read_csv_bytes(combined_bytes, max_rows)
            combined_detected = detect_columns(combined_df)
            postings_df = combined_df
            if combined_detected.get("skills"):
                skills_df = combined_df
        if postings_bytes:
            postings_df = read_csv_bytes(postings_bytes, max_rows)
        if skills_bytes:
            skills_df = read_csv_bytes(skills_bytes, max_rows)

        if postings_df is None and skills_df is None:
            raise ValueError("No CSV data provided")

        job_rows: List[Tuple] = []
        job_id_map: Dict[str, int] = {}

        if postings_df is not None:
            det = detect_columns(postings_df)
            job_rows, w, pst = parse_postings_df(postings_df, det)
            warnings.extend(w)
            stats.update(pst)
            stats["columns_detected_postings"] = det

            if job_rows:
                buf = io.StringIO(newline="")
                wcsv = csv.writer(buf)
                for row in job_rows:
                    wcsv.writerow([session_id, *row])
                buf.seek(0)
                cur.copy_expert(
                    """
                    COPY jobs (session_id, job_link, title, company, location,
                               title_lc, location_lc, country, country_lc)
                    FROM STDIN WITH (FORMAT CSV)
                    """,
                    buf,
                )
                con.commit()

                links = [r[0] for r in job_rows]
                cur.execute(
                    "SELECT job_link, job_id FROM jobs WHERE session_id = %s AND job_link = ANY(%s)",
                    (session_id, links),
                )
                job_id_map = dict(cur.fetchall())

        # Skills-only upload: build map from existing session jobs
        if not job_id_map and skills_df is not None:
            cur.execute(
                "SELECT job_link, job_id FROM jobs WHERE session_id = %s",
                (session_id,),
            )
            job_id_map = dict(cur.fetchall())
            if not job_id_map:
                warnings.append(
                    "Skills CSV uploaded but no matching jobs in session — upload postings first or include job_link"
                )

        all_skill_names: Set[str] = set()
        js_pairs: List[Tuple] = []

        if skills_df is not None:
            det = detect_columns(skills_df) if not combined_detected else combined_detected
            names, pairs, w = parse_skills_from_df(skills_df, det, job_id_map, session_id)
            all_skill_names.update(names)
            js_pairs.extend(pairs)
            warnings.extend(w)
            stats["columns_detected_skills"] = det

        if all_skill_names:
            for s in sorted(all_skill_names):
                cur.execute(
                    "INSERT INTO skills (name, name_lc) VALUES (%s, %s) ON CONFLICT (name_lc) DO NOTHING",
                    (s, s),
                )
            con.commit()

        if js_pairs:
            cur.execute(
                """
                CREATE TEMP TABLE IF NOT EXISTS job_skills_stage(
                    session_id TEXT,
                    job_id INTEGER,
                    skill_name_lc TEXT
                ) ON COMMIT DROP
                """
            )
            buf = io.StringIO(newline="")
            wcsv = csv.writer(buf)
            for trip in js_pairs:
                wcsv.writerow(trip)
            buf.seek(0)
            cur.copy_expert(
                "COPY job_skills_stage (session_id, job_id, skill_name_lc) FROM STDIN WITH (FORMAT CSV)",
                buf,
            )
            cur.execute(
                """
                INSERT INTO job_skills(session_id, job_id, skill_id)
                SELECT DISTINCT st.session_id, st.job_id, sk.skill_id
                FROM job_skills_stage st
                JOIN skills sk ON sk.name_lc = st.skill_name_lc
                ON CONFLICT (session_id, job_id, skill_id) DO NOTHING
                """
            )
            con.commit()

        cur.execute("SELECT COUNT(*) FROM jobs WHERE session_id = %s", (session_id,))
        jobs_cnt = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM job_skills js
            JOIN jobs j ON j.job_id = js.job_id AND j.session_id = js.session_id
            WHERE js.session_id = %s
            """,
            (session_id,),
        )
        js_cnt = cur.fetchone()[0]
        con.commit()

        stats.update({"jobs_inserted": jobs_cnt, "skill_links_inserted": js_cnt})
        return {
            "ok": True,
            "session_id": session_id,
            "stats": stats,
            "warnings": warnings,
            "columns_detected": stats.get("columns_detected_postings")
            or stats.get("columns_detected_skills")
            or combined_detected,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        cur.close()
        con.close()


def ensure_session_schema(cfg: dict) -> None:
    """Apply deploy schema migrations if needed."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema_deploy.sql")
    if not os.path.isfile(schema_path):
        return
    with open(schema_path, "r", encoding="utf-8") as f:
        ddl = f.read()
    info = cfg["jobs_db"]
    con = psycopg2.connect(info["dsn"])
    con.autocommit = True
    cur = con.cursor()
    cur.execute(ddl)
    cur.close()
    con.close()