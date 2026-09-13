"""Resume tailoring as a controlled transformation, not a rewrite-from-scratch.

Employment history, dates, and education are copied VERBATIM from
profile.md (see candidate_context.extract_md_section) -- the LLM never even
sees them as something to edit, it only drafts a summary, a small set of
relevance-highlighting bullets, and a project/skill selection. This is the
structural guarantee (not just a prompt instruction) that tailoring can
never change employment dates or education history, and can never turn a
project into professional experience: the professional-experience section
IS the untouched source text, and anything the LLM contributes about a
project is rendered under its own clearly-labeled "Relevant Projects"
heading, never merged into the professional-experience section.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_validation import validate_content
from llm_provider import LLMProvider, LLMProviderError, parse_json_response
from prompts import RESUME_TAILORING_PROMPT_VERSION, TRUTHFULNESS_SYSTEM_PROMPT, build_resume_tailoring_prompt
from text_quality import find_whitespace_issues, normalize_whitespace

REQUIRED_RESUME_FIELDS = ["summary", "highlighted_experience", "highlighted_projects", "skills_to_feature", "keywords_used"]


def _validate_shape(content: dict[str, Any]) -> list[str]:
    return [f for f in REQUIRED_RESUME_FIELDS if f not in content]


def _clean_content_whitespace(content: dict[str, Any]) -> dict[str, Any]:
    """Apply the safe normalize_whitespace() pass to every LLM-contributed
    string field before it's rendered or persisted -- fixes redundant
    whitespace in the actual content dict, not just the rendered text."""
    content = dict(content)
    content["summary"] = normalize_whitespace(content.get("summary", ""))
    content["highlighted_experience"] = [normalize_whitespace(b) for b in content.get("highlighted_experience", [])]
    content["highlighted_projects"] = [normalize_whitespace(b) for b in content.get("highlighted_projects", [])]
    return content


def render_resume_text(content: dict[str, Any], candidate_ctx: dict[str, Any]) -> str:
    """Deterministically assemble the final resume text: verbatim contact/
    experience/education sections from the candidate's own profile.md,
    interleaved with the LLM's tailored summary/highlights/skills. No date,
    employer, or degree text here ever comes from the LLM."""
    candidate = candidate_ctx["candidate"]
    lines = [
        candidate.get("name", ""),
        candidate_ctx.get("contact_section", ""),
        "",
        "SUMMARY",
        content.get("summary", ""),
        "",
        "PROFESSIONAL EXPERIENCE",
        candidate_ctx.get("experience_section", ""),
        "",
        "HIGHLIGHTS FOR THIS APPLICATION",
        *[f"- {b}" for b in content.get("highlighted_experience", [])],
        "",
        "RELEVANT PROJECTS",
        *[f"- {b}" for b in content.get("highlighted_projects", [])],
        "",
        "SKILLS",
        ", ".join(content.get("skills_to_feature", [])),
        "",
        "EDUCATION",
        candidate_ctx.get("education_section", ""),
    ]
    return "\n".join(str(line) for line in lines)


def tailor_resume(
    job: dict[str, Any], analysis_record: dict[str, Any], candidate_ctx: dict[str, Any], provider: LLMProvider,
) -> dict[str, Any]:
    """Returns {job_id, status, content, rendered_text, validation, generated_at}.

    Requires a COMPLETED analysis_record (from job_analysis.analyze_job) --
    resume tailoring never re-derives job understanding on its own, it reuses
    the one shared analysis. Skills the LLM proposes to feature are further
    restricted to the candidate's actual skills list, on top of the general
    claim_validation pass, so a fabricated skill can never survive both checks.
    """
    job_id = job["id"]
    if analysis_record.get("status") not in ("COMPLETED", "NEEDS_REVIEW"):
        return {
            "job_id": job_id, "status": "FAILED",
            "error": f"Cannot tailor a resume without a completed job analysis (analysis status: {analysis_record.get('status')}).",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    candidate, evidence_blob = candidate_ctx["candidate"], candidate_ctx["evidence_blob"]
    prompt = build_resume_tailoring_prompt(job, analysis_record.get("analysis", {}), candidate, evidence_blob)
    try:
        raw = provider.complete(system=TRUTHFULNESS_SYSTEM_PROMPT, prompt=prompt, max_tokens=3000)
        content = parse_json_response(raw)
    except (LLMProviderError, json.JSONDecodeError) as e:
        return {"job_id": job_id, "status": "FAILED", "error": str(e), "generated_at": datetime.now(timezone.utc).isoformat()}

    missing = _validate_shape(content)
    if missing:
        return {
            "job_id": job_id, "status": "FAILED", "error": f"Resume content missing required fields: {missing}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    content = _clean_content_whitespace(content)
    real_skills = {s.lower() for s in candidate.get("skills", [])}
    fabricated_skills = [s for s in content.get("skills_to_feature", []) if s.lower() not in real_skills]

    rendered_text = render_resume_text(content, candidate_ctx)
    # Validate ONLY the LLM-contributed text (summary + highlight bullets),
    # never the verbatim experience_section/education_section -- those are
    # copied byte-for-byte from profile.md and are truthful by construction,
    # but naturally contain dates/years that won't appear in evidence_blob
    # (built from skills + capability evidence only), which would otherwise
    # trip the numeric-claim check on every resume regardless of content.
    llm_contributed_text = "\n".join([
        content.get("summary", ""),
        *content.get("highlighted_experience", []),
        *content.get("highlighted_projects", []),
    ])
    validation = validate_content(
        llm_contributed_text, candidate_years=candidate.get("total_experience_years", 0),
        verified_employers=candidate_ctx["verified_employers"], evidence_blob=evidence_blob,
        target_company=job.get("company"),
        # The resume must never narrate the candidate's own experience gap --
        # see claim_validation.check_experience_gap_language()'s docstring.
        # Unlike an interview-style answer, a resume has no "question" that
        # invites this disclosure, so zero mentions are allowed here.
        max_gap_mentions=0,
    )
    extra_issues = find_whitespace_issues(llm_contributed_text)
    if extra_issues:
        validation = {"status": "NEEDS_REVIEW", "issues": validation["issues"] + extra_issues}
    if fabricated_skills:
        validation = {
            "status": "NEEDS_REVIEW",
            "issues": validation["issues"] + [f"Skill(s) not in candidate profile: {fabricated_skills}"],
        }

    return {
        "job_id": job_id,
        "status": "NEEDS_REVIEW" if validation["status"] == "NEEDS_REVIEW" else "COMPLETED",
        "prompt_version": RESUME_TAILORING_PROMPT_VERSION,
        "content": content,
        "rendered_text": rendered_text,
        "validation": validation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_resume_file(job_id: str, rendered_text: str, output_dir: Path) -> Path:
    """applications/resumes/<job_id>-tailored-resume.txt -- the base resume
    at profile/resume/ is never touched or overwritten by this function."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{job_id}-tailored-resume.txt"
    path.write_text(rendered_text, encoding="utf-8")
    return path
