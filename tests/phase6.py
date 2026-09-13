"""Tests for the Phase 6 Application Intelligence layer (scripts/job_analysis.py,
resume_tailoring.py, cover_letter.py, application_answers.py, claim_validation.py,
application_package.py, run_phase6.py).

No live LLM call anywhere in this suite -- every test uses llm_provider.MockProvider
with a canned response. Phase 5 fixtures (job dicts) are hand-built, not loaded
from the real data/jobs.json, so this suite never depends on live discovery data.

Run directly:
    python tests/phase6.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import application_answers as aa
import application_package as ap
import claim_validation as cv
import cover_letter as cl
import job_analysis as ja
import resume_tailoring as rt
import run_phase6 as r6
from llm_provider import LLMProviderError, MockProvider

CANDIDATE = {
    "name": "Test Candidate",
    "total_experience_years": 1.0,
    "target_titles": ["Business Analyst"],
    "secondary_titles": [],
    "skills": ["Business Analysis", "Requirement Gathering", "SQL"],
    "target_salary_min_inr_per_year": 350000,
    "target_salary_max_inr_per_year": 400000,
    "work_authorization": "Authorized to work in India; no visa sponsorship needed",
    "preferred_locations": ["Pune", "Mumbai"],
    "earliest_start_date": "2026-10-02",
    "capabilities": {
        "business_analysis": {"strength": "professional", "evidence": ["Requirement gathering and BRD documentation at Tibicle LLP"]},
        "personal_projects": {"strength": "practical", "evidence": ["Expense Tracker App: led requirement gathering (personal project)"]},
    },
}
CANDIDATE_CTX = {
    "candidate": CANDIDATE,
    "profile_hash": "fixedhash1",
    "evidence_blob": ja.__name__ and None,  # placeholder, replaced below
}
CANDIDATE_CTX["evidence_blob"] = "business analysis requirement gathering sql requirement gathering and brd documentation at tibicle llp expense tracker app: led requirement gathering (personal project)"
CANDIDATE_CTX["verified_employers"] = ["Tibicle LLP"]
CANDIDATE_CTX["experience_section"] = "- Current role: Business Analyst, Tibicle LLP (Apr 2025-Present)"
CANDIDATE_CTX["education_section"] = "- B.Tech Computer Engineering, Marwadi University, 2022-2025"
CANDIDATE_CTX["contact_section"] = "- Email: test@example.com"


def make_job(**overrides) -> dict:
    base = {
        "id": "job-1", "title": "Business Analyst", "company": "Acme",
        "application_decision": "REVIEW", "score": 90, "role_family": "BUSINESS_ANALYSIS",
        "career_level": "JUNIOR", "description": "We need a Business Analyst with SQL skills.",
        "application_url": "https://example.com/apply/1",
        "required_skills": ["Business Analysis"], "preferred_skills": [],
        "min_experience_years": 0, "max_experience_years": 1,
    }
    base.update(overrides)
    return base


VALID_ANALYSIS_JSON = json.dumps({
    "target_role": "Business Analyst", "company": "Acme", "core_mission": "Support delivery.",
    "must_have_requirements": ["SQL"], "nice_to_have_requirements": [],
    "core_responsibilities": ["Requirement gathering"],
    "technical_skills": ["SQL"], "product_skills": [], "business_skills": ["Business Analysis"],
    "project_skills": [], "communication_skills": ["Stakeholder communication"],
    "domain_requirements": [], "seniority_signals": [], "success_signals": [],
    "candidate_matches": ["Professional business analysis experience at Tibicle LLP"],
    "candidate_gaps": [], "evidence_to_highlight": ["Requirement gathering and BRD documentation at Tibicle LLP"],
    "unsupported_claims_to_avoid": ["Do not claim more than 1 year of professional experience"],
    "resume_keywords": ["Business Analysis"], "cover_letter_angles": ["Cross-functional coordination"],
    "application_strategy": "Emphasize BA experience.", "confidence": 0.8,
})

VALID_RESUME_JSON = json.dumps({
    "summary": "Business Analyst with hands-on requirement gathering experience.",
    "highlighted_experience": ["Requirement gathering and BRD documentation at Tibicle LLP"],
    "highlighted_projects": ["Expense Tracker App: led requirement gathering (personal project)"],
    "skills_to_feature": ["Business Analysis", "SQL"],
    "keywords_used": ["Business Analysis"],
})

OVERCLAIMED_RESUME_JSON = json.dumps({
    "summary": "Business Analyst with 3+ years of professional experience.",
    "highlighted_experience": ["Managed teams at Fabricated Corp"],
    "highlighted_projects": [],
    "skills_to_feature": ["Business Analysis", "Kubernetes"],
    "keywords_used": [],
})

COVER_LETTER_NEEDED_JSON = json.dumps({"cover_letter_needed": True, "reason": "Strong fit", "body": "Dear Hiring Manager, I am excited to apply."})
COVER_LETTER_NOT_NEEDED_JSON = json.dumps({"cover_letter_needed": False, "reason": "ATS posting", "body": ""})

# Phase 6.1 quality-fix fixtures: reproduce the two live-observed defect
# classes (self-disqualifying language, whitespace artifacts) so the fixes
# are tested against realistic content, not just unit-level regexes.
GAP_LANGUAGE_RESUME_JSON = json.dumps({
    "summary": "Business Analyst with requirement gathering experience, though I only have 1 year, below the required 3 years for this role.",
    "highlighted_experience": ["Requirement gathering and BRD documentation at Tibicle LLP"],
    "highlighted_projects": [],
    "skills_to_feature": ["Business Analysis"],
    "keywords_used": [],
})
WHITESPACE_ARTIFACT_RESUME_JSON = json.dumps({
    "summary": "Business Analyst with strong requirement gathering skills.  Note:candidate has led cross-functional teams.",
    "highlighted_experience": ["Requirement gathering and BRD documentation at Tibicle LLP"],
    "highlighted_projects": [],
    "skills_to_feature": ["Business Analysis"],
    "keywords_used": [],
})
EXCESSIVE_GAP_LANGUAGE_COVER_LETTER_JSON = json.dumps({
    "cover_letter_needed": True, "reason": "Relevant background",
    "body": (
        "Dear Hiring Manager, I want to be transparent that my experience is below the required years for "
        "this role. I know I only have 1 year and this does not meet the stated minimum, but I bring strong "
        "requirement gathering experience from Tibicle LLP."
    ),
})
SINGLE_GAP_MENTION_COVER_LETTER_JSON = json.dumps({
    "cover_letter_needed": True, "reason": "Relevant background",
    "body": (
        "Dear Hiring Manager, my requirement gathering and stakeholder management experience at Tibicle LLP "
        "maps closely to this role. While my experience gap against the stated years is real, I believe my "
        "hands-on delivery track record makes me a strong candidate worth considering."
    ),
})
ANSWER_JSON = json.dumps({"answer": "I led requirement gathering at Tibicle LLP.", "confidence": "HIGH", "reason": "Directly from verified experience."})
ANSWER_NEEDS_INPUT_JSON = json.dumps({"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT", "reason": "Not in candidate profile."})
# Reproduces the exact live failure: parseable JSON, but an empty answer and
# reason with a "valid" confidence level -- observed for real on 6 open-ended
# questions for one Accenture job during the live Claude smoke test.
EMPTY_ANSWER_JSON = json.dumps({"answer": "", "confidence": "LOW", "reason": ""})
WHITESPACE_ANSWER_JSON = json.dumps({"answer": "   ", "confidence": "LOW", "reason": ""})


# --------------------------------------------------------------------------
# 1-3: Job selection respects Phase 5's decision
# --------------------------------------------------------------------------

class JobSelectionTestCase(unittest.TestCase):
    def test_auto_apply_job_is_selected(self):
        jobs = [make_job(id="a", application_decision="AUTO_APPLY")]
        self.assertEqual([j["id"] for j in r6.select_jobs(jobs)], ["a"])

    def test_skip_job_is_not_selected_by_default(self):
        jobs = [make_job(id="a", application_decision="SKIP")]
        self.assertEqual(r6.select_jobs(jobs), [])

    def test_skip_job_can_be_selected_explicitly_by_id_for_debugging(self):
        jobs = [make_job(id="a", application_decision="SKIP")]
        self.assertEqual([j["id"] for j in r6.select_jobs(jobs, job_id="a")], ["a"])

    def test_review_job_is_selected(self):
        jobs = [make_job(id="a", application_decision="REVIEW")]
        self.assertEqual([j["id"] for j in r6.select_jobs(jobs)], ["a"])


# --------------------------------------------------------------------------
# 4-5: Application answers -- deterministic + NEEDS_USER_INPUT
# --------------------------------------------------------------------------

class ApplicationAnswerTestCase(unittest.TestCase):
    def test_known_salary_preference_is_high_confidence_no_llm_call(self):
        provider = MockProvider(fail=True)  # must never be called
        result = aa.answer_question("What are your salary expectations?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["confidence"], "HIGH")
        self.assertIn("350,000", result["answer"])

    def test_notice_period_is_needs_user_input_deterministically(self):
        provider = MockProvider(fail=True)
        result = aa.answer_question("What is your current notice period?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["confidence"], "NEEDS_USER_INPUT")
        self.assertEqual(result["answer"], "NEEDS_USER_INPUT")

    def test_years_threshold_question_answered_deterministically_as_no(self):
        provider = MockProvider(fail=True)
        result = aa.answer_question("Do you have at least 5 years of experience?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertTrue(result["answer"].startswith("No"))
        self.assertEqual(result["confidence"], "HIGH")

    def test_open_ended_question_needing_llm_returns_needs_user_input_when_llm_says_so(self):
        provider = MockProvider(response=ANSWER_NEEDS_INPUT_JSON)
        result = aa.answer_question("What is your favorite programming language?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["confidence"], "NEEDS_USER_INPUT")

    def test_open_ended_question_with_grounded_answer_is_completed(self):
        provider = MockProvider(response=ANSWER_JSON)
        result = aa.answer_question("Describe a relevant project.", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertTrue(result["answer"].strip(), "a COMPLETED answer must have real content")

    def test_deterministic_authorization_answer_is_unchanged(self):
        provider = MockProvider(fail=True)  # must never be called
        result = aa.answer_question("Are you authorized to work in this location?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertIn("Authorized to work in India", result["answer"])

    # -- Regression coverage for the empty-COMPLETED-answer bug found live --

    def test_empty_llm_answer_is_not_completed(self):
        provider = MockProvider(response=EMPTY_ANSWER_JSON)
        result = aa.answer_question("Why do you want this role?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertNotEqual(result["status"], "COMPLETED")
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("error", result)

    def test_whitespace_only_llm_answer_is_not_completed(self):
        provider = MockProvider(response=WHITESPACE_ANSWER_JSON)
        result = aa.answer_question("Tell us about yourself.", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertNotEqual(result["status"], "COMPLETED")
        self.assertEqual(result["status"], "FAILED")

    def test_provider_failure_does_not_produce_completed_empty_answer(self):
        provider = MockProvider(fail=True)
        result = aa.answer_question("Why this company?", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("answer", result)

    def test_malformed_json_does_not_produce_completed_empty_answer(self):
        provider = MockProvider(response="this is not json")
        result = aa.answer_question("Describe a challenge you solved.", make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("answer", result)

    def test_no_answer_record_from_answer_all_is_ever_completed_with_empty_text(self):
        # Sweep every standard question through the exact empty-response
        # pattern observed live and assert the invariant holds across all of
        # them, not just one hand-picked question.
        provider = MockProvider(responses=[EMPTY_ANSWER_JSON] * len(aa.STANDARD_QUESTIONS))
        records = aa.answer_all(aa.STANDARD_QUESTIONS, make_job(), {"status": "COMPLETED", "analysis": {}}, CANDIDATE_CTX, provider)
        for record in records:
            if record["status"] == "COMPLETED":
                self.assertTrue(
                    (record.get("answer") or "").strip(),
                    f"COMPLETED answer for {record['question']!r} must not be empty: {record}",
                )


# --------------------------------------------------------------------------
# 6-7: Claim validation catches fabrication; evidence is never invented
# --------------------------------------------------------------------------

class ClaimValidationTestCase(unittest.TestCase):
    def test_overclaimed_years_of_experience_is_flagged(self):
        issues = cv.check_years_of_experience_claims("I have 3 years of professional experience.", candidate_years=1.0)
        self.assertTrue(issues)

    def test_truthful_years_of_experience_is_not_flagged(self):
        issues = cv.check_years_of_experience_claims("I have 1 year of professional experience.", candidate_years=1.0)
        self.assertEqual(issues, [])

    def test_unverified_employer_is_flagged(self):
        issues = cv.check_unverified_employers("I worked at Fabricated Corp on this.", verified_employers=["Tibicle LLP"])
        self.assertTrue(issues)

    def test_verified_employer_is_not_flagged(self):
        issues = cv.check_unverified_employers("I worked at Tibicle LLP on this.", verified_employers=["Tibicle LLP"])
        self.assertEqual(issues, [])

    def test_target_company_mention_is_not_flagged_as_fabricated_employer(self):
        # A cover letter naturally says "excited to apply to Acme" or
        # "interest in the role at Acme" -- the company being applied TO is
        # not a claim of past employment there, and must never be confused
        # with one. Found live during the Phase 6 smoke test.
        issues = cv.check_unverified_employers(
            "I am writing to express my interest in the Business Analyst position at Fixture Co.",
            verified_employers=["Tibicle LLP"], target_company="Fixture Co",
        )
        self.assertEqual(issues, [])

    def test_different_unverified_employer_still_flagged_alongside_target_company(self):
        issues = cv.check_unverified_employers(
            "I am applying to Fixture Co and previously worked at Fabricated Corp.",
            verified_employers=["Tibicle LLP"], target_company="Fixture Co",
        )
        self.assertTrue(any("Fabricated Corp" in i for i in issues))

    def test_unsupported_numeric_claim_is_flagged(self):
        issues = cv.check_unsupported_numeric_claims("I reduced costs by 47%.", evidence_blob="requirement gathering brd documentation")
        self.assertTrue(issues)

    def test_resume_with_fabricated_content_is_needs_review(self):
        provider = MockProvider(response=OVERCLAIMED_RESUME_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = rt.tailor_resume(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(result["validation"]["issues"])

    def test_resume_with_fabricated_skill_is_needs_review(self):
        provider = MockProvider(response=OVERCLAIMED_RESUME_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = rt.tailor_resume(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertTrue(any("Kubernetes" in issue for issue in result["validation"]["issues"]))

    # -- Phase 6.1: check_experience_gap_language unit coverage --

    def test_gap_language_flagged_when_disallowed(self):
        issues = cv.check_experience_gap_language("I only have 1 year, below the required 3 years.", max_mentions=0)
        self.assertTrue(issues)

    def test_gap_language_absent_text_passes_zero_mentions(self):
        issues = cv.check_experience_gap_language("I led requirement gathering at Tibicle LLP.", max_mentions=0)
        self.assertEqual(issues, [])

    def test_gap_language_single_mention_allowed_under_higher_limit(self):
        issues = cv.check_experience_gap_language("My experience gap against the stated years is real.", max_mentions=1)
        self.assertEqual(issues, [])

    def test_gap_language_exceeding_limit_is_flagged(self):
        text = "This is below the required years. I only have 1 year, which does not meet the minimum."
        issues = cv.check_experience_gap_language(text, max_mentions=1)
        self.assertTrue(issues)


# --------------------------------------------------------------------------
# 8-10: Resume tailoring structural guarantees
# --------------------------------------------------------------------------

class ResumeTailoringStructureTestCase(unittest.TestCase):
    def test_employment_dates_are_preserved_verbatim(self):
        content = json.loads(VALID_RESUME_JSON)
        rendered = rt.render_resume_text(content, CANDIDATE_CTX)
        self.assertIn(CANDIDATE_CTX["experience_section"], rendered)

    def test_education_is_preserved_verbatim(self):
        content = json.loads(VALID_RESUME_JSON)
        rendered = rt.render_resume_text(content, CANDIDATE_CTX)
        self.assertIn(CANDIDATE_CTX["education_section"], rendered)

    def test_professional_experience_years_not_altered_by_llm_content(self):
        # Even if the LLM's summary claims a higher figure, the rendered
        # PROFESSIONAL EXPERIENCE section is the untouched source text --
        # claim_validation is what catches the summary's overclaim separately.
        content = json.loads(OVERCLAIMED_RESUME_JSON)
        rendered = rt.render_resume_text(content, CANDIDATE_CTX)
        self.assertIn(CANDIDATE_CTX["experience_section"], rendered)  # untouched real history present
        self.assertIn("3+ years", rendered)  # the overclaim is present in text FOR validation to catch
        validation = cv.validate_content(rendered, candidate_years=1.0, verified_employers=["Tibicle LLP"], evidence_blob=CANDIDATE_CTX["evidence_blob"])
        self.assertEqual(validation["status"], "NEEDS_REVIEW")

    def test_base_resume_file_is_never_written_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "resumes"
            path = rt.write_resume_file("job-1", "some content", output_dir)
            self.assertTrue(path.name.startswith("job-1"))
            self.assertNotIn("profile", str(path))

    # -- Phase 6.1 quality fixes --

    def test_resume_with_explicit_gap_language_is_needs_review(self):
        provider = MockProvider(response=GAP_LANGUAGE_RESUME_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = rt.tailor_resume(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any("gap" in issue.lower() for issue in result["validation"]["issues"]))

    def test_resume_with_whitespace_artifact_is_needs_review_and_gets_cleaned(self):
        provider = MockProvider(response=WHITESPACE_ARTIFACT_RESUME_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = rt.tailor_resume(make_job(), analysis_record, CANDIDATE_CTX, provider)
        # The double-space is auto-normalized (safe fix)...
        self.assertNotIn("  ", result["content"]["summary"])
        # ...but the missing-space-after-colon artifact is a real defect
        # normalize_whitespace() correctly does NOT try to auto-fix (it only
        # ever removes redundant whitespace, never inserts a space into
        # text) -- so it must still be caught and flagged for review.
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any("punctuation" in issue.lower() for issue in result["validation"]["issues"]))

    def test_clean_resume_with_no_gap_language_is_completed(self):
        provider = MockProvider(response=VALID_RESUME_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = rt.tailor_resume(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["validation"]["issues"], [])


class CoverLetterQualityTestCase(unittest.TestCase):
    """Phase 6.1: cover letters may acknowledge a material gap at most once
    -- never repeatedly, never as the letter's primary subject."""

    def test_excessive_gap_language_is_needs_review(self):
        provider = MockProvider(response=EXCESSIVE_GAP_LANGUAGE_COVER_LETTER_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = cl.generate_cover_letter(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any("gap" in issue.lower() for issue in result["validation"]["issues"]))

    def test_single_gap_mention_is_allowed(self):
        provider = MockProvider(response=SINGLE_GAP_MENTION_COVER_LETTER_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = cl.generate_cover_letter(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "COMPLETED")

    def test_clean_cover_letter_is_completed_with_no_issues(self):
        provider = MockProvider(response=COVER_LETTER_NEEDED_JSON)
        analysis_record = {"status": "COMPLETED", "analysis": json.loads(VALID_ANALYSIS_JSON)}
        result = cl.generate_cover_letter(make_job(), analysis_record, CANDIDATE_CTX, provider)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["validation"]["issues"], [])


# --------------------------------------------------------------------------
# 11: Application package job_id correctness
# --------------------------------------------------------------------------

class ApplicationPackageTestCase(unittest.TestCase):
    def test_package_references_correct_job_id(self):
        job = make_job(id="job-42")
        package = ap.assemble_package(job, None, None, None, [])
        self.assertEqual(package["job_id"], "job-42")

    def test_review_decision_always_requires_human_review(self):
        job = make_job(application_decision="REVIEW", decision_reason="Score below auto-apply bar.")
        package = ap.assemble_package(job, None, None, None, [])
        self.assertTrue(package["requires_human_review"])
        self.assertIn("Score below auto-apply bar.", package["review_reasons"])

    def test_auto_apply_not_forced_into_review_by_default(self):
        job = make_job(application_decision="AUTO_APPLY")
        package = ap.assemble_package(job, None, None, None, [])
        self.assertNotIn(job.get("decision_reason", "Phase 5 marked this job REVIEW."), package["review_reasons"])

    def test_incomplete_package_is_not_ready_for_automation(self):
        job = make_job(application_decision="AUTO_APPLY")
        package = ap.assemble_package(job, None, None, None, [])
        self.assertFalse(package["ready_for_browser_automation"])
        self.assertEqual(package["readiness"], "NOT_READY")


# --------------------------------------------------------------------------
# 12-14: Caching / idempotency / invalidation
# --------------------------------------------------------------------------

class JobAnalysisCacheTestCase(unittest.TestCase):
    def test_cache_prevents_unnecessary_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            provider = MockProvider(response=VALID_ANALYSIS_JSON)
            job = make_job()
            ja.analyze_job(job, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            ja.analyze_job(job, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            self.assertEqual(len(provider.calls), 1, "second call with unchanged inputs must be served from cache")

    def test_changed_job_description_invalidates_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_ANALYSIS_JSON])
            job1 = make_job(description="Original description.")
            job2 = make_job(description="Materially different description.")
            ja.analyze_job(job1, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            ja.analyze_job(job2, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            self.assertEqual(len(provider.calls), 2, "a changed description must invalidate the cached analysis")

    def test_changed_candidate_profile_hash_invalidates_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_ANALYSIS_JSON])
            job = make_job()
            ja.analyze_job(job, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            ja.analyze_job(job, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v2", provider, cache_dir=cache_dir)
            self.assertEqual(len(provider.calls), 2, "a changed candidate profile hash must invalidate the cached analysis")

    def test_unrelated_job_cache_is_not_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_ANALYSIS_JSON, VALID_ANALYSIS_JSON])
            job_a, job_b = make_job(id="job-a"), make_job(id="job-b")
            ja.analyze_job(job_a, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            ja.analyze_job(job_b, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            # Re-run job_a unchanged -- must still be a cache hit even though job_b was processed in between.
            ja.analyze_job(job_a, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=cache_dir)
            self.assertEqual(len(provider.calls), 2)


# --------------------------------------------------------------------------
# 15: Failed LLM call handling
# --------------------------------------------------------------------------

class FailureHandlingTestCase(unittest.TestCase):
    def test_failed_llm_call_produces_failed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = MockProvider(fail=True)
            job = make_job()
            record = ja.analyze_job(job, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=Path(tmp))
            self.assertEqual(record["status"], "FAILED")
            self.assertIn("error", record)

    def test_failed_llm_call_does_not_corrupt_job_data(self):
        original_job = make_job()
        job_copy = dict(original_job)
        provider = MockProvider(fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            ja.analyze_job(job_copy, CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=Path(tmp))
        self.assertEqual(job_copy, original_job)

    def test_malformed_json_response_produces_failed_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = MockProvider(response="this is not json")
            record = ja.analyze_job(make_job(), CANDIDATE, CANDIDATE_CTX["evidence_blob"], "hash-v1", provider, cache_dir=Path(tmp))
            self.assertEqual(record["status"], "FAILED")


# --------------------------------------------------------------------------
# 16-20: Phase 5 immutability -- Phase 6 must only READ these fields
# --------------------------------------------------------------------------

class Phase5ImmutabilityTestCase(unittest.TestCase):
    def _run_full_pipeline(self, job, tmp_root):
        provider = MockProvider(responses=[
            VALID_ANALYSIS_JSON, VALID_RESUME_JSON, COVER_LETTER_NOT_NEEDED_JSON,
            *([ANSWER_JSON] * 6),  # 6 of the 10 standard questions require an LLM call
        ])
        return r6.process_job(
            job, CANDIDATE_CTX, provider,
            cache_dir=tmp_root / "cache", pending_dir=tmp_root / "pending",
            resumes_dir=tmp_root / "resumes", cover_letters_dir=tmp_root / "cover_letters",
        )

    def test_phase5_fields_unchanged_after_processing(self):
        job = make_job(application_decision="AUTO_APPLY", score=91, role_family="BUSINESS_ANALYSIS", career_level="JUNIOR")
        snapshot = dict(job)
        with tempfile.TemporaryDirectory() as tmp:
            self._run_full_pipeline(job, Path(tmp))
        # Items 16-20: decision, score, role_family, career_level all untouched.
        self.assertEqual(job["application_decision"], snapshot["application_decision"])
        self.assertEqual(job["score"], snapshot["score"])
        self.assertEqual(job["role_family"], snapshot["role_family"])
        self.assertEqual(job["career_level"], snapshot["career_level"])
        self.assertEqual(job, snapshot, "process_job must not mutate the job dict at all")

    def test_package_result_reports_the_original_phase5_decision(self):
        job = make_job(application_decision="REVIEW")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_full_pipeline(job, Path(tmp))
        self.assertEqual(result["package"]["phase5_decision"], "REVIEW")


class FullPipelineIdempotencyTestCase(unittest.TestCase):
    """Section 21: re-running Phase 6 on an unchanged job must not regenerate
    the analysis OR the resume/cover-letter/answers -- every artifact is
    reused from applications/pending/<job_id>/, not just the analysis cache."""

    def test_second_run_on_unchanged_job_makes_zero_llm_calls(self):
        job = make_job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            first_provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_RESUME_JSON, COVER_LETTER_NOT_NEEDED_JSON, *([ANSWER_JSON] * 6)])
            r6.process_job(job, CANDIDATE_CTX, first_provider, cache_dir=tmp_root / "cache",
                            pending_dir=tmp_root / "pending", resumes_dir=tmp_root / "resumes", cover_letters_dir=tmp_root / "cover_letters")

            second_provider = MockProvider(fail=True)  # must never be called
            result = r6.process_job(job, CANDIDATE_CTX, second_provider, cache_dir=tmp_root / "cache",
                                     pending_dir=tmp_root / "pending", resumes_dir=tmp_root / "resumes", cover_letters_dir=tmp_root / "cover_letters")
            self.assertEqual(len(second_provider.calls), 0)
            self.assertEqual(result["resume"]["status"], "COMPLETED")

    def test_force_bypasses_all_reuse(self):
        job = make_job()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            first_provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_RESUME_JSON, COVER_LETTER_NOT_NEEDED_JSON, *([ANSWER_JSON] * 6)])
            r6.process_job(job, CANDIDATE_CTX, first_provider, cache_dir=tmp_root / "cache",
                            pending_dir=tmp_root / "pending", resumes_dir=tmp_root / "resumes", cover_letters_dir=tmp_root / "cover_letters")

            second_provider = MockProvider(responses=[VALID_ANALYSIS_JSON, VALID_RESUME_JSON, COVER_LETTER_NOT_NEEDED_JSON, *([ANSWER_JSON] * 6)])
            r6.process_job(job, CANDIDATE_CTX, second_provider, force=True, cache_dir=tmp_root / "cache",
                            pending_dir=tmp_root / "pending", resumes_dir=tmp_root / "resumes", cover_letters_dir=tmp_root / "cover_letters")
            self.assertEqual(len(second_provider.calls), 9)


if __name__ == "__main__":
    unittest.main()
