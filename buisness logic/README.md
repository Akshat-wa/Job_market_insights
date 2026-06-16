<h1>Job Market Insights - Find what job suits you and find candidates which suit your role!</h1>

<h2>Job-only queries (jobs_db)</h2>
<h3> Count jobs requiring a skill</h3>

- task = "count_jobs_by_skill"
- DB: jobs_db

- Example: <b>How many jobs require Python in India?</b>

<h3>Top-K skills for a role</h3>

- task = "top_skills_for_role"

DB: jobs_db

Example:

Top 10 skills for data scientist in India

<h3>List jobs for a role</h3>

task = "list_jobs_for_role"

DB: jobs_db

Example:

List jobs for machine learning engineer in Bangalore

<h3>Eligible companies for a candidate (by role + optional skills)</h3>

task = "eligible_companies_for_candidate"

DB: jobs_db

Example:

Which companies am I eligible for as data engineer in India, skills: Python, Spark, SQL

<h2>B. Candidate-only queries (candidates_db)</h2>

Filter candidates by min years (+ optional role & location)

task = "filter_candidates_by_experience_role"

Single-source case when no role → only candidates_db.

Example:

List candidates with at least 3 years of experience in India

Filter candidates by skills

task = "filter_candidates_by_skills"

DB: candidates_db

Example:

Show candidates with skills: Python, TensorFlow, SQL in Bangalore

<h3>Candidates by location only</h3>

task = "filter_candidates_by_location"

DB: candidates_db

Example:

Which candidates are in Mumbai?

<h3>Candidates by projects/topic</h3>

task = "filter_candidates_by_projects"

DB: candidates_db (via projects table)

Example:

Show candidates who have done projects on fraud detection systems

<h3>Top candidates by skill count</h3>

task = "top_candidates_by_skill_count"

DB: candidates_db

Example:

Show top 10 candidates with the most skills in India

C. Federated queries (jobs_db + candidates_db)

<h3>Candidate readiness for a role</h3>

task = "candidate_readiness"

DBs: jobs_db (role skills) + candidates_db (candidate skills, experience)

Example:

Which candidates are ready for machine learning engineer in India, top 20?

<h3>Filter candidates by experience AND role skills</h3>

task = "filter_candidates_by_experience_role"

DBs: candidates_db + jobs_db (if role is present)

Example:

Which candidates have at least 5 years of experience for data engineer in India?

<h3>Candidate profile + eligible jobs</h3>

task = "candidate_profile_and_eligible_jobs"

DBs: candidates_db + jobs_db

Example:

Which jobs is candidate John Doe eligible for in Bangalore?
<h3>Show profile for candidate John Doe</h3>
