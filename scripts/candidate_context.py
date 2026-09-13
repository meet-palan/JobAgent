"""Loads the externalized candidate profile and derives the small set of
verified facts Phase 6 needs -- reused by job_analysis.py, resume_tailoring.py,
cover_letter.py, application_answers.py, and claim_validation.py so none of
them re-implement profile loading or duplicate a "what's actually true about
this candidate" definition.

Everything here reads from profile/*.md and data/candidate_profile.json --
the same externalized source Phase 5 already uses. Nothing about Meet
specifically is hardcoded in this module; a future multi-candidate version
only needs to change what `load_candidate_profile()` points at.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANDIDATE_PROFILE_PATH = REPO_ROOT / "data" / "candidate_profile.json"
DEFAULT_PROFILE_MD_PATH = REPO_ROOT / "profile" / "profile.md"
DEFAULT_SKILLS_MD_PATH = REPO_ROOT / "profile" / "skills.md"


def load_candidate_profile(path: Path = DEFAULT_CANDIDATE_PROFILE_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def candidate_profile_hash(candidate: dict[str, Any]) -> str:
    """Stable fingerprint of the candidate profile's content, used as part of
    the job-analysis cache key (candidate_context.py's docstring / job_analysis.py)
    so a profile edit invalidates every candidate-dependent cached artifact."""
    canonical = json.dumps(candidate, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_evidence_blob(candidate: dict[str, Any]) -> str:
    """The full text of everything the candidate can truthfully point to:
    every skill plus every capability's evidence quotes, lowercased. This is
    the same construction score_job.py's build_candidate_evidence_blob() uses
    for evidence-phrase scoring -- reused here (not reimplemented) as the
    ground truth claim_validation.py checks generated content against."""
    parts = list(candidate.get("skills", []))
    for cap in candidate.get("capabilities", {}).values():
        parts.extend(cap.get("evidence", []))
    return " ".join(parts).lower()


_EMPLOYER_LINE_RE = re.compile(
    r"^\s*-?\s*(?:Current / most recent role|Previous roles?)\s*:\s*(.+)$", re.MULTILINE
)
_ROLE_LINE_RE = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)


def extract_verified_employers(profile_md_path: Path = DEFAULT_PROFILE_MD_PATH) -> list[str]:
    """Parse profile.md's "Experience overview" section for real employer
    names, so claim_validation.py can flag a generated employer name that
    was never actually verified. Best-effort text parsing (profile.md is
    human-edited prose, not a strict schema) -- a parsing miss only makes
    validation slightly less strict, it never invents an employer."""
    if not profile_md_path.exists():
        return []
    text = profile_md_path.read_text(encoding="utf-8")
    employers: list[str] = []

    def employer_from_role_line(line: str) -> str | None:
        # "Business Analyst and Project Coordinator, Tibicle LLP — Ahmedabad, ..."
        before_dash = re.split(r"\s+—\s+|\s+-\s+(?=[A-Z])", line)[0]
        segments = [s.strip() for s in before_dash.split(",")]
        return segments[-1] if len(segments) > 1 else None

    for m in _EMPLOYER_LINE_RE.finditer(text):
        rest = m.group(1).strip()
        if rest and not rest.endswith(":"):
            name = employer_from_role_line(rest)
            if name:
                employers.append(name)
        block_start = m.end()
        next_heading = text.find("\n## ", block_start)
        block = text[block_start:next_heading if next_heading != -1 else block_start + 500]
        for role_line in _ROLE_LINE_RE.finditer(block):
            name = employer_from_role_line(role_line.group(1))
            if name:
                employers.append(name)

    seen, unique = set(), []
    for name in employers:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return unique


def extract_md_section(markdown_text: str, heading: str) -> str:
    """Return the raw text under a "## {heading}" line, up to the next "## "
    heading or end of file -- verbatim, no reformatting. Used to copy
    employment history/education/contact info into a rendered resume exactly
    as the candidate wrote them, so Phase 6 never risks altering a real date
    or degree title while tailoring content around them."""
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.MULTILINE)
    m = pattern.search(markdown_text)
    if not m:
        return ""
    start = m.end()
    next_heading = markdown_text.find("\n## ", start)
    return markdown_text[start:next_heading if next_heading != -1 else len(markdown_text)].strip()


def build_candidate_context(
    candidate_path: Path = DEFAULT_CANDIDATE_PROFILE_PATH,
    profile_md_path: Path = DEFAULT_PROFILE_MD_PATH,
) -> dict[str, Any]:
    """One bundle every Phase 6 module can depend on: the raw profile dict,
    its hash (for cache keys), the evidence blob (for prompts + validation),
    and the verified employer list (for claim validation)."""
    candidate = load_candidate_profile(candidate_path)
    profile_md_text = profile_md_path.read_text(encoding="utf-8") if profile_md_path.exists() else ""
    return {
        "candidate": candidate,
        "profile_hash": candidate_profile_hash(candidate),
        "evidence_blob": build_evidence_blob(candidate),
        "verified_employers": extract_verified_employers(profile_md_path),
        "profile_md_text": profile_md_text,
        "experience_section": extract_md_section(profile_md_text, "Experience overview"),
        "education_section": extract_md_section(profile_md_text, "Education"),
        "contact_section": extract_md_section(profile_md_text, "Contact"),
    }
