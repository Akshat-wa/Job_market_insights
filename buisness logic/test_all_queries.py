import subprocess
import textwrap
import os
from datetime import datetime

# Path config (assumes this script is in the same folder as run_query.py)
HERE = os.path.dirname(os.path.abspath(__file__))
RUN_QUERY = os.path.join(HERE, "run_query.py")
CONFIG_PATH = os.path.join(HERE, "config.yaml")

PYTHON_EXE = "python"  # change to "python3" if needed

# ---- Define test queries: cover all categories + some variants ----
# TEST_QUERIES = [
#     # 1) count_jobs_by_skill
#     "How many jobs require Python?",
#     "Count jobs with SQL in India.",

#     # 2) top_skills_for_role
#     "Top 10 skills for data scientist in India.",
#     "Most common 5 skills for machine learning engineer.",

#     # 3) list_jobs_for_role
#     "Show jobs as data scientist in United States.",
#     "List jobs for backend engineer in Germany.",

#     # 4) candidate_readiness
#     "Show candidates matching data scientist in United States",
#     "Which candidates are ready for machine learning engineer in India top 20?",

#     # 5) filter_candidates_by_experience_role
#     "List candidates with at least 3 years of experience in data engineering in Canada.",
#     "Show candidates with minimum 5 years of experience in software engineering.",

#     # 6) eligible_companies_for_candidate
#     "Which companies am I eligible for as data scientist in India skills: Python, SQL, TensorFlow, AWS?",
#     "List companies I am eligible for as backend engineer in Europe skills: Java, Spring, Kubernetes.",

#     # 7) filter_candidates_by_skills
#     "Show candidates with skills: Python, SQL, TensorFlow in United States.",
#     "List candidates having skills: Java, Spring, Microservices.",

#     # 8) filter_candidates_by_location
#     "Show candidates in Bangalore.",
#     "List candidates from San Francisco.",

#     # 9) filter_candidates_by_projects
#     "Show candidates with projects related to fraud detection.",
#     "List candidates with projects on recommendation systems.",

#     # 10) top_candidates_by_skill_count
#     "Show top 20 candidates with the most skills in India.",
#     "List top 10 candidates by skills in United States.",

#     # 11) candidate_profile_and_eligible_jobs
#     "Which jobs is candidate John Doe eligible for in India top 30?",
#     "Show profile for candidate Jane Smith in United States.",

#     # --- Fuzzier / messy queries to exercise llm_parse ---
#     "I want the best 15 candidates for ML in Europe.",
#     "Give me 8 candidates with > 4 years experience for SDE roles in India.",
#     "What companies could I join as an Machine Learning engineer if I know Python, PyTorch, and Docker?",
# ]

# TEST_QUERIES = [
#     # Q1: Federated readiness & hiring plan
#     "i am looking for people to build my website, for what job roles should i hire for and top 10 potential candidates",

#     # Q2: Top-k skills for a role
#     "Top 5 skills for Data Engineer",

#     # Q3: Jobs listing (SPJ)
#     "Jobs listing for software engineer in London",

#     # Q4: Eligible companies (backend engineer, Europe)
#     "List companies I am eligible for as backend engineer in Europe skills: Java, Spring, Kubernetes.",

#     # Q5: Eligible companies (ML engineer, specific skills)
#     "What companies could I join as an Machine Learning engineer if I know Python, PyTorch, and Docker?",
# ]

