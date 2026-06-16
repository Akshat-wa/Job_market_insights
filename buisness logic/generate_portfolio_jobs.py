"""
Generate a large synthetic combined jobs CSV for portfolio demos.

Default target: 12 MB on disk → ~50–70k jobs, ~5 skills each, ~60–75 MB in PostgreSQL.

Usage:
  python generate_portfolio_jobs.py
  python generate_portfolio_jobs.py --target-mb 12 --force
"""
from __future__ import annotations

import argparse
import csv
import os
import random
from typing import List, Tuple

ROLES: List[str] = [
    "Data Scientist", "Machine Learning Engineer", "Data Engineer", "Backend Engineer",
    "Frontend Engineer", "Full Stack Developer", "DevOps Engineer", "Cloud Engineer",
    "Software Engineer", "Android Developer", "iOS Developer", "QA Engineer",
    "Business Analyst", "Product Manager", "UI UX Designer", "Cybersecurity Analyst",
    "Database Administrator", "Solutions Architect", "Site Reliability Engineer",
    "Python Developer", "Java Developer", "React Developer", "Node.js Developer",
]

COMPANIES: List[str] = [
    "Acme Analytics", "CloudNine Tech", "DataPipe Inc", "PixelSoft", "InfraWorks",
    "Insight Co", "CodeForge", "Nimbus Labs", "Vertex Systems", "BlueRiver Digital",
    "Quantum Hire", "StackBridge", "OpenTalent", "SkillMesh", "HireGrid",
    "TalentNova", "CoreLogic IT", "Apex Innovations", "Silverline Software",
    "Northstar Data", "FusionWorks", "BrightPath", "TechVista", "NextWave",
]

LOCATIONS: List[Tuple[str, str]] = [
    ("Bangalore", "India"), ("Hyderabad", "India"), ("Mumbai", "India"),
    ("Pune", "India"), ("Chennai", "India"), ("Delhi", "India"),
    ("Kolkata", "India"), ("Gurgaon", "India"), ("Noida", "India"),
    ("San Francisco", "United States"), ("New York", "United States"),
    ("Seattle", "United States"), ("Austin", "United States"),
    ("Boston", "United States"), ("Chicago", "United States"),
    ("London", "United Kingdom"), ("Berlin", "Germany"),
    ("Toronto", "Canada"), ("Sydney", "Australia"), ("Singapore", "Singapore"),
    ("Dubai", "United Arab Emirates"), ("Amsterdam", "Netherlands"),
]

ROLE_SKILLS: dict[str, List[str]] = {
    "data": ["python", "sql", "pandas", "numpy", "scikit-learn", "tensorflow", "pytorch", "spark", "tableau", "statistics"],
    "ml": ["python", "machine learning", "deep learning", "pytorch", "tensorflow", "nlp", "computer vision", "mlops", "docker"],
    "backend": ["java", "spring", "python", "django", "nodejs", "postgresql", "redis", "kubernetes", "microservices", "rest api"],
    "frontend": ["javascript", "react", "typescript", "html", "css", "redux", "webpack", "nextjs", "figma"],
    "devops": ["docker", "kubernetes", "aws", "terraform", "jenkins", "linux", "ci cd", "ansible", "prometheus"],
    "mobile": ["kotlin", "swift", "android", "ios", "react native", "flutter", "firebase"],
    "general": ["git", "agile", "communication", "problem solving", "sql", "python", "java", "javascript"],
}

ROLE_TO_POOL = {
    "Data Scientist": "data", "Machine Learning Engineer": "ml", "Data Engineer": "data",
    "Backend Engineer": "backend", "Frontend Engineer": "frontend", "Full Stack Developer": "backend",
    "DevOps Engineer": "devops", "Cloud Engineer": "devops", "Software Engineer": "general",
    "Android Developer": "mobile", "iOS Developer": "mobile", "Python Developer": "data",
    "Java Developer": "backend", "React Developer": "frontend", "Node.js Developer": "backend",
}


def _skill_pool(role: str, sk_min: int = 4, sk_max: int = 5) -> List[str]:
    key = ROLE_TO_POOL.get(role, "general")
    base = list(ROLE_SKILLS[key])
    extra = random.sample(ROLE_SKILLS["general"], k=min(2, len(ROLE_SKILLS["general"])))
    merged = list(dict.fromkeys(base + extra))
    random.shuffle(merged)
    hi = min(sk_max, len(merged))
    lo = min(sk_min, hi)
    return merged[: random.randint(lo, hi)]


def generate_csv(
    output_path: str,
    target_mb: float,
    seed: int = 42,
    skills_min: int = 4,
    skills_max: int = 5,
) -> dict:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    target_bytes = int(target_mb * 1024 * 1024)
    random.seed(seed)

    rows_written = 0
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["job_link", "job_title", "company", "job_location", "job_skills"])

        while f.tell() < target_bytes:
            role = random.choice(ROLES)
            company = random.choice(COMPANIES)
            city, country = random.choice(LOCATIONS)
            loc = f"{city} {country}"
            link = f"https://portfolio-demo.jobs/{rows_written + 1}"
            skills = ", ".join(_skill_pool(role, skills_min, skills_max))
            writer.writerow([link, role, company, loc, skills])
            rows_written += 1

            if rows_written % 5000 == 0:
                f.flush()
                print(f"  … {rows_written:,} rows, {f.tell() / (1024*1024):.1f} MB", end="\r")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\n✅ Wrote {rows_written:,} rows → {output_path} ({size_mb:.1f} MB)")
    return {"rows": rows_written, "path": output_path, "size_mb": round(size_mb, 2)}


def main():
    ap = argparse.ArgumentParser(description="Generate portfolio-scale synthetic jobs CSV")
    ap.add_argument("--target-mb", type=float, default=12.0, help="Target CSV size on disk")
    ap.add_argument("--output", default="data/portfolio_jobs_combined.csv")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="Regenerate even if cached file exists")
    ap.add_argument("--skills-min", type=int, default=4)
    ap.add_argument("--skills-max", type=int, default=5)
    args = ap.parse_args()

    if os.path.isfile(args.output) and not args.force:
        existing_mb = os.path.getsize(args.output) / (1024 * 1024)
        if existing_mb >= args.target_mb * 0.85 and existing_mb <= args.target_mb * 1.15:
            print(f"ℹ️  Cached file exists ({existing_mb:.1f} MB): {args.output}")
            print("   Pass --force to regenerate.")
            return

    print(f"🛠  Generating ~{args.target_mb} MB synthetic jobs CSV …")
    generate_csv(
        args.output,
        args.target_mb,
        seed=args.seed,
        skills_min=args.skills_min,
        skills_max=args.skills_max,
    )


if __name__ == "__main__":
    main()
