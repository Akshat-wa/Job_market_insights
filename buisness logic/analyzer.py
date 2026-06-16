#analyser.py
import regex as re
from dataclasses import dataclass
from typing import Literal, Optional
import json
from typing import Any, Dict, List


# @dataclass
# class ParsedQuery:
#     mode: Literal["SPJ","AGG"]
#     task: str
#     # Core “business” slots
#     role: Optional[str] = None
#     location: Optional[str] = None
#     skill: Optional[str] = None
#     topk: Optional[int] = None
#     llm_subquery: Optional[str] = None
#     min_years: Optional[float] = None
#     skills_text: Optional[str] = None

#     # Candidate-specific
#     candidate_name: Optional[str] = None
#     candidate_id: Optional[int] = None
#     project_query: Optional[str] = None

#     # Your new 3-part decomposition:
#     # 1) attributes to SELECT
#     select_attributes: Optional[list[str]] = None
#     # 3) blocking parameters (WHERE-style filters)
#     filters: Optional[dict] = None
#     is_multi_subplan: bool = False


@dataclass
class ParsedQuery:
    # Core
    mode: str                     # "SPJ", "AGG", "LLM"
    task: str                     # e.g. "filter_candidates_by_location"

    # Generic SQL shape
    select_attributes: Optional[List[str]] = None
    filters: Optional[Dict[str, Any]] = None
    domain: Optional[str] = None          # "candidates", "jobs", or None

    # High-level semantics
    role: Optional[str] = None            # role / job title phrase
    location: Optional[str] = None        # country / city phrase
    min_years: Optional[int] = None       # years of experience (for candidates)
    skill: Optional[str] = None           # single focus skill (for counts)
    skills: Optional[List[str]] = None
    skills_text: Optional[str] = None     # free-text skill list ("Python, SQL, Spark")
    topk: Optional[int] = None
    project_query: Optional[str] = None   # free-text project topic ("fraud detection")
    candidate_id: Optional[int] = None    # specific candidate ID
    candidate_name: Optional[str] = None  # human name for LLM summaries, if available
    topk: Optional[int] = None            # e.g., top N results to fetch
    is_multi_subplan: bool = False        # mark cloned subplans for federated queries

    # LLM extras
    llm_subquery: Optional[str] = None    # natural language hint for LLM enrichment
TASK_DOMAIN_MAP = {
    # --- candidate-only tasks ---
    "filter_candidates_by_location": "candidates",
    "filter_candidates_by_experience_role": "candidates",
    "filter_candidates_by_skills": "candidates",
    "filter_candidates_by_projects": "candidates",
    "top_candidates_by_skill_count": "candidates",
    "candidate_readiness": "candidates",
    "candidate_multi": "candidates",   # synthetic multi-candidate plan

    # --- candidate–jobs mixed tasks ---
    "candidate_profile_and_eligible_jobs": "both",
    "eligible_companies_for_candidate": "both",

    # --- jobs-only tasks ---
    "list_jobs_for_role": "jobs",
    "filter_jobs_by_role": "jobs",
    "filter_jobs_by_skills": "jobs",
    "filter_jobs_by_location": "jobs",
    "count_jobs_by_skill": "jobs",
    "top_skills_for_role": "jobs",
    "similar_roles_for_role": "jobs",
    "similar_skills_for_skill": "jobs",
    "jobs_multi": "jobs",              # synthetic multi-jobs plan
}
def _with_domain(**kwargs) -> ParsedQuery:
    """
    Convenience helper: infer domain from task using TASK_DOMAIN_MAP
    and build a ParsedQuery with domain set.
    """
    task = kwargs.get("task")
    dom = TASK_DOMAIN_MAP.get(task)
    return ParsedQuery(domain=dom, **kwargs)

def _strip(x: Optional[str]) -> Optional[str]:
    if not x:
        return x
    x = x.strip()
    # Drop trailing sentence punctuation like "United States." -> "United States"
    x = re.sub(r"[?.!]+$", "", x).strip()
    return x

def _normalize_role(role: Optional[str]) -> Optional[str]:
    """
    Clean up role phrases like 'backend engineer roles' -> 'backend engineer',
    'data scientist jobs' -> 'data scientist', etc.
    """
    role = _strip(role)
    if not role:
        return role

    rl = role.lower()
    # strip trailing generic words that aren't part of the title
    for suffix in (" role", " roles", " job", " jobs", " position", " positions", " opening", " openings", "vacancy", " vacancies"):
        if rl.endswith(suffix):
            role = role[: -len(suffix)].strip()
            rl = role.lower()
            break

    return role