TEST_QUERIES = [
    # ----------------------------------------------------------------------------
    #  A. Candidate-style queries: looking for jobs
    # ----------------------------------------------------------------------------

    "How many jobs require Python?",
    "How many jobs need SQL in India?",
    "Count data analyst jobs that require Excel.",
    "Number of jobs worldwide that mention TensorFlow.",
    "Roughly how many jobs ask for Docker experience?",
    "How many backend engineer jobs need Java?",
    # "Count jobs that require AWS in the United States.",
    # "How many postings mention Kubernetes in Europe?",
    # "Number of jobs that list deep learning as a skill.",
    # "How many data scientist roles require R in Canada?",
    # "How many remote jobs require Python and SQL?",
    # "Count jobs which require Tableau in India.",
    # "How many jobs mention Apache Spark as a skill?",
    # "Number of jobs needing Git experience in Germany.",
    # "How many jobs require machine learning in the UK?",

    # "Top 5 skills for Data Engineer.",
    # "Top 10 skills for data scientist in India.",
    # "What are the most common skills for backend engineer roles?",
    # "List top 8 skills for machine learning engineer jobs.",
    # "Which 10 skills are most important for full stack developers?",
    # "Top 5 skills for cloud architect roles in Europe.",
    # "Top 7 skills needed for DevOps engineer in the USA.",
    # "Most common skills for business analyst.",
    # "Top 10 skills for data analyst roles in Canada.",
    # "What skills matter most for software engineer jobs in Germany?",
    # "Top 6 skills for front end developer roles.",
    # "Most frequent skills for cybersecurity analyst jobs.",
    # "Top 5 skills required for MLOps engineer.",
    # "Key skills for big data engineer in India.",
    # "Top 10 technologies for AI engineer roles.",

    # "Jobs listing for software engineer in London.",
    # "Show data scientist jobs in Bangalore.",
    # "List backend engineer roles in Berlin.",
    # "Show me data analyst jobs in United States.",
    # "List full stack developer positions in Canada.",
    # "Show DevOps engineer jobs in India.",
    # "List machine learning engineer roles in California.",
    # "Show cloud architect jobs in Europe.",
    # "List business analyst jobs in Singapore.",
    # "Find product manager roles in New York.",
    # "Show remote data scientist jobs.",
    # "List software engineer positions in San Francisco.",
    # "Show data engineer roles in Hyderabad.",
    # "Find AI researcher jobs in the UK.",
    # "List cybersecurity roles in Australia.",
    # "Show front end developer jobs in Toronto.",
    # "List backend developer positions in the Netherlands.",
    # "Show jobs for site reliability engineer in USA.",
    # "Jobs listing for data engineer in Mumbai.",
    # "Show senior data scientist jobs in Germany.",
    # "List junior data analyst roles in India.",
    # "Show entry-level software developer jobs.",
    # "List internships for data science in United States.",
    # "Show machine learning internships in Europe.",
    # "List remote Python developer jobs.",
    # "Show part-time data analyst jobs.",
    # "List contract software engineer roles.",
    # "Show full-time data engineering positions.",
    # "List jobs for Golang backend engineer.",
    # "Show jobs for React front end developer in Paris.",

    # "Which companies could hire me as a data scientist in India if I know Python, SQL, and TensorFlow?",
    # "List companies I am eligible for as backend engineer in Europe skills: Java, Spring, Kubernetes.",
    # "What companies could I join as a Machine Learning engineer if I know Python, PyTorch, and Docker?",
    # "Which companies might hire me as a cloud engineer if I know AWS, Terraform, and Linux?",
    # "List companies that would consider me for data analyst if I have Excel, Power BI, and SQL skills.",
    # "Which companies can I apply to as full stack developer if I use React, Node.js, and MongoDB?",
    # "What companies could I join in the US as DevOps engineer with Docker, Kubernetes, and CI/CD skills?",
    # "List companies in Canada I am eligible for as data engineer with Spark, Hadoop, and Scala.",
    # "Which companies in Europe could hire me as AI engineer with Python, deep learning, and NLP skills?",
    # "What companies might hire me as Android developer if I know Kotlin and Jetpack Compose?",
    # "Which companies can I join as SDE if I know C++, data structures, and algorithms?",
    # "List companies open to junior data scientist roles for someone with Python, Pandas, and Scikit-learn.",
    # "Which companies hire MLOps engineers who know MLflow, Docker, and Kubernetes?",
    # "What companies might consider me for business analyst roles with Excel, SQL, and Tableau skills?",
    # "Which companies in India could hire me as cyber security analyst if I know Linux and networking basics?",
    # "What companies would consider me for QA engineer with Selenium and Java skills?",
    # "Which companies might hire me as data engineer with GCP, BigQuery, and Airflow experience?",
    # "What companies could I join as BI developer if I know Power BI, SQL Server, and DAX?",
    # "Which companies would hire a fresher data analyst with Excel and basic SQL?",
    # "What companies in Europe can I apply to as ML engineer if I know Python, TensorFlow, and cloud?",

    # "Show jobs where I can work remotely as a data scientist.",
    # "Find jobs that are hybrid data analyst roles in India.",
    # "Show junior machine learning engineer jobs in Bangalore.",
    # "Find mid-level backend developer jobs in Berlin.",
    # "Show senior software engineer roles in New York.",
    # "Show jobs that fit a fresher data analyst profile in India.",
    # "Find roles for full stack developer with 2 years of experience in Canada.",
    # "Show jobs for data scientist with at least 5 years of experience in USA.",
    # "Find jobs for backend engineer with 3+ years experience in microservices.",
    # "Show jobs for cloud architect with 8 or more years of experience.",
    # "Find internships for AI engineer in Europe.",
    # "Show entry level software engineer roles in Germany.",
    # "Find data analyst jobs for someone with Excel and SQL only.",
    # "Show jobs where machine learning is optional but Python is required.",
    # "Find roles where SQL and Tableau are mandatory in United States.",

    # "What jobs is candidate 61 eligible for?",
    # "What jobs is candidate 100 eligible for in India?",
    # "Show jobs that candidate 25 can apply for in the US.",
    # "Which roles fit candidate 10 in Europe?",
    # "Show eligible jobs for candidate 5 in Canada.",
    # "Which jobs is candidate 8 eligible for in Bangalore?",
    # "Show jobs for candidate named John Doe.",
    # "Which jobs are suitable for candidate Jane Smith in India?",
    # "Show profile and eligible jobs for candidate Akash in United States.",
    # "Which roles would fit candidate Priya in Europe top 20?",

    # # ----------------------------------------------------------------------------
    # #  B. Recruiter-style queries: looking for candidates
    # # ----------------------------------------------------------------------------

    # "Show candidates with skills: Python, SQL, TensorFlow in United States.",
    # "List candidates having skills: Java, Spring, Microservices.",
    # "Find candidates with skills: React, Node.js, MongoDB in Europe.",
    # "Show candidates with skills: AWS, Docker, Kubernetes in India.",
    # "List candidates who know Power BI and SQL in Canada.",
    # "Show candidates with skills: Python, Pandas, Scikit-learn.",
    # "Find candidates who have skills: C++, data structures, algorithms.",
    # "Show candidates with skills: GCP, BigQuery, Airflow.",
    # "List candidates with skills: Azure, Databricks, Spark.",
    # "Show candidates having skills: Tableau, Excel, SQL in UK.",
    # "Find candidates with skills: NLP, deep learning, PyTorch.",
    # "Show candidates who know Kubernetes and Terraform.",
    # "List candidates with skills: JavaScript, TypeScript, React.",
    # "Show candidates with skills: Linux, networking, security.",
    # "Find candidates skilled in Go and microservices architecture.",
    # "Show candidates with skills: Snowflake, dbt, SQL.",
    # "List candidates who know Django and REST APIs.",
    # "Find candidates with skills: Flutter, Dart, mobile development.",
    # "Show candidates with skills: Rust and systems programming.",
    # "List candidates with skills: Hadoop, Hive, MapReduce.",

    # "Show candidates in Bangalore.",
    # "List candidates from San Francisco.",
    # "Show candidates located in India.",
    # "List candidates based in Germany.",
    # "Show candidates in New York.",
    # "List candidates from Toronto.",
    # "Show candidates in London.",
    # "List candidates from Sydney.",
    # "Show candidates located in Europe.",
    # "List candidates in Singapore.",
    # "Show candidates in California.",
    # "List candidates from Mumbai.",
    # "Show candidates in Hyderabad.",
    # "List candidates from remote locations.",
    # "Show candidates based in the UK.",

    # "Show candidates with projects related to fraud detection.",
    # "List candidates with projects on recommendation systems.",
    # "Find candidates with projects about time series forecasting.",
    # "Show candidates who built a chatbot using NLP.",
    # "List candidates with computer vision projects.",
    # "Show candidates with projects involving credit risk modeling.",
    # "Find candidates who worked on anomaly detection projects.",
    # "Show candidates with projects in ecommerce analytics.",
    # "List candidates who built a search engine or ranking system.",
    # "Show candidates with projects related to A/B testing.",
    # "List candidates with customer churn prediction projects.",
    # "Find candidates with projects in sentiment analysis.",
    # "Show candidates who built end-to-end ML pipelines.",
    # "List candidates with MLOps or model deployment projects.",
    # "Show candidates with projects in supply chain optimization.",

    # "Show top 20 candidates with the most skills in India.",
    # "List top 10 candidates by skills in United States.",
    # "Show top 15 candidates globally with the largest skill set.",
    # "List 25 candidates with the highest skills_count.",
    # "Show top 30 multi-skilled candidates in Europe.",
    # "Show top 10 candidates with the most diverse tech stack.",
    # "List top 5 candidates in Canada by number of skills.",
    # "Show top 20 skill-rich candidates in Germany.",
    # "List 15 candidates with the largest skill variety in India.",
    # "Show top 12 candidates with the most tools and technologies.",

    # "List candidates with at least 3 years of experience in data engineering in Canada.",
    # "Show candidates with minimum 5 years of experience in software engineering.",
    # "Find candidates with 2+ years of experience as data scientists in India.",
    # "Show candidates with at least 4 years of DevOps experience in Europe.",
    # "List candidates having more than 6 years in backend development.",
    # "Show candidates with 1 or more years of experience in machine learning.",
    # "Find candidates with at least 8 years of cloud architecture experience.",
    # "Show candidates having 10+ years in software engineering in USA.",
    # "List candidates with at least 2 years in data analysis in UK.",
    # "Show candidates with 3+ years experience in full stack development.",
    # "List candidates with at least 5 years experience and skills: Python, SQL.",
    # "Show candidates with 4+ years experience in Java and Spring.",
    # "Find candidates with minimum 2 years in React front end development.",
    # "Show candidates with 3 or more years in mobile app development.",
    # "List candidates with 2+ years of cyber security experience.",
    # "Show candidates with at least 7 years in BI or analytics.",
    # "Find candidates with 5+ years in big data technologies.",
    # "Show candidates having 3+ years experience and projects in recommendation systems.",
    # "List candidates with 4+ years experience and projects in fraud detection.",
    # "Show candidates with 2+ years experience and projects related to NLP.",

    # "Which candidates are ready for data scientist roles in United States?",
    # "Show candidates matching machine learning engineer in India top 20.",
    # "Which candidates are ready for backend engineer roles in Europe?",
    # "Show candidates who are good fit for full stack developer in Canada.",
    # "Which candidates match DevOps engineer positions in Germany?",
    # "Show candidates ready for cloud architect roles in USA.",
    # "Which candidates are best suited for data engineering roles in India?",
    # "Show top 30 candidates ready for product data scientist jobs.",
    # "Which candidates are good fit for MLOps engineer in Europe?",
    # "Show matching candidates for business analyst roles in UK.",
    # "Which candidates are ready for AI engineer in United States?",
    # "Show candidates that fit junior data analyst roles in India.",
    # "Which candidates are near-ready for senior data scientist in Germany?",
    # "Show candidates ready for SDE-1 roles in India.",
    # "Which candidates would be good for software engineer roles in Canada?",
    # "Show candidates matching site reliability engineer in Europe.",
    # "Which candidates are ready for data engineer positions in USA?",
    # "Show candidates best suited for analytics engineer in UK.",
    # "Which candidates align well with ML researcher roles?",
    # "Show candidates ready to be hired as BI developer.",

    # # ----------------------------------------------------------------------------
    # #  C. Modular / complex queries (skills + location + projects + experience)
    # # ----------------------------------------------------------------------------

    # "Find candidates in Bangalore with at least 3 years of experience who know Python and SQL and have projects in fraud detection.",
    # "Show candidates in Europe with 5+ years experience, skills: Java, Spring Boot, microservices, and projects related to ecommerce.",
    # "List candidates in United States with skills: Python, TensorFlow, Docker and projects in computer vision, at least 2 years experience.",
    # "Find candidates in India with skills: React, Node.js, MongoDB, 3+ years experience and projects in full stack web development.",
    # "Show candidates in Canada with at least 4 years experience, skills: AWS, Kubernetes, Terraform and projects about cloud migration.",
    # "Find candidates in UK with skills: Power BI, SQL, Excel and projects in sales analytics, minimum 2 years experience.",
    # "Show candidates in Germany with skills: Spark, Scala, Hadoop, 3+ years big data experience and projects in ETL pipelines.",
    # "Find candidates in Singapore with skills: Python, NLP, deep learning and projects related to chatbots, 2+ years experience.",
    # "Show candidates located in USA with skills: Docker, CI/CD, Kubernetes and projects on deployment automation, 3+ years experience.",
    # "Find candidates in India with skills: C++, DSA and competitive programming projects, 1+ years experience.",
    # "Show candidates in Europe with skills: GCP, BigQuery, Airflow and data engineering projects, 3+ years experience.",
    # "Find candidates in Australia with skills: Tableau, SQL and business intelligence projects, 2+ years experience.",
    # "Show candidates in remote locations with skills: Python, PyTorch, MLflow, and projects in model deployment.",
    # "Find candidates in Bangalore with skills: Django, REST APIs, PostgreSQL, 2+ years experience and projects in ecommerce.",
    # "Show candidates in USA with skills: React, TypeScript, Next.js and front-end projects, 3+ years experience.",
    # "Find candidates in India with skills: Flutter, Dart and mobile app projects, at least 1 year experience.",
    # "Show candidates in Canada with skills: Azure, Databricks, Spark, 3+ years data engineering experience.",
    # "Find candidates in Germany with skills: Linux, networking, security, 2+ years and cyber security projects.",
    # "Show candidates in Europe with skills: Snowflake, dbt, SQL and modern data stack projects.",
    # "Find candidates in UK with skills: Python, time series forecasting and projects in demand prediction.",
    # "Show candidates in India with skills: NLP, HuggingFace Transformers and text classification projects.",
    # "Find candidates in United States with skills: Rust, systems programming and low-level performance projects.",
    # "Show candidates in Singapore with skills: Kubernetes, Helm, Prometheus and observability projects.",
    # "Find candidates in USA with skills: Java, Spring, Kafka and event-driven microservices projects.",
    # "Show candidates in India with skills: TensorFlow, Keras, CNNs and image classification projects.",
    # "Find candidates in Europe with skills: Python, Scikit-learn, XGBoost and tabular ML projects.",
    # "Show candidates in Canada with skills: SQL, Snowflake, Looker and analytics engineering projects.",
    # "Find candidates in UK with skills: R, Shiny and statistical modeling projects.",
    # "Show candidates in Bangalore with skills: Power BI, DAX and dashboarding projects.",
    # "Find candidates in Germany with skills: Golang, gRPC and distributed systems projects.",
    # "Show candidates in India with skills: Python, Airflow, dbt and ELT pipeline projects.",
    # "Find candidates in USA with skills: JavaScript, Node.js, Express and backend APIs projects.",
    # "Show candidates in India with skills: ML, recommendation systems and 2+ years experience.",
    # "Find candidates in Europe with skills: Python, anomaly detection and risk modeling projects.",
    # "Show candidates in Canada with skills: SQL, Python, forecasting and financial modeling projects.",
    # "Find candidates in Singapore with skills: Kubernetes, Docker and SRE-style projects.",
    # "Show candidates in UK with skills: PySpark, data lakes and big data projects.",
    # "Find candidates in India with skills: NLP, topic modeling and social media analytics projects.",
    # "Show candidates in United States with skills: Tableau, A/B testing and growth analytics projects.",

    # # ----------------------------------------------------------------------------
    # #  D. Messy / fuzzy user queries to stress llm_parse
    # # ----------------------------------------------------------------------------

    # "I am a fresher with Python and SQL skills in India, what roles can I go for?",
    # "I know React and Node.js, show me some suitable jobs in Europe.",
    # "I only know Excel and a bit of SQL, what analyst jobs can I apply for in Bangalore?",
    # "Looking for remote ML engineer roles, I know Python and PyTorch.",
    # "Show me some backend dev jobs where Java and Spring are required.",
    # "What are good companies for a junior data scientist in Canada?",
    # "I want the best 15 candidates for ML in Europe.",
    # "Give me 8 candidates with > 4 years experience for SDE roles in India.",
    # "Find strong data engineering candidates in USA who know Spark and AWS.",
    # "I want candidates for a full stack web app project, React + Node preferred.",
    # "Need people with cloud + Kubernetes experience, at least 3 years.",
    # "Show some candidates for a fintech recommendation system project.",
    # "Looking for talent for an ecommerce analytics team in Europe.",
    # "Need top machine learning profiles in India for an NLP product.",
    # "Find me people who can do BI dashboards and SQL in the US.",
    # "I’m planning to build an AI chatbot, what roles should I hire and which candidates look promising?",
    # "We are creating a data lake on the cloud, find appropriate data engineer candidates.",
    # "I want to see candidates in Bangalore with strong ML projects.",
    # "Can you list some good backend profiles in Germany with microservices?",
    # "Show me candidates who are good with experimentation and A/B testing.",

    # # A few more variants to push to ~220 queries
    # "Top 10 skills needed for backend developer roles in the US.",
    # "How many jobs require both Python and SQL in Europe?",
    # "List jobs for AI engineer in India.",
    # "Show data scientist jobs in Canada that mention Tableau.",
    # "List candidates in India with projects about recommendation engines and Python skills.",
    # "Which candidates are ready for senior machine learning engineer roles in USA?",
    # "Show candidates in Europe with skills: Python, SQL and dashboards.",
    # "List companies in India that might hire me as a backend developer with Java and Spring Boot.",
    # "Show jobs for a fresher ML engineer in Bangalore.",
    # "Find candidates with 2+ years experience, Python skills and projects in fraud analytics.",
]
# TEST_QUERIES = [
#     # --- Candidate multi-filter (years + skills + projects) ---
#     "Give me candidates who have more than 5 years of experience in Python, machine learning and have projects in natural language processing",

