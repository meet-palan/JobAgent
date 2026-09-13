"""Versioned Phase 6 prompts, kept centrally rather than scattered inline in
each intelligence module (job_analysis.py, resume_tailoring.py, etc.).

Every prompt version constant below is part of the cache key job_analysis.py
computes -- bump the version whenever you change a prompt's wording or its
expected output schema, so previously cached analysis/content generated
under the old prompt is never silently reused as if it matched the new one.
"""

from __future__ import annotations

import json
from typing import Any

JOB_ANALYSIS_PROMPT_VERSION = "job_analysis_v1"
RESUME_TAILORING_PROMPT_VERSION = "resume_tailoring_v1"
COVER_LETTER_PROMPT_VERSION = "cover_letter_v1"
APPLICATION_ANSWERS_PROMPT_VERSION = "application_answers_v1"

# Shared across every Phase 6 prompt: the one non-negotiable instruction.
TRUTHFULNESS_SYSTEM_PROMPT = (
    "You are a careful, truthful job-application assistant. You may only use "
    "the candidate facts given to you below -- you must never invent, guess, "
    "or embellish an employer, job title, date, years of experience, "
    "technology, certification, project, or quantified achievement. If the "
    "candidate's real experience is a personal project, an internship, "
    "coursework, or a certification, describe it as exactly that -- never "
    "reword it to sound like paid professional experience. If information "
    "needed to answer something is not present in the candidate facts, say "
    "so explicitly rather than filling the gap. Respond with ONLY the "
    "requested JSON object -- no prose before or after it."
)


def _candidate_facts_block(candidate: dict[str, Any], evidence_blob: str) -> str:
    return (
        f"CANDIDATE FACTS (the ONLY true source of candidate information):\n"
        f"- Total professional experience: {candidate.get('total_experience_years')} years\n"
        f"- Target titles: {', '.join(candidate.get('target_titles', []))}\n"
        f"- Skills: {', '.join(candidate.get('skills', []))}\n"
        f"- Capabilities (strength: professional = paid work, practical = project/course/tool, "
        f"none = no evidence):\n"
        + "\n".join(
            f"  - {key} ({cap.get('strength')}): " + "; ".join(cap.get("evidence", []))
            for key, cap in candidate.get("capabilities", {}).items()
            if cap.get("strength") != "none"
        )
        + f"\n- Work authorization: {candidate.get('work_authorization')}\n"
        f"- Earliest start date: {candidate.get('earliest_start_date')}\n"
    )


def build_job_analysis_prompt(job: dict[str, Any], candidate: dict[str, Any], evidence_blob: str) -> str:
    job_block = (
        f"JOB POSTING:\n"
        f"- Title: {job.get('title')}\n"
        f"- Company: {job.get('company')}\n"
        f"- Location: {job.get('location')} ({job.get('work_mode')})\n"
        f"- Required skills (as listed by the employer): {', '.join(job.get('required_skills') or [])}\n"
        f"- Preferred skills: {', '.join(job.get('preferred_skills') or [])}\n"
        f"- Stated experience: {job.get('min_experience_years')}-{job.get('max_experience_years')} years\n"
        f"- Description:\n{(job.get('description') or '')[:4000]}\n"
    )
    schema = {
        "target_role": "string", "company": "string", "core_mission": "one sentence",
        "must_have_requirements": ["string"], "nice_to_have_requirements": ["string"],
        "core_responsibilities": ["string"],
        "technical_skills": ["string"], "product_skills": ["string"], "business_skills": ["string"],
        "project_skills": ["string"], "communication_skills": ["string"],
        "domain_requirements": ["string"], "seniority_signals": ["string"], "success_signals": ["string"],
        "candidate_matches": ["string -- must cite which real candidate fact supports each match"],
        "candidate_gaps": ["string"],
        "evidence_to_highlight": ["string -- must be a real candidate fact, quoted or closely paraphrased"],
        "unsupported_claims_to_avoid": ["string -- things that would be tempting but untrue to claim"],
        "resume_keywords": ["string"], "cover_letter_angles": ["string"],
        "application_strategy": "string", "confidence": "number 0.0-1.0",
    }
    return (
        f"{job_block}\n{_candidate_facts_block(candidate, evidence_blob)}\n"
        f"Analyze this job against this candidate. Return ONLY a JSON object matching exactly this shape "
        f"(fill every field; use an empty list/string if genuinely nothing applies):\n{json.dumps(schema, indent=2)}"
    )


