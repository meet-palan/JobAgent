"""Phase 6 orchestrator: for each selected job, run job analysis (cached),
resume tailoring, cover-letter drafting, application answers, and assemble
one application_package.json under applications/pending/<job_id>/.

Job selection is intentionally narrow by default -- see select_jobs(): only
AUTO_APPLY and REVIEW jobs are processed automatically, because SKIP jobs
were already excluded by Phase 5 and sending them to an LLM would just be
wasted cost for a job the candidate isn't going to apply to. A specific SKIP
job CAN still be processed by passing its id via --job-id, for debugging or
testing the intelligence layer itself -- that override never changes the
stored Phase 5 decision, it only lets Phase 6 run for inspection.

Usage:
    python scripts/run_phase6.py --max-jobs 3                      # AUTO_APPLY+REVIEW, capped
    python scripts/run_phase6.py --job-id <id>                     # one specific job (any decision)
    python scripts/run_phase6.py --provider mock --mock-response '{...}'   # offline dry run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candidate_context as cc
import job_analysis as ja
import resume_tailoring as rt
import cover_letter as cl
import application_answers as aa
import application_package as ap
from llm_provider import ClaudeProvider, LLMProvider, MockProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS_PATH = REPO_ROOT / "data" / "jobs.json"
DEFAULT_RESUMES_DIR = REPO_ROOT / "applications" / "resumes"
DEFAULT_COVER_LETTERS_DIR = REPO_ROOT / "applications" / "cover_letters"
DEFAULT_PENDING_DIR = REPO_ROOT / "applications" / "pending"

PROCESSABLE_DECISIONS = ("AUTO_APPLY", "REVIEW")


def load_jobs(path: Path = DEFAULT_JOBS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def select_jobs(jobs: list[dict[str, Any]], *, job_id: str | None = None, max_jobs: int | None = None) -> list[dict[str, Any]]:
    """Default production selection: AUTO_APPLY + REVIEW only (see module
    docstring). Passing job_id processes exactly that one job regardless of
    its decision -- the only sanctioned way to run Phase 6 on a SKIP job."""
    if job_id:
        match = [j for j in jobs if j.get("id") == job_id]
        return match
    selected = [j for j in jobs if j.get("application_decision") in PROCESSABLE_DECISIONS]
    return selected[:max_jobs] if max_jobs else selected


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _reusable(record: Any, analysis_cache_key: str) -> bool:
    """An on-disk artifact (resume/cover-letter/answers) is safe to reuse
    only if it was generated against the SAME job-analysis cache key --
    i.e. nothing about the job, the candidate profile, or the prompt version
    has changed since. See job_analysis.py's docstring for what changes
    that key. A prior FAILED artifact is never treated as reusable, so a
    failed run is always retried rather than getting stuck (Section 20)."""
    return bool(record) and record.get("analysis_cache_key") == analysis_cache_key and record.get("status") in ("COMPLETED", "NEEDS_REVIEW")


def process_job(
    job: dict[str, Any], candidate_ctx: dict[str, Any], provider: LLMProvider, *, force: bool = False,
    cache_dir: Path | None = None, pending_dir: Path = DEFAULT_PENDING_DIR,
    resumes_dir: Path = DEFAULT_RESUMES_DIR, cover_letters_dir: Path = DEFAULT_COVER_LETTERS_DIR,
) -> dict[str, Any]:
    """Run the full Phase 6 pipeline for one job. Never touches job's own
    Phase 5 fields (score/application_decision/role_family/career_level) --
    it only reads them. `cache_dir`/`pending_dir`/`resumes_dir`/`cover_letters_dir`
    default to the real project paths but are overridable so tests never
    write into real project data.

    Idempotent beyond just the job analysis (job_analysis.py's own cache):
    resume/cover-letter/answers already written for this job under
    `pending_dir` are reused as-is whenever they were generated against the
    same analysis cache key -- re-running Phase 6 on an unchanged job makes
    zero LLM calls at all, not just one fewer.
    """
    original_decision = job.get("application_decision")
    job_dir_path = pending_dir / job["id"]

    analysis_record = ja.analyze_job(
        job, candidate_ctx["candidate"], candidate_ctx["evidence_blob"], candidate_ctx["profile_hash"],
        provider, force=force, cache_dir=cache_dir or ja.DEFAULT_CACHE_DIR,
    )
    analysis_key = analysis_record.get("cache_key")

    existing_resume = None if force else _load_json(job_dir_path / "resume.json")
    if _reusable(existing_resume, analysis_key):
        resume_record = existing_resume
    else:
        resume_record = rt.tailor_resume(job, analysis_record, candidate_ctx, provider)
        resume_record["analysis_cache_key"] = analysis_key

    existing_cover_letter = None if force else _load_json(job_dir_path / "cover_letter.json")
    if _reusable(existing_cover_letter, analysis_key):
        cover_letter_record = existing_cover_letter
    else:
        cover_letter_record = cl.generate_cover_letter(job, analysis_record, candidate_ctx, provider)
        cover_letter_record["analysis_cache_key"] = analysis_key

    existing_answers = None if force else _load_json(job_dir_path / "answers.json")
    if existing_answers and analysis_key == (existing_answers[0].get("analysis_cache_key") if existing_answers else None) \
            and all(a.get("status") == "COMPLETED" for a in existing_answers):
        answer_records = existing_answers
    else:
        answer_records = aa.answer_all(aa.STANDARD_QUESTIONS, job, analysis_record, candidate_ctx, provider)
        for a in answer_records:
            a["analysis_cache_key"] = analysis_key

    package = ap.assemble_package(job, analysis_record, resume_record, cover_letter_record, answer_records)

    job_dir = ap.write_package(job["id"], package, analysis_record, resume_record, cover_letter_record, answer_records, pending_dir=pending_dir)
    if resume_record.get("status") in ("COMPLETED", "NEEDS_REVIEW") and resume_record.get("rendered_text"):
        rt.write_resume_file(job["id"], resume_record["rendered_text"], resumes_dir)
    if cover_letter_record.get("cover_letter_needed") and cover_letter_record.get("body"):
        cl.write_cover_letter_file(job["id"], cover_letter_record["body"], cover_letters_dir)

    assert job.get("application_decision") == original_decision, "Phase 6 must never alter the Phase 5 decision"

    return {
        "job_id": job["id"], "package": package, "package_dir": str(job_dir),
        "analysis": analysis_record, "resume": resume_record, "cover_letter": cover_letter_record,
        "answers": answer_records,
    }


def build_provider(name: str, mock_response: str | None) -> LLMProvider:
    if name == "claude":
        return ClaudeProvider()
    if name == "mock":
        return MockProvider(response=mock_response)
    raise ValueError(f"Unknown provider: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS_PATH)
    parser.add_argument("--job-id", type=str, default=None, help="Process exactly this job id, regardless of its decision")
    parser.add_argument("--max-jobs", type=int, default=None, help="Cap on jobs processed in this run")
    parser.add_argument("--provider", choices=["claude", "mock"], default="claude")
    parser.add_argument("--mock-response", type=str, default=None, help="Canned JSON response, --provider mock only")
    parser.add_argument("--force", action="store_true", help="Bypass the analysis cache and regenerate")
    args = parser.parse_args(argv)

    jobs = load_jobs(args.jobs)
    selected = select_jobs(jobs, job_id=args.job_id, max_jobs=args.max_jobs)
    if not selected:
        print("No jobs selected (check --job-id, or that AUTO_APPLY/REVIEW jobs exist).")
        return 0

    candidate_ctx = cc.build_candidate_context()
    provider = build_provider(args.provider, args.mock_response)

    for job in selected:
        result = process_job(job, candidate_ctx, provider, force=args.force)
        pkg = result["package"]
        print(f"{job['id']} [{pkg['phase5_decision']}] -> readiness={pkg['readiness']} "
              f"requires_human_review={pkg['requires_human_review']}")
        if pkg["readiness_reasons"]:
            for r in pkg["readiness_reasons"]:
                print(f"    - {r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