#     # years + skills + location
#     "Find candidates with at least 3 years of experience in backend development who know Node.js and Docker and are based in India",

#     # skills + projects
#     "Show candidates who mention React and TypeScript and have projects related to frontend dashboards",

#     # years + projects
#     "List candidates with more than 7 years of experience who have projects in computer vision",

#     # years + skills + projects + location
#     "Give me candidates in the United States with at least 4 years of experience in data engineering who know SQL and Spark and have projects in ETL pipelines",

#     # --- Candidate single-filter sanity checks ---
#     "Find candidates with at least 10 years of experience",
#     "Show candidates who know Kubernetes and Docker",
#     "Find candidates who have projects in recommendation systems",
#     "List candidates from Canada",

#     # readiness-style
#     "How ready are candidates for a senior backend engineer role in Germany?",

#     # --- Jobs multi-filter (role + skills + location) ---
#     "Show me backend engineer jobs that require Python and Docker in Germany",
#     "Find senior data engineer roles that require SQL and Spark",
#     "List data scientist jobs in Canada that mention machine learning",
#     "Show jobs in the United States that require machine learning and NLP",

#     # --- Jobs single-filter sanity checks ---
#     "List backend engineer jobs",
#     "Find jobs in Australia",
#     "Show jobs that require Python and Kubernetes",
# ]
# TEST_QUERIES = [

