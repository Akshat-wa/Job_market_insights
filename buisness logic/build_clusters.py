"""
build_role_and_skill_clusters_efficient.py

Usage example:
    python build_role_and_skill_clusters_efficient.py \
        --config config.yaml \
        --n_job_clusters 100 \
        --n_skill_clusters 50 \
        --max_features 30000 \
        --min_df 10 \
        --top_skills_for_coocc 10000 \
        --max_jobs_for_clustering 400000

Outputs (CSV files under CLUSTER_PATH):
    role_cluster_info.csv
    job_role_clusters.csv
    skill_cluster_info.csv
    skill_cluster_membership.csv
"""

import argparse
import yaml
import psycopg2
import pandas as pd
import numpy as np
from collections import Counter, defaultdict
import os
import time

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import normalize

import scipy.sparse as sp
from tqdm import tqdm

#  Config paths
CLUSTER_PATH = "cluster"
os.makedirs(CLUSTER_PATH, exist_ok=True)


#  Config / DB helpers 
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_connection(cfg: dict) -> psycopg2.extensions.connection:
    info = cfg["jobs_db"]
    if info.get("backend") != "postgres":
        raise ValueError("jobs_db.backend must be 'postgres'")
    con = psycopg2.connect(info["dsn"])
    con.autocommit = False
    return con


#  Job role clustering 

def fetch_job_texts(con) -> pd.DataFrame:
    """
    Build a text representation per job:
      text = title_lc + ' ' + concatenated skill names
    """
    print("📥 Fetching job titles + skills from DB...")
    q = """
        SELECT
            j.job_id,
            COALESCE(j.title_lc, '') AS title_lc,
            COALESCE(string_agg(DISTINCT s.name_lc, ' '), '') AS skills_text
        FROM jobs j
        LEFT JOIN job_skills js ON js.job_id = j.job_id
        LEFT JOIN skills s ON s.skill_id = js.skill_id
        GROUP BY j.job_id, j.title_lc
    """
    t0 = time.time()
    df = pd.read_sql(q, con)
    df["text"] = (df["title_lc"].fillna("") + " " +
                  df["skills_text"].fillna("")).str.strip()
    print(f"🔢 Got {len(df):,} jobs with text (query took {time.time() - t0:.1f}s)")
    return df


