# database_log.py
import argparse
import datetime as dt
import time
from textwrap import shorten

import psycopg2
import yaml


def load_dsn(config_path: str, db_key: str) -> str:
    """
    Read DSN for jobs_db or candidates_db from your existing config.yaml.

    Example config.yaml:

      jobs_db:
        backend: "postgres"
        dsn: "host=localhost port=5433 dbname=job_market user=postgres password=changeme"

      candidates_db:
        backend: "postgres"
        dsn: "host=localhost port=5432 dbname=candidates user=postgres password=changeme"
    """
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if db_key not in cfg:
        raise SystemExit(f"[ERROR] No entry '{db_key}' in {config_path}")
    dsn = cfg[db_key].get("dsn")
    if not dsn:
        raise SystemExit(f"[ERROR] '{db_key}' has no 'dsn' in {config_path}")
    return dsn


def main():
    parser = argparse.ArgumentParser(
        description="Simple Postgres activity logger using pg_stat_activity."
    )
    parser.add_argument(
        "--db-key",
        required=True,
        choices=["jobs_db", "candidates_db"],
        help="Which DB config entry to use from config.yaml",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to your config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Polling interval in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--outfile",
        default=None,
        help="Log file path (default: <db-key>_activity.log)",
    )
    args = parser.parse_args()

    dsn = load_dsn(args.config, args.db_key)
    outfile = args.outfile or f"{args.db_key}_activity.log"

    print(f"[INFO] Connecting to Postgres for {args.db_key}")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()

    seen = set()

    with open(outfile, "a", encoding="utf-8") as f:
        header = f"# Starting activity logger for {args.db_key} at {dt.datetime.now()}\n"
        f.write(header)
        f.flush()
        print(header.strip())

        try:
            while True:
                cur.execute(
                    """
                    SELECT
                        pid,
                        query_start,
                        now() AS seen_at,
                        usename,
                        client_addr,
                        state,
                        query
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND state = 'active'
                      AND query NOT ILIKE '%pg_stat_activity%'
                    ORDER BY query_start DESC;
                    """
                )

                rows = cur.fetchall()
                for pid, q_start, seen_at, user, client, state, query in rows:
                    key = (pid, q_start)
                    if key in seen:
                        continue
                    seen.add(key)

                    line = (
                        f"[{seen_at:%Y-%m-%d %H:%M:%S}] "
                        f"pid={pid} user={user} client={client} state={state} "
                        f"query={shorten(query, width=200, placeholder='...')}\n"
                    )
                    f.write(line)
                    f.flush()
                    print(line, end="")

                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[INFO] Stopping logger.")
        finally:
            cur.close()
            conn.close()


if __name__ == "__main__":
    main()