# # CANDIDATE_CARTESIAN_QUERIES = [
#     # Y
#     # "Find candidates with at least 5 years of experience.",

#     # # S
#     # "Show candidates who have skills: Python, SQL.",

#     # # P
#     # "List candidates who have projects about fraud detection systems.",

#     # # L
#     # "List candidates from Germany.",

#     # # Y + S
#     # "Find candidates with at least 3 years of experience who have skills: Java, Spring Boot.",

#     # # Y + P
#     # "Show candidates with more than 6 years of experience who have projects in recommendation systems.",

#     # # Y + L
#     # "Find candidates in Canada with at least 4 years of experience.",

#     # # S + P
#     # "Show candidates who have skills: React, TypeScript and have projects related to frontend dashboards.",

#     # # S + L
#     # "List candidates in India who have skills: Node.js, Docker.",

#     # # P + L
#     # "Show candidates in the United States who have projects in computer vision.",

#     # # Y + S + P
#     # "Find candidates with at least 5 years of experience, skills: Python, TensorFlow and projects in deep learning.",

#     # # Y + S + L
#     # "List candidates in Europe with minimum 3 years of experience who have skills: Go, Kubernetes.",

#     # # Y + P + L
#     # "Show candidates in Singapore with more than 7 years of experience who have projects in high-frequency trading systems.",

