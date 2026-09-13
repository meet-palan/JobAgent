"""Structured job analysis: the one LLM call whose output every other Phase 6
module (resume tailoring, cover letter, application answers) reuses, instead
of each re-asking "what does this job need?" on its own. See prompts.py's
JOB_ANALYSIS_PROMPT_VERSION for the schema the LLM is asked to fill.

Caching / idempotency: an analysis is cached to disk keyed by a fingerprint
of (job_id, job description+title, candidate profile hash, prompt version).
Re-running analyze_job() on an unchanged job/profile/prompt combination never
calls the LLM again -- it returns the cached result. Changing the job
description, editing the candidate profile, or bumping the prompt version
each invalidate only the affected job's cache entry; unrelated jobs are
untouched, because each job has its own cache file.

A failed LLM call is never cached as if it succeeded: only a COMPLETED
analysis satisfies a cache-key match on the next call, so a FAILED job is
automatically retried rather than getting stuck.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm_provider import LLMProvider, LLMProviderError, parse_json_response
from prompts import JOB_ANALYSIS_PROMPT_VERSION, TRUTHFULNESS_SYSTEM_PROMPT, build_job_analysis_prompt

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "application_intelligence"

# Explicit states -- see CLAUDE.md / docs for why "COMPLETED" is the only
# state a cache hit may return, and why NOT_PROCESSED is distinct from FAILED
# (never processed vs. attempted and failed are different facts to show the
# dashboard/operator).
STATUS_NOT_PROCESSED = "NOT_PROCESSED"
STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_FAILED = "FAILED"

REQUIRED_ANALYSIS_FIELDS = [
    "target_role", "company", "core_mission", "must_have_requirements", "nice_to_have_requirements",
    "core_responsibilities", "technical_skills", "product_skills", "business_skills", "project_skills",
    "communication_skills", "domain_requirements", "seniority_signals", "success_signals",
    "candidate_matches", "candidate_gaps", "evidence_to_highlight", "unsupported_claims_to_avoid",
    "resume_keywords", "cover_letter_angles", "application_strategy", "confidence",
]


def compute_cache_key(job: dict[str, Any], candidate_profile_hash: str, prompt_version: str = JOB_ANALYSIS_PROMPT_VERSION) -> str:
    """Deterministic fingerprint: job id + description/title + candidate
    profile hash + prompt version. Any change to any of these four inputs
    changes the key, which is exactly what should invalidate a cached
    analysis -- see this module's docstring."""
    fingerprint_source = "|".join([
        str(job.get("id", "")),
        job.get("title", "") or "",
        job.get("description", "") or "",
        candidate_profile_hash,
        prompt_version,
    ])
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()


def _cache_path(job_id: str, cache_dir: Path) -> Path:
    safe_id = job_id.replace("/", "_")
    return cache_dir / f"{safe_id}.json"


def load_cached_analysis(job_id: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> dict[str, Any] | None:
    path = _cache_path(job_id, cache_dir)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_cache_record(record: dict[str, Any], cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(record["job_id"], cache_dir)
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def _validate_analysis_shape(analysis: dict[str, Any]) -> list[str]:
    """Deterministic, Python-side structural check -- the LLM's JSON must
    contain every required field. This never judges TRUTHFULNESS (that's
    claim_validation.py's job on the downstream generated content) -- only
    that the analysis is machine-readable and complete enough to build on."""
    return [f for f in REQUIRED_ANALYSIS_FIELDS if f not in analysis]


def analyze_job(
    job: dict[str, Any], candidate: dict[str, Any], evidence_blob: str, candidate_profile_hash: str,
    provider: LLMProvider, *, cache_dir: Path = DEFAULT_CACHE_DIR, force: bool = False,
) -> dict[str, Any]:
    """Return the cache record for this job: {job_id, cache_key, status,
    generated_at, analysis|error}. Calls the LLM only when no COMPLETED
    cache entry matches the current cache key, or when force=True.
    """
    job_id = job["id"]
    cache_key = compute_cache_key(job, candidate_profile_hash)

    if not force:
        cached = load_cached_analysis(job_id, cache_dir)
        if cached and cached.get("cache_key") == cache_key and cached.get("status") == STATUS_COMPLETED:
            return cached

    prompt = build_job_analysis_prompt(job, candidate, evidence_blob)
    try:
        raw = provider.complete(system=TRUTHFULNESS_SYSTEM_PROMPT, prompt=prompt, max_tokens=6000)
        analysis = parse_json_response(raw)
    except LLMProviderError as e:
        record = {
            "job_id": job_id, "cache_key": cache_key, "analysis_version": JOB_ANALYSIS_PROMPT_VERSION,
            "status": STATUS_FAILED, "error": str(e), "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_cache_record(record, cache_dir)
        return record
    except json.JSONDecodeError as e:
        record = {
            "job_id": job_id, "cache_key": cache_key, "analysis_version": JOB_ANALYSIS_PROMPT_VERSION,
            "status": STATUS_FAILED, "error": f"LLM response was not valid JSON: {e}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        save_cache_record(record, cache_dir)
        return record

    missing = _validate_analysis_shape(analysis)
    status = STATUS_NEEDS_REVIEW if missing else STATUS_COMPLETED
    record = {
        "job_id": job_id, "cache_key": cache_key, "analysis_version": JOB_ANALYSIS_PROMPT_VERSION,
        "status": status, "analysis": analysis, "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if missing:
        record["error"] = f"Analysis missing required fields: {missing}"
    save_cache_record(record, cache_dir)
    return record