def parse(nlq: str) -> ParsedQuery | None:
    q = nlq.strip()

    # --- "people to ... top N potential candidates" -> candidate_readiness (generic) ---
    #
    # Handles queries like:
    #   "i am looking for people to build my website, for what job roles should i hire for and top 10 potential candidates"
    #   "looking for people to build a mobile app and top 5 potential candidates"
    #
    m = re.search(
        r"(?i)people\s+to\s+(?P<intent>.+?)"
        r"(?:,|\band\b|\?|$).*?"
        r"(?:top\s+(?P<topk>\d{1,3}))\s+potential\s+candidates",
        q,
    )
    if m:
        intent_raw = _strip(m.group("intent")) or ""
        intent = intent_raw.lower()

        # Map common intents to roles (reusable, not tied to a single sentence)
        ROLE_HINTS = [
            # website / web presence
            (r"\bweb\s*site\b|\bwebsite\b|\bweb\s+page\b", "web developer"),
            (r"\bfrontend\b|\bfront[- ]end\b", "frontend developer"),
            (r"\bbackend\b|\bback[- ]end\b", "backend developer"),
            # mobile
            (r"\bmobile\b|\bandroid\b|\bios\b|\biphone\b", "mobile app developer"),
            # data / pipelines
            (r"\bdata\s+pipeline\b|\bdata\s+warehouse\b|\betl\b", "data engineer"),
        ]

        role: Optional[str] = None
        for pat, rname in ROLE_HINTS:
            if re.search(pat, intent):
                role = rname
                break

        # Fallback: if we don't hit a hint, treat the last 2–3 words of intent as a role-ish phrase
        if role is None:
            words = [w for w in intent_raw.split() if w]
            if len(words) >= 3:
                role = " ".join(words[-3:])
            elif len(words) >= 1:
                role = " ".join(words)
            else:
                role = "general developer"  # ultra-safe default

        topk_s = m.group("topk")
        topk = int(topk_s) if topk_s else 10

        return _with_domain(
            mode="AGG",
            task="candidate_readiness",
            role=role,
            location=None,
            topk=topk,
            llm_subquery=f"Give brief interview prep pointers for '{role}'",
        )

    # --- AGG: count jobs requiring a skill [in <loc>] ---
    m = re.search(
        r"(?i)\b(how many|count)\s+jobs.*?\b(requir(?:e|ing)|with|need(?:ing)?)\s+(?P<skill>[\w +#.\-]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        skill = _strip(m.group("skill"))
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="AGG",
            task="count_jobs_by_skill",
            skill=skill,
            location=loc,
            llm_subquery=f"Give recent commentary about demand and market context for skill '{skill}'"
        )

    # # --- AGG: Top-K skills for a ROLE [in <loc>] ---
    # m = re.search(
    #     r"(?i)\b(?:top|most\s+common)\s+(?P<topk>\d{1,3})\s+skills.*?\b(?:for|of)\s+(?P<role>[\w +#.\-]+?)"
    #     r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
    #     q,
    # )
    # if m:
    #     role = _strip(m.group("role"))
    #     loc = _strip(m.group("loc"))
    #     return ParsedQuery(
    #         mode="AGG",
    #         task="top_skills_for_role",
    #         topk=int(m.group("topk")),
    #         role=role,
    #         location=loc,
    #         llm_subquery=f"Summarize emerging tools and trends for the role '{role}'"
    #     )
    # --- AGG: Top-K skills for a ROLE [in <loc>] ---

    # Case 1: explicit "top N ..." or "top N most common ..."
    # Examples:
    #   "Top 10 skills for backend engineer roles"
    #   "What are the top 5 most common skills for data scientist roles in India?"
    m = re.search(
        r"(?i)\b(?:top|most\s+common)\s+(?P<topk>\d{1,3})\s+skills.*?\b(?:for|of)\s+(?P<role>[\w +#.\-]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        raw_role = m.group("role")
        role = _normalize_role(raw_role)
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="AGG",
            task="top_skills_for_role",
            topk=int(m.group("topk")),
            role=role,
            location=loc,
            llm_subquery=f"Summarize emerging tools and trends for the role '{role}'"
        )


    # Case 2: "most common skills for X" or "top skills for X" (no explicit N → default 10)
    # Examples:
    #   "Most common skills for backend engineer roles"
    #   "Top skills for machine learning engineer in Europe"
    m = re.search(
        r"(?i)\b(?:top|most\s+common)\s+skills.*?\b(?:for|of)\s+"
        r"(?P<role>[\w +#.\-]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        role = _strip(m.group("role"))
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="AGG",
            task="top_skills_for_role",
            topk=10,  # sensible default when user didn't specify a number
            role=role,
            location=loc,
            llm_subquery=f"Summarize emerging tools and trends for the role '{role}'",
        )

    # --- SPJ: list jobs for ROLE [in <loc>] ---
    m = re.search(
        r"(?i)\b(list|show)\s+jobs.*?\b(?:for|as)\s+(?P<role>[\w +#.\-]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        role = _strip(m.group("role"))
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="SPJ",
            task="list_jobs_for_role",
            role=role,
            location=loc,
            llm_subquery=f"Extract short advice for candidates applying to '{role}' roles"
        )

    # --- Candidates readiness (robust) ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*candidates?\s*"
        r"(?:who\s+are\s+|are\s+)?"
        r"(?:ready|prepared|fit|matching|match(?:ed)?)\s*"
        r"(?:for\s+)?"
        r"(?P<role>[\w +#./&\-\(\)]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?"
        r"(?:\s+(?:top|best)\s+(?P<topk>\d{1,3}))?"
        r"\s*[?.!]*\s*$",
        q,
    )
    if m:
        role = _strip(m.group("role"))
        loc  = _strip(m.group("loc"))
        topk = m.group("topk")
        ALIASES = {
            "ml": "machine learning",
            "ds": "data science",
            "de": "data engineer",
            "sde": "software engineer",
            "be": "backend engineer",
            "fe": "frontend engineer",
            "software devlopment": "software development",
        }
        role_lc = (role or "").lower().strip()
        role = ALIASES.get(role_lc, role)
        return _with_domain(
            mode="AGG",
            task="candidate_readiness",
            role=role,
            location=loc,
            topk=int(topk) if topk else None,
            llm_subquery=f"Give brief interview prep pointers for '{role}'"
        )

    # --- Filter candidates by min years + role/location ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*candidates?.*?"
        r"(?:at\s+least|minimum|min|>=)\s*(?P<yrs>\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of\s+)?experience"
        r"(?:\s*(?:in|for)\s+(?P<role>[\w +#./&\-\(\)]+))?"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        role = _strip(m.group("role"))
        loc  = _strip(m.group("loc"))
        yrs  = float(m.group("yrs"))
        return _with_domain(
            mode="AGG",
            task="filter_candidates_by_experience_role",
            role=role,
            location=loc,
            min_years=yrs,
            llm_subquery=(f"Summarize key screening cues for {yrs}+ years in '{role}'" if role else
                          f"Summarize key screening cues for {yrs}+ years professional experience"),
        )

    # --- Eligible companies for a candidate (role/loc; optional inline skills) ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*companies?\s+(?:am\s+i\s+|i\s+am\s+)?eligible\s+for"
        r"(?:\s+(?:as|for))?\s+(?P<role>[\w +#./&\-\(\)]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?"
        r"(?:.*?\bskills?\s*:\s*(?P<skills>[^|]+))?\s*[?.!]*$",
        q,
    )
    if m:
        role = _strip(m.group("role"))
        loc  = _strip(m.group("loc"))
        skills_text = _strip(m.group("skills"))
        return _with_domain(
            mode="SPJ",
            task="eligible_companies_for_candidate",
            role=role,
            location=loc,
            skills_text=skills_text,
            llm_subquery=f"Give concise screening heuristics for eligibility to '{role}' roles",
        )

    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*candidates?.*?"
        r"(?:with|having)\s+skills?\s*:\s*(?P<skills>[^?!.]+?)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        skills_text = _strip(m.group("skills"))
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="SPJ",
            task="filter_candidates_by_skills",
            location=loc,
            skills_text=skills_text,
            llm_subquery=f"Suggest how to screen candidates with skills: {skills_text}",
        )

    # --- Candidates by location only (no experience filter) ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*candidates?\s+"
        r"(?:in|from|based\s+in)\s+(?P<loc>[\w .,\-]+)\s*[?.!]*$",
        q,
    )
    if m:
        loc = _strip(m.group("loc"))
        return _with_domain(
            mode="SPJ",
            task="filter_candidates_by_location",
            location=loc,
            llm_subquery=f"Give quick notes on evaluating candidates located in {loc}",
        )

    # --- Candidates by projects (candidates_db: projects table) ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*candidates?.*projects?\s+"
        r"(?:about|on|related\s+to)?\s*(?P<topic>.+?)\s*[?.!]*$",
        q,
    )
    if m:
        topic = _strip(m.group("topic"))
        return _with_domain(
            mode="SPJ",
            task="filter_candidates_by_projects",
            project_query=topic,
            llm_subquery=f"Identify key skills and technologies relevant to projects about '{topic}'",
        )

    # --- Top candidates by skill count (candidates_db only) ---
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*top\s*(?P<topk>\d{1,3})\s+"
        r"candidates?\s+(?:by|with\s+the\s+most)\s+skills\s*"
        r"(?:in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        topk = int(m.group("topk"))
        loc  = _strip(m.group("loc"))
        return _with_domain(
            mode="AGG",
            task="top_candidates_by_skill_count",
            topk=topk,
            location=loc,
            llm_subquery=f"Give advice on interviewing multi-skilled candidates (top {topk})",
        )

        # --- Candidate profile + eligible jobs (federated) ---
    # Pattern 1: "Which jobs is candidate John Doe eligible for?"
    #            "What jobs is candidate 61 elidgible for?"
    m = re.search(
        r"(?i)^\s*(?:which|what|list|show)?\s*jobs\s+"
        r"(?:is|are)\s+candidate\s+(?P<who>[\w .'\-]+?)\s+"
        r"(?:eligible|elidgible|eligble|suited|suitable)\s+for"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?"
        r"(?:\s+top\s+(?P<topk>\d{1,3}))?\s*[?.!]*$",
        q,
    )
    if m:
        raw_who = _strip(m.group("who"))
        loc = _strip(m.group("loc"))
        topk = m.group("topk")

        candidate_name: Optional[str] = None
        candidate_id: Optional[int] = None

        if raw_who:
            # If there's any integer in the token (e.g. "61", "id 61"), treat it as cand_id
            nums = re.findall(r"\d+", raw_who)
            if nums:
                try:
                    candidate_id = int(nums[0])
                except ValueError:
                    candidate_id = None
            if candidate_id is None:
                candidate_name = raw_who

        label = candidate_name if candidate_name else (f"ID {candidate_id}" if candidate_id is not None else "the candidate")

        return _with_domain(
            mode="SPJ",
            task="candidate_profile_and_eligible_jobs",
            candidate_name=candidate_name,
            candidate_id=candidate_id,
            location=loc,
            topk=int(topk) if topk else None,
            llm_subquery=f"Suggest roles suitable for candidate {label} given their skills and location {loc or 'N/A'}",
            # basic 3-part structure
            select_attributes=["job_id", "job_title", "company", "job_location", "overlap_skills"],
            filters={"candidate_id": candidate_id, "candidate_name": candidate_name, "location": loc} if (candidate_id or candidate_name or loc) else None,
        )

    # Pattern 2: "Show profile for candidate John Doe" or "Show profile for candidate 61"
    m = re.search(
        r"(?i)^\s*(?:show|list)\s*(?:profile|details)?\s*"
        r"(?:for\s+)?candidate\s+(?P<who>[\w .'\-]+)"
        r"(?:\s+in\s+(?P<loc>[\w .,\-]+))?\s*[?.!]*$",
        q,
    )
    if m:
        raw_who = _strip(m.group("who"))
        loc = _strip(m.group("loc"))

        candidate_name: Optional[str] = None
        candidate_id: Optional[int] = None

        if raw_who:
            nums = re.findall(r"\d+", raw_who)
            if nums:
                try:
                    candidate_id = int(nums[0])
                except ValueError:
                    candidate_id = None
            if candidate_id is None:
                candidate_name = raw_who

        label = candidate_name if candidate_name else (f"ID {candidate_id}" if candidate_id is not None else "the candidate")

        return _with_domain(
            mode="SPJ",
            task="candidate_profile_and_eligible_jobs",
            candidate_name=candidate_name,
            candidate_id=candidate_id,
            location=loc,
            llm_subquery=f"Summarize strengths and suitable roles for candidate {label}",
            select_attributes=["candidate_profile", "eligible_jobs"],
            filters={"candidate_id": candidate_id, "candidate_name": candidate_name, "location": loc} if (candidate_id or candidate_name or loc) else None,
        )
        # --- AGG: Similar roles for a ROLE (cluster-based) ---
    # Examples:
    #   "similar roles to backend engineer"
    #   "top 10 similar roles for data scientist"
    #   "what roles are similar to machine learning engineer?"
    m = re.search(
        r"(?i)\b(?:top\s+(?P<topk>\d{1,3})\s+)?"
        r"(?:similar|related)\s+(?:roles?|job\s+titles?)\s*(?:for|to)\s+"
        r"(?P<role>[\w +#./&\-\(\)]+?)\s*[?.!]*$",
        q,
    )
    if not m:
        m = re.search(
            r"(?i)^(?:what|which)\s+(?:are\s+)?(?:the\s+)?"
            r"(?:similar|related)\s+(?:roles?|job\s+titles?)\s+"
            r"(?:to|for)\s+(?P<role>[\w +#./&\-\(\)]+?)\s*[?.!]*$",
            q,
        )
    if m:
        role = _strip(m.group("role"))
        topk = m.group("topk")
        return _with_domain(
            mode="AGG",
            task="similar_roles_for_role",
            role=role,
            topk=int(topk) if topk else None,
            llm_subquery=(
                f"Explain how the role '{role}' compares with these related roles "
                f"and when someone might transition between them."
            ),
        )
    # --- AGG: Similar/related skills for a SKILL (cluster-based) ---
    # Examples:
    #   "similar skills to Kubernetes"
    #   "top 15 related skills for React"
    #   "what skills are similar to Python?"
    m = re.search(
        r"(?i)\b(?:top\s+(?P<topk>\d{1,3})\s+)?"
        r"(?:similar|related)\s+skills?\s*(?:for|to)\s+"
        r"(?P<skill>[\w +#./&\-\(\)]+?)\s*[?.!]*$",
        q,
    )
    if not m:
        m = re.search(
            r"(?i)^(?:what|which)\s+(?:are\s+)?(?:the\s+)?"
            r"(?:skills?)\s+(?:that\s+are\s+)?(?:similar|related)\s+to\s+"
            r"(?P<skill>[\w +#./&\-\(\)]+?)\s*[?.!]*$",
            q,
        )
    if m:
        skill = _strip(m.group("skill"))
        topk = m.group("topk")
        return _with_domain(
            mode="AGG",
            task="similar_skills_for_skill",
            skill=skill,
            topk=int(topk) if topk else None,
            llm_subquery=(
                f"Group these skills by theme and describe how they complement "
                f"or build on '{skill}'."
            ),
        )

    return None


