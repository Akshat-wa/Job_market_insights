#!/usr/bin/env python3
"""Review captured failures and user ratings — human loop before shipping fixes."""
from __future__ import annotations

import argparse
import json

from config_loader import load_config
from feedback.capture import list_failures, log_path, mark_failure
from feedback.ratings import list_ratings, mark_rating, ratings_log_path


def main():
    ap = argparse.ArgumentParser(description="Review NL/query failures and user ratings")
    ap.add_argument("--config", default="config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List failure records")
    p_list.add_argument("--status", default="open", choices=["open", "reviewed", "fixed", "all"])
    p_list.add_argument("--limit", type=int, default=30)

    p_show = sub.add_parser("show", help="Show one failure by id (prefix ok)")
    p_show.add_argument("failure_id")

    p_mark = sub.add_parser("mark", help="Update failure review status")
    p_mark.add_argument("failure_id")
    p_mark.add_argument("--status", default="reviewed", choices=["open", "reviewed", "fixed"])
    p_mark.add_argument("--notes", default="", help="What you changed / plan to fix")

    p_ratings = sub.add_parser("ratings", help="List user thumbs ratings (human-priority queue)")
    p_ratings.add_argument("--status", default="open", choices=["open", "reviewed", "fixed", "acknowledged", "all"])
    p_ratings.add_argument("--negative-only", action="store_true")
    p_ratings.add_argument("--limit", type=int, default=30)

    p_rate_mark = sub.add_parser("rate-mark", help="Mark a user rating as reviewed")
    p_rate_mark.add_argument("rating_id")
    p_rate_mark.add_argument("--status", default="reviewed", choices=["open", "reviewed", "fixed", "acknowledged"])
    p_rate_mark.add_argument("--notes", default="")

    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "list":
        rows = list_failures(cfg, status=args.status, limit=args.limit)
        print(f"Log: {log_path(cfg)} ({len(rows)} shown)\n")
        for r in rows:
            diag = r.get("diagnosis") or {}
            analysis = diag.get("analysis") or {}
            fix = analysis.get("suggested_fix") if isinstance(analysis, dict) else ""
            print(f"{r.get('id','')[:8]}  {r.get('ts','')[:19]}  [{r.get('kind')}]  {r.get('review_status')}")
            print(f"  Q: {(r.get('question') or '')[:90]}")
            if r.get("error"):
                print(f"  Err: {str(r.get('error'))[:100]}")
            if fix:
                print(f"  Fix hint: {str(fix)[:120]}")
            print()

    elif args.cmd == "show":
        prefix = args.failure_id
        rows = list_failures(cfg, status="all", limit=10_000)
        match = next((r for r in rows if r.get("id", "").startswith(prefix)), None)
        if not match:
            print(f"No failure with id prefix: {prefix}")
            return
        print(json.dumps(match, indent=2, ensure_ascii=False))

    elif args.cmd == "mark":
        rows = list_failures(cfg, status="all", limit=10_000)
        full = next((r["id"] for r in rows if r.get("id", "").startswith(args.failure_id)), None)
        if not full:
            print("Failure id not found.")
            return
        ok = mark_failure(full, status=args.status, notes=args.notes, cfg=cfg)
        print("Updated." if ok else "Update failed.")

    elif args.cmd == "ratings":
        rows = list_ratings(
            cfg,
            status=args.status,
            limit=args.limit,
            negative_only=args.negative_only,
        )
        print(f"Ratings log: {ratings_log_path(cfg)} ({len(rows)} shown)\n")
        for r in rows:
            thumb = "👍" if r.get("rating", 0) > 0 else "👎"
            pri = r.get("review_priority", "")
            print(
                f"{r.get('id','')[:8]}  {r.get('ts','')[:19]}  {thumb}  "
                f"[{r.get('review_status')}]  priority={pri}"
            )
            print(f"  Q: {(r.get('question') or '')[:90]}")
            if r.get("comment"):
                print(f"  Comment: {str(r.get('comment'))[:120]}")
            if r.get("linked_failure_id"):
                print(f"  Linked failure: {str(r.get('linked_failure_id'))[:8]}…")
            print()

    elif args.cmd == "rate-mark":
        rows = list_ratings(cfg, status="all", limit=10_000)
        full = next((r["id"] for r in rows if r.get("id", "").startswith(args.rating_id)), None)
        if not full:
            print("Rating id not found.")
            return
        ok = mark_rating(full, status=args.status, notes=args.notes, cfg=cfg)
        print("Updated." if ok else "Update failed.")


if __name__ == "__main__":
    main()
