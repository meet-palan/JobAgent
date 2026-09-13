"""Tests for scripts/discover_jobs.py.

No network calls -- connectors are tested against fixture payloads shaped
like real Greenhouse/Lever/Workday responses, captured from live API calls
made while building this script.

Run directly:
    python tests/discover_jobs.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import discover_jobs as dj  # noqa: E402


def make_config(**overrides) -> dict:
    base = {"max_jobs_per_source": 50, "max_pages_per_source": 3, "delay": 0, "timeout": 5}
    base.update(overrides)
    return base


def make_candidate(**overrides) -> dict:
    base = {
        "total_experience_years": 1.0,
        "target_titles": ["Business Analyst", "Project Manager", "Product Manager", "Associate Product Manager"],
        "secondary_titles": ["Project Coordinator"],
        "skills": ["Business Analysis", "SQL", "Tableau", "Excel", "Agile", "Scrum"],
        "preferred_industries": ["IT services", "Product"],
        "avoid_industries": [],
        "preferred_locations": ["Ahmedabad", "Pune", "Bangalore", "Bengaluru", "Mumbai"],
        "preferred_work_modes": ["remote", "hybrid", "onsite"],
        "min_acceptable_salary_inr_per_year": 300000,
        "target_salary_min_inr_per_year": 350000,
        "target_salary_max_inr_per_year": 400000,
        "preferred_company_sizes": ["startup", "mid-size", "large enterprise", "MNC"],
        "capabilities": {
            "business_analysis": {"strength": "professional", "evidence": ["Business Analyst role: requirement gathering, BRD documentation"]},
            "requirements_gathering": {"strength": "professional", "evidence": ["Requirement gathering at current role"]},
            "stakeholder_communication": {"strength": "professional", "evidence": ["Client POC, stakeholder alignment"]},
            "documentation": {"strength": "professional", "evidence": ["BRD/SOW documentation"]},
            "project_management": {"strength": "professional", "evidence": ["Managed 6+ concurrent projects"]},
            "agile_scrum": {"strength": "professional", "evidence": ["Facilitated daily Agile scrum meetings"]},
            "product_management": {"strength": "practical", "evidence": ["Product Manager certification; scoped features for a project"]},
            "analytics": {"strength": "practical", "evidence": ["SQL, Tableau, Excel skills"]},
            "design_figma": {"strength": "none", "evidence": []},
            "testing_qa": {"strength": "none", "evidence": []},
        },
    }
    base.update(overrides)
    return base


class ExperienceParsingTestCase(unittest.TestCase):
    def test_range(self):
        self.assertEqual(dj.parse_experience_years("Experience Landscape: 4-8 years of experience"), (4.0, 8.0))

    def test_plus(self):
        self.assertEqual(dj.parse_experience_years("3+ years of experience in product management"), (3.0, None))

    def test_minimum(self):
        self.assertEqual(dj.parse_experience_years("Minimum 7.5 year(s) of experience is required"), (7.5, None))

    def test_none_found(self):
        self.assertEqual(dj.parse_experience_years("No experience mentioned here"), (None, None))

    def test_empty_text(self):
        self.assertEqual(dj.parse_experience_years(None), (None, None))
        self.assertEqual(dj.parse_experience_years(""), (None, None))


class WorkModeClassificationTestCase(unittest.TestCase):
    def test_hybrid(self):
        self.assertEqual(dj.classify_work_mode("This is a hybrid role, 3 days in office"), "hybrid")

    def test_remote(self):
        self.assertEqual(dj.classify_work_mode("This is a fully remote position"), "remote")

    def test_onsite_from_office_phrase(self):
        self.assertEqual(dj.classify_work_mode("This position is based at our Pune office."), "onsite")

    def test_unknown(self):
        self.assertIsNone(dj.classify_work_mode("Some unrelated description text"))


class RelevanceFilterTestCase(unittest.TestCase):
    def setUp(self):
        self.keywords = ["Business Analyst", "Project Manager", "Product Manager", "Associate Product Manager", "Project Coordinator"]

    def test_exact_title_match(self):
        self.assertTrue(dj.title_is_relevant("Business Analyst", self.keywords))

    def test_word_overlap_match(self):
        self.assertTrue(dj.title_is_relevant("Program/Project Management Lead", self.keywords))
        self.assertTrue(dj.title_is_relevant("Product Management Analyst", self.keywords))

    def test_unrelated_title_rejected(self):
        self.assertFalse(dj.title_is_relevant("Senior Backend Engineer", self.keywords))

    def test_location_relevant(self):
        self.assertTrue(dj.location_is_relevant("Bengaluru, Karnataka", ["Bangalore", "Bengaluru"]))
        self.assertTrue(dj.location_is_relevant("Pune", ["Pune"]))

    def test_location_not_relevant(self):
        self.assertFalse(dj.location_is_relevant("Gurugram", ["Pune", "Mumbai"]))

    def test_no_preferred_locations_means_anything_is_relevant(self):
        self.assertTrue(dj.location_is_relevant("Anywhere", []))


class GreenhouseNormalizationTestCase(unittest.TestCase):
    def test_normalize_greenhouse_job(self):
        raw = {
            "id": 12345,
            "title": "Business Analyst",
            "location": {"name": "Bengaluru, India"},
            "content": "<p>Great role. 2-4 years of experience needed. This is a hybrid role.</p>",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
        }
        job = dj.normalize_greenhouse_job(raw, "acme")
        self.assertEqual(job["company"], "acme")
        self.assertEqual(job["title"], "Business Analyst")
        self.assertEqual(job["location"], "Bengaluru, India")
        self.assertEqual(job["min_experience_years"], 2.0)
        self.assertEqual(job["max_experience_years"], 4.0)
        self.assertEqual(job["work_mode"], "hybrid")
        self.assertEqual(job["application_url"], "https://boards.greenhouse.io/acme/jobs/12345")
        self.assertEqual(job["source"], "greenhouse:acme")
        for f in dj.JOB_SCHEMA_FIELDS:
            self.assertIn(f, job)


class LeverNormalizationTestCase(unittest.TestCase):
    def test_normalize_lever_job_splits_required_and_preferred(self):
        raw = {
            "id": "abc-123",
            "text": "Product Manager (Internal Platform)",
            "categories": {"location": "Bangalore, Karnataka"},
            "descriptionPlain": "Own the platform product roadmap.",
            "hostedUrl": "https://jobs.lever.co/acme/abc-123",
            "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
            "lists": [
                {
                    "text": "What you will need",
                    "content": (
                        "<ul><li>3+ years of experience in product management</li>"
                        "<li>Strong stakeholder management skills</li>"
                        "<li>Experience with cloud platforms is a plus</li></ul>"
                    ),
                }
            ],
        }
        job = dj.normalize_lever_job(raw, "acme")
        self.assertEqual(job["company"], "acme")
        self.assertEqual(job["location"], "Bangalore, Karnataka")
        self.assertEqual(job["min_experience_years"], 3.0)
        self.assertIn("Strong stakeholder management skills", job["required_skills"])
        self.assertIn("Experience with cloud platforms is a plus", job["preferred_skills"])
        # The experience-years bullet itself should not also show up as a "skill".
        self.assertFalse(any("3+ years" in s for s in job["required_skills"] + job["preferred_skills"]))
        self.assertEqual(job["application_url"], "https://jobs.lever.co/acme/abc-123")


class WorkdayNormalizationTestCase(unittest.TestCase):
    def test_normalize_workday_job(self):
        detail = {
            "jobPostingInfo": {
                "title": "Business Analyst",
                "location": "Pune",
                "jobDescription": (
                    "Project Role : Business Analyst<br>"
                    "Must have skills : Business Requirements Analysis<br>"
                    "Good to have skills : Scrum, Data Analysis<br>"
                    "Minimum 5 year(s) of experience is required<br>"
                    "This position is based at our Pune office."
                ),
                "jobReqId": "ATCI-1234",
                "externalUrl": "https://acme.wd1.myworkdayjobs.com/Careers/job/Pune/Business-Analyst_ATCI-1234",
            }
        }
        job = dj.normalize_workday_job(detail, "Acme", "workday:acme")
        self.assertEqual(job["title"], "Business Analyst")
        self.assertEqual(job["location"], "Pune")
        self.assertEqual(job["min_experience_years"], 5.0)
        self.assertEqual(job["required_skills"], ["Business Requirements Analysis"])
        self.assertEqual(job["preferred_skills"], ["Scrum", "Data Analysis"])
        self.assertEqual(job["work_mode"], "onsite")
        self.assertEqual(job["source"], "workday:acme")

    def test_good_to_have_na_is_dropped(self):
        detail = {
            "jobPostingInfo": {
                "title": "Business Analyst",
                "location": "Pune",
                "jobDescription": "Must have skills : SQL<br>Good to have skills : NA<br>",
                "jobReqId": "X1",
            }
        }
        job = dj.normalize_workday_job(detail, "Acme", "workday:acme")
        self.assertEqual(job["preferred_skills"], [])


class DedupeTestCase(unittest.TestCase):
    def test_identical_postings_produce_same_key(self):
        job_a = {"company": "Accenture", "title": "Business Analyst", "location": "Pune", "required_skills": ["Avaloq Wealth"]}
        job_b = {"company": "accenture", "title": "Business Analyst", "location": "pune", "required_skills": ["avaloq wealth"]}
        self.assertEqual(dj.dedupe_key(job_a), dj.dedupe_key(job_b))

    def test_different_required_skills_are_not_duplicates(self):
        job_a = {"company": "Accenture", "title": "Business Analyst", "location": "Pune", "required_skills": ["Avaloq Wealth"]}
        job_b = {"company": "Accenture", "title": "Business Analyst", "location": "Pune", "required_skills": ["Life Sciences R&D"]}
        self.assertNotEqual(dj.dedupe_key(job_a), dj.dedupe_key(job_b))


LONG_DESCRIPTION = "A detailed job description with enough real content to look like a legitimate listing rather than a stub. " * 2


class ScoreAndAnnotateTestCase(unittest.TestCase):
    def test_annotates_job_with_score_and_status(self):
        job = {
            "id": "job-1", "company": "Acme", "title": "Business Analyst", "location": "Pune",
            "work_mode": "hybrid", "industry": "IT services", "company_size": "mid-size",
            "salary_min": 350000, "salary_max": 400000,
            "min_experience_years": 0, "max_experience_years": 1,
            "required_skills": ["Business Analysis", "SQL"], "preferred_skills": ["Tableau"],
            "description": LONG_DESCRIPTION, "application_url": "https://example.com", "source": "test", "date_discovered": "2026-01-01",
        }
        dj.score_and_annotate(job, make_candidate())
        self.assertEqual(job["status"], "shortlisted")
        self.assertEqual(job["application_decision"], "AUTO_APPLY")
        self.assertIn("role_family", job)
        self.assertIn("career_level", job)
        self.assertIn("score_breakdown", job)

    def test_low_scoring_job_is_not_shortlisted(self):
        job = {
            "id": "job-2", "company": "Acme", "title": "Senior Corporate Counsel", "location": "Nowhere",
            "work_mode": "onsite", "industry": "Unrelated", "company_size": "unknown",
            "salary_min": 50000, "salary_max": 60000,
            "min_experience_years": 10, "max_experience_years": 15,
            "required_skills": ["Skill A"], "preferred_skills": [],
            "description": LONG_DESCRIPTION, "application_url": "https://example.com", "source": "test", "date_discovered": "2026-01-01",
        }
        dj.score_and_annotate(job, make_candidate())
        self.assertEqual(job["status"], "discovered")
        self.assertEqual(job["application_decision"], "SKIP")


class PersistenceTestCase(unittest.TestCase):
    def test_load_existing_jobs_missing_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            self.assertEqual(dj.load_existing_jobs(path), [])

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            jobs = [{"id": "a", "title": "Business Analyst"}]
            dj.save_jobs(path, jobs)
            self.assertEqual(dj.load_existing_jobs(path), jobs)

    def test_load_existing_jobs_handles_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(dj.load_existing_jobs(path), [])


class RunDiscoveryIntegrationTestCase(unittest.TestCase):
    """Exercises the pipeline end-to-end with fake collectors (no network)."""

    def test_dedup_and_shortlist_flow_without_network(self):
        candidate = make_candidate()
        job_1 = {
            "id": "job-1", "company": "Acme", "title": "Business Analyst", "location": "Pune",
            "work_mode": "hybrid", "industry": "IT services", "company_size": "mid-size",
            "salary_min": 350000, "salary_max": 400000,
            "min_experience_years": 0, "max_experience_years": 1,
            "required_skills": ["Business Analysis", "SQL"], "preferred_skills": ["Tableau"],
            "description": LONG_DESCRIPTION, "application_url": "https://example.com/1", "source": "test", "date_discovered": "2026-01-01",
        }
        duplicate_of_job_1 = dict(job_1, id="job-1-dupe", application_url="https://example.com/1-dupe")
        job_2_low_score = {
            "id": "job-2", "company": "Acme", "title": "Senior Engineering Director", "location": "Nowhere",
            "work_mode": "onsite", "industry": "Unrelated", "company_size": "unknown",
            "salary_min": 50000, "salary_max": 60000,
            "min_experience_years": 10, "max_experience_years": 15,
            "required_skills": ["Skill A"], "preferred_skills": [],
            "description": LONG_DESCRIPTION, "application_url": "https://example.com/2", "source": "test", "date_discovered": "2026-01-01",
        }
        raw_jobs = [job_1, duplicate_of_job_1, job_2_low_score]

        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            dj.save_jobs(jobs_path, [])

            existing = dj.load_existing_jobs(jobs_path)
            existing_keys = {dj.dedupe_key(j) for j in existing}
            seen, unique_new, duplicates = set(), [], 0
            for job in raw_jobs:
                key = dj.dedupe_key(job)
                if key in existing_keys or key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                unique_new.append(job)

            self.assertEqual(duplicates, 1)
            self.assertEqual(len(unique_new), 2)

            for job in unique_new:
                dj.score_and_annotate(job, candidate)
            dj.save_jobs(jobs_path, existing + unique_new)

            saved = dj.load_existing_jobs(jobs_path)
            self.assertEqual(len(saved), 2)
            statuses = {j["id"]: j["status"] for j in saved}
            self.assertEqual(statuses["job-1"], "shortlisted")
            self.assertEqual(statuses["job-2"], "discovered")


class AshbyNormalizationTestCase(unittest.TestCase):
    def test_normalize_ashby_job(self):
        raw = {
            "id": "abc-123",
            "title": "Associate Product Manager",
            "location": "Bengaluru, India",
            "isRemote": False,
            "workplaceType": "Hybrid",
            "publishedAt": "2026-08-01T10:00:00.000+00:00",
            "jobUrl": "https://jobs.ashbyhq.com/acme/abc-123",
            "applyUrl": "https://jobs.ashbyhq.com/acme/abc-123/application",
            "descriptionHtml": "<p>3+ years of experience required.</p>",
            "compensation": {"scrapeableCompensationSalarySummary": "$40K - $60K"},
        }
        job = dj.normalize_ashby_job(raw, "acme")
        self.assertEqual(job["company"], "acme")
        self.assertEqual(job["title"], "Associate Product Manager")
        self.assertEqual(job["location"], "Bengaluru, India")
        self.assertEqual(job["work_mode"], "hybrid")
        self.assertEqual(job["min_experience_years"], 3.0)
        self.assertEqual(job["salary_min"], 40000)
        self.assertEqual(job["salary_max"], 60000)
        self.assertEqual(job["salary_currency"], "USD")
        self.assertEqual(job["posted_date"], "2026-08-01")
        self.assertEqual(job["application_url"], "https://jobs.ashbyhq.com/acme/abc-123/application")
        self.assertEqual(job["source"], "ashby:acme")
        self.assertEqual(job["source_job_id"], "abc-123")
        for f in dj.JOB_SCHEMA_FIELDS:
            self.assertIn(f, job)

    def test_missing_compensation_and_remote_flag(self):
        raw = {
            "id": "xyz-1",
            "title": "Product Analyst",
            "location": "",
            "isRemote": True,
            "workplaceType": None,
            "publishedAt": None,
            "jobUrl": "https://jobs.ashbyhq.com/acme/xyz-1",
            "applyUrl": None,
            "descriptionHtml": "<p>No fixed experience mentioned.</p>",
            "compensation": None,
        }
        job = dj.normalize_ashby_job(raw, "acme")
        self.assertEqual(job["location"], "Remote")
        self.assertIsNone(job["salary_min"])
        self.assertIsNone(job["salary_max"])
        self.assertIsNone(job["posted_date"])
        self.assertEqual(job["application_url"], "https://jobs.ashbyhq.com/acme/xyz-1")


class SmartRecruitersNormalizationTestCase(unittest.TestCase):
    def test_normalize_smartrecruiters_job(self):
        raw = {
            "id": "744000148454651",
            "name": "Business Analyst",
            "location": {"city": "Pune", "fullLocation": "Pune, India", "remote": False, "hybrid": True},
            "industry": {"id": "computer_software", "label": "Computer Software"},
            "experienceLevel": {"id": "associate", "label": "Associate"},
            "releasedDate": "2026-09-09T09:43:26.403Z",
        }
        job = dj.normalize_smartrecruiters_job(raw, "acme")
        self.assertEqual(job["company"], "acme")
        self.assertEqual(job["title"], "Business Analyst")
        self.assertEqual(job["location"], "Pune, India")
        self.assertEqual(job["work_mode"], "hybrid")
        self.assertEqual(job["posted_date"], "2026-09-09")
        self.assertEqual(job["application_url"], "https://jobs.smartrecruiters.com/acme/744000148454651")
        self.assertEqual(job["source"], "smartrecruiters:acme")
        for f in dj.JOB_SCHEMA_FIELDS:
            self.assertIn(f, job)

    def test_missing_location_and_industry(self):
        raw = {"id": "1", "name": "Project Coordinator", "location": {}, "experienceLevel": None, "releasedDate": None}
        job = dj.normalize_smartrecruiters_job(raw, "acme")
        self.assertEqual(job["location"], "")
        self.assertIsNone(job["industry"])
        self.assertIsNone(job["posted_date"])


class MissingFieldsHighRecallTestCase(unittest.TestCase):
    """Discovery must never reject or crash on a posting just because a
    non-critical field (salary, experience, location, description, posting
    date) is missing -- those are unknown, not disqualifying."""

    def test_greenhouse_missing_salary_experience_and_location(self):
        raw = {"id": 1, "title": "Business Analyst", "location": {}, "content": "", "absolute_url": "https://x.io/1"}
        job = dj.normalize_greenhouse_job(raw, "acme")
        self.assertIsNone(job["salary_min"])
        self.assertIsNone(job["min_experience_years"])
        self.assertEqual(job["location"], "")
        self.assertEqual(job["description"], "")

    def test_lever_missing_description_and_posted_date(self):
        raw = {"id": "1", "text": "Project Manager", "categories": {}, "hostedUrl": "https://jobs.lever.co/acme/1"}
        job = dj.normalize_lever_job(raw, "acme")
        self.assertEqual(job["description"], "")
        self.assertIsNone(job["posted_date"])
        self.assertEqual(job["required_skills"], [])

    def test_missing_location_does_not_reject_during_collection(self):
        # A posting with no location string at all must still pass the
        # relevance filter used during collection -- unknown, not irrelevant.
        self.assertTrue(dj.location_is_relevant("", ["Pune", "Bangalore"]))
        self.assertTrue(dj.location_is_relevant(None, ["Pune", "Bangalore"]))

    def test_remote_location_always_relevant(self):
        self.assertTrue(dj.location_is_relevant("Remote - Anywhere", ["Pune"]))
        self.assertTrue(dj.location_is_relevant("Work From Home, India", ["Pune"]))


class QueryExpansionTestCase(unittest.TestCase):
    def test_product_family_expansion(self):
        expanded = dj.expand_target_titles(["Associate Product Manager"], [])
        lowered = {t.lower() for t in expanded}
        self.assertIn("apm", lowered)
        self.assertIn("product analyst", lowered)
        self.assertIn("product operations analyst", lowered)

    def test_project_family_expansion_from_secondary_title(self):
        expanded = dj.expand_target_titles([], ["Project Coordinator"])
        lowered = {t.lower() for t in expanded}
        self.assertIn("pmo analyst", lowered)
        self.assertIn("program analyst", lowered)
        self.assertIn("program manager", lowered)

    def test_does_not_expand_into_unrelated_families(self):
        expanded = dj.expand_target_titles(["Associate Product Manager"], [])
        lowered = {t.lower() for t in expanded}
        self.assertNotIn("software engineer", lowered)
        self.assertNotIn("marketing manager", lowered)
        self.assertNotIn("sales manager", lowered)

    def test_business_analysis_family_only_expands_when_triggered(self):
        # No BA/project-flavored title at all -> no BA variants added.
        expanded = dj.expand_target_titles(["Associate Product Manager"], [])
        lowered = {t.lower() for t in expanded}
        self.assertNotIn("functional analyst", lowered)


class UrlNormalizationTestCase(unittest.TestCase):
    def test_strips_query_string_and_trailing_slash(self):
        a = dj.canonical_application_url("https://boards.greenhouse.io/acme/jobs/1?gh_src=abc")
        b = dj.canonical_application_url("https://boards.greenhouse.io/acme/jobs/1/")
        self.assertEqual(a, b)

    def test_is_case_insensitive_on_host(self):
        a = dj.canonical_application_url("https://Boards.Greenhouse.io/acme/jobs/1")
        b = dj.canonical_application_url("https://boards.greenhouse.io/acme/jobs/1")
        self.assertEqual(a, b)

    def test_none_for_missing_url(self):
        self.assertIsNone(dj.canonical_application_url(None))
        self.assertIsNone(dj.canonical_application_url(""))


class ConfidenceTieredDuplicateTestCase(unittest.TestCase):
    def test_same_source_job_id_is_duplicate(self):
        job_a = {"source": "lever:acme", "source_job_id": "abc", "company": "acme", "title": "PM", "location": "Pune",
                 "application_url": "https://jobs.lever.co/acme/abc", "required_skills": []}
        job_b = dict(job_a, application_url="https://jobs.lever.co/acme/abc?utm_source=x")
        index = dj.build_identity_index([job_a])
        self.assertIsNotNone(dj.find_duplicate(index, job_b))

    def test_cross_source_same_application_url_is_duplicate(self):
        job_a = {"source": "greenhouse:acme", "source_job_id": "1", "company": "acme", "title": "Business Analyst",
                  "location": "Pune", "application_url": "https://boards.greenhouse.io/acme/jobs/1", "required_skills": []}
        job_b = {"source": "some-aggregator:acme", "source_job_id": "different-id", "company": "acme",
                  "title": "Business Analyst", "location": "Pune",
                  "application_url": "https://boards.greenhouse.io/acme/jobs/1/", "required_skills": []}
        index = dj.build_identity_index([job_a])
        self.assertIsNotNone(dj.find_duplicate(index, job_b))

    def test_different_skills_same_title_and_location_not_duplicate(self):
        job_a = {"source": "workday:acme", "source_job_id": "1", "company": "Acme", "title": "Business Analyst",
                 "location": "Bengaluru", "application_url": "https://acme.com/jobs/1", "required_skills": ["Murex"]}
        job_b = {"source": "workday:acme", "source_job_id": "2", "company": "Acme", "title": "Business Analyst",
                 "location": "Bengaluru", "application_url": "https://acme.com/jobs/2", "required_skills": ["Openlink Endur"]}
        index = dj.build_identity_index([job_a])
        self.assertIsNone(dj.find_duplicate(index, job_b))


class HistoricalPersistenceTestCase(unittest.TestCase):
    """Rediscovering the same job on a later run must update last_seen_at
    without creating a duplicate row or losing the original first_seen_at."""

    def _fake_collect(self, jobs_to_return):
        def _collect(sources, keywords, preferred_locations, config, raw_jobs, stats_list):
            stats = dj.new_stats("fake")
            stats_list.append(stats)
            for job in jobs_to_return:
                j = dict(job)
                j["_source_stats"] = stats
                raw_jobs.append(j)
        return _collect

    def test_rediscovery_updates_last_seen_without_duplicating(self):
        job = {
            "id": "job-1", "company": "Acme", "title": "Business Analyst", "location": "Pune",
            "work_mode": "hybrid", "industry": "IT services", "company_size": "mid-size",
            "salary_min": 350000, "salary_max": 400000, "min_experience_years": 0, "max_experience_years": 1,
            "required_skills": ["Business Analysis", "SQL"], "preferred_skills": ["Tableau"],
            "description": LONG_DESCRIPTION, "application_url": "https://example.com/job-1",
            "source": "test:acme", "source_job_id": "1", "date_discovered": "2026-01-01",
        }
        candidate = make_candidate()
        no_op = lambda *a, **k: None
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "jobs.json"
            shortlist_dir = Path(tmp) / "shortlisted"
            dj.save_jobs(jobs_path, [])
            fake = self._fake_collect([job])
            config = make_config()
            with patch.object(dj, "collect_lever", fake), patch.object(dj, "collect_greenhouse", no_op), \
                 patch.object(dj, "collect_ashby", no_op), patch.object(dj, "collect_smartrecruiters", no_op), \
                 patch.object(dj, "collect_workday", no_op), patch.object(dj, "SHORTLIST_DIR", shortlist_dir):
                dj.run_discovery({}, candidate, jobs_path, config, dry_run=False)

            saved_after_first = dj.load_existing_jobs(jobs_path)
            self.assertEqual(len(saved_after_first), 1)
            self.assertEqual(saved_after_first[0]["first_seen_at"], dj.TODAY)
            self.assertEqual(saved_after_first[0]["last_seen_at"], dj.TODAY)

            with patch.object(dj, "collect_lever", fake), patch.object(dj, "collect_greenhouse", no_op), \
                 patch.object(dj, "collect_ashby", no_op), patch.object(dj, "collect_smartrecruiters", no_op), \
                 patch.object(dj, "collect_workday", no_op), patch.object(dj, "SHORTLIST_DIR", shortlist_dir):
                dj.run_discovery({}, candidate, jobs_path, config, dry_run=False)

            saved_after_second = dj.load_existing_jobs(jobs_path)
            self.assertEqual(len(saved_after_second), 1, "rediscovered job must not be duplicated")
            self.assertEqual(saved_after_second[0]["first_seen_at"], dj.TODAY)
            self.assertEqual(saved_after_second[0]["last_seen_at"], dj.TODAY)

    def test_backfill_history_fields_uses_date_discovered(self):
        job = {"date_discovered": "2026-01-01"}
        dj.backfill_history_fields(job)
        self.assertEqual(job["first_seen_at"], "2026-01-01")
        self.assertEqual(job["last_seen_at"], "2026-01-01")


class MalformedSourceDataTestCase(unittest.TestCase):
    def test_greenhouse_posting_missing_title_is_malformed_not_added(self):
        raw_jobs, stats_list = [], []
        sources = {"greenhouse_boards": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        postings = [
            {"id": 1, "title": "", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/1"},
            {"id": 2, "title": "Business Analyst", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/2"},
        ]
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=postings):
            dj.collect_greenhouse(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)
        self.assertEqual(stats_list[0]["malformed"], 1)
        self.assertEqual(stats_list[0]["parsed"], 1)
        self.assertEqual(len(raw_jobs), 1)


class UnavailableSourceTestCase(unittest.TestCase):
    def test_network_error_marks_source_unavailable_and_continues(self):
        raw_jobs, stats_list = [], []
        sources = {"greenhouse_boards": ["acme", "other"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        good_posting = [{"id": 1, "title": "Business Analyst", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/1"}]

        def fake_fetch(token, timeout=20):
            if token == "acme":
                raise urllib.error.URLError("connection refused")
            return good_posting

        with patch.object(dj, "fetch_greenhouse_jobs", side_effect=fake_fetch):
            dj.collect_greenhouse(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)

        acme_stats = next(s for s in stats_list if s["source"] == "greenhouse:acme")
        other_stats = next(s for s in stats_list if s["source"] == "greenhouse:other")
        self.assertEqual(acme_stats["status"], "unavailable")
        self.assertEqual(acme_stats["errors"], 1)
        self.assertEqual(other_stats["status"], "ok")
        self.assertEqual(len(raw_jobs), 1, "the other source must still be collected")


class SourceTimeoutTestCase(unittest.TestCase):
    def test_timeout_treated_as_unavailable(self):
        raw_jobs, stats_list = [], []
        sources = {"lever_companies": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        with patch.object(dj, "fetch_lever_jobs", side_effect=TimeoutError("timed out")):
            dj.collect_lever(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)
        self.assertEqual(stats_list[0]["status"], "unavailable")
        self.assertEqual(len(raw_jobs), 0)


class PaginationTestCase(unittest.TestCase):
    def test_smartrecruiters_pagination_stops_on_short_page(self):
        raw_jobs, stats_list = [], []
        sources = {"smartrecruiters_companies": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])

        def make_page(n, start_id):
            return {"content": [
                {"id": str(start_id + i), "name": "Business Analyst", "location": {"fullLocation": "Pune, India"}}
                for i in range(n)
            ]}

        pages = [make_page(100, 0), make_page(30, 100)]  # second page shorter than page_size -> stop

        def fake_page(company, offset, limit, timeout=20):
            idx = offset // 100
            return pages[idx] if idx < len(pages) else {"content": []}

        with patch.object(dj, "fetch_smartrecruiters_page", side_effect=fake_page):
            dj.collect_smartrecruiters(sources, keywords, ["Pune"], make_config(max_pages_per_source=5, max_jobs_per_source=1000), raw_jobs, stats_list)

        self.assertEqual(stats_list[0]["found"], 130)
        self.assertEqual(stats_list[0]["attempted"], 2)

    def test_pagination_respects_max_pages_per_source(self):
        raw_jobs, stats_list = [], []
        sources = {"smartrecruiters_companies": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])

        def make_page(n, start_id):
            return {"content": [
                {"id": str(start_id + i), "name": "Business Analyst", "location": {"fullLocation": "Pune, India"}}
                for i in range(n)
            ]}

        # Every page is full -- without a max-pages cap this would loop forever.
        with patch.object(dj, "fetch_smartrecruiters_page", side_effect=lambda company, offset, limit, timeout=20: make_page(100, offset)):
            dj.collect_smartrecruiters(sources, keywords, ["Pune"], make_config(max_pages_per_source=3, max_jobs_per_source=10_000), raw_jobs, stats_list)

        self.assertEqual(stats_list[0]["attempted"], 3, "must stop at MAX_PAGES_PER_SOURCE even though every page is full")


class SourceStatisticsTestCase(unittest.TestCase):
    def test_new_stats_shape(self):
        stats = dj.new_stats("greenhouse:acme")
        for key in ("source", "attempted", "found", "parsed", "malformed", "duplicates", "new_unique", "errors", "status"):
            self.assertIn(key, stats)
        self.assertEqual(stats["status"], "ok")

    def test_collect_greenhouse_reports_accurate_counts(self):
        raw_jobs, stats_list = [], []
        sources = {"greenhouse_boards": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        postings = [
            {"id": 1, "title": "Business Analyst", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/1"},
            {"id": 2, "title": "Senior Backend Engineer", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/2"},
        ]
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=postings):
            dj.collect_greenhouse(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)
        stats = stats_list[0]
        self.assertEqual(stats["found"], 2)
        self.assertEqual(stats["parsed"], 1, "the unrelated engineering title must be filtered, not counted as parsed")
        self.assertEqual(stats["malformed"], 0)


class SourceHealthRejectedCounterTestCase(unittest.TestCase):
    """A posting that is well-formed but filtered by title/location relevance is
    neither 'malformed' nor silently invisible -- it must be visible in source
    health reporting as 'rejected', distinct from a structurally broken record."""

    def test_greenhouse_relevance_miss_is_rejected_not_malformed(self):
        raw_jobs, stats_list = [], []
        sources = {"greenhouse_boards": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        postings = [{"id": 1, "title": "Senior Backend Engineer", "location": {"name": "Pune"}, "content": "", "absolute_url": "https://x/1"}]
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=postings):
            dj.collect_greenhouse(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)
        stats = stats_list[0]
        self.assertEqual(stats["malformed"], 0)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["parsed"], 0)

    def test_lever_relevance_miss_is_rejected(self):
        raw_jobs, stats_list = [], []
        sources = {"lever_companies": ["acme"]}
        candidate = make_candidate()
        keywords = dj.expand_target_titles(candidate["target_titles"], candidate["secondary_titles"])
        postings = [{"id": "1", "text": "Site Reliability Engineer", "categories": {"location": "Pune"}}]
        with patch.object(dj, "fetch_lever_jobs", return_value=postings):
            dj.collect_lever(sources, keywords, ["Pune"], make_config(), raw_jobs, stats_list)
        self.assertEqual(stats_list[0]["rejected"], 1)

    def test_new_stats_includes_rejected_key(self):
        self.assertIn("rejected", dj.new_stats("greenhouse:acme"))

    def test_format_stats_table_includes_rejected_column(self):
        stats_list = [dj.new_stats("greenhouse:acme")]
        stats_list[0]["rejected"] = 3
        table = dj.format_stats_table(stats_list)
        self.assertIn("REJECTED", table)
        self.assertIn("3", table)


if __name__ == "__main__":
    unittest.main()
