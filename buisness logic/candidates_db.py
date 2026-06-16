from __future__ import annotations
import argparse, os, re, time, json, csv
from typing import Any, Dict, List, Tuple, Iterable
from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
import psycopg2
import psycopg2.extras as pgx
from tqdm import tqdm
import pycountry
import yaml
import math
# ---------------- Helpers ----------------
UNKY = {"unknown", "not provided", ""}

def norm(x: Any) -> str:
    if x is None:
        return ""
    s = str(x).strip()
    return s

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
    d  = m.group(3).zfill(2) if m.group(3) else ""
    if y and mo and d:
        return f"{y}-{mo}-{d}"
    if y and mo:
        return f"{y}-{mo}"
    return y

def join_if_any(items: Iterable[str], sep=", "):
    vals = [clean_val(t) for t in items if clean_val(t)]
    return sep.join(vals)
# ---------------- Skill NER + Legitimacy (inline from iia_legitimacy) ----------------

MODEL_NAME = "Nucha/Nucha_ITSkillNER_BERT"  # IT Skill NER model


def load_skill_ner():
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForTokenClassification.from_pretrained(MODEL_NAME)

    skill_ner = pipeline(
        "ner",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy="simple",  # group sub-tokens into spans
    )
    return skill_ner


def canonicalize_skill(name: str) -> str:
    if not isinstance(name, str):
        return ""
    s = name.strip().lower()
    s = s.replace("c plus plus", "c++")
    s = s.replace("c sharp", "c#")
    s = s.replace("node.js", "nodejs")
    s = s.replace("react.js", "react")
    s = s.replace("reactjs", "react")
    if s in ("javascript", "java script"):
        s = "js"
    s = re.sub(r"\s+", " ", s)
    return s


