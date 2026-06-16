"""
load_clusters_to_db.py

Loads clustering outputs from CSVs into Postgres tables so run_query/analyzer
can use them via SQL JOINs.

Assumes the following files exist (from build_role_and_skill_clusters_efficient.py):

    cluster/role_cluster_info.csv
    cluster/job_role_clusters.csv
    cluster/skill_cluster_info.csv
    cluster/skill_cluster_membership.csv

Usage:
    python load_clusters_to_db.py --config config.yaml
"""

import argparse
import os
import yaml
import psycopg2

CLUSTER_PATH = "cluster"  # same as in your clustering script


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


def drop_cluster_tables(cur):
    """
    Drop existing cluster tables so we can recreate them cleanly.
    """
    print("🗑️ Dropping existing cluster tables (if any)...")

    # Order doesn't strictly matter here since there are no FKs between them,
    # but we drop the "membership" tables first for cleanliness.
    cur.execute("DROP TABLE IF EXISTS job_role_clusters;")
    cur.execute("DROP TABLE IF EXISTS role_clusters;")
    cur.execute("DROP TABLE IF EXISTS skill_cluster_membership;")
    cur.execute("DROP TABLE IF EXISTS skill_clusters;")


def ensure_cluster_tables(cur):
    """
    Create cluster tables (and indexes) if they don't exist yet.
    """
    print("📐 Ensuring cluster tables exist...")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS role_clusters (
            role_cluster_id INTEGER PRIMARY KEY,
            label           TEXT,
            size            INTEGER,
            top_terms       TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_role_clusters (
            job_id          INTEGER PRIMARY KEY,
            role_cluster_id INTEGER NOT NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS skill_clusters (
            skill_cluster_id INTEGER PRIMARY KEY,
            label            TEXT,
            size             INTEGER
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS skill_cluster_membership (
            skill_id         INTEGER PRIMARY KEY,
            skill_cluster_id INTEGER NOT NULL,
            is_core          BOOLEAN NOT NULL DEFAULT TRUE,
            FOREIGN KEY (skill_id) REFERENCES skills(skill_id)
        );
    """)


    # Helpful indexes for querying by cluster
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_job_role_clusters_cluster
        ON job_role_clusters(role_cluster_id);
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_skill_cluster_membership_cluster
        ON skill_cluster_membership(skill_cluster_id);
    """)


def copy_csv(cur, table_name: str, cols, filename: str):
    """
    COPY a CSV file (with header row) into a table.
    """
    path = os.path.join(CLUSTER_PATH, filename)
    print(f"Loading {path} -> {table_name} ...")

    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV file not found: {path}")

    col_list = ", ".join(cols)
    sql = f"""
        COPY {table_name} ({col_list})
        FROM STDIN WITH (FORMAT CSV, HEADER TRUE)
    """

    with open(path, "r", encoding="utf-8") as f:
        cur.copy_expert(sql, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    con = get_connection(cfg)
    cur = con.cursor()

    try:
        # Drop & recreate tables for a completely clean load
        drop_cluster_tables(cur)
        ensure_cluster_tables(cur)

        # Load role clusters
        copy_csv(
            cur,
            table_name="role_clusters",
            cols=["role_cluster_id", "label", "size", "top_terms"],
            filename="role_cluster_info.csv",
        )
        copy_csv(
            cur,
            table_name="job_role_clusters",
            cols=["job_id", "role_cluster_id"],
            filename="job_role_clusters.csv",
        )

        # Load skill clusters
        copy_csv(
            cur,
            table_name="skill_clusters",
            cols=["skill_cluster_id", "label", "size"],
            filename="skill_cluster_info.csv",
        )
        copy_csv(
            cur,
            table_name="skill_cluster_membership",
            cols=["skill_id", "skill_cluster_id", "is_core"],
            filename="skill_cluster_membership.csv",
        )


        con.commit()
        print("Cluster tables loaded successfully.")

    except Exception as e:
        con.rollback()
        print("Error while loading cluster tables:", e)
        raise
    finally:
        cur.close()
        con.close()


if __name__ == "__main__":
    main()
