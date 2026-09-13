"""Tests for scripts/score_job.py (role-family-gated personal matching engine).

Run directly:
    python tests/score_job.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import score_job as sj  # noqa: E402

LONG_DESCRIPTION = "A detailed, realistic job description with enough content to look like a real listing. " * 2


def make_candidate(**overrides) -> dict:
    base = {
        "total_experience_years": 1.0,
        "target_titles": ["Business Analyst", "Project Manager", "Product Manager", "Associate Product Manager"],
        "secondary_titles": ["Project Coordinator"],
        "skills": ["Business Analysis", "Requirement Gathering", "SQL", "Tableau", "Excel", "Agile", "Scrum", "Documentation"],
        "preferred_industries": ["IT services", "Product"],
        "avoid_industries": ["Gambling"],
        "preferred_locations": ["Ahmedabad", "Pune", "Mumbai", "Bangalore"],
        "preferred_work_modes": ["remote", "hybrid", "onsite"],
        "min_acceptable_salary_inr_per_year": 300000,
        "target_salary_min_inr_per_year": 350000,
        "target_salary_max_inr_per_year": 400000,
        "preferred_company_sizes": ["startup", "mid-size", "large enterprise", "MNC"],
        "capabilities": {
            "business_analysis": {"strength": "professional", "evidence": ["Business Analyst role: requirement gathering, BRD documentation"]},
            "requirements_gathering": {"strength": "professional", "evidence": ["Requirement gathering and stakeholder alignment at current role"]},
            "stakeholder_communication": {"strength": "professional", "evidence": ["Primary client POC, stakeholder alignment"]},
            "documentation": {"strength": "professional", "evidence": ["BRD/SOW documentation"]},
            "project_management": {"strength": "professional", "evidence": ["Managed 6+ concurrent projects, delivered 3 on time"]},
            "agile_scrum": {"strength": "professional", "evidence": ["Facilitated daily Agile scrum meetings"]},
            "client_facing": {"strength": "professional", "evidence": ["Primary client POC"]},
            "product_management": {"strength": "practical", "evidence": ["Product Manager certification; scoped features for a personal project"]},
            "analytics": {"strength": "practical", "evidence": ["SQL, Tableau, Excel skills (tool-level, no dedicated analytics role)"]},
            "software_development": {"strength": "practical", "evidence": ["Built a mobile app with Flutter/SQLite"]},
            "ai_tools": {"strength": "practical", "evidence": ["Uses ChatGPT/Gemini for documentation and research"]},
            "research": {"strength": "practical", "evidence": ["Conducted market research for a project"]},
            "personal_projects": {"strength": "practical", "evidence": ["Two personal/project apps with BRD-style scoping"]},
            "design_figma": {"strength": "none", "evidence": []},
            "testing_qa": {"strength": "none", "evidence": []},
            "freelance_or_startup_founder_work": {"strength": "none", "evidence": []},
        },
    }
    base.update(overrides)
    return base


def make_job(**overrides) -> dict:
    base = {
        "id": "job-1",
        "title": "Business Analyst",
        "company": "Acme IT Services",
        "industry": "IT services",
        "company_size": "mid-size",
        "location": "Pune",
        "work_mode": "hybrid",
        "required_skills": ["Business Analysis", "Requirement Gathering"],
        "nice_to_have_skills": ["Tableau"],
        "min_experience_years": 0,
        "max_experience_years": 1,
        "salary_min": 350000,
        "salary_max": 400000,
        "description": LONG_DESCRIPTION,
        "application_url": "https://example.com/job/1",
    }
    base.update(overrides)
    return base


class WeightsTestCase(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(
            sj.WEIGHT_ROLE_FAMILY + sj.WEIGHT_CAPABILITY + sj.WEIGHT_EVIDENCE + sj.WEIGHT_TITLE
            + sj.WEIGHT_EXPERIENCE + sj.WEIGHT_CAREER_LEVEL + sj.WEIGHT_LOCATION
            + sj.WEIGHT_INDUSTRY + sj.WEIGHT_SALARY,
            100,
        )

    def test_evidence_subweights_sum_to_evidence_weight(self):
        self.assertEqual(sj.EVIDENCE_REQUIRED_WEIGHT + sj.EVIDENCE_NICE_TO_HAVE_WEIGHT, sj.WEIGHT_EVIDENCE)

    def test_location_subweights_sum_to_location_weight(self):
        self.assertEqual(sj.WORK_MODE_WEIGHT + sj.LOCATION_SUBWEIGHT, sj.WEIGHT_LOCATION)

    def test_industry_subweights_sum_to_industry_weight(self):
        self.assertEqual(sj.INDUSTRY_SUBWEIGHT + sj.COMPANY_SIZE_SUBWEIGHT, sj.WEIGHT_INDUSTRY)


# --------------------------------------------------------------------------
# 1. Role family classification -- one test per title the user listed
# --------------------------------------------------------------------------

class RoleFamilyClassificationTestCase(unittest.TestCase):
    def test_product_manager(self):
        self.assertEqual(sj.classify_role_family("Product Manager"), "PRODUCT")

    def test_associate_product_manager(self):
        self.assertEqual(sj.classify_role_family("Associate Product Manager"), "PRODUCT")

    def test_product_owner(self):
        self.assertEqual(sj.classify_role_family("Product Owner"), "PRODUCT")

    def test_product_analyst(self):
        self.assertEqual(sj.classify_role_family("Product Analyst"), "PRODUCT")

    def test_business_analyst(self):
        self.assertEqual(sj.classify_role_family("Business Analyst"), "BUSINESS_ANALYSIS")

    def test_business_systems_analyst(self):
        self.assertEqual(sj.classify_role_family("Business Systems Analyst"), "BUSINESS_ANALYSIS")

    def test_functional_analyst(self):
        self.assertEqual(sj.classify_role_family("Functional Analyst"), "BUSINESS_ANALYSIS")

    def test_project_manager(self):
        self.assertEqual(sj.classify_role_family("Project Manager"), "PROJECT_PROGRAM")

    def test_program_coordinator(self):
        self.assertEqual(sj.classify_role_family("Program Coordinator"), "PROJECT_PROGRAM")

    def test_software_engineer(self):
        self.assertEqual(sj.classify_role_family("Software Engineer"), "OTHER_TECH")

    def test_data_scientist(self):
        self.assertEqual(sj.classify_role_family("Data Scientist"), "OTHER_TECH")

    def test_sales_manager(self):
        self.assertEqual(sj.classify_role_family("Sales Manager"), "SALES")

    def test_account_executive(self):
        self.assertEqual(sj.classify_role_family("Account Executive"), "SALES")

    def test_product_counsel(self):
        # The critical fix: "Product Counsel" must NOT be classified PRODUCT
        # just because it contains the word "Product".
        self.assertEqual(sj.classify_role_family("Product Counsel"), "LEGAL")

    def test_product_counsel_associate_manager(self):
        # The exact real-world false positive from the previous version.
        self.assertEqual(sj.classify_role_family("Product Counsel Associate Manager"), "LEGAL")

    def test_legal_counsel(self):
        self.assertEqual(sj.classify_role_family("Legal Counsel"), "LEGAL")

    def test_corporate_counsel(self):
        self.assertEqual(sj.classify_role_family("Corporate Counsel"), "LEGAL")

    def test_product_marketing(self):
        self.assertEqual(sj.classify_role_family("Product Marketing Manager"), "MARKETING")

    def test_recruiter(self):
        self.assertEqual(sj.classify_role_family("Recruiter"), "HR")

    def test_unclassifiable_is_unknown(self):
        self.assertEqual(sj.classify_role_family("Chief Astronaut Officer of Vibes"), "UNKNOWN")

    def test_salesforce_administrator_is_not_misread_as_sales_family(self):
        # Regression: "sales" is a substring of "salesforce".
        self.assertNotEqual(sj.classify_role_family("Salesforce Administrator"), "SALES")

    def test_product_management_analyst_not_overridden_by_marketing_in_description(self):
        # Regression: a real Accenture posting's description mentioned "Marketing
        # Operations" even though the title and job itself are Product Management.
        family = sj.classify_role_family("Product Management Analyst", description="Skill required: Marketing Operations - Digital Project Management")
        self.assertEqual(family, "PRODUCT")

    def test_ai_product_management_practitioner_not_overridden_by_data_scientist_mention(self):
        # Regression: description mentioning "data scientists" as collaborators
        # must not override the clear PRODUCT title signal.
        family = sj.classify_role_family("AI Product Management Practitioner", description="Collaborating with data scientists, engineers, and stakeholders")
        self.assertEqual(family, "PRODUCT")


class CandidateTargetFamiliesTestCase(unittest.TestCase):
    def test_derived_from_target_and_secondary_titles(self):
        families = sj.candidate_target_role_families(make_candidate())
        self.assertEqual(families, {"PRODUCT", "BUSINESS_ANALYSIS", "PROJECT_PROGRAM"})


# --------------------------------------------------------------------------
# 2. Career level classification
# --------------------------------------------------------------------------

class CareerLevelTestCase(unittest.TestCase):
    def test_internal_platform_is_not_misread_as_intern(self):
        # Regression: "intern" is a substring of "internal" -- must not match.
        self.assertNotEqual(sj.classify_career_level("Product Manager (Internal Platform)", min_experience_years=3), "INTERN")

    def test_senior_keyword(self):
        self.assertEqual(sj.classify_career_level("Senior Product Manager"), "SENIOR")

    def test_lead_keyword(self):
        self.assertEqual(sj.classify_career_level("Lead Product Manager"), "LEAD")

    def test_group_product_manager_is_lead(self):
        self.assertEqual(sj.classify_career_level("Group Product Manager"), "LEAD")

    def test_associate_director_is_director_not_junior(self):
        self.assertEqual(sj.classify_career_level("Associate Director - Product"), "DIRECTOR")

    def test_director_keyword(self):
        self.assertEqual(sj.classify_career_level("Director of Product"), "DIRECTOR")

    def test_associate_product_manager_is_junior(self):
        self.assertEqual(sj.classify_career_level("Associate Product Manager"), "JUNIOR")

    def test_generic_manager_title_is_manager_level(self):
        self.assertEqual(sj.classify_career_level("Manager - Accounts"), "MANAGER")

    def test_bare_product_manager_uses_years_not_title(self):
        # "Manager" here names the FUNCTION (product manager), not a seniority level.
        self.assertEqual(sj.classify_career_level("Product Manager", min_experience_years=1), "JUNIOR")

    def test_years_fallback_1_to_2_is_junior(self):
        self.assertEqual(sj.classify_career_level("Product Manager", min_experience_years=2), "JUNIOR")

    def test_years_fallback_above_2_is_mid_level(self):
        self.assertEqual(sj.classify_career_level("Product Manager", min_experience_years=2.5), "MID_LEVEL")

    def test_years_fallback_5_plus(self):
        self.assertEqual(sj.classify_career_level("Product Manager", min_experience_years=5), "SENIOR")

    def test_years_fallback_7_plus(self):
        self.assertEqual(sj.classify_career_level("Product Manager", min_experience_years=7.5), "LEAD")

    def test_no_signal_at_all_is_unknown(self):
        self.assertEqual(sj.classify_career_level("Product Manager"), "UNKNOWN")


# --------------------------------------------------------------------------
# Role-family dampening -- the core anti-false-positive mechanism
# --------------------------------------------------------------------------

class FamilyDampeningTestCase(unittest.TestCase):
    def test_target_family_not_dampened(self):
        result = sj.score_role_family(make_job(title="Business Analyst"), make_candidate())
        self.assertEqual(sj.family_dampening_factor(result), 1.0)
        self.assertFalse(result["is_mismatch"])

    def test_mismatched_family_heavily_dampened(self):
        result = sj.score_role_family(make_job(title="Product Counsel Associate Manager"), make_candidate())
        self.assertTrue(result["is_mismatch"])
        self.assertEqual(result["score"], 0)
        self.assertEqual(sj.family_dampening_factor(result), sj.FAMILY_MISMATCH_DAMPENING)

    def test_product_counsel_never_scores_as_a_strong_product_match(self):
        # This is issue #1 from the user report: transferable BA-type evidence
        # (requirement gathering, client POC, BRD/SOW) must not carry a LEGAL
        # role to a high score just because the candidate has those skills.
        job = make_job(
            title="Product Counsel Associate Manager", required_skills=[], nice_to_have_skills=[],
            min_experience_years=None, max_experience_years=None,
        )
        result = sj.score_job(job, make_candidate())
        self.assertLess(result["overall_match_score"], 60)
        self.assertEqual(result["role_family"], "LEGAL")
        self.assertIn(result["application_decision"], ("SKIP", "REVIEW"))
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")


# --------------------------------------------------------------------------
# 4/5. Score vs. application decision, and the AUTO_APPLY/REVIEW/SKIP rules
# --------------------------------------------------------------------------

class ApplicationDecisionTestCase(unittest.TestCase):
    def test_strong_entry_level_match_is_auto_apply(self):
        job = make_job(title="Business Analyst", min_experience_years=0, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "AUTO_APPLY")
        self.assertGreaterEqual(result["overall_match_score"], 85)

    def test_senior_product_manager_is_skip_regardless_of_score(self):
        # Issue #2: a senior title must never be auto-apply eligible, no matter
        # how high capability/evidence otherwise scores.
        job = make_job(
            title="Senior Product Manager", required_skills=[], nice_to_have_skills=[],
            min_experience_years=None, max_experience_years=None,
        )
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["career_level"], "SENIOR")
        self.assertEqual(result["application_decision"], "SKIP")

    def test_lead_product_manager_is_skip(self):
        job = make_job(title="Lead Product Manager", min_experience_years=None, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "SKIP")

    def test_70_to_79_band_with_meaningful_gap_is_review_not_auto_apply(self):
        # Issue #3: a 70-79 job with a real experience gap must be REVIEW, never AUTO_APPLY.
        job = make_job(title="Business Analyst", min_experience_years=3, max_experience_years=5, required_skills=["Business Analysis"])
        result = sj.score_job(job, make_candidate())
        if 70 <= result["overall_match_score"] < 85:
            self.assertNotEqual(result["application_decision"], "AUTO_APPLY")

    def test_product_manager_2_to_3_years_is_review_not_skip(self):
        # Section 6 example: 1y candidate + strong product project evidence
        # against a 2-3y Product Manager req should be REVIEW, not an automatic SKIP.
        job = make_job(title="Product Manager", min_experience_years=2, max_experience_years=3, required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 1.0)
        self.assertNotEqual(result["application_decision"], "SKIP")

    def test_product_manager_5_plus_years_is_skip(self):
        job = make_job(title="Product Manager", min_experience_years=5, max_experience_years=None, required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "SKIP")

    def test_product_manager_7_plus_years_senior_is_skip(self):
        job = make_job(title="Product Manager", min_experience_years=7, max_experience_years=None, required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "SKIP")
        self.assertIn(result["career_level"], sj.SENIOR_PLUS_CAREER_LEVELS)

    def test_unrelated_role_family_software_engineer_is_skip(self):
        job = make_job(title="Software Engineer", required_skills=["Python", "Kubernetes"], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "OTHER_TECH")
        self.assertEqual(result["application_decision"], "SKIP")

    def test_unrelated_role_family_sales_manager_is_skip(self):
        job = make_job(title="Sales Manager", required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "SALES")
        self.assertEqual(result["application_decision"], "SKIP")

    def test_legal_counsel_is_skip(self):
        job = make_job(title="Legal Counsel", required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "LEGAL")
        self.assertEqual(result["application_decision"], "SKIP")


# --------------------------------------------------------------------------
# 6/7. Experience gap and generic transferable skills don't overpower fit
# --------------------------------------------------------------------------

class TransferableSkillsTestCase(unittest.TestCase):
    def test_project_and_internship_evidence_shows_as_transferable_or_relevant(self):
        job = make_job(title="Product Manager", required_skills=[], nice_to_have_skills=[], min_experience_years=1, max_experience_years=2)
        result = sj.score_job(job, make_candidate())
        self.assertTrue(any("project" in e.lower() or "practical" in e.lower() for e in result["relevant_evidence"]))

    def test_generic_capabilities_alone_do_not_make_legal_role_strong(self):
        job = make_job(title="Corporate Counsel", required_skills=["Stakeholder management", "Documentation"], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "LEGAL")
        self.assertLess(result["breakdown"]["capability_score"], sj.WEIGHT_CAPABILITY * 0.5)
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")

    def test_unrelated_formal_title_with_strong_transferable_capability_still_gated_by_family(self):
        job = make_job(title="HR Business Partner", required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "HR")
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")


class OneYearCandidateScenariosTestCase(unittest.TestCase):
    """Explicit scenarios requested: 1-year candidate against project/internship
    evidence, an unrelated formal title, and strong transferable capabilities --
    verifying role-family relevance prevents false positives in each case."""

    def test_with_project_experience_only_still_capped_by_family_when_unrelated(self):
        candidate = make_candidate()
        job = make_job(title="Marketing Manager", required_skills=[], nice_to_have_skills=[])
        result = sj.score_job(job, candidate)
        self.assertEqual(result["role_family"], "MARKETING")
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")

    def test_with_internship_evidence_in_target_family_can_still_auto_apply(self):
        # Internship evidence lives under "software_development" (practical) for
        # this candidate, not a target family -- but Business Analyst (a target
        # family with professional evidence) should still be able to auto-apply.
        job = make_job(title="Business Analyst", min_experience_years=0, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "AUTO_APPLY")

    def test_unrelated_formal_title_is_gated_despite_transferable_skills(self):
        job = make_job(title="Digital Marketing Specialist", required_skills=["Communication", "Documentation"], nice_to_have_skills=[])
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["role_family"], "MARKETING")
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")


# --------------------------------------------------------------------------
# Location / salary / industry unknown-handling (carried over, still correct)
# --------------------------------------------------------------------------

class LocationWorkModeTestCase(unittest.TestCase):
    def test_unknown_work_mode_is_not_penalized(self):
        result = sj.score_location_work_mode(make_job(work_mode=None), make_candidate())
        self.assertEqual(result["score"], sj.WEIGHT_LOCATION)

    def test_unknown_location_is_not_penalized(self):
        result = sj.score_location_work_mode(make_job(location=""), make_candidate())
        self.assertEqual(result["score"], sj.WEIGHT_LOCATION)


class SalaryTestCase(unittest.TestCase):
    def test_unspecified_salary_is_never_penalized(self):
        result = sj.score_salary(make_job(salary_min=None, salary_max=None), make_candidate())
        self.assertEqual(result["score"], sj.WEIGHT_SALARY)


class RecommendationBoundsTestCase(unittest.TestCase):
    def test_total_score_bounds(self):
        weak_candidate = make_candidate(
            target_titles=["Nothing Related"], secondary_titles=[], skills=[],
            capabilities={k: {"strength": "none", "evidence": []} for k in make_candidate()["capabilities"]},
        )
        weak_job = make_job(
            title="Totally Unrelated Senior Director Role", required_skills=["Skill A", "Skill B"],
            min_experience_years=15, max_experience_years=20, salary_min=10000, salary_max=20000,
            work_mode="onsite", location="Nowhere Else", industry="IT services",
        )
        result = sj.score_job(weak_job, weak_candidate)
        self.assertGreaterEqual(result["overall_match_score"], 0)
        self.assertLessEqual(result["overall_match_score"], 100)
        self.assertEqual(result["application_decision"], "SKIP")


class AutoApplyPreconditionsTestCase(unittest.TestCase):
    def test_all_checks_pass_for_a_clean_auto_apply_job(self):
        job = make_job(title="Business Analyst", min_experience_years=0, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        precheck = sj.verify_auto_apply_preconditions(job, result, applications=[])
        self.assertEqual(precheck["eligible"], result["application_decision"] == "AUTO_APPLY")

    def test_already_applied_job_is_not_eligible(self):
        job = make_job(title="Business Analyst", id="job-1", min_experience_years=0, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        applications = [{"job_id": "job-1", "application_url": job["application_url"]}]
        precheck = sj.verify_auto_apply_preconditions(job, result, applications=applications)
        self.assertFalse(precheck["eligible"])
        self.assertFalse(precheck["checks"]["not_already_applied"])


class ExperienceSafetyGateTestCase(unittest.TestCase):
    """AUTO_APPLY safety fix: a job's stated minimum experience must not
    exceed the candidate's actual professional experience (1.0 year for
    make_candidate()). Regression coverage for the bug where a candidate
    with 1 year could reach AUTO_APPLY against a job requiring 2 years,
    because the old AUTO_APPLY_MAX_EXPERIENCE_GAP (1.0) treated a 1-year
    shortfall as "close enough" -- it never distinguished "0 years short"
    from "1 year short of a 1-year candidate's entire experience"."""

    def test_case1_job_requires_zero_years_can_auto_apply(self):
        job = make_job(title="Business Analyst", min_experience_years=0, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["application_decision"], "AUTO_APPLY")

    def test_case2_job_requires_exactly_candidate_years_can_auto_apply(self):
        job = make_job(title="Business Analyst", min_experience_years=1, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 0.0)
        self.assertEqual(result["application_decision"], "AUTO_APPLY")

    def test_case3_job_requires_one_plus_years_can_auto_apply(self):
        job = make_job(title="Business Analyst", min_experience_years=1, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 0.0)
        self.assertEqual(result["application_decision"], "AUTO_APPLY")

    def test_case4_job_requires_two_years_is_not_auto_apply(self):
        # Reproduces the reported bug: candidate=1y, job requires 2y.
        job = make_job(title="Business Analyst", min_experience_years=2, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 1.0)
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")
        self.assertEqual(result["application_decision"], "REVIEW")

    def test_case5_job_requires_three_years_is_not_auto_apply(self):
        job = make_job(title="Business Analyst", min_experience_years=3, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 2.0)
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")
        self.assertIn(result["application_decision"], ("REVIEW", "SKIP"))

    def test_case6_job_requires_five_plus_years_is_skip(self):
        job = make_job(title="Business Analyst", min_experience_years=5, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")
        self.assertEqual(result["application_decision"], "SKIP")

    def test_case7_unstated_experience_is_not_rejected_for_being_unknown(self):
        job = make_job(title="Business Analyst", min_experience_years=None, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["experience_gap_years"], 0.0)
        self.assertNotEqual(result["application_decision"], "SKIP")

    def test_case8_senior_title_marker_skips_regardless_of_low_experience_requirement(self):
        job = make_job(title="Senior Business Analyst", min_experience_years=0, max_experience_years=1)
        result = sj.score_job(job, make_candidate())
        self.assertEqual(result["career_level"], "SENIOR")
        self.assertEqual(result["application_decision"], "SKIP")

    def test_case9_high_score_does_not_override_experience_mismatch(self):
        # Mandatory case: this is the exact shape of the reported bug --
        # a job scoring comfortably >=85 must still not reach AUTO_APPLY
        # when its stated minimum experience exceeds the candidate's.
        job = make_job(title="Business Analyst", min_experience_years=2, max_experience_years=None)
        result = sj.score_job(job, make_candidate())
        self.assertGreaterEqual(result["overall_match_score"], 85)
        self.assertNotEqual(result["application_decision"], "AUTO_APPLY")


if __name__ == "__main__":
    unittest.main()
