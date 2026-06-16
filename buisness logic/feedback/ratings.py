"""Explicit thumbs feedback on successful answers — human review takes priority over auto-fixes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from feedback.capture import _append_record, _feedback_cfg, capture_failure, feedback_enabled


def ratings_enabled(cfg: dict | None) -> bool:
    if not feedback_enabled(cfg):
        return False
    fb = _feedback_cfg(cfg)
    return fb.get("ratings_enabled", True) is not False


def ratings_log_path(cfg: dict | None) -> Path:
    fb = _feedback_cfg(cfg)
    rel = fb.get("ratings_log_path") or "feedback/ratings.jsonl"
    base = Path(__file__).resolve().parent.parent
    return base / rel if not rel.startswith("/") else Path(rel)


def _read_all(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    rows: List[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def capture_rating(
    cfg: dict | None,
    *,
    rating: int,
    question: str = "",
    session_id: str = "",
    query_id: str = "",
    summary: str = "",
    task: str = "",
    summary_source: str = "",
    mode: str = "",
    comment: str = "",
    linked_failure_id: str = "",
) -> Optional[dict]:
    """
    Store a thumbs up/down rating. Negative ratings can escalate to the failure log
    for human review (auto-diagnosis still runs, but humans decide what ships).
    """
    if not ratings_enabled(cfg):
        return None

    if rating not in (1, -1):
        raise ValueError("rating must be 1 (helpful) or -1 (not helpful)")

    path = ratings_log_path(cfg)
    record: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "rating": rating,
        "question": (question or "")[:2000],
        "session_id": session_id or "",
        "query_id": query_id or "",
        "summary_snippet": (summary or "")[:800],
        "task": task or "",
        "summary_source": summary_source or "",
        "mode": mode or "",
        "comment": (comment or "")[:2000],
        "linked_failure_id": linked_failure_id or "",
        "review_status": "open" if rating < 0 else "acknowledged",
        "review_notes": "",
        "review_priority": "human" if rating < 0 else "low",
    }

    fb = _feedback_cfg(cfg)
    escalate = fb.get("escalate_negative_to_failure", True)
    failure_id = None
    if rating < 0 and escalate:
        failure_id = capture_failure(
            cfg,
            kind="user_rating_negative",
            question=question,
            session_id=session_id,
            error=comment or "User marked response as not helpful",
            context={
                "stage": "user_rating",
                "query_id": query_id,
                "task": task,
                "summary_source": summary_source,
                "summary_snippet": record["summary_snippet"],
                "rating": rating,
                "review_priority": "human",
            },
            fallback_used=summary_source or "unknown",
        )
        if failure_id:
            record["linked_failure_id"] = failure_id

    rid = _append_record(path, record)
    print(f"[feedback] rating {rating:+d} id={rid[:8]}... -> {path}")
    return {"id": rid, "linked_failure_id": record.get("linked_failure_id")}


def list_ratings(
    cfg: dict | None = None,
    *,
    status: str = "open",
    limit: int = 50,
    negative_only: bool = False,
) -> List[dict]:
    path = ratings_log_path(cfg)
    rows = _read_all(path)

    if negative_only:
        rows = [r for r in rows if r.get("rating", 0) < 0]
    if status and status != "all":
        rows = [r for r in rows if r.get("review_status") == status]

    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    rows.sort(key=lambda r: 0 if r.get("review_priority") == "human" else 1)
    return rows[:limit]


def mark_rating(
    rating_id: str,
    *,
    status: str = "reviewed",
    notes: str = "",
    cfg: dict | None = None,
) -> bool:
    path = ratings_log_path(cfg)
    if not path.is_file():
        return False

    updated = False
    out_lines: List[str] = []
    for rec in _read_all(path):
        if rec.get("id") == rating_id or rec.get("id", "").startswith(rating_id):
            rec["review_status"] = status
            if notes:
                rec["review_notes"] = notes
            rec["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            updated = True
        out_lines.append(json.dumps(rec, ensure_ascii=False))

    if updated:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    return updated
