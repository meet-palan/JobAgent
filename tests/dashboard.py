"""Tests for dashboard.py -- the read-only local job dashboard.

No network, no server startup, no browser: exercises the pure filtering
functions directly, plus load_jobs()'s tolerance of malformed data.

Run directly:
    python tests/dashboard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard as db  # noqa: E402

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def make_job(**overrides) -> dict:
    base = {
        "id": "job-1", "title": "Business Analyst", "company": "Acme", "location": "Pune",
        "work_mode": "hybrid", "score": 80, "application_decision": "REVIEW",
        "role_family": "BUSINESS_ANALYSIS", "career_level": "ENTRY_LEVEL",
        "discovered_at": TODAY, "date_discovered": TODAY, "first_seen_at": TODAY, "last_seen_at": TODAY,
        "source": "greenhouse:acme", "description": "Great BA role at Acme.",
    }
    base.update(overrides)
    return base


class TodayFilterTestCase(unittest.TestCase):
    def test_job_discovered_today_is_today(self):
        job = make_job(discovered_at=TODAY, last_seen_at=YESTERDAY)
        self.assertTrue(db.is_today_job(job, TODAY))

    def test_job_last_seen_today_is_today(self):
        job = make_job(discovered_at=YESTERDAY, last_seen_at=TODAY)
        self.assertTrue(db.is_today_job(job, TODAY))

    def test_old_job_rediscovered_today_counts_as_today(self):
        # An old job (first_seen_at long ago) whose last_seen_at was just
        # updated to today (rediscovered by discover_jobs.py) must show up.
        job = make_job(first_seen_at="2020-01-01", discovered_at="2020-01-01", last_seen_at=TODAY)
        self.assertTrue(db.is_today_job(job, TODAY))

    def test_yesterdays_job_excluded(self):
        job = make_job(discovered_at=YESTERDAY, last_seen_at=YESTERDAY)
        self.assertFalse(db.is_today_job(job, TODAY))

    def test_missing_discovered_at_falls_back_to_last_seen_at(self):
        job = make_job()
        del job["discovered_at"]
        job["last_seen_at"] = TODAY
        self.assertTrue(db.is_today_job(job, TODAY))

    def test_missing_both_date_fields_is_not_today(self):
        job = make_job()
        del job["discovered_at"]
        del job["last_seen_at"]
        self.assertFalse(db.is_today_job(job, TODAY))

    def test_filter_and_sort_view_today(self):
        jobs = [make_job(id="a", last_seen_at=TODAY), make_job(id="b", last_seen_at=YESTERDAY, discovered_at=YESTERDAY)]
        result = db.filter_and_sort(jobs, view="today", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])


class DecisionAndRoleFilterTestCase(unittest.TestCase):
    def test_decision_filter(self):
        jobs = [make_job(id="a", application_decision="AUTO_APPLY"), make_job(id="b", application_decision="SKIP")]
        result = db.filter_and_sort(jobs, view="all", decision="AUTO_APPLY", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_decision_all_returns_everything(self):
        jobs = [make_job(id="a", application_decision="AUTO_APPLY"), make_job(id="b", application_decision="SKIP")]
        result = db.filter_and_sort(jobs, view="all", decision="All", today=TODAY)
        self.assertEqual(len(result), 2)

    def test_role_family_filter(self):
        jobs = [make_job(id="a", role_family="PRODUCT"), make_job(id="b", role_family="BUSINESS_ANALYSIS")]
        result = db.filter_and_sort(jobs, view="all", role_family="PRODUCT", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_missing_role_family_never_matches_a_specific_filter(self):
        jobs = [make_job(id="a")]
        del jobs[0]["role_family"]
        result = db.filter_and_sort(jobs, view="all", role_family="PRODUCT", today=TODAY)
        self.assertEqual(result, [])


class SearchTestCase(unittest.TestCase):
    def test_search_matches_title(self):
        jobs = [make_job(id="a", title="Product Analyst"), make_job(id="b", title="Legal Counsel")]
        result = db.filter_and_sort(jobs, view="all", search="product", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_search_matches_description_case_insensitively(self):
        jobs = [make_job(id="a", description="Owns the ROADMAP for a fintech product.")]
        result = db.filter_and_sort(jobs, view="all", search="roadmap", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_search_no_match_returns_empty(self):
        jobs = [make_job(id="a", title="Business Analyst", description="")]
        result = db.filter_and_sort(jobs, view="all", search="zzz-nomatch", today=TODAY)
        self.assertEqual(result, [])

    def test_search_matches_required_skill(self):
        jobs = [
            make_job(id="a", title="Business Analyst", required_skills=["SQL", "Tableau"]),
            make_job(id="b", title="Legal Counsel", required_skills=["Contract Law"]),
        ]
        result = db.filter_and_sort(jobs, view="all", search="tableau", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_search_matches_preferred_skill(self):
        jobs = [make_job(id="a", preferred_skills=["Figma"])]
        result = db.filter_and_sort(jobs, view="all", search="figma", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])


class MissingFieldsTestCase(unittest.TestCase):
    def test_missing_score_never_crashes_min_score_filter(self):
        jobs = [make_job(id="a")]
        del jobs[0]["score"]
        result = db.filter_and_sort(jobs, view="all", min_score=50, today=TODAY)
        self.assertEqual(result, [])  # unknown score can't satisfy a minimum

    def test_missing_location_does_not_crash_location_filter(self):
        jobs = [make_job(id="a")]
        del jobs[0]["location"]
        result = db.filter_and_sort(jobs, view="all", location="Pune", today=TODAY)
        self.assertEqual(result, [])

    def test_sparse_job_still_renders_via_build_payload(self):
        sparse = {"id": "bare"}
        payload = db.build_payload([sparse])
        self.assertEqual(payload["counts"]["all"], 1)
        self.assertEqual(len(payload["jobs"]), 1)

    def test_build_payload_preserves_every_detail_view_field(self):
        # The job detail panel (dashboard/index.html) reads these fields directly
        # off the job object the API returns -- build_payload must never strip
        # any of them (no whitelist/projection down to a summary shape).
        rich = make_job(
            id="rich", salary_min=300000, salary_max=400000, salary_currency="INR",
            min_experience_years=0, max_experience_years=1, posted_date="2026-01-01",
            required_skills=["SQL"], preferred_skills=["Tableau"],
            relevant_evidence=["Led BRD documentation"], transferable_evidence=["Stakeholder mgmt"],
            missing_requirements=["No formal PM title"], decision_reason="Score 91 >= 85",
            application_url="https://x/apply", source_url="https://x/job", source_job_id="42",
        )
        payload = db.build_payload([rich])
        returned = payload["jobs"][0]
        for field in (
            "salary_min", "salary_max", "salary_currency", "min_experience_years", "max_experience_years",
            "posted_date", "required_skills", "preferred_skills", "relevant_evidence", "transferable_evidence",
            "missing_requirements", "decision_reason", "application_url", "source_url", "source_job_id",
            "first_seen_at", "last_seen_at",
        ):
            self.assertIn(field, returned, f"detail view field {field!r} was stripped by build_payload")
            self.assertEqual(returned[field], rich[field])


class MalformedJobRecordTestCase(unittest.TestCase):
    def test_load_jobs_skips_non_dict_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps([make_job(id="a"), "not-a-job", None, 42]), encoding="utf-8")
            jobs = db.load_jobs(path)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["id"], "a")

    def test_load_jobs_handles_top_level_non_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
            self.assertEqual(db.load_jobs(path), [])

    def test_load_jobs_handles_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(db.load_jobs(path), [])

    def test_load_jobs_handles_missing_file(self):
        self.assertEqual(db.load_jobs(Path("/nonexistent/jobs.json")), [])

    def test_load_jobs_handles_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(db.load_jobs(path), [])


class SortingTestCase(unittest.TestCase):
    def test_sort_by_score_descending(self):
        jobs = [make_job(id="a", score=50), make_job(id="b", score=90)]
        result = db.filter_and_sort(jobs, view="all", sort="score", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b", "a"])

    def test_sort_by_company_ascending(self):
        jobs = [make_job(id="a", company="Zeta"), make_job(id="b", company="Acme")]
        result = db.filter_and_sort(jobs, view="all", sort="company", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b", "a"])

    def test_sort_by_title_ascending(self):
        jobs = [make_job(id="a", title="Zonal Manager"), make_job(id="b", title="Business Analyst")]
        result = db.filter_and_sort(jobs, view="all", sort="title", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b", "a"])

    def test_sort_by_newest_discovered_descending(self):
        jobs = [
            make_job(id="a", last_seen_at=YESTERDAY, discovered_at=YESTERDAY, first_seen_at=YESTERDAY),
            make_job(id="b", last_seen_at=TODAY, discovered_at=TODAY, first_seen_at=TODAY),
        ]
        result = db.filter_and_sort(jobs, view="all", sort="newest", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b", "a"])


class LocationSourceScoreFilterTestCase(unittest.TestCase):
    def test_location_filter_matches_substring_case_insensitively(self):
        jobs = [make_job(id="a", location="Pune, Maharashtra"), make_job(id="b", location="Remote - US")]
        result = db.filter_and_sort(jobs, view="all", location="pune", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["a"])

    def test_source_filter_matches_substring(self):
        jobs = [make_job(id="a", source="greenhouse:acme"), make_job(id="b", source="lever:acme")]
        result = db.filter_and_sort(jobs, view="all", source="lever", today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b"])

    def test_min_score_filter_excludes_lower_scores(self):
        jobs = [make_job(id="a", score=60), make_job(id="b", score=90)]
        result = db.filter_and_sort(jobs, view="all", min_score=75, today=TODAY)
        self.assertEqual([j["id"] for j in result], ["b"])

    def test_min_score_filter_excludes_missing_score(self):
        jobs = [make_job(id="a")]
        del jobs[0]["score"]
        result = db.filter_and_sort(jobs, view="all", min_score=1, today=TODAY)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