def build_resume_tailoring_prompt(job: dict[str, Any], analysis: dict[str, Any], candidate: dict[str, Any], evidence_blob: str) -> str:
    schema = {
        "summary": "2-3 sentence professional summary, truthful, tailored to this job",
        "highlighted_experience": ["string -- a real professional-experience bullet, reworded for relevance"],
        "highlighted_projects": ["string -- a real project/internship bullet, clearly labeled as such"],
        "skills_to_feature": ["string -- must be a subset of the candidate's real skills"],
        "keywords_used": ["string -- resume_keywords from the analysis actually incorporated above"],
    }
    return (
        f"{_candidate_facts_block(candidate, evidence_blob)}\n"
        f"JOB ANALYSIS:\n{json.dumps(analysis, indent=2)[:3000]}\n\n"
        f"Produce tailored resume CONTENT (not a full document -- education, employment dates, and contact "
        f"info are rendered separately, verbatim, from the candidate profile and must not be repeated or "
        f"altered here). Emphasize what's genuinely relevant to this job; do not invent metrics or "
        f"responsibilities.\n\n"
        f"CRITICAL: a resume presents strengths -- it never narrates the candidate's own weaknesses. Even "
        f"though the job analysis above may list candidate_gaps, DO NOT mention, hint at, or acknowledge in "
        f"any way that the candidate has fewer years of experience than required, is missing a stated "
        f"requirement, or that this is a 'stretch' application. Never write phrases like 'below the required "
        f"X years', 'I only have', 'does not meet', or similar self-disqualifying language. Simply present "
        f"the strongest truthful, relevant experience the candidate actually has, worded naturally with "
        f"normal spacing between every word -- and say nothing about what they lack.\n\n"
        f"Return ONLY a JSON object matching exactly this shape:\n{json.dumps(schema, indent=2)}"
    )


def build_cover_letter_prompt(job: dict[str, Any], analysis: dict[str, Any], candidate: dict[str, Any], evidence_blob: str) -> str:
    schema = {
        "cover_letter_needed": "boolean",
        "reason": "string -- why a cover letter helps or doesn't for this posting",
        "body": "string -- the full cover letter text if needed, else empty string",
    }
    return (
        f"{_candidate_facts_block(candidate, evidence_blob)}\n"
        f"JOB ANALYSIS:\n{json.dumps(analysis, indent=2)[:3000]}\n\n"
        f"Decide whether a cover letter would genuinely add value for this posting (many ATS postings do not "
        f"need one). If yes, write one that explains: (1) why this role is relevant, (2) which REAL candidate "
        f"experience connects to it, (3) what value the candidate brings, (4) why the company/role is "
        f"interesting -- only using facts actually present in the job posting, never invented company facts, "
        f"products, or metrics.\n\n"
        f"Tone: confident and natural, like a real person wrote it -- not defensive, not apologetic. Focus on "
        f"transferable evidence and genuine relevance, not on cataloguing what the candidate lacks. If a "
        f"material experience gap exists, you may acknowledge it AT MOST ONCE, briefly, only where genuinely "
        f"strategic -- the letter must never be primarily about why the candidate is underqualified, and must "
        f"never repeat the gap or apologize for it more than that single time. Keep it reasonably concise "
        f"(roughly 3-4 short paragraphs) with normal spacing between every word.\n\n"
        f"Return ONLY a JSON object matching exactly this shape:\n{json.dumps(schema, indent=2)}"
    )


def build_application_answer_prompt(question: str, job: dict[str, Any], analysis: dict[str, Any], candidate: dict[str, Any], evidence_blob: str) -> str:
    schema = {
        "answer": "string, or exactly 'NEEDS_USER_INPUT' if the information isn't in the candidate facts",
        "confidence": "one of HIGH, MEDIUM, LOW, NEEDS_USER_INPUT",
        "reason": "string -- why this confidence level, or what information is missing",
    }
    return (
        f"{_candidate_facts_block(candidate, evidence_blob)}\n"
        f"JOB ANALYSIS:\n{json.dumps(analysis, indent=2)[:2000]}\n\n"
        f"APPLICATION QUESTION: {question!r}\n\n"
        f"Answer using ONLY verified candidate facts and the job analysis above. If the question asks for "
        f"information not present in the candidate facts (a specific number, a personal preference, a date "
        f"not stated), return 'NEEDS_USER_INPUT' as the answer rather than guessing. Return ONLY a JSON "
        f"object matching exactly this shape:\n{json.dumps(schema, indent=2)}"
    )