def build_job_role_clusters(
    con,
    n_clusters: int = 100,
    max_features: int = 30_000,
    min_df: int = 10,
    max_jobs_for_clustering: int | None = 400_000,
    svd_components: int = 128
):
    """
    1) Fetch job text (title + skills).
    2) Optionally sample jobs for clustering.
    3) Vectorize with TF-IDF (sparse).
    4) Dimensionality reduction via TruncatedSVD (dense, small).
    5) Cluster reduced vectors with MiniBatchKMeans.
    6) Save cluster info + job assignments to CSV.
    """
    jobs_df_full = fetch_job_texts(con)

    if jobs_df_full.empty:
        print("⚠️ No jobs found, skipping job-role clustering.")
        return

    # Optional sampling for safety
    if max_jobs_for_clustering and len(jobs_df_full) > max_jobs_for_clustering:
        print(f"⚠️ Sampling {max_jobs_for_clustering:,} jobs out of {len(jobs_df_full):,} for clustering...")
        jobs_df = jobs_df_full.sample(max_jobs_for_clustering, random_state=42).reset_index(drop=True)
    else:
        jobs_df = jobs_df_full

    texts = jobs_df["text"].tolist()
    job_ids = jobs_df["job_id"].to_numpy()

    print("🧮 Building TF-IDF matrix for jobs...")
    t0 = time.time()
    vectorizer = TfidfVectorizer(
        max_features=max_features,
        min_df=min_df,
        ngram_range=(1, 1)  # unigrams only for efficiency
    )
    X = vectorizer.fit_transform(texts)  # shape: [n_jobs_sample, n_features]
    print(f"   -> TF-IDF matrix shape: {X.shape}, built in {time.time() - t0:.1f}s")

    print(f"🔻 Reducing TF-IDF to {svd_components} dimensions with TruncatedSVD...")
    t0 = time.time()
    svd = TruncatedSVD(n_components=svd_components, random_state=42)
    X_reduced = svd.fit_transform(X)  # dense [n_jobs_sample, svd_components]
    X_reduced = X_reduced.astype(np.float32)
    print(f"   -> Reduced matrix shape: {X_reduced.shape}, SVD took {time.time() - t0:.1f}s")

    print(f"🤖 Clustering {X_reduced.shape[0]:,} jobs into {n_clusters} clusters...")
    t0 = time.time()

    print("🧠 Using CPU MiniBatchKMeans...")
    km = MiniBatchKMeans(
        n_clusters=n_clusters,
        batch_size=10_000,
        max_iter=100,
        random_state=42,
        verbose=1  # prints progress per mini-batch
    )
    labels = km.fit_predict(X_reduced)

    print(f"   -> Clustering completed in {time.time() - t0:.1f}s")

    jobs_df["role_cluster_id"] = labels
    cluster_sizes = Counter(labels)

    feature_names = np.array(vectorizer.get_feature_names_out())
    role_cluster_rows = []

    print("📝 Summarising role clusters (top terms)...")
    # Simple: approximate label by most frequent title words in cluster:
    for cid in range(n_clusters):
        cluster_jobs = jobs_df[jobs_df["role_cluster_id"] == cid]
        # Take up to 500 titles for label creation
        sample_titles = cluster_jobs["title_lc"].dropna().head(500).tolist()
        tokens = " ".join(sample_titles).split()
        if tokens:
            common = Counter(tokens).most_common(5)
            top_terms = [t for t, _ in common]
        else:
            top_terms = []
        label = " / ".join(top_terms[:3]) if top_terms else f"cluster_{cid}"
        size = cluster_sizes.get(cid, 0)
        role_cluster_rows.append({
            "role_cluster_id": cid,
            "label": label,
            "size": size,
            "top_terms": "|".join(top_terms)
        })

    role_cluster_info = pd.DataFrame(role_cluster_rows)
    job_role_clusters = jobs_df[["job_id", "role_cluster_id"]].copy()

    print("💾 Writing role cluster CSVs...")
    role_cluster_info.to_csv(os.path.join(CLUSTER_PATH, "role_cluster_info.csv"), index=False)
    job_role_clusters.to_csv(os.path.join(CLUSTER_PATH, "job_role_clusters.csv"), index=False)
    print("✅ Job role clustering done.")


#  Skill clustering 

def fetch_top_skills_and_pairs(con, top_n: int = 10_000):
    """
    1) Get top-N frequent skills (by job count).
    2) Get all job-skill pairs for those skills.
    """
    cur = con.cursor()

    print("📥 Fetching top frequent skills...")
    t0 = time.time()
    cur.execute("""
        SELECT s.skill_id, s.name_lc, COUNT(*) AS freq
        FROM skills s
        JOIN job_skills js ON js.skill_id = s.skill_id
        GROUP BY s.skill_id, s.name_lc
        ORDER BY freq DESC
        LIMIT %s;
    """, (top_n,))
    rows = cur.fetchall()
    skills_df = pd.DataFrame(rows, columns=["skill_id", "name_lc", "freq"])
    print(f"   -> Got {len(skills_df):,} skills in {time.time() - t0:.1f}s")

    if skills_df.empty:
        print("⚠️ No skills found.")
    cur.close()

    if skills_df.empty:
        return None, None

    skill_ids = skills_df["skill_id"].tolist()

    print("📥 Fetching job-skill pairs for these skills...")
    t0 = time.time()
    cur = con.cursor()
    cur.execute("""
        SELECT js.job_id, js.skill_id
        FROM job_skills js
        WHERE js.skill_id = ANY(%s);
    """, (skill_ids,))
    pairs = cur.fetchall()
    cur.close()
    print(f"   -> Got {len(pairs):,} pairs in {time.time() - t0:.1f}s")

    return skills_df, pairs
