"""Tests for scripts/review.py and scripts/review_application.py (Phase 6.1
human review workflow). No LLM calls anywhere -- this layer only reads
Phase 6's already-generated packages and records human judgments about them.

Run directly:
    python tests/review.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import review as rv  # noqa: E402
import review_application as ra  # noqa: E402


def make_job(**overrides) -> dict:
    base = {
        "id": "job-1", "title": "Business Analyst", "company": "Acme",
        "application_decision": "REVIEW", "score": 83, "role_family": "BUSINESS_ANALYSIS",
        "career_level": "MID_LEVEL", "decision_reason": "Worth a human look.",
    }
    base.update(overrides)
    return base


def write_package(pending_dir: Path, job_id: str, **overrides) -> Path:
    package = {
        "job_id": job_id, "company": "Acme", "title": "Business Analyst",
        "phase5_decision": "REVIEW", "phase5_score": 83,
        "analysis_status": "COMPLETED", "resume_status": "COMPLETED",
        "cover_letter_status": "COMPLETED", "answers_status": "COMPLETED",
        "requires_human_review": True, "review_reasons": ["Worth a human look."],
        "ready_for_browser_automation": False, "readiness": "NOT_READY",
        "readiness_reasons": [], "generated_at": "2026-09-13T00:00:00+00:00",
    }
    package.update(overrides)
    job_dir = pending_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "application_package.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(package, f)
    return path


# --------------------------------------------------------------------------
# REVIEW MODEL
# --------------------------------------------------------------------------

class ReviewRecordTestCase(unittest.TestCase):
    def test_fresh_record_is_valid_pending(self):
        record = rv.make_review_record("job-1")
        self.assertEqual(rv.validate_review_record(record), [])
        self.assertEqual(record["review_status"], "PENDING")

    def test_valid_approved_record(self):
        record = rv.make_review_record("job-1")
        record.update(review_status="APPROVED", overall_quality=5)
        self.assertEqual(rv.validate_review_record(record), [])

    def test_valid_needs_changes_record(self):
        record = rv.make_review_record("job-1")
        record.update(review_status="NEEDS_CHANGES", resume_quality=2, needs_regeneration=True)
        self.assertEqual(rv.validate_review_record(record), [])

    def test_valid_rejected_record(self):
        record = rv.make_review_record("job-1")
        record.update(review_status="REJECTED", review_notes="Not a genuine fit")
        self.assertEqual(rv.validate_review_record(record), [])

    def test_invalid_status_is_rejected(self):
        record = rv.make_review_record("job-1")
        record["review_status"] = "MAYBE"
        errors = rv.validate_review_record(record)
        self.assertTrue(errors)

    def test_invalid_quality_score_is_rejected(self):
        record = rv.make_review_record("job-1")
        record["resume_quality"] = 7
        errors = rv.validate_review_record(record)
        self.assertTrue(any("resume_quality" in e for e in errors))

    def test_reviewer_is_not_forced_to_score_every_field(self):
        # Partial review: only overall_quality set, everything else stays None.
        record = rv.make_review_record("job-1")
        record["overall_quality"] = 4
        self.assertEqual(rv.validate_review_record(record), [])

    def test_review_persistence_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            record = rv.make_review_record("job-1")
            record["review_status"] = "APPROVED"
            rv.save_review(record, review_dir)
            reloaded = rv.load_review("job-1", review_dir)
            self.assertEqual(reloaded["review_status"], "APPROVED")

    def test_save_invalid_record_raises_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            record = rv.make_review_record("job-1")
            record["review_status"] = "BOGUS"
            with self.assertRaises(ValueError):
                rv.save_review(record, review_dir)
            self.assertIsNone(rv.load_review("job-1", review_dir))

    def test_load_review_for_never_reviewed_job_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rv.load_review("never-reviewed", Path(tmp)))


# --------------------------------------------------------------------------
# WORKFLOW
# --------------------------------------------------------------------------

class ReviewWorkflowTestCase(unittest.TestCase):
    def test_pending_review_listing_includes_review_job_with_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-1")
            jobs = [make_job(id="job-1", application_decision="REVIEW")]
            summaries = rv.list_pending_review(jobs, pending_dir=pending_dir, review_dir=review_dir)
            self.assertEqual([s["job_id"] for s in summaries], ["job-1"])
            self.assertEqual(summaries[0]["review_status"], "PENDING")

    def test_pending_review_listing_excludes_skip_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-1")  # even if a package somehow exists
            jobs = [make_job(id="job-1", application_decision="SKIP")]
            summaries = rv.list_pending_review(jobs, pending_dir=pending_dir, review_dir=review_dir)
            self.assertEqual(summaries, [])

    def test_pending_review_listing_excludes_review_job_without_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            jobs = [make_job(id="job-1", application_decision="REVIEW")]
            summaries = rv.list_pending_review(jobs, pending_dir=pending_dir, review_dir=review_dir)
            self.assertEqual(summaries, [])

    def test_pending_review_listing_includes_auto_apply_with_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-2", phase5_decision="AUTO_APPLY")
            jobs = [make_job(id="job-2", application_decision="AUTO_APPLY")]
            summaries = rv.list_pending_review(jobs, pending_dir=pending_dir, review_dir=review_dir)
            self.assertEqual([s["job_id"] for s in summaries], ["job-2"])

    def test_approve_action_saves_approved_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            record = rv.apply_review_action("job-1", "approve", review_dir=review_dir, review_notes="Looks good")
            self.assertEqual(record["review_status"], "APPROVED")
            self.assertEqual(rv.load_review("job-1", review_dir)["review_notes"], "Looks good")

    def test_needs_changes_action_records_issues_and_regeneration_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            record = rv.apply_review_action(
                "job-1", "needs_changes", review_dir=review_dir,
                quality_scores={"resume_quality": 2}, issues=["Summary too defensive"], needs_regeneration=True,
            )
            self.assertEqual(record["review_status"], "NEEDS_CHANGES")
            self.assertEqual(record["resume_quality"], 2)
            self.assertEqual(record["issues"], ["Summary too defensive"])
            self.assertTrue(record["needs_regeneration"])

    def test_reject_action_saves_rejected_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            record = rv.apply_review_action("job-1", "reject", review_dir=review_dir, review_notes="Not a fit")
            self.assertEqual(record["review_status"], "REJECTED")

    def test_save_action_persists_without_forcing_a_status_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            rv.apply_review_action("job-1", "approve", review_dir=review_dir)
            record = rv.apply_review_action("job-1", "save", review_dir=review_dir, quality_scores={"overall_quality": 4})
            self.assertEqual(record["review_status"], "APPROVED")  # unchanged by "save"
            self.assertEqual(record["overall_quality"], 4)

    def test_invalid_quality_field_name_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                rv.apply_review_action("job-1", "approve", review_dir=Path(tmp), quality_scores={"bogus_quality": 3})

    def test_unknown_action_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                rv.apply_review_action("job-1", "bogus_action", review_dir=Path(tmp))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

class ReviewApplicationCliTestCase(unittest.TestCase):
    def test_list_pending_runs_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps([make_job(id="job-1", application_decision="REVIEW")]), encoding="utf-8")
            pending_dir = Path(tmp) / "pending"
            write_package(pending_dir, "job-1")
            code = ra.main(["--list-pending", "--jobs", str(jobs_path), "--pending-dir", str(pending_dir), "--review-dir", str(Path(tmp) / "review")])
            self.assertEqual(code, 0)

    def test_show_view_for_nonexistent_job_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps([make_job(id="job-1")]), encoding="utf-8")
            code = ra.main(["--job-id", "no-such-job", "--jobs", str(jobs_path)])
            self.assertEqual(code, 1)

    def test_approve_via_cli_persists_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps([make_job(id="job-1")]), encoding="utf-8")
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-1")
            code = ra.main([
                "--job-id", "job-1", "--approve", "--notes", "Good to go",
                "--jobs", str(jobs_path), "--pending-dir", str(pending_dir), "--review-dir", str(review_dir),
            ])
            self.assertEqual(code, 0)
            self.assertEqual(rv.load_review("job-1", review_dir)["review_status"], "APPROVED")

    def test_action_without_existing_package_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps([make_job(id="job-1")]), encoding="utf-8")
            code = ra.main([
                "--job-id", "job-1", "--approve",
                "--jobs", str(jobs_path), "--pending-dir", str(Path(tmp) / "pending"), "--review-dir", str(Path(tmp) / "review"),
            ])
            self.assertEqual(code, 1)


# --------------------------------------------------------------------------
# INTEGRATION -- Phase 5 / Phase 6 invariants must hold through the review layer
# --------------------------------------------------------------------------

class Phase5IntegrityThroughReviewTestCase(unittest.TestCase):
    def test_reviewing_a_job_never_touches_the_phase5_job_record(self):
        job = make_job(id="job-1", score=83, role_family="BUSINESS_ANALYSIS", career_level="MID_LEVEL", application_decision="REVIEW")
        snapshot = dict(job)
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-1")
            rv.apply_review_action("job-1", "approve", review_dir=review_dir, review_notes="ok")
        self.assertEqual(job, snapshot)

    def test_review_action_never_writes_to_application_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            package_path = write_package(pending_dir, "job-1")
            before = package_path.read_text(encoding="utf-8")
            rv.apply_review_action("job-1", "reject", review_dir=review_dir, review_notes="no")
            after = package_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_review_status_defaults_to_pending_until_a_human_acts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_dir, review_dir = Path(tmp) / "pending", Path(tmp) / "review"
            write_package(pending_dir, "job-1")
            jobs = [make_job(id="job-1", application_decision="REVIEW")]
            summaries = rv.list_pending_review(jobs, pending_dir=pending_dir, review_dir=review_dir)
            self.assertEqual(summaries[0]["review_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