def llm_parse(nlq: str, cfg: dict) -> ParsedQuery | None:
    """
    Uses Gemini to parse arbitrary hiring/candidate/job questions into a ParsedQuery.

    This version explicitly decomposes queries into:
      - task (query type)
      - select_attributes (projection)
      - filters (blocking parameters)

    Returns None if LLM disabled or cannot parse.
    """
    llm_cfg = (cfg or {}).get("llm", {})
    if not llm_cfg.get("enabled"):
        return None

    from LLM.llm_router import generate_with_fallback

    # system = (
    #     "You convert messy hiring-related questions into a SINGLE strict JSON object. "
    #     "You NEVER add explanations, comments, or code fences. Output ONLY JSON.\n\n"
    #     "Allowed tasks (string 'task' field):\n"
    #     "  - count_jobs_by_skill\n"
    #     "  - top_skills_for_role\n"
    #     "  - list_jobs_for_role\n"
    #     "  - candidate_readiness\n"
    #     "  - filter_candidates_by_experience_role\n"
    #     "  - eligible_companies_for_candidate\n"
    #     "  - filter_candidates_by_skills\n"
    #     "  - filter_candidates_by_location\n"
    #     "  - filter_candidates_by_projects\n"
    #     "  - top_candidates_by_skill_count\n"
    #     "  - candidate_profile_and_eligible_jobs\n"
    #     "  - similar_roles_for_role\n"
    #     "  - similar_skills_for_skill\n\n"
    #     "JSON fields (all OPTIONAL except 'task'):\n"
    #     "  task: string, one of the allowed tasks (REQUIRED)\n"
    #     "  role: string – job role like 'data scientist', 'machine learning engineer'\n"
    #     "  location: string – city/country/region like 'India', 'Bangalore'\n"
    #     "  skill: string – single skill, e.g. 'Python', for count_jobs_by_skill or similar_skills_for_skill\n"
    #     "  skills_text: string – comma/pipe separated skills from user, e.g. 'Python, SQL, TensorFlow'\n"
    #     "  topk: integer – limit like top N skills, top N candidates, top N jobs\n"
    #     "  min_years: number – minimum years of experience\n"
    #     "  candidate_name: string – name of candidate when query mentions a specific candidate\n"
    #     "  candidate_id: integer – numeric candidate id if user writes 'candidate 61'\n"
    #     "  project_query: string – text describing project topic, e.g. 'fraud detection systems'\n"
    #     "  select_attributes: array of strings – which attributes the user wants SELECTed, "
    #     "e.g. ['job_title','company','job_location','salary','candidate_name','skills']\n"
    #     "  filters: object – generic WHERE-style filters like "
    #     "{'role':'data scientist','location':'India','min_years':2,'skills':['Python','SQL'],"
    #     "'candidate_id':61}\n"
    #     "\n"
    #     "Task selection rules:\n"
    #     "  - If user asks 'how many/ count jobs requiring skill X', use task='count_jobs_by_skill' and set skill, location if given.\n"
    #     "  - If user asks 'top N skills for role R', use task='top_skills_for_role' with role, topk, and optional location.\n"
    #     "  - If user asks to list or show jobs for a role, use task='list_jobs_for_role' with role and optional location.\n"
    #     "  - If user asks for 'candidates who are ready/matching/fit for role R', or 'top N candidates for R', "
    #     "use task='candidate_readiness' with role and optional topk/location.\n"
    #     "  - If user asks for candidates with 'at least/minimum/>= X years of experience', use "
    #     "task='filter_candidates_by_experience_role' with min_years, and optional role/location.\n"
    #     "  - If user asks 'which companies am I eligible for as role R', use task='eligible_companies_for_candidate' "
    #     "with role, optional location, and any inline skills as skills_text.\n"
    #     "  - If user asks for candidates with specific skills (e.g. 'candidates with skills: Python, SQL'), "
    #     "use task='filter_candidates_by_skills' and put those skills in skills_text, plus optional location.\n"
    #     "  - If user asks for candidates in a location only (e.g. 'candidates in Bangalore'), use "
    #     "task='filter_candidates_by_location' with location.\n"
    #     "  - If user asks for candidates based on projects or project topics (e.g. 'projects on fraud detection'), "
    #     "use task='filter_candidates_by_projects' and set project_query to the topic text.\n"
    #     "  - If user asks 'top N candidates with the most skills', use task='top_candidates_by_skill_count' "
    #     "with topk and optional location.\n"
    #             "  - If user asks about a specific candidate's profile or which jobs they are eligible for "
    #     "(e.g. 'which jobs is candidate John Doe eligible for', 'which jobs is candidate 61 eligible for', "
    #     "'show profile for candidate John Doe'), use task='candidate_profile_and_eligible_jobs' with "
    #     "candidate_name and/or candidate_id, optional role (to restrict to jobs for a specific role), "
    #     "optional location (applied to both the candidate and nearby jobs), and optional topk.\n"

    #     "  - If user asks for similar or related job roles/titles to a given role "
    #     "(e.g. 'similar roles to backend engineer'), use task='similar_roles_for_role' and set 'role' "
    #     "to the anchor role.\n"
    #     "  - If user asks for similar or related skills to a given skill "
    #     "(e.g. 'skills related to Kubernetes'), use task='similar_skills_for_skill' and set 'skill' "
    #     "to the anchor skill.\n"
    #     "\n"
    #     "Normalization rules:\n"
    #     "  - Fix simple typos in roles, e.g. 'software devlopment' -> 'software development'.\n"
    #     "  - Map common abbreviations: 'SDE' -> 'software engineer', 'DS' -> 'data scientist', 'ML' -> 'machine learning'.\n"
    #     "  - Trim whitespace around all strings.\n"
    #     "  - If the user says 'top N candidates' for a role, set topk=N.\n"
    #     "  - If it's clearly about years of experience, always set min_years.\n"
    #     "  - If user clearly mentions numeric candidate id (e.g. 'candidate 61'), set candidate_id=61.\n"
    #     "\n"
    #     "OUTPUT FORMAT:\n"
    #     "  - Always output exactly ONE JSON object.\n"
    #     "  - No comments, no extra text, no markdown, no code fences.\n"
    #     "  - Example (for guidance, do NOT copy literally):\n"
    #     "    {\"task\": \"top_skills_for_role\", "
    #     "\"role\": \"data scientist\", \"location\": \"India\", \"topk\": 10,\n"
    #     "     \"select_attributes\": [\"skill_name\",\"frequency\"], "
    #     "     \"filters\": {\"role\":\"data scientist\",\"location\":\"India\"}}\n"
    # )
    system = (
        "You are an expert hiring-analytics query parser for a federated jobs & candidates "
        "database system. Your job is to convert ANY messy natural-language hiring or job-market "
        "question into one single, precise JSON object that strictly follows the allowed schema.\n\n"

        "----------------------------\n"
        "🎯 YOUR OBJECTIVE\n"
        "----------------------------\n"
        "1. Identify WHAT the user is asking (task) — classification is your main job.\n"
        "2. Extract WHERE the user wants it filtered (location).\n"
        "3. Extract WHICH role, skill, or project the user is referring to.\n"
        "4. Extract supporting parameters (skills, min_years, topk, candidate id or name, etc.).\n"
        "5. Normalize typos, abbreviations, plural/singular, and noisy phrasing.\n"
        "6. Output ONLY a single JSON object with fields in the allowed schema. NO explanations.\n\n"

        "----------------------------\n"
        "📌 ALLOWED TASKS (choose EXACTLY one)\n"
        "----------------------------\n"
        "  - count_jobs_by_skill               (how many jobs require X?)\n"
        "  - top_skills_for_role               (top-N or most common skills for a given role)\n"
        "  - list_jobs_for_role                (list jobs matching a given role)\n"
        "  - candidate_readiness               (find candidates fit for a role)\n"
        "  - filter_candidates_by_experience_role (min years+role)\n"
        "  - eligible_companies_for_candidate  (companies/jobs user is eligible for as a role)\n"
        "  - filter_candidates_by_skills       (candidates with specific skills)\n"
        "  - filter_candidates_by_location     (candidates in a specific location only)\n"
        "  - filter_candidates_by_projects     (candidates with projects on a topic)\n"
        "  - top_candidates_by_skill_count     (candidates with most skills)\n"
        "  - candidate_profile_and_eligible_jobs (job eligibility + profile for candidate)\n"
        "  - similar_roles_for_role            (roles similar to a given role)\n"
        "  - similar_skills_for_skill          (skills similar to a given skill)\n"
        "\n"
        "NEVER invent a new task. If unsure: choose the closest match.\n\n"

        "----------------------------\n"
        "📌 FIELD NORMALIZATION RULES\n"
        "----------------------------\n"
        "- Trim whitespace.\n"
        "- Remove trailing words like 'jobs', 'roles', 'positions'.\n"
        "- Abbreviations → full form: ML→machine learning, DS→data science, SDE→software engineer.\n"
        "- Handle typos (e.g. 'devlopment' → 'development').\n"
        "- If user says 'top N ...', ALWAYS set topk.\n"
        "- If years appear (e.g. '>= 3 years'), ALWAYS set min_years.\n"
        "- If skills are comma-separated or in text, normalize into skills_text AND skills list.\n"
        "- If candidate mentioned with digits (e.g. 'candidate 51'), set candidate_id.\n"
        "- If user gives only a name, set candidate_name.\n"
        "- If multiple intents appear, choose the dominant one (the one the SQL system supports).\n\n"

        "----------------------------\n"
        "📌 LOCATION EXTRACTION\n"
        "----------------------------\n"
        "- Extract ANY location phrase: countries, cities, states, continents, regions.\n"
        "- Examples: 'Canada', 'Toronto', 'Bangalore', 'Europe', 'United States', 'UK'.\n"
        "- Do NOT over-normalize continents or provinces—keep the user's phrase.\n"
        "- If question contains more than one location, choose the main one.\n\n"

        "----------------------------\n"
        "📌 ROLE & SKILL EXTRACTION\n"
        "----------------------------\n"
        "- Extract the central role being asked about (backend engineer, data scientist, SDE, etc.).\n"
        "- Extract ONE main skill for tasks that require a single skill (skill field).\n"
        "- For inline lists ('skills: Python, SQL, Kafka'), store raw list in skills_text AND "
        "split into skills[].\n\n"

        "----------------------------\n"
        "📌 OUTPUT FORMAT (STRICT)\n"
        "----------------------------\n"
        "You MUST output exactly ONE JSON object with these keys (optional except task):\n"
        "{\n"
        '  "task": string,\n'
        '  "role": string or null,\n'
        '  "location": string or null,\n'
        '  "skill": string or null,\n'
        '  "skills": [list of strings] or null,\n'
        '  "skills_text": string or null,\n'
        '  "project_query": string or null,\n'
        '  "candidate_name": string or null,\n'
        '  "candidate_id": integer or null,\n'
        '  "topk": integer or null,\n'
        '  "min_years": number or null,\n'
        '  "select_attributes": [strings] or null,\n'
        '  "filters": object or null\n'
        "}\n\n"

        "----------------------------\n"
        "⚠️ RULES\n"
        "----------------------------\n"
        "- DO NOT add any explanation.\n"
        "- DO NOT output markdown.\n"
        "- DO NOT output comments.\n"
        "- DO NOT wrap JSON in code fences.\n"
        "- Produce valid JSON ONLY.\n"
    )

    user = f"Query: {nlq.strip()}\nReturn ONLY compact JSON with those keys, no prose."

    raw, _provider = generate_with_fallback(cfg, system, user, json_mode=True)
    if not raw:
        return None

    try:
        obj = json.loads(raw)
    except Exception:
        # Sometimes models wrap code fences; try to strip
        raw2 = raw.strip().strip("`").strip()
        if "{" in raw2 and "}" in raw2:
            raw2 = raw2[raw2.find("{"): raw2.rfind("}") + 1]
        try:
            obj = json.loads(raw2)
        except Exception:
            return None

    if isinstance(obj, list):
        if not obj:
            return None
        obj = obj[0]

    if not isinstance(obj, dict):
        return None

    task = obj.get("task")
    if not isinstance(task, str) or not task.strip():
        return None
    task = task.strip()

    # --- base fields ---
    role = (obj.get("role") or "").strip() or None
    loc  = (obj.get("location") or "").strip() or None

    # single skill (for count / similar_skills tasks)
    skill = (obj.get("skill") or "").strip() or None

    topk = obj.get("topk")
    min_years = obj.get("min_years")

    # raw skills text (jobs/candidates)
    skills_text = obj.get("skills_text")
    if isinstance(skills_text, list):
        skills_text = ", ".join(str(s).strip() for s in skills_text if str(s).strip())
    elif isinstance(skills_text, str):
        skills_text = skills_text.strip() or None
    else:
        skills_text = None

    candidate_name = (obj.get("candidate_name") or "").strip() or None
    project_query = obj.get("project_query")
    if isinstance(project_query, str):
        project_query = project_query.strip() or None

    # NEW: normalized list of skills (for jobs multi-federation)
    skills_list: list[str] | None = None

    # top-level 'skills' field, if model provided it
    raw_skills = obj.get("skills")
    if isinstance(raw_skills, list):
        tmp = [str(s).strip() for s in raw_skills if str(s).strip()]
        if tmp:
            skills_list = tmp
    elif isinstance(raw_skills, str) and raw_skills.strip():
        # fold a string into skills_text; we'll split later
        if skills_text:
            skills_text = skills_text + ", " + raw_skills.strip()
        else:
            skills_text = raw_skills.strip()

    # --- select_attributes + filters ---
    select_attrs = obj.get("select_attributes") or obj.get("projection")
    if isinstance(select_attrs, list):
        select_attrs = [str(a).strip() for a in select_attrs if str(a).strip()]
        if not select_attrs:
            select_attrs = None
    else:
        select_attrs = None

    filters_obj = obj.get("filters")
    if isinstance(filters_obj, dict):
        filters: dict = {}
        for k, v in filters_obj.items():
            if isinstance(v, str):
                filters[k] = v.strip()
            else:
                filters[k] = v
    else:
        filters = None

    # --- numeric candidate id if supplied ---
    cid_raw = obj.get("candidate_id")
    candidate_id = None
    if isinstance(cid_raw, (int, float)):
        candidate_id = int(cid_raw)
    elif isinstance(cid_raw, str):
        s = cid_raw.strip()
        if s.isdigit():
            candidate_id = int(s)

    # fold values from filters into top-level fields
    if filters:
        if not role and isinstance(filters.get("role"), str):
            role = filters["role"] or role
        if not loc and isinstance(filters.get("location"), str):
            loc = filters["location"] or loc
        if not skill and isinstance(filters.get("skill"), str):
            skill = filters["skill"] or skill
        if min_years is None and "min_years" in filters:
            min_years = filters["min_years"]
        if topk is None and "topk" in filters:
            topk = filters["topk"]
        if candidate_name is None and isinstance(filters.get("candidate_name"), str):
            candidate_name = filters["candidate_name"] or None
        if candidate_id is None and filters.get("candidate_id") is not None:
            cid2 = filters.get("candidate_id")
            if isinstance(cid2, (int, float)):
                candidate_id = int(cid2)
            elif isinstance(cid2, str) and cid2.strip().isdigit():
                candidate_id = int(cid2.strip())
        if filters.get("skills") is not None:
            val = filters["skills"]
            if isinstance(val, list):
                # extend skills_list
                extra = [str(s).strip() for s in val if str(s).strip()]
                if extra:
                    skills_list = (skills_list or []) + extra
            else:
                text_val = str(val).strip()
                if text_val:
                    if skills_text:
                        skills_text = skills_text + ", " + text_val
                    else:
                        skills_text = text_val


    # if candidate_name looks numeric but no id yet, treat it as id
    if candidate_id is None and candidate_name and candidate_name.strip().isdigit():
        candidate_id = int(candidate_name.strip())
    if skills_list is None and skills_text:
        parts = re.split(
            r"\b(?:and|as well as)\b|[,|;/]",
            skills_text,
            flags=re.IGNORECASE,
        )
        skills_list = [p.strip() for p in parts if p.strip()]
    # aliases/typos quick-fix for roles
    ALIASES = {
        "ml": "machine learning",
        "ds": "data science",
        "de": "data engineer",
        "sde": "software engineer",
        "be": "backend engineer",
        "fe": "frontend engineer",
        "software devlopment": "software development",
    }
    if role:
        rl = role.lower()
        role = ALIASES.get(rl, role)

    # normalize numeric fields
    def _to_int(v):
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            if s.isdigit():
                return int(s)
        return None

    def _to_float(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            s = v.strip()
            try:
                return float(s)
            except ValueError:
                return None
        return None

    topk_val = _to_int(topk)
    min_years_val = _to_float(min_years)

    # llm_subquery for advisor
    llm_sub = None
    if task == "count_jobs_by_skill" and skill:
        llm_sub = f"Give recent commentary about demand and market context for skill '{skill}'"
    elif task == "top_skills_for_role" and role:
        llm_sub = f"Summarize emerging tools and trends for the role '{role}'"
    elif task == "list_jobs_for_role" and role:
        llm_sub = f"Extract short advice for candidates applying to '{role}' roles"
    elif task == "candidate_readiness" and role:
        llm_sub = f"Give brief interview prep pointers for '{role}'"
    elif task == "filter_candidates_by_experience_role":
        if role and min_years_val is not None:
            llm_sub = f"Summarize key screening cues for {min_years_val}+ years in '{role}'"
        elif min_years_val is not None:
            llm_sub = f"Summarize key screening cues for {min_years_val}+ years professional experience"
    elif task == "eligible_companies_for_candidate" and role:
        llm_sub = f"Give concise screening heuristics for eligibility to '{role}' roles"
    elif task == "candidate_profile_and_eligible_jobs":
        label = candidate_name or (f"ID {candidate_id}" if candidate_id is not None else "the candidate")
        loc_part = loc or "N/A"
        llm_sub = (
                f"Suggest roles suitable for candidate {label} given their skills and location {loc_part}. "
                f"When you see skills marked as derived/auto_extracted in the structured JSON, explicitly call "
                f"them out as skills auto-extracted from projects and experience rather than self-declared skills."
            )
    elif task == "similar_roles_for_role" and role:
        llm_sub = (
            f"Explain how the role '{role}' compares with these related roles and "
            f"what typical career transitions look like between them."
        )
    elif task == "similar_skills_for_skill" and skill:
        llm_sub = (
            f"Group these related skills into 2–3 themes and explain how they "
            f"complement or deepen expertise in '{skill}'."
        )

    mode = "AGG" if task in {
        "count_jobs_by_skill",
        "top_skills_for_role",
        "candidate_readiness",
        "filter_candidates_by_experience_role",
        "top_candidates_by_skill_count",
        "similar_roles_for_role",
        "similar_skills_for_skill",
    } else "SPJ"

    return _with_domain(
        mode=mode,
        task=task,
        role=role,
        location=loc,
        skill=skill,
        skills=skills_list,
        topk=topk_val,
        llm_subquery=llm_sub,
        min_years=min_years_val,
        skills_text=skills_text,
        candidate_name=candidate_name,
        candidate_id=candidate_id,
        project_query=project_query,
        select_attributes=select_attrs,
        filters=filters,
    )