def fetch_tail_skill_pairs(con, head_skill_ids):
    """
    Get job-skill pairs for skills that are NOT in the top-N head set.
    Used to 'attach' tail skills to existing clusters.
    """
    if not head_skill_ids:
        return []

    cur = con.cursor()
    t0 = time.time()
    print("📥 Fetching job-skill pairs for tail (non-top-N) skills...")
    cur.execute("""
        SELECT js.job_id, js.skill_id
        FROM job_skills js
        WHERE NOT (js.skill_id = ANY(%s));
    """, (head_skill_ids,))
    rows = cur.fetchall()
    cur.close()
    print(f"   -> Got {len(rows):,} tail pairs in {time.time() - t0:.1f}s")
    return rows


def build_skill_matrix(skills_df, pairs):
    """
    Build a skill-by-job sparse matrix where:
      rows   = skills
      cols   = jobs
      value  = 1 if skill appears in job
    """
    if not pairs:
        return None, None, None

    skill_ids = skills_df["skill_id"].tolist()
    skill_idx = {sid: i for i, sid in enumerate(skill_ids)}

    job_ids = sorted({jid for (jid, _) in pairs})
    job_idx = {jid: j for j, jid in enumerate(job_ids)}

    print(f"🔢 Building sparse matrix for {len(skill_ids)} skills x {len(job_ids)} jobs...")

    data = []
    rows = []
    cols = []

    for jid, sid in pairs:
        rows.append(skill_idx[sid])
        cols.append(job_idx[jid])
        data.append(1)

    mat = sp.csr_matrix(
        (data, (rows, cols)),
        shape=(len(skill_ids), len(job_ids)),
        dtype=np.float32
    )
    return mat, skill_idx, job_idx