def extract_claimed_skills_for_legitimacy(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Structured skills from rec['skills'] – used only for legitimacy.
    (We ignore 'unknown' style values.)
    """
    out: List[Dict[str, Any]] = []
    skills = rec.get("skills", {}) or {}
    tech = skills.get("technical", {}) or {}

    if isinstance(tech, dict):
        for category, lst in tech.items():
            if not isinstance(lst, list):
                continue
            for s in lst:
                if not isinstance(s, dict):
                    continue
                name = s.get("name")
                if not name or not isinstance(name, str):
                    continue
                if name.strip().lower() in ("unknown", "not provided"):
                    continue
                level = s.get("level", "Unknown")
                out.append(
                    {
                        "category": category,
                        "name": name,
                        "level": level,
                    }
                )

    # Optional: languages as skills
    langs = skills.get("languages", []) or []
    if isinstance(langs, list):
        for s in langs:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            if not name or not isinstance(name, str):
                continue
            if name.strip().lower() in ("unknown", "not provided"):
                continue
            level = s.get("level", "Unknown")
            out.append({"category": "language", "name": name, "level": level})

    return out


def extract_contexts_for_legitimacy(rec: Dict[str, Any]) -> Tuple[List[str], str]:
    """
    Return (structured_tokens, full_text) for a candidate.
    structured_tokens = technologies/tools/methodologies/etc.
    full_text = concatenation of responsibilities, project descriptions, etc.
    """
    texts: List[str] = []
    structured_tokens: List[str] = []

    # Experience
    for exp in rec.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue

        for resp in exp.get("responsibilities", []) or []:
            if isinstance(resp, str):
                texts.append(resp)

        te = exp.get("technical_environment", {}) or {}
        if isinstance(te, dict):
            for key in ["technologies", "tools", "methodologies", "databases", "operating_systems"]:
                vals = te.get(key, []) or []
                if isinstance(vals, list):
                    for v in vals:
                        if isinstance(v, str):
                            structured_tokens.append(v)
                            texts.append(v)

    # Projects
    for proj in rec.get("projects", []) or []:
        if not isinstance(proj, dict):
            continue
        for key in ["description", "impact", "role", "name"]:
            v = proj.get(key)
            if isinstance(v, str):
                texts.append(v)
        techs = proj.get("technologies", []) or []
        if isinstance(techs, list):
            for v in techs:
                if isinstance(v, str):
                    structured_tokens.append(v)
                    texts.append(v)

    # Certifications
    certs = rec.get("certifications", None)
    if isinstance(certs, str):
        texts.append(certs)
    elif isinstance(certs, list):
        for c in certs:
            if isinstance(c, str):
                texts.append(c)

    # Education
    for edu in rec.get("education", []) or []:
        if not isinstance(edu, dict):
            continue
        deg = edu.get("degree", {}) or {}
        if isinstance(deg, dict):
            for key in ["field", "major", "level"]:
                v = deg.get(key)
                if isinstance(v, str):
                    texts.append(v)
        ach = edu.get("achievements", {}) or {}
        if isinstance(ach, dict):
            rc = ach.get("relevant_coursework", []) or []
            if isinstance(rc, list):
                for v in rc:
                    if isinstance(v, str):
                        texts.append(v)

    full_text = " ".join(str(t) for t in texts if t)
    return structured_tokens, full_text


def extract_text_skills_transformer(full_text: str, skill_ner) -> Dict[str, int]:
    """
    Run skill NER on full_text and count each canonical skill occurrence.
    Returns: {canonical_skill_name: count}
    """
    if not full_text.strip():
        return {}

    ner_results = skill_ner(full_text)

    counts: Dict[str, int] = {}
    for ent in ner_results:
        # ent has keys like: {'entity_group', 'word', 'score', 'start', 'end'}
        label = ent.get("entity_group", "") or ent.get("entity", "")
        text = ent.get("word", "")
        if not isinstance(text, str):
            continue

        # Heuristic: keep entities whose label looks skill-related
        if "skill" not in label.lower() and "tech" not in label.lower():
            continue

        canon = canonicalize_skill(text)
        if not canon:
            continue

        counts[canon] = counts.get(canon, 0) + 1

    return counts


def base_evidence_score(total_evidence: int, alpha: float = math.log(2.0)) -> float:
    """
    Map evidence count to [0,1]:
      - 0 -> 0
      - 1 -> ~0.5
      - 2 -> ~0.75, etc.
    """
    if total_evidence <= 0:
        return 0.0
    return 1.0 - math.exp(-alpha * total_evidence)


def compute_candidate_score(rec: Dict[str, Any], skill_ner) -> Tuple[float, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Uses transformer-based text skill extraction + structured tokens.

    Candidate-level rules:
      - 0.0: no skills section & no skills extracted from text
      - 0.5: no skills section but skills extracted from text
      - 0.4: skills in section but declared skills have zero evidence
      - else: evidence-based (per-skill score = 0.5 + 0.5 * base_evidence_score)
    """
    claimed = extract_claimed_skills_for_legitimacy(rec)
    structured_tokens, full_text = extract_contexts_for_legitimacy(rec)

    # text skills via transformer
    text_skill_counts = extract_text_skills_transformer(full_text, skill_ner)

    declared_canons = {
        canonicalize_skill(s.get("name", ""))
        for s in claimed
        if s.get("name")
    }
    declared_canons = {c for c in declared_canons if c}

    has_claimed = len(declared_canons) > 0
    has_text_skills = len(text_skill_counts) > 0

    details: List[Dict[str, Any]] = []
    declared_total_evidence = 0

    # ----- Declared skills -----
    for s in claimed:
        name = s.get("name")
        if not name:
            continue
        canon = canonicalize_skill(name)

        # structured count: how many structured tokens match canonical name
        struct_count = sum(
            1 for tok in structured_tokens
            if canonicalize_skill(tok) == canon
        )
        text_count = text_skill_counts.get(canon, 0)
        total_ev = struct_count + text_count
        declared_total_evidence += total_ev
        b_score = base_evidence_score(total_ev)

        details.append(
            {
                "origin": "declared",
                "category": s.get("category", "technical"),
                "name": name,
                "canonical_name": canon,
                "level": s.get("level", "Unknown"),
                "structured_evidence": struct_count,
                "text_evidence": text_count,
                "total_evidence": total_ev,
                "base_evidence_score": b_score,
            }
        )

    # ----- Text-only skills (present in text but not declared) -----
    text_only_canons = [
        c for c in text_skill_counts.keys()
        if c not in declared_canons
    ]
    for canon in text_only_canons:
        struct_count = sum(
            1 for tok in structured_tokens
            if canonicalize_skill(tok) == canon
        )
        text_count = text_skill_counts.get(canon, 0)
        total_ev = struct_count + text_count
        b_score = base_evidence_score(total_ev)

        details.append(
            {
                "origin": "text_only",
                "category": "text_only",
                "name": canon,
                "canonical_name": canon,
                "level": "Unknown",
                "structured_evidence": struct_count,
                "text_evidence": text_count,
                "total_evidence": total_ev,
                "base_evidence_score": b_score,
            }
        )

    # ----- Apply rules -----

    # 0.0: no skills mentioned + no skills extracted
    if not has_claimed and not has_text_skills:
        legitimacy = 0.0
        reason = "no_skills_anywhere"

    # 0.5: no skills section but skills in text
    elif not has_claimed and has_text_skills:
        legitimacy = 0.5
        reason = "text_skills_only"

    # 0.4: skills in section but no evidence in body for them
    elif has_claimed and declared_total_evidence == 0:
        legitimacy = 0.4
        reason = "declared_but_no_evidence"

    # Evidence-based
    else:
        reason = "evidence_based"
        if details:
            for d in details:
                d["final_skill_score"] = 0.5 + 0.5 * d["base_evidence_score"]
            legitimacy = sum(d["final_skill_score"] for d in details) / len(details)
        else:
            legitimacy = 0.0

    meta = {
        "has_claimed_skills": has_claimed,
        "has_text_skills": has_text_skills,
        "declared_total_evidence": declared_total_evidence,
        "reason": reason,
        # we also return the raw text_skill_counts so caller can add derived skills
        "text_skill_counts": text_skill_counts,
    }

    return legitimacy, details, meta

def add_derived_skills(cur_local, cand_id: int, skills: Iterable[str], source: str):
    """
    Insert derived skills (from projects/experience/auto-extracted) into
    candidate_derived_skills, without consulting jobs_db.
    """
    rows = []
    for s in skills:
        s_clean = clean_val(s)
        if s_clean:
            rows.append((cand_id, s_clean, source))

    if not rows:
        return

    # de-duplicate per (skill_name, source) for this candidate
    uniq = {}
    for cid, name, src in rows:
        uniq[(name, src)] = True

    rows2 = [(cand_id, name, src) for (name, src) in uniq.keys()]
    pgx.execute_values(
        cur_local,
        "INSERT INTO candidate_derived_skills(cand_id, skill_name, source) VALUES %s",
        rows2,
        page_size=5000,
    )

COUNTRY_MAP = {c.name.lower(): c.name for c in pycountry.countries}
COUNTRY_MAP.update({
    "usa": "United States",
    "us": "United States",
    "united states of america": "United States",
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
    "remote": None,
})

def canonical_country(loc: str | None) -> str | None:
    if not loc:
        return None
    parts = loc.split(",")
    candidate = parts[-1].strip().lower() if parts else ""
    return COUNTRY_MAP.get(candidate, None)

# ---------------- Schema ----------------
DDL = """
DROP TABLE IF EXISTS candidates;
DROP TABLE IF EXISTS candidate_skills;
DROP TABLE IF EXISTS experience;
DROP TABLE IF EXISTS education;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS languages;
DROP TABLE IF EXISTS candidate_skill_legitimacy;
DROP TABLE IF EXISTS candidate_derived_skills;

CREATE TABLE IF NOT EXISTS candidates(
    cand_id BIGSERIAL PRIMARY KEY,
    name TEXT,
    email TEXT,
    phone TEXT,
    location TEXT,
    location_lc TEXT,
    country TEXT,
    country_source TEXT,
    summary TEXT,
    certifications TEXT,
    search_blob TEXT
);

CREATE TABLE IF NOT EXISTS candidate_skills(
    cand_id BIGINT,
    skill_id BIGINT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS experience(
    cand_id BIGINT,
    company TEXT,
    title TEXT,
    location TEXT,
    location_lc TEXT,
    start_date TEXT,
    end_date TEXT,
    duration TEXT,
    responsibilities TEXT
);

CREATE TABLE IF NOT EXISTS education(
    cand_id BIGINT,
    institution TEXT,
    degree TEXT,
    major TEXT,
    start_date TEXT,
    end_date TEXT,
    gpa TEXT,
    honors TEXT,
    accreditation TEXT
);

CREATE TABLE IF NOT EXISTS projects(
    cand_id BIGINT,
    name TEXT,
    role TEXT,
    description TEXT,
    impact TEXT,
    url TEXT,
    technologies TEXT
);

CREATE TABLE IF NOT EXISTS languages(
    cand_id BIGINT,
    language TEXT,
    level TEXT
);

CREATE TABLE IF NOT EXISTS candidate_skill_legitimacy(
    cand_id BIGINT PRIMARY KEY,
    legitimacy_score DOUBLE PRECISION,
    num_skills_total INT,
    has_claimed_skills BOOLEAN,
    has_text_skills BOOLEAN,
    declared_total_evidence INT,
    reason TEXT
);

-- Derived / auto-extracted skills that are NOT matched to job skills
CREATE TABLE IF NOT EXISTS candidate_derived_skills(
    cand_id BIGINT,
    skill_name TEXT,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_cand_loc ON candidates(location_lc);
CREATE INDEX IF NOT EXISTS idx_cand_country ON candidates(country);
CREATE INDEX IF NOT EXISTS idx_exp_loc ON experience(location_lc);
CREATE INDEX IF NOT EXISTS idx_cskill_cid ON candidate_skills(cand_id);

-- Optional but useful for full-text search on search_blob
CREATE INDEX IF NOT EXISTS idx_cand_search_blob
ON candidates
USING GIN (to_tsvector('english', search_blob));
"""

# ---------------- Cross-DB Skill Mapper ----------------
def get_matching_skill_id(cur_jobs, name: str) -> int | None:
    """
    Return skill_id ONLY if the skill already exists in the jobs skills table.
    NO insertion into jobs_db.skills here.
    """
    name = clean_val(name)
    if not name:
        return None
    name_lc = to_lc(name)

    cur_jobs.execute("SELECT skill_id FROM skills WHERE name_lc=%s", (name_lc,))
    row = cur_jobs.fetchone()
    return row[0] if row else None


def add_skill_list(cur_local, cur_jobs, cand_id: int, skills: Iterable[str], source: str):
    """
    For declared/experience skills:
      - Only insert into candidate_skills if the skill exists in jobs_db.skills.
    """
    rows = []
    for s in skills:
        sid = get_matching_skill_id(cur_jobs, s)
        if sid is not None:
            rows.append((cand_id, sid, source))
    if rows:
        pgx.execute_values(
            cur_local,
            "INSERT INTO candidate_skills(cand_id, skill_id, source) VALUES %s",
            rows,
            page_size=5000
        )

# ---------------- Extraction Helpers ----------------
def extract_core(rec: Dict[str, Any]) -> Dict[str, str]:
    pi = rec.get("personal_info", {}) or {}
    name = clean_val(pi.get("name"))
    email = clean_val(pi.get("email"))
    phone = clean_val(pi.get("phone"))
    loc = pi.get("location") or {}
    city = clean_val(loc.get("city"))
    country_raw = clean_val(loc.get("country"))
    location = join_if_any([city, country_raw], sep=", ")
    country_val = canonical_country(location)
    summary = clean_val(pi.get("summary"))
    certs = clean_val(rec.get("certifications"))
    return dict(
        name=name, email=email, phone=phone,
        location=location, location_lc=to_lc(location),
        country=country_val, summary=summary, certifications=certs
    )

def extract_skills(rec: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    sk = rec.get("skills") or {}
    tech = sk.get("technical") or {}

    def pull_obj_name_list(arr) -> List[str]:
        vals = []
        for it in as_list(arr):
            if isinstance(it, dict):
                vals.append(clean_val(it.get("name")))
            else:
                vals.append(clean_val(it))
        return [v for v in vals if v]

    buckets = {
        "technical.programming_languages": tech.get("programming_languages"),
        "technical.frameworks": tech.get("frameworks"),
        "technical.databases": tech.get("databases"),
        "technical.cloud": tech.get("cloud"),
        "technical.tools": tech.get("tools"),
        "technical.operating_systems": tech.get("operating_systems"),
        "technical.testing": tech.get("testing"),
        "technical.other": tech.get("other"),
        "technical.automation": tech.get("automation"),
        "technical.project_management": tech.get("project_management"),
        "technical.software": tech.get("software"),
        "technical.software_tools": tech.get("software_tools"),
        "technical.web_technologies": tech.get("web_technologies"),
        # auto-extracted transformer skills — will be treated separately
        "technical.auto_extracted": tech.get("auto_extracted"),
    }
    for cat, arr in buckets.items():
        out[cat] = pull_obj_name_list(arr)
    return out

def extract_languages(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    langs = []
    sk = rec.get("skills") or {}
    for obj in as_list(sk.get("languages")):
        if isinstance(obj, dict):
            name = clean_val(obj.get("name") or obj.get("language"))
            lvl  = clean_val(obj.get("level"))
        else:
            name = clean_val(obj); lvl = ""
        if name:
            langs.append((name, lvl))
    return langs

def extract_experience(rec: Dict[str, Any]):
    rows = []
    extra_skills: Dict[str, List[str]] = {}
    for e in as_list(rec.get("experience")):
        if not isinstance(e, dict):
            continue
        company = clean_val(e.get("company"))
        title   = clean_val(e.get("title"))
        eloc    = clean_val(e.get("location"))
        dates   = e.get("dates") or {}
        start   = iso(dates.get("start"))
        end     = iso(dates.get("end"))
        duration= clean_val(dates.get("duration"))
        resp_list = as_list(e.get("responsibilities"))
        resp = join_if_any(resp_list, sep=" | ")
        rows.append(dict(
            company=company, title=title, location=eloc, location_lc=to_lc(eloc),
            start_date=start, end_date=end, duration=duration, responsibilities=resp
        ))
        tenv = e.get("technical_environment") or {}

        def pull_env(cat_key, arr_key):
            cat = f"experience.tech_env.{cat_key}"
            arr = tenv.get(arr_key)
            vals = []
            for t in as_list(arr):
                vals.append(clean_val(t))
            vals = [v for v in vals if v]
            if vals:
                extra_skills.setdefault(cat, []).extend(vals)

        # We treat these as "mentioned" skills and try to match to job skills
        pull_env("technologies", "technologies")
        pull_env("tools", "tools")
        pull_env("methodologies", "methodologies")
        pull_env("operating_systems", "operating_systems")
        pull_env("databases", "databases")
    return rows, extra_skills

def extract_education(rec: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for ed in as_list(rec.get("education")):
        if not isinstance(ed, dict):
            continue
        degree = ed.get("degree") or {}
        institution = ed.get("institution") or {}
        dates = ed.get("dates") or {}
        ach = ed.get("achievements") or {}
        rows.append(dict(
            institution   = clean_val(institution.get("name")),
            degree        = clean_val(degree.get("level")),
            major         = clean_val(degree.get("field") or ed.get("major") or ed.get("degree")),
            start_date    = iso(dates.get("start")),
            end_date      = iso(dates.get("end") or dates.get("expected_graduation")),
            gpa           = clean_val(ach.get("gpa")),
            honors        = clean_val(ach.get("honors")),
            accreditation = clean_val(institution.get("accreditation")),
        ))
    return rows

def extract_projects(rec: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    for p in as_list(rec.get("projects")):
        if not isinstance(p, dict):
            continue
        techs = join_if_any(as_list(p.get("technologies")))
        rows.append(dict(
            name        = clean_val(p.get("name") or p.get("title")),
            role        = clean_val(p.get("role")),
            description = clean_val(p.get("description")),
            impact      = clean_val(p.get("impact")),
            url         = clean_val(p.get("url")),
            technologies= techs,
        ))
    return rows

def build_search_blob(rec: Dict[str, Any]) -> str:
    """
    Denormalized text field to make project/summary/experience based search easier.
    """
    parts: List[str] = []

    # Personal info
    pi = rec.get("personal_info") or {}
    parts.append(clean_val(pi.get("summary")))
    parts.append(clean_val(rec.get("certifications")))

    # Skills: technical + languages
    sk = rec.get("skills") or {}
    tech = sk.get("technical") or {}

    def add_tech(arr):
        for it in as_list(arr):
            if isinstance(it, dict):
                parts.append(clean_val(it.get("name")))
            else:
                parts.append(clean_val(it))

    for key in (
        "programming_languages","frameworks","databases","cloud","tools",
        "operating_systems","testing","other","automation","project_management",
        "software","software_tools","web_technologies","auto_extracted"
    ):
        add_tech(tech.get(key))

    for obj in as_list(sk.get("languages")):
        if isinstance(obj, dict):
            parts.append(clean_val(obj.get("name") or obj.get("language")))
        else:
            parts.append(clean_val(obj))

    # Experience: company, title, responsibilities, technical_environment
    for e in as_list(rec.get("experience")):
        if not isinstance(e, dict):
            continue
        parts.append(clean_val(e.get("company")))
        parts.append(clean_val(e.get("title")))
        for r in as_list(e.get("responsibilities")):
            parts.append(clean_val(r))
        tenv = e.get("technical_environment") or {}
        for k in ("technologies","tools","databases","operating_systems","methodologies"):
            for t in as_list(tenv.get(k)):
                parts.append(clean_val(t))

    # Projects: name/title, description, technologies
    for p in as_list(rec.get("projects")):
        if not isinstance(p, dict):
            continue
        parts.append(clean_val(p.get("name") or p.get("title")))
        parts.append(clean_val(p.get("description")))
        for t in as_list(p.get("technologies")):
            parts.append(clean_val(t))

    # Education: institution, degree, field, relevant coursework
    for ed in as_list(rec.get("education")):
        if not isinstance(ed, dict):
            continue
        degree = ed.get("degree") or {}
        institution = ed.get("institution") or {}
        parts.append(clean_val(institution.get("name")))
        parts.append(clean_val(degree.get("field")))
        parts.append(clean_val(degree.get("level")))
        ach = ed.get("achievements") or {}
        for c in as_list(ach.get("relevant_coursework")):
            parts.append(clean_val(c))

    tokens = [clean_val(p) for p in parts if clean_val(p)]
    return " ".join(tokens)

# ---------------- Simple JSONL Loader ----------------
def load_jsonl(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records

# ---------------- Legitimacy CSV Loader ----------------
def load_legitimacy_map(path: str) -> Dict[int, Dict[str, Any]]:
    """
    Load candidate_skill_legitimacy_scores_transformer.csv into a dict:
    {candidate_id (0-based index): {...columns...}}
    """
    mp: Dict[int, Dict[str, Any]] = {}
    if not path or not os.path.exists(path):
        return mp

    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int((row.get("candidate_id") or "").strip())
            except (TypeError, ValueError):
                continue

            def as_int(key, default=0):
                try:
                    return int(row.get(key, default) or default)
                except (TypeError, ValueError):
                    return default

            def as_float(key, default=0.0):
                try:
                    return float(row.get(key, default) or default)
                except (TypeError, ValueError):
                    return default

            def as_bool(key):
                v = (row.get(key, "") or "").strip().lower()
                return v in ("true", "t", "1", "yes", "y")

            mp[idx] = {
                "legitimacy_score": as_float("legitimacy_score"),
                "num_skills_total": as_int("num_skills_total"),
                "has_claimed_skills": as_bool("has_claimed_skills"),
                "has_text_skills": as_bool("has_text_skills"),
                "declared_total_evidence": as_int("declared_total_evidence"),
                "reason": row.get("reason", "") or "",
            }
    return mp

# ---------------- Main Ingest ----------------
def ingest_hf(cfg: Dict[str, Any]):
    # Connect both DBs
    info_local = cfg["candidates_db"]
    job_info = cfg["jobs_db"]

    con_local = psycopg2.connect(info_local["dsn"])
    con_jobs  = psycopg2.connect(job_info["dsn"])
    cur_local = con_local.cursor()
    cur_jobs  = con_jobs.cursor()

    con_local.autocommit = False
    con_jobs.autocommit  = False

    # Schema for local candidates DB
    cur_local.execute(DDL)
    con_local.commit()

    # Prefer enriched JSONL if provided, else fall back to original
    # enriched_path = cfg.get("candidates_enriched_jsonl")
    raw_path      = cfg.get("candidates_csv")  # e.g. data/master_resumes.jsonl

    # if enriched_path and os.path.exists(enriched_path):
    #     print(f"Loading candidates from enriched JSONL: {enriched_path}")
    #     ds = load_jsonl(enriched_path)
    if raw_path and os.path.exists(raw_path):
        print(f"Loading candidates from raw JSONL: {raw_path}")
        ds = load_jsonl(raw_path)
    else:
        raise ValueError("No existing candidates_enriched_jsonl or candidates_csv configured in config.yaml")
    print("Loading transformer skill NER model for legitimacy scoring...")
    skill_ner = load_skill_ner()
    print("Model loaded.")
    # Optional legitimacy scores CSV produced by transformer script
    # legit_path = cfg.get("candidate_legitimacy_csv")
    # legitimacy_map: Dict[int, Dict[str, Any]] = {}
    # if legit_path:
    #     print(f"Loading legitimacy scores from {legit_path!r} (if exists)...")
    #     legitimacy_map = load_legitimacy_map(legit_path)
    #     print(f"Loaded legitimacy scores for {len(legitimacy_map)} candidates.")
    # else:
    #     print("No candidate_legitimacy_csv configured; skipping candidate_skill_legitimacy.")

    start = time.time()
    for idx, rec in enumerate(tqdm(ds, desc="Inserting candidates", unit="record")):
        core = extract_core(rec)
        search_blob = build_search_blob(rec)

        cur_local.execute(
            """INSERT INTO candidates(name,email,phone,location,location_lc,country,summary,certifications,search_blob)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING cand_id""",
            (
                core["name"], core["email"], core["phone"],
                core["location"], core["location_lc"], core["country"],
                core["summary"], core["certifications"], search_blob
            )
        )
        cand_id = cur_local.fetchone()[0]

        # --- Real-time legitimacy scoring + transformer-based skills ---
        legitimacy_score, skill_details, meta = compute_candidate_score(rec, skill_ner)

        cur_local.execute(
            """
            INSERT INTO candidate_skill_legitimacy(
                cand_id,
                legitimacy_score,
                num_skills_total,
                has_claimed_skills,
                has_text_skills,
                declared_total_evidence,
                reason
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                cand_id,
                float(legitimacy_score),
                len(skill_details),
                bool(meta["has_claimed_skills"]),
                bool(meta["has_text_skills"]),
                int(meta["declared_total_evidence"]),
                meta["reason"],
            ),
        )

        # Also treat transformer-extracted skills as derived skills
        # (canonical names), regardless of jobs_db.skills.
        text_skill_counts = meta.get("text_skill_counts", {}) or {}
        if text_skill_counts:
            transformer_skills = list(text_skill_counts.keys())
            add_derived_skills(cur_local, cand_id, transformer_skills, source="transformer.ner")

        # technical skills
        tech_sk = extract_skills(rec)
        for cat, vals in tech_sk.items():
            if cat == "technical.auto_extracted":
                # Auto-extracted (transformer) skills are treated as *derived* evidence,
                # not as primary claimed skills.
                add_derived_skills(cur_local, cand_id, vals, source=cat)
            else:
                # EXPLICIT skills section on the resume:
                # keep these as primary skills, but only if they exist in jobs_db.skills.
                add_skill_list(cur_local, cur_jobs, cand_id, vals, source=cat)

        # experience + environment (these are evidence skills -> derived table)
        exp_rows, env_sk = extract_experience(rec)
        if exp_rows:
            rows = [
                (cand_id, r["company"], r["title"], r["location"], r["location_lc"],
                 r["start_date"], r["end_date"], r["duration"], r["responsibilities"])
                for r in exp_rows
            ]
            pgx.execute_values(
                cur_local,
                """INSERT INTO experience(cand_id,company,title,location,location_lc,start_date,end_date,duration,responsibilities)
                   VALUES %s""",
                rows, page_size=5000
            )

        # ⬇️ Now treat technical_environment skills as *derived*, regardless of jobs_db.skills
        for cat, vals in env_sk.items():
            add_derived_skills(cur_local, cand_id, vals, source=cat)


        # education
        ed_rows = extract_education(rec)
        if ed_rows:
            rows = [
                (cand_id, ed["institution"], ed["degree"], ed["major"], ed["start_date"],
                 ed["end_date"], ed["gpa"], ed["honors"], ed["accreditation"])
                for ed in ed_rows
            ]
            pgx.execute_values(
                cur_local,
                """INSERT INTO education(cand_id,institution,degree,major,start_date,end_date,gpa,honors,accreditation)
                   VALUES %s""",
                rows, page_size=5000
            )

        # projects
        pr_rows = extract_projects(rec)
        if pr_rows:
            rows = [
                (cand_id, p["name"], p["role"], p["description"], p["impact"], p["url"], p["technologies"])
                for p in pr_rows
            ]
            pgx.execute_values(
                cur_local,
                """INSERT INTO projects(cand_id,name,role,description,impact,url,technologies)
                   VALUES %s""",
                rows, page_size=5000
            )

            # Also treat project technologies as derived skills, even if they
            # don't exist in jobs_db.skills.
            proj_skills: List[str] = []
            for p in pr_rows:
                tech_str = p.get("technologies") or ""
                if not tech_str:
                    continue
                # Split on common separators: comma, slash, or vertical bar
                for token in re.split(r"[,/|]", tech_str):
                    token = token.strip()
                    if token:
                        proj_skills.append(token)

            add_derived_skills(cur_local, cand_id, proj_skills, source="projects.technologies")


        # languages
        langs = extract_languages(rec)
        if langs:
            pgx.execute_values(
                cur_local,
                "INSERT INTO languages(cand_id,language,level) VALUES %s",
                [(cand_id, lang, lvl) for (lang, lvl) in langs],
                page_size=5000
            )

    elapsed = time.time() - start
    print(f"✅ Ingested {len(ds):,} candidates in {elapsed/60:.2f} minutes.")
    con_local.commit(); con_jobs.commit()
    cur_local.close(); cur_jobs.close()
    con_local.close(); con_jobs.close()
    print("✅ Ingest complete. Skills mapped to job_market DB (only for skills that exist in jobs).")
    print("✅ Derived / auto-extracted skills stored in candidate_derived_skills.")

# ---------------- Entrypoint ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    ingest_hf(cfg)

if __name__ == "__main__":
    main()
