"""Cover letter generation -- only when it genuinely adds value. The LLM
itself decides cover_letter_needed as part of its structured response (see
prompts.build_cover_letter_prompt); this module never forces a letter to be
written for a posting that doesn't call for one, and never accepts invented
company facts, products, or metrics -- the prompt is explicitly restricted
to what the job posting itself states.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_validation import validate_content
from llm_provider import LLMProvider, LLMProviderError, parse_json_response
from prompts import COVER_LETTER_PROMPT_VERSION, TRUTHFULNESS_SYSTEM_PROMPT, build_cover_letter_prompt
from text_quality import find_whitespace_issues, normalize_whitespace

REQUIRED_COVER_LETTER_FIELDS = ["cover_letter_needed", "reason", "body"]


def generate_cover_letter(
    job: dict[str, Any], analysis_record: dict[str, Any], candidate_ctx: dict[str, Any], provider: LLMProvider,
) -> dict[str, Any]:
    """Returns {job_id, status, cover_letter_needed, body, validation, generated_at}.
    status is COMPLETED (needed or not -- both are a valid, complete result),
    NEEDS_REVIEW (body failed a claim-validation check), or FAILED."""
    job_id = job["id"]
    if analysis_record.get("status") not in ("COMPLETED", "NEEDS_REVIEW"):
        return {
            "job_id": job_id, "status": "FAILED",
            "error": f"Cannot draft a cover letter without a completed job analysis (status: {analysis_record.get('status')}).",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    candidate, evidence_blob = candidate_ctx["candidate"], candidate_ctx["evidence_blob"]
    prompt = build_cover_letter_prompt(job, analysis_record.get("analysis", {}), candidate, evidence_blob)
    try:
        raw = provider.complete(system=TRUTHFULNESS_SYSTEM_PROMPT, prompt=prompt, max_tokens=2500)
        result = parse_json_response(raw)
    except (LLMProviderError, json.JSONDecodeError) as e:
        return {"job_id": job_id, "status": "FAILED", "error": str(e), "generated_at": datetime.now(timezone.utc).isoformat()}

    missing = [f for f in REQUIRED_COVER_LETTER_FIELDS if f not in result]
    if missing:
        return {
            "job_id": job_id, "status": "FAILED", "error": f"Cover letter response missing fields: {missing}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    needed = bool(result["cover_letter_needed"])
    body = normalize_whitespace(result.get("body", "")) if needed else ""
    if needed:
        validation = validate_content(
            body, candidate_years=candidate.get("total_experience_years", 0),
            verified_employers=candidate_ctx["verified_employers"], evidence_blob=evidence_blob,
            target_company=job.get("company"),
            # Unlike the resume (0 mentions), a cover letter may acknowledge
            # a material experience gap ONCE if strategically useful -- but
            # must never make the letter primarily about why the candidate
            # is underqualified. See claim_validation.check_experience_gap_language().
            max_gap_mentions=1,
        )
        whitespace_issues = find_whitespace_issues(body)
        if whitespace_issues:
            validation = {"status": "NEEDS_REVIEW", "issues": validation["issues"] + whitespace_issues}
    else:
        validation = {"status": "OK", "issues": []}

    return {
        "job_id": job_id,
        "status": "NEEDS_REVIEW" if validation["status"] == "NEEDS_REVIEW" else "COMPLETED",
        "prompt_version": COVER_LETTER_PROMPT_VERSION,
        "cover_letter_needed": needed,
        "reason": result.get("reason", ""),
        "body": body,
        "validation": validation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_cover_letter_file(job_id: str, body: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_id}-cover-letter.txt"
    path.write_text(body, encoding="utf-8")
    return path