def build_skill_clusters(
    con,
    n_clusters: int = 50,
    top_skills_for_coocc: int = 10_000
):
    """
    1) Take top-N most frequent skills (head).
    2) Build skill-by-job co-occurrence matrix for head skills.
    3) Cluster head skills by their job distribution with MiniBatchKMeans.
    4) Project remaining (tail) skills onto these clusters using job co-occurrence.
    5) Save cluster info + membership (with is_core flag) to CSV.
    """
    skills_df, pairs = fetch_top_skills_and_pairs(con, top_skills_for_coocc)
    if skills_df is None:
        print("⚠️ Skipping skill clustering (no skills).")
        return

    mat, skill_idx, job_idx = build_skill_matrix(skills_df, pairs)
    if mat is None:
        print("⚠️ Skipping skill clustering (no pairs).")
        return

    print("🧮 Normalising skill vectors...")
    t0 = time.time()
    mat_norm = normalize(mat, norm="l2", axis=1)
    print(f"   -> Normalised in {time.time() - t0:.1f}s")

    print(f"🤖 Clustering {mat_norm.shape[0]} skills into {n_clusters} clusters...")
    t0 = time.time()
    km = MiniBatchKMeans(
        n_clusters=n_clusters,
        batch_size=1000,
        max_iter=100,
        random_state=42,
        verbose=1
    )
    labels = km.fit_predict(mat_norm)
    print(f"   -> Skill clustering completed in {time.time() - t0:.1f}s")

    # Assign cluster ids to head (top-N) skills
    skills_df["skill_cluster_id"] = labels

    print("📝 Summarising skill clusters (core/head skills only for labels)...")
    cluster_to_names = defaultdict(list)
    for _, row in skills_df.iterrows():
        cluster_to_names[int(row["skill_cluster_id"])].append(row["name_lc"])

    cluster_rows = []
    for cid in range(n_clusters):
        names = cluster_to_names.get(cid, [])
        tokens = " ".join(names).split()
        top_token = Counter(tokens).most_common(1)[0][0] if tokens else ""
        # size will be filled later using full membership (head + tail)
        cluster_rows.append({
            "skill_cluster_id": cid,
            "label": top_token,
            "size": 0
        })

    # Enrich clusters with tail skills

    # 1) Head membership with is_core = True
    head_membership = skills_df[["skill_id", "skill_cluster_id"]].copy()
    head_membership["is_core"] = True

    # 2) Build job -> cluster Counter from head skills
    head_cluster_by_skill = dict(
        zip(skills_df["skill_id"], skills_df["skill_cluster_id"])
    )
    job_to_clusters = defaultdict(Counter)
    for job_id, skill_id in pairs:
        cid = head_cluster_by_skill.get(skill_id)
        if cid is not None:
            job_to_clusters[job_id][cid] += 1

    # 3) Fetch tail job-skill pairs (skills not in top-N)
    head_skill_ids = skills_df["skill_id"].tolist()
    tail_pairs = fetch_tail_skill_pairs(con, head_skill_ids)

    tail_membership = None
    if tail_pairs:
        # group job ids by tail skill
        tail_skill_jobs = defaultdict(list)
        for job_id, sid in tail_pairs:
            tail_skill_jobs[sid].append(job_id)

        tail_rows = []
        print("🧩 Assigning tail skills to nearest existing clusters...")
        for sid, job_ids in tail_skill_jobs.items():
            c_counter = Counter()
            for jid in job_ids:
                if jid in job_to_clusters:
                    c_counter.update(job_to_clusters[jid])
            if not c_counter:
                # this tail skill only appears in jobs with no head skills -> skip
                continue
            cid, _ = c_counter.most_common(1)[0]
            tail_rows.append({
                "skill_id": sid,
                "skill_cluster_id": cid,
                "is_core": False
            })

        if tail_rows:
            tail_membership = pd.DataFrame(tail_rows)

    # 4) Combine head + tail membership
    if tail_membership is not None and not tail_membership.empty:
        skill_cluster_membership = pd.concat(
            [head_membership, tail_membership],
            ignore_index=True
        )
    else:
        skill_cluster_membership = head_membership

    # 5) Recompute cluster sizes based on full membership
    cluster_sizes = (
        skill_cluster_membership.groupby("skill_cluster_id")["skill_id"]
        .size()
        .to_dict()
    )
    for row in cluster_rows:
        row["size"] = int(cluster_sizes.get(row["skill_cluster_id"], 0))

    skill_cluster_info = pd.DataFrame(cluster_rows)

    print("💾 Writing skill cluster CSVs...")
    skill_cluster_info.to_csv(
        os.path.join(CLUSTER_PATH, "skill_cluster_info.csv"), index=False
    )
    skill_cluster_membership.to_csv(
        os.path.join(CLUSTER_PATH, "skill_cluster_membership.csv"), index=False
    )
    print("✅ Skill clustering done (head + tail enrichment).")

# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--n_job_clusters", type=int, default=100)
    ap.add_argument("--n_skill_clusters", type=int, default=50)
    ap.add_argument("--max_features", type=int, default=30000,
                    help="max TF-IDF features for job texts")
    ap.add_argument("--min_df", type=int, default=10,
                    help="min_df for TF-IDF (ignore very rare terms)")
    ap.add_argument("--top_skills_for_coocc", type=int, default=10_000,
                    help="number of most frequent skills for skill clustering")
    ap.add_argument("--max_jobs_for_clustering", type=int, default=400000,
                    help="max number of jobs to sample for role clustering")
    ap.add_argument("--svd_components", type=int, default=128,
                    help="dimension after TruncatedSVD for job vectors")
    args = ap.parse_args()

    cfg = load_config(args.config)
    con = get_connection(cfg)

    try:
        build_job_role_clusters(
            con,
            n_clusters=args.n_job_clusters,
            max_features=args.max_features,
            min_df=args.min_df,
            max_jobs_for_clustering=args.max_jobs_for_clustering,
            svd_components=args.svd_components,
        )
        build_skill_clusters(
            con,
            n_clusters=args.n_skill_clusters,
            top_skills_for_coocc=args.top_skills_for_coocc,
        )
    finally:
        con.close()


if __name__ == "__main__":
    main()