#     # # S + P + L
#     # "Find candidates in Australia who have skills: Django, PostgreSQL and projects related to ecommerce platforms.",

#     # # Y + S + P + L
#     # "Give me candidates in the United States with at least 4 years of experience who have skills: data engineering, Spark, Kafka and projects in real-time data pipelines.",
# # ]
# # JOB_CARTESIAN_QUERIES = [
#     # R
#     "List backend engineer jobs.",

#     # S
#     "Show jobs that require Python and SQL.",

#     # L
#     "Find jobs in Canada.",

#     # R + S
#     "Show backend engineer roles that require Docker and Kubernetes.",

#     # R + L
#     "List data scientist jobs in India.",

#     # S + L
#     "Find jobs in Germany that require React and TypeScript.",

#     # R + S + L
#     "Show senior data engineer roles in the United States that require SQL and Spark.",
# ]
TEST_QUERIES = [
    "i am looking for people to build my website, for what job roles should i hire for and top 10 potential candidates",
    "What skills are related to Kubernetes?",
    "jobs listing for software engineer in London",
    "List companies I am eligible for as backend engineer in Europe skills: Java, Spring, Kubernetes.",
    "What companies could I join as an Machine Learning engineer if I know Python, PyTorch, and Docker?"
]
def run_single_query(q: str) -> str:
    """Run run_query.py for a single query and return its stdout as a string."""
    cmd = [
        PYTHON_EXE,
        RUN_QUERY,
        q,
        "--config",
        CONFIG_PATH,
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    header = "=" * 80
    block = [
        header,
        f"QUERY: {q}",
        header,
        proc.stdout,
    ]
    if proc.stderr.strip():
        block.append("\n[STDERR]\n" + proc.stderr)
    block.append("\n\n")
    return "\n".join(block)


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(HERE, f"tests_output_report_{timestamp}.txt")

    print(f"Running {len(TEST_QUERIES)} test queries...")
    print(f"Using run_query.py: {RUN_QUERY}")
    print(f"Using config:       {CONFIG_PATH}")
    print(f"Python executable:  {PYTHON_EXE}\n")

    all_output = []
    for i, q in enumerate(TEST_QUERIES, start=1):
        print(f"[{i}/{len(TEST_QUERIES)}] {q}")
        try:
            out = run_single_query(q)
            print(out)
            all_output.append(out)
        except Exception as e:
            all_output.append(
                f"\n{'!'*80}\nERROR while running query: {q}\n{e}\n{'!'*80}\n"
            )

    final_text = "\n".join(all_output)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(final_text)

    print("\nDone.")
    print(f"Full output saved to: {out_path}")
    print("You can now open that file, copy interesting sections, and share them back for review.")


if __name__ == "__main__":
    main()
