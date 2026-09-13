"""Phase 6.1: the human review record and workflow for a Phase 6 application
package. This is a review GATE, not an execution surface -- nothing here
calls an LLM, touches a browser, or submits anything. It only reads a
package Phase 6 already wrote (applications/pending/<job_id>/) and records
a human's judgment about it, in applications/review/<job_id>/review.json.

Phase 5's job record is read-only input here (company/title/score/decision/
role_family/career_level for display) -- this module never writes back to
data/jobs.json, and a review verdict never changes those fields or the
underlying application_package.json Phase 6 produced.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REVIEW_DIR = REPO_ROOT / "applications" / "review"
DEFAULT_PENDING_DIR = REPO_ROOT / "applications" / "pending"
DEFAULT_JOBS_PATH = REPO_ROOT / "data" / "jobs.json"

REVIEW_STATUSES = {"PENDING", "APPROVED", "NEEDS_CHANGES", "REJECTED"}
QUALITY_SCORES = {1, 2, 3, 4, 5}
QUALITY_FIELDS = [
    "analysis_quality", "resume_quality", "cover_letter_quality", "answers_quality",
    "truthfulness", "overall_quality",
]
# Jobs a human reviewer is ever asked to look at -- SKIP jobs never reach
# Phase 6 at all (see run_phase6.select_jobs()), so they never reach review
# either; this set exists so callers here don't have to re-derive that rule.
REVIEWABLE_DECISIONS = {"AUTO_APPLY", "REVIEW"}


def make_review_record(job_id: str) -> dict[str, Any]:
    """A fresh, unreviewed record matching the schema exactly."""
    return {
        "job_id": job_id,
        "review_status": "PENDING",
        "analysis_quality": None,
        "resume_quality": None,
        "cover_letter_quality": None,
        "answers_quality": None,
        "truthfulness": None,
        "overall_quality": None,
        "issues": [],
        "needs_regeneration": False,
        "review_notes": "",
        "reviewed_at": None,
    }


def validate_review_record(record: dict[str, Any]) -> list[str]:
    """Return a list of validation errors, or [] if the record is well-formed.
    Reviewers are never forced to score every component -- a quality field
    left as None is valid; only a NON-None value must be a real 1-5 score."""
    errors: list[str] = []
    if not record.get("job_id"):
        errors.append("job_id is required")
    status = record.get("review_status")
    if status not in REVIEW_STATUSES:
        errors.append(f"review_status must be one of {sorted(REVIEW_STATUSES)}, got {status!r}")
    for field in QUALITY_FIELDS:
        value = record.get(field)
        if value is not None and value not in QUALITY_SCORES:
            errors.append(f"{field} must be null or one of {sorted(QUALITY_SCORES)} (1=unacceptable..5=excellent), got {value!r}")
    if not isinstance(record.get("issues", []), list):
        errors.append("issues must be a list of strings")
    if not isinstance(record.get("needs_regeneration", False), bool):
        errors.append("needs_regeneration must be a boolean")
    if not isinstance(record.get("review_notes", ""), str):
        errors.append("review_notes must be a string")
    return errors


def _review_path(job_id: str, review_dir: Path) -> Path:
    return review_dir / job_id / "review.json"


def load_review(job_id: str, review_dir: Path = DEFAULT_REVIEW_DIR) -> dict[str, Any] | None:
    """Return the persisted review record for a job, or None if it has never
    been reviewed (not the same as an invalid record -- a missing file is a
    normal, expected state for anything still PENDING)."""
    path = _review_path(job_id, review_dir)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_review(record: dict[str, Any], review_dir: Path = DEFAULT_REVIEW_DIR) -> Path:
    """Validate and persist a review record. Raises ValueError (never writes
    anything) if the record is malformed -- a reviewer typo must never
    silently corrupt the review file."""
    errors = validate_review_record(record)
    if errors:
        raise ValueError("Invalid review record: " + "; ".join(errors))
    path = _review_path(record["job_id"], review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def apply_review_action(
    job_id: str, action: str, *, review_dir: Path = DEFAULT_REVIEW_DIR,
    quality_scores: dict[str, int | None] | None = None, issues: list[str] | None = None,
    review_notes: str | None = None, needs_regeneration: bool | None = None,
) -> dict[str, Any]:
    """Load the existing review (or start a fresh PENDING one), apply the
    given action plus any provided fields, validate, save, and return the
    resulting record. `action` is one of "approve", "needs_changes",
    "reject", "save" (save persists whatever fields were given without
    forcing a status change -- it keeps the existing/default status)."""
    action_to_status = {"approve": "APPROVED", "needs_changes": "NEEDS_CHANGES", "reject": "REJECTED"}
    if action not in (*action_to_status, "save"):
        raise ValueError(f"Unknown review action {action!r}; must be one of approve/needs_changes/reject/save")

    record = load_review(job_id, review_dir) or make_review_record(job_id)
    if action in action_to_status:
        record["review_status"] = action_to_status[action]

    if quality_scores:
        for field, value in quality_scores.items():
            if field not in QUALITY_FIELDS:
                raise ValueError(f"Unknown quality field {field!r}; must be one of {QUALITY_FIELDS}")
            record[field] = value
    if issues is not None:
        record["issues"] = list(issues)
    if review_notes is not None:
        record["review_notes"] = review_notes
    if needs_regeneration is not None:
        record["needs_regeneration"] = needs_regeneration

    record["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    save_review(record, review_dir)
    return record


def load_application_package(job_id: str, pending_dir: Path = DEFAULT_PENDING_DIR) -> dict[str, Any] | None:
    path = pending_dir / job_id / "application_package.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def list_pending_review(
    jobs: list[dict[str, Any]], *, pending_dir: Path = DEFAULT_PENDING_DIR, review_dir: Path = DEFAULT_REVIEW_DIR,
) -> list[dict[str, Any]]:
    """Summaries for every job appropriate for Phase 6.1 review: it must
    have a Phase 5 decision Phase 6 actually processes (AUTO_APPLY or
    REVIEW -- SKIP is never included, matching run_phase6.select_jobs()'s
    own rule) AND already have a Phase 6 application package on disk. A
    REVIEW-decision job without a package yet just hasn't been through
    Phase 6 -- nothing to review until it has been."""
    summaries = []
    for job in jobs:
        if job.get("application_decision") not in REVIEWABLE_DECISIONS:
            continue
        package = load_application_package(job["id"], pending_dir)
        if package is None:
            continue
        review = load_review(job["id"], review_dir)
        summaries.append({
            "job_id": job["id"],
            "company": job.get("company"),
            "title": job.get("title"),
            "score": job.get("score"),
            "application_decision": job.get("application_decision"),
            "readiness": package.get("readiness"),
            "review_status": (review or {}).get("review_status", "PENDING"),
        })
    return summaries
