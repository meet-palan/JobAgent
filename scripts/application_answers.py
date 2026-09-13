"""Application-question answers, generated from verified candidate facts
only. A question whose answer is a stored, structured profile fact (salary
expectations, work authorization, preferred locations, or a direct
years-of-experience threshold check) is answered deterministically in
Python -- no LLM call needed, per the token-efficiency policy: don't spend a
model call on something a dict lookup already answers exactly and safely.

Only open-ended reasoning questions ("why this role", "describe a project")
go to the LLM, and even then the answer is validated the same way resume/
cover-letter content is (see claim_validation.py) before it's trusted.

Per Section 13's safety rule: only HIGH-confidence answers are meant to ever
become eligible for automated submission in a later phase. Phase 6 itself
never submits anything -- it only labels confidence so that later stage can
decide.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from claim_validation import validate_content
from llm_provider import LLMProvider, LLMProviderError, parse_json_response
from prompts import APPLICATION_ANSWERS_PROMPT_VERSION, TRUTHFULNESS_SYSTEM_PROMPT, build_application_answer_prompt

CONFIDENCE_LEVELS = {"HIGH", "MEDIUM", "LOW", "NEEDS_USER_INPUT"}

STANDARD_QUESTIONS = [
    "Why do you want this role?",
    "Why this company?",
    "Tell us about yourself.",
    "Describe a relevant project.",
    "Describe a challenge you solved.",
    "Why should we hire you?",
    "What are your salary expectations?",
    "What is your current notice period?",
    "Are you willing to relocate?",
    "Are you authorized to work in this location?",
]

_YEARS_THRESHOLD_QUESTION_RE = re.compile(r"(?:do you have|have you|at least)\D{0,15}(\d+(?:\.\d+)?)\+?\s*years?", re.I)


def _deterministic_answer(question: str, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Return a fully-answered {answer, confidence, reason} from a direct
    profile lookup, or None if this question needs LLM reasoning instead."""
    q = question.lower()

    threshold_match = _YEARS_THRESHOLD_QUESTION_RE.search(question)
    if threshold_match:
        required = float(threshold_match.group(1))
        years = candidate.get("total_experience_years", 0)
        has_enough = years >= required
        return {
            "answer": f"{'Yes' if has_enough else 'No'} -- {years:g} year(s) of verified professional experience.",
            "confidence": "HIGH",
            "reason": f"Computed directly from the candidate profile ({years:g} years) against the "
                      f"stated {required:g}-year threshold; not eligible per profile if 'No'.",
        }

    if "notice period" in q:
        return {"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT",
                "reason": "Candidate profile does not contain a verified notice period."}

    if "salary" in q or "compensation" in q:
        lo, hi = candidate.get("target_salary_min_inr_per_year"), candidate.get("target_salary_max_inr_per_year")
        if lo:
            return {"answer": f"INR {lo:,}" + (f" - {hi:,}" if hi else "") + " per year",
                    "confidence": "HIGH", "reason": "Directly from the candidate's stated target salary range."}
        return {"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT", "reason": "No salary preference stored."}

    if "authorized to work" in q or "work authorization" in q or "visa" in q:
        auth = candidate.get("work_authorization")
        if auth:
            return {"answer": auth, "confidence": "HIGH", "reason": "Directly from the candidate's stated work authorization."}
        return {"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT", "reason": "No work authorization stored."}

    if "relocate" in q:
        locations = candidate.get("preferred_locations", [])
        if locations:
            return {"answer": f"Open to roles in: {', '.join(locations)}.", "confidence": "MEDIUM",
                     "reason": "Derived from the candidate's preferred locations; willingness for a specific "
                               "city not asked in the posting is not separately confirmed."}
        return {"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT", "reason": "No location preference stored."}

    if "start date" in q or "when can you" in q or "availability" in q:
        start = candidate.get("earliest_start_date")
        if start:
            return {"answer": start, "confidence": "HIGH", "reason": "Directly from the candidate's stated earliest start date."}
        return {"answer": "NEEDS_USER_INPUT", "confidence": "NEEDS_USER_INPUT", "reason": "No start date stored."}

    return None


def answer_question(
    question: str, job: dict[str, Any], analysis_record: dict[str, Any], candidate_ctx: dict[str, Any],
    provider: LLMProvider,
) -> dict[str, Any]:
    """Returns {question, status, answer, confidence, reason, generated_at}."""
    candidate = candidate_ctx["candidate"]

    deterministic = _deterministic_answer(question, candidate)
    if deterministic is not None:
        return {"question": question, "status": "COMPLETED", "generated_at": datetime.now(timezone.utc).isoformat(), **deterministic}

    if analysis_record.get("status") not in ("COMPLETED", "NEEDS_REVIEW"):
        return {
            "question": question, "status": "FAILED",
            "error": f"Cannot answer without a completed job analysis (status: {analysis_record.get('status')}).",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    evidence_blob = candidate_ctx["evidence_blob"]
    prompt = build_application_answer_prompt(question, job, analysis_record.get("analysis", {}), candidate, evidence_blob)
    try:
        raw = provider.complete(system=TRUTHFULNESS_SYSTEM_PROMPT, prompt=prompt, max_tokens=1500)
        result = parse_json_response(raw)
    except (LLMProviderError, json.JSONDecodeError) as e:
        return {"question": question, "status": "FAILED", "error": str(e), "generated_at": datetime.now(timezone.utc).isoformat()}

    confidence = result.get("confidence")
    answer = result.get("answer", "")
    if confidence not in CONFIDENCE_LEVELS:
        confidence = "LOW"

    if answer == "NEEDS_USER_INPUT":
        confidence = "NEEDS_USER_INPUT"
    elif not answer or not answer.strip():
        # The LLM returned parseable JSON but no usable answer text, and did
        # not use the literal "NEEDS_USER_INPUT" marker either (observed live:
        # answer="", reason="", confidence="LOW" for every open-ended question
        # on one real job). That is a failed generation, not a completed one
        # with nothing to say -- COMPLETED must always carry real content, so
        # this is FAILED, matching how a malformed/unparseable response is
        # already treated above. run_phase6.py's cache-reuse gate already
        # requires every answer to be COMPLETED before reusing the batch, so
        # marking this FAILED (instead of the previous silent empty
        # COMPLETED) means the next run retries it automatically.
        return {
            "question": question, "status": "FAILED",
            "error": f"LLM returned an empty answer (confidence reported as {confidence!r}) "
                     f"instead of usable content or the NEEDS_USER_INPUT marker.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        validation = validate_content(
            answer, candidate_years=candidate.get("total_experience_years", 0),
            verified_employers=candidate_ctx["verified_employers"], evidence_blob=evidence_blob,
            target_company=job.get("company"),
        )
        if validation["status"] == "NEEDS_REVIEW":
            confidence = "LOW"
            result["reason"] = result.get("reason", "") + " [claim validation flagged: " + "; ".join(validation["issues"]) + "]"

    return {
        "question": question, "status": "COMPLETED", "answer": answer, "confidence": confidence,
        "reason": result.get("reason", ""), "prompt_version": APPLICATION_ANSWERS_PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def answer_all(
    questions: list[str], job: dict[str, Any], analysis_record: dict[str, Any], candidate_ctx: dict[str, Any],
    provider: LLMProvider,
) -> list[dict[str, Any]]:
    return [answer_question(q, job, analysis_record, candidate_ctx, provider) for q in questions]
