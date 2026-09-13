"""Tests for scripts/discover_companies.py (Greenhouse/Lever/Ashby company
discovery & verification -- separate from job-level discovery/scoring, which
scripts/discover_jobs.py and tests/discover_jobs.py already cover and which
this file does not duplicate).

No network calls -- fetch_greenhouse_jobs / fetch_lever_jobs / fetch_ashby_jobs
are mocked at the discover_jobs module level, same pattern tests/discover_jobs.py
already uses.

Run directly:
    python tests/discover_companies.py
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import discover_companies as dc  # noqa: E402
import discover_jobs as dj  # noqa: E402

KEYWORDS = dj.expand_target_titles(
    ["Business Analyst", "Project Manager", "Product Manager", "Associate Product Manager"],
    ["Project Coordinator"],
)


def gh_posting(title="Business Analyst", job_id=1):
    return {"id": job_id, "title": title, "location": {"name": "Remote"}, "content": "", "updated_at": "2026-01-01"}


def lever_posting(title="Business Analyst", job_id="abc"):
    return {"id": job_id, "text": title, "categories": {"location": "Remote"}, "createdAt": 1700000000000}


def ashby_posting(title="Business Analyst", job_id="xyz"):
    return {"id": job_id, "title": title, "location": "Remote", "isListed": True}


def make_config(**overrides):
    base = {"max_companies_per_source": 50, "delay": 0, "timeout": 5}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

class GreenhouseCompanyDiscoveryTestCase(unittest.TestCase):
    def test_valid_board_is_verified(self):
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=[gh_posting()]):
            result = dc.verify_greenhouse("realcompany", timeout=5)
        self.assertTrue(result["exists"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["postings"]), 1)

    def test_invalid_company_is_not_verified(self):
        with patch.object(dj, "fetch_greenhouse_jobs", side_effect=urllib.error.HTTPError("u", 404, "Not Found", {}, None)):
            result = dc.verify_greenhouse("bogus-company-xyz", timeout=5)
        self.assertFalse(result["exists"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["postings"], [])

    def test_empty_board_is_verified_but_has_no_jobs(self):
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=[]):
            result = dc.verify_greenhouse("quietcompany", timeout=5)
        self.assertTrue(result["exists"])
        self.assertEqual(result["postings"], [])

    def test_single_request_no_pagination(self):
        # Greenhouse's public boards-api returns the full board in one call --
        # verify_greenhouse must never loop/paginate.
        mock = patch.object(dj, "fetch_greenhouse_jobs", return_value=[gh_posting()]).start()
        self.addCleanup(patch.stopall)
        dc.verify_greenhouse("realcompany", timeout=5)
        self.assertEqual(mock.call_count, 1)

    def test_relevant_job_is_counted(self):
        postings = [gh_posting(title="Business Analyst"), gh_posting(title="Software Engineer", job_id=2)]
        count = dc.count_relevant(postings, "title", KEYWORDS)
        self.assertEqual(count, 1)


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------

class LeverCompanyDiscoveryTestCase(unittest.TestCase):
    def test_valid_board_is_verified(self):
        with patch.object(dj, "fetch_lever_jobs", return_value=[lever_posting()]):
            result = dc.verify_lever("realcompany", timeout=5)
        self.assertTrue(result["exists"])
        self.assertEqual(len(result["postings"]), 1)

    def test_invalid_company_is_not_verified(self):
        with patch.object(dj, "fetch_lever_jobs", side_effect=urllib.error.HTTPError("u", 404, "Not Found", {}, None)):
            result = dc.verify_lever("bogus-company-xyz", timeout=5)
        self.assertFalse(result["exists"])
        self.assertEqual(result["postings"], [])

    def test_empty_board_is_verified_but_has_no_jobs(self):
        with patch.object(dj, "fetch_lever_jobs", return_value=[]):
            result = dc.verify_lever("quietcompany", timeout=5)
        self.assertTrue(result["exists"])
        self.assertEqual(result["postings"], [])

    def test_single_request_no_pagination(self):
        mock = patch.object(dj, "fetch_lever_jobs", return_value=[lever_posting()]).start()
        self.addCleanup(patch.stopall)
        dc.verify_lever("realcompany", timeout=5)
        self.assertEqual(mock.call_count, 1)

    def test_relevant_job_is_counted_on_text_field(self):
        postings = [lever_posting(title="Project Coordinator"), lever_posting(title="Backend Engineer", job_id="2")]
        count = dc.count_relevant(postings, "text", KEYWORDS)
        self.assertEqual(count, 1)


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------

class AshbyCompanyDiscoveryTestCase(unittest.TestCase):
    def test_valid_board_is_verified(self):
        with patch.object(dj, "fetch_ashby_jobs", return_value=[ashby_posting()]):
            result = dc.verify_ashby("realorg", timeout=5)
        self.assertTrue(result["exists"])
        self.assertEqual(len(result["postings"]), 1)

    def test_invalid_organization_is_not_verified(self):
        with patch.object(dj, "fetch_ashby_jobs", side_effect=urllib.error.HTTPError("u", 404, "Not Found", {}, None)):
            result = dc.verify_ashby("bogus-org-xyz", timeout=5)
        self.assertFalse(result["exists"])
        self.assertEqual(result["postings"], [])

    def test_empty_board_is_verified_but_has_no_jobs(self):
        with patch.object(dj, "fetch_ashby_jobs", return_value=[]):
            result = dc.verify_ashby("quietorg", timeout=5)
        self.assertTrue(result["exists"])
        self.assertEqual(result["postings"], [])

    def test_single_request_no_pagination(self):
        mock = patch.object(dj, "fetch_ashby_jobs", return_value=[ashby_posting()]).start()
        self.addCleanup(patch.stopall)
        dc.verify_ashby("realorg", timeout=5)
        self.assertEqual(mock.call_count, 1)

    def test_does_not_assume_relevance_zero_match_is_reported_separately(self):
        postings = [ashby_posting(title="Site Reliability Engineer")]
        count = dc.count_relevant(postings, "title", KEYWORDS)
        self.assertEqual(count, 0)


# --------------------------------------------------------------------------
# check_connector -- orchestration: skip-active, cap, classification buckets
# --------------------------------------------------------------------------

class CheckConnectorTestCase(unittest.TestCase):
    def test_already_active_company_is_skipped_not_requeried(self):
        sources = {"greenhouse_boards": ["existingco"]}
        with patch.object(dj, "fetch_greenhouse_jobs") as mock_fetch:
            result = dc.check_connector("greenhouse", ["existingco"], sources, KEYWORDS, make_config())
        mock_fetch.assert_not_called()
        self.assertEqual(result["already_active"], 1)
        self.assertEqual(result["checked"], 0)

    def test_verified_with_relevant_job_goes_to_relevant_bucket(self):
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=[gh_posting(title="Product Manager")]):
            result = dc.check_connector("greenhouse", ["newco"], {}, KEYWORDS, make_config())
        self.assertEqual([e["id"] for e in result["verified_relevant"]], ["newco"])
        self.assertEqual(result["verified_no_relevant_match"], [])

    def test_verified_without_relevant_job_goes_to_no_match_bucket(self):
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=[gh_posting(title="Site Reliability Engineer")]):
            result = dc.check_connector("greenhouse", ["newco"], {}, KEYWORDS, make_config())
        self.assertEqual(result["verified_relevant"], [])
        self.assertEqual([e["id"] for e in result["verified_no_relevant_match"]], ["newco"])

    def test_unreachable_company_goes_to_not_found_bucket(self):
        with patch.object(dj, "fetch_greenhouse_jobs", side_effect=urllib.error.HTTPError("u", 404, "Not Found", {}, None)):
            result = dc.check_connector("greenhouse", ["ghostco"], {}, KEYWORDS, make_config())
        self.assertEqual(result["verified_relevant"], [])
        self.assertEqual(result["verified_no_relevant_match"], [])
        self.assertEqual([e["id"] for e in result["not_found_or_unavailable"]], ["ghostco"])

    def test_respects_max_companies_per_source_cap(self):
        with patch.object(dj, "fetch_greenhouse_jobs", return_value=[gh_posting()]) as mock_fetch:
            dc.check_connector(
                "greenhouse", ["a", "b", "c", "d"], {}, KEYWORDS, make_config(max_companies_per_source=2),
            )
        self.assertEqual(mock_fetch.call_count, 2)


# --------------------------------------------------------------------------
# apply_results -- merging verified companies into discovery_sources.json
# --------------------------------------------------------------------------

class ApplyResultsTestCase(unittest.TestCase):
    def test_adds_only_verified_relevant_companies(self):
        sources = {"greenhouse_boards": ["oldco"], "_comment": "keep me"}
        results = [{
            "connector": "greenhouse",
            "verified_relevant": [{"id": "newco", "total_jobs": 3, "relevant_jobs": 1}],
            "verified_no_relevant_match": [{"id": "irrelevantco", "total_jobs": 5, "relevant_jobs": 0}],
        }]
        updated = dc.apply_results(sources, results)
        self.assertEqual(sorted(updated["greenhouse_boards"]), ["newco", "oldco"])
        self.assertNotIn("irrelevantco", updated["greenhouse_boards"])
        self.assertEqual(updated["_comment"], "keep me")

    def test_does_not_duplicate_existing_entry(self):
        sources = {"lever_companies": ["zeta"]}
        results = [{"connector": "lever", "verified_relevant": [{"id": "zeta", "total_jobs": 1, "relevant_jobs": 1}]}]
        updated = dc.apply_results(sources, results)
        self.assertEqual(updated["lever_companies"].count("zeta"), 1)

    def test_leaves_other_connector_keys_untouched(self):
        sources = {"ashby_boards": ["existing"], "workday": [{"name": "Accenture"}]}
        results = [{"connector": "ashby", "verified_relevant": [{"id": "neworg", "total_jobs": 2, "relevant_jobs": 2}]}]
        updated = dc.apply_results(sources, results)
        self.assertEqual(updated["workday"], [{"name": "Accenture"}])


if __name__ == "__main__":
    unittest.main()
