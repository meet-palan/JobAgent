"""Phase 6.1 human review CLI -- the explicit gate between Phase 6's
generated application package and any future submission step. Flag-driven
(not an interactive prompt loop) so it's simple to script and to test: with
no action flag, it just shows the package for a human to read; with exactly
one action flag, it records that verdict and persists review.json.

This script does not call an LLM, does not open a browser, and does not
submit anything -- it only reads what scripts/run_phase6.py already wrote
and records a human's judgment about it.

Usage:
    python scripts/review_application.py --list-pending
    python scripts/review_application.py --job-id <id>
    python scripts/review_application.py --job-id <id> --approve --notes "Looks good"
    python scripts/review_application.py --job-id <id> --needs-changes \\
        --resume-quality 2 --issue "Summary mentions the experience gap" --notes "Regenerate resume"
    python scripts/review_application.py --job-id <id> --reject --notes "Not a genuine fit"
    python scripts/review_application.py --job-id <id> --save --overall-quality 3 --notes "Partial review so far"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review as rv

DEFAULT_JOBS_PATH = rv.DEFAULT_JOBS_PATH


def load_jobs(path: Path = DEFAULT_JOBS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def find_job(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    return next((j for j in jobs if j.get("id") == job_id), None)


def print_pending_table(summaries: list[dict[str, Any]]) -> None:
    if not summaries:
        print("No packages awaiting review (only AUTO_APPLY/REVIEW jobs with an existing Phase 6 package qualify).")
        return
    header = f"{'JOB ID':<55}{'COMPANY':<16}{'TITLE':<32}{'SCORE':>6}  {'DECISION':<11}{'READINESS':<12}{'REVIEW':<14}"
    print(header)
    for s in summaries:
        print(
            f"{s['job_id']:<55.55}{(s['company'] or ''):<16.16}{(s['title'] or ''):<32.32}"
            f"{s['score'] if s['score'] is not None else '-':>6}  {s['application_decision']:<11}"
            f"{(s['readiness'] or '-'):<12}{s['review_status']:<14}"
        )


def print_package_view(job: dict[str, Any], package: dict[str, Any] | None, review: dict[str, Any] | None, pending_dir: Path) -> None:
    print(f"Job:              {job.get('title')} @ {job.get('company')}  ({job['id']})")
    print(f"Phase 5 score:    {job.get('score')}")
    print(f"Role family:      {job.get('role_family')}")
    print(f"Career level:     {job.get('career_level')}")
    print(f"Decision:         {job.get('application_decision')}  -- {job.get('decision_reason')}")
    if package is None:
        print("\nNo Phase 6 application package found for this job yet -- nothing to review.")
        return
    print(f"Readiness:        {package.get('readiness')}  (ready_for_browser_automation={package.get('ready_for_browser_automation')})")
    print(f"Requires review:  {package.get('requires_human_review')}  {package.get('review_reasons')}")
    job_dir = pending_dir / job["id"]
    print("\nGenerated artifacts:")
    for name in ("analysis.json", "resume.json", "cover_letter.json", "answers.json"):
        path = job_dir / name
        print(f"  {name:<20} {'present' if path.exists() else 'MISSING'}  ({path})")

    print("\nCurrent review status:")
    review = review or rv.make_review_record(job["id"])
    for field in ["review_status", *rv.QUALITY_FIELDS, "issues", "needs_regeneration", "review_notes", "reviewed_at"]:
        print(f"  {field:<20} {review.get(field)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", type=Path, default=rv.DEFAULT_JOBS_PATH)
    parser.add_argument("--pending-dir", type=Path, default=rv.DEFAULT_PENDING_DIR)
    parser.add_argument("--review-dir", type=Path, default=rv.DEFAULT_REVIEW_DIR)
    parser.add_argument("--job-id", type=str)
    parser.add_argument("--list-pending", action="store_true")

    action = parser.add_mutually_exclusive_group()
    action.add_argument("--approve", action="store_true")
    action.add_argument("--needs-changes", action="store_true")
    action.add_argument("--reject", action="store_true")
    action.add_argument("--save", action="store_true")

    for field in rv.QUALITY_FIELDS:
        parser.add_argument(f"--{field.replace('_', '-')}", type=int, default=None, choices=sorted(rv.QUALITY_SCORES))
    parser.add_argument("--issue", action="append", default=None, help="Repeatable; replaces the issues list when given")
    parser.add_argument("--notes", type=str, default=None)
    parser.add_argument("--needs-regeneration", action="store_true", default=None)

    args = parser.parse_args(argv)

    if args.list_pending:
        jobs = load_jobs(args.jobs)
        summaries = rv.list_pending_review(jobs, pending_dir=args.pending_dir, review_dir=args.review_dir)
        print_pending_table(summaries)
        return 0

    if not args.job_id:
        parser.error("--job-id is required unless --list-pending is given")

    jobs = load_jobs(args.jobs)
    job = find_job(jobs, args.job_id)
    if job is None:
        print(f"No job with id {args.job_id!r} found in {args.jobs}", file=sys.stderr)
        return 1

    action_taken = next((a for a in ("approve", "needs_changes", "reject", "save") if getattr(args, a)), None)
    if action_taken is None:
        package = rv.load_application_package(args.job_id, args.pending_dir)
        review = rv.load_review(args.job_id, args.review_dir)
        print_package_view(job, package, review, args.pending_dir)
        return 0

    package = rv.load_application_package(args.job_id, args.pending_dir)
    if package is None:
        print(f"No Phase 6 application package found for {args.job_id!r} -- nothing to review.", file=sys.stderr)
        return 1

    quality_scores = {field: getattr(args, field) for field in rv.QUALITY_FIELDS if getattr(args, field) is not None}
    try:
        record = rv.apply_review_action(
            args.job_id, action_taken, review_dir=args.review_dir,
            quality_scores=quality_scores or None, issues=args.issue,
            review_notes=args.notes, needs_regeneration=args.needs_regeneration,
        )
    except ValueError as e:
        print(f"Could not save review: {e}", file=sys.stderr)
        return 1

    print(f"Saved review for {args.job_id}: review_status={record['review_status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
