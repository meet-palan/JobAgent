"""Assembles one structured application package per Phase-6-processed job and
runs the deterministic readiness check. Phase 5's application_decision is
read here, never recomputed or overridden -- see check_readiness()'s first
gate. Nothing in this module ever sets ready_for_browser_automation=True by
itself; it can only report what already passed every check, and Phase 7
(not built here) is the only thing that would ever act on that flag.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING_DIR = REPO_ROOT / "applications" / "pending"


def _artifact_status(record: dict[str, Any] | None) -> str:
    if record is None:
        return "NOT_PROCESSED"
    return record.get("status", "FAILED")


def check_readiness(
    job: dict[str, Any], analysis_record: dict[str, Any] | None, resume_record: dict[str, Any] | None,
    cover_letter_record: dict[str, Any] | None, answer_records: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Returns ("READY"|"NOT_READY", [reasons]). Every reason listed is a
    concrete, checkable fact -- never a vague catch-all -- so a human (or
    Phase 7 later) knows exactly what to fix or provide."""
    reasons = []

    if not (job.get("id") and job.get("title") and job.get("company")):
        reasons.append("Job record is missing id/title/company.")
    url = job.get("application_url") or ""
    if not (url.startswith("http")):
        reasons.append("Job has no valid application_url.")

    decision = job.get("application_decision")
    if decision not in ("AUTO_APPLY", "REVIEW"):
        reasons.append(f"Phase 5 decision is {decision!r} -- Phase 6 packages are only readied for AUTO_APPLY/REVIEW.")

    if _artifact_status(analysis_record) != "COMPLETED":
        reasons.append(f"Job analysis is not COMPLETED (status: {_artifact_status(analysis_record)}).")

    if _artifact_status(resume_record) != "COMPLETED":
        reasons.append(f"Resume is not COMPLETED (status: {_artifact_status(resume_record)}).")
    elif resume_record.get("validation", {}).get("status") != "OK":
        reasons.append(f"Resume failed claim validation: {resume_record['validation']['issues']}")

    if cover_letter_record and cover_letter_record.get("cover_letter_needed"):
        if _artifact_status(cover_letter_record) != "COMPLETED":
            reasons.append(f"Cover letter is needed but not COMPLETED (status: {_artifact_status(cover_letter_record)}).")
        elif cover_letter_record.get("validation", {}).get("status") != "OK":
            reasons.append(f"Cover letter failed claim validation: {cover_letter_record['validation']['issues']}")

    needs_input = [a["question"] for a in answer_records if a.get("confidence") == "NEEDS_USER_INPUT"]
    if needs_input:
        reasons.append(f"Application question(s) still need user input: {needs_input}")
    failed_answers = [a["question"] for a in answer_records if a.get("status") == "FAILED"]
    if failed_answers:
        reasons.append(f"Application question(s) failed to generate: {failed_answers}")

    return ("NOT_READY", reasons) if reasons else ("READY", [])


def assemble_package(
    job: dict[str, Any], analysis_record: dict[str, Any] | None, resume_record: dict[str, Any] | None,
    cover_letter_record: dict[str, Any] | None, answer_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Builds application_package.json's content. requires_human_review is
    always True for a REVIEW-decision job (Phase 5's own reasoning is
    surfaced verbatim in review_reasons) and additionally True whenever any
    artifact came back NEEDS_REVIEW -- REVIEW is never silently upgraded to
    behave like AUTO_APPLY, and an AUTO_APPLY job is never treated as if
    human review were unnecessary just because Phase 5 said AUTO_APPLY."""
    readiness, readiness_reasons = check_readiness(job, analysis_record, resume_record, cover_letter_record, answer_records)

    review_reasons = []
    if job.get("application_decision") == "REVIEW":
        review_reasons.append(job.get("decision_reason", "Phase 5 marked this job REVIEW."))
    for label, record in (("Resume", resume_record), ("Cover letter", cover_letter_record), ("Analysis", analysis_record)):
        if record and record.get("status") == "NEEDS_REVIEW":
            review_reasons.append(f"{label} needs review: {record.get('validation', {}).get('issues') or record.get('error')}")

    return {
        "job_id": job["id"], "company": job.get("company"), "title": job.get("title"),
        "phase5_decision": job.get("application_decision"), "phase5_score": job.get("score"),
        "analysis_status": _artifact_status(analysis_record),
        "resume_status": _artifact_status(resume_record),
        "cover_letter_status": _artifact_status(cover_letter_record),
        "answers_status": "COMPLETED" if answer_records and all(a.get("status") == "COMPLETED" for a in answer_records) else "INCOMPLETE",
        "requires_human_review": bool(review_reasons),
        "review_reasons": review_reasons,
        "ready_for_browser_automation": readiness == "READY",
        "readiness": readiness,
        "readiness_reasons": readiness_reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_package(job_id: str, package: dict[str, Any], analysis_record, resume_record, cover_letter_record,
                   answer_records: list[dict[str, Any]], pending_dir: Path = DEFAULT_PENDING_DIR) -> Path:
    job_dir = pending_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("analysis.json", analysis_record), ("resume.json", resume_record),
        ("cover_letter.json", cover_letter_record), ("answers.json", answer_records),
        ("application_package.json", package),
    ):
        if data is None:
            continue
        with (job_dir / name).open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return job_dir
