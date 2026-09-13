"""Deterministic, Python-side validation of LLM-generated content (resume
content, cover letters, application answers) against the candidate's
verified evidence. The LLM may draft text; only this module decides whether
that text is safe to treat as truthful -- it never trusts the LLM's own
self-assessment.

This is intentionally a best-effort structural check, not an exhaustive
fact-checker: it catches the concrete, high-value risks Phase 6 must never
let through (an overclaimed years-of-experience figure, a fabricated
employer, an unsupported number), while accepting that some phrasing will
always require a human to actually read. A clean result NEVER means "this is
certainly truthful"; a NEEDS_REVIEW verdict from it always overrides a clean
one from anywhere else, and the caller is expected to prefer under-trusting
generated content over over-trusting it.
"""

from __future__ import annotations

import re
from typing import Any

_YEARS_CLAIM_RE = re.compile(r"(\d+(?:\.\d+)?)\+?\s*years?\s+(?:of\s+)?(?:professional\s+|full-?time\s+)?experience", re.I)
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})\b")
_NUMBER_CLAIM_RE = re.compile(r"\b(\d+(?:\.\d+)?%|\d{2,}\+?)\b")

# Words that legitimately capitalize mid-sentence (titles, common resume
# section words) and would otherwise trip the proper-noun heuristic below.
_GENERIC_CAPITALIZED_TERMS = {
    "business analyst", "project manager", "product manager", "project coordinator",
    "associate product manager", "agile", "scrum", "sql", "excel", "tableau",
    "microsoft word", "google", "linkedin learning", "iiba",
}


def check_years_of_experience_claims(text: str, candidate_years: float) -> list[str]:
    """Flag any stated years-of-experience figure the candidate profile
    doesn't support. A small tolerance (0.5y) allows for rounding phrases
    like "over 1 year" against a 1.0-year candidate."""
    issues = []
    for m in _YEARS_CLAIM_RE.finditer(text):
        claimed = float(m.group(1))
        if claimed > candidate_years + 0.5:
            issues.append(
                f"Text claims {claimed:g}+ years of experience; candidate profile verifies only "
                f"{candidate_years:g} year(s)."
            )
    return issues


def check_unverified_employers(text: str, verified_employers: list[str], target_company: str | None = None) -> list[str]:
    """Flag a capitalized-phrase-shaped mention that looks like an employer
    name (appears near "at"/"for") and isn't one of the candidate's actual
    verified past employers. `target_company` -- the job's OWN company, i.e.
    the employer being applied TO, never one being claimed as past experience
    -- is explicitly exempted: a cover letter naturally says "excited to
    apply to Acme" or "interest in the role at Acme", and that must never be
    confused with a claim that the candidate previously worked at Acme."""
    issues = []
    verified_lower = {e.lower() for e in verified_employers}
    target_lower = (target_company or "").strip().lower()
    for m in re.finditer(r"\b(?:at|for)\s+([A-Z][a-zA-Z&]+(?:\s+[A-Z][a-zA-Z&]+){0,3})\b", text):
        phrase = m.group(1).strip()
        if phrase.lower() in _GENERIC_CAPITALIZED_TERMS or phrase.lower() in verified_lower:
            continue
        if target_lower and (phrase.lower() == target_lower or phrase.lower() in target_lower or target_lower in phrase.lower()):
            continue
        # Also allow the phrase if it's a substring of, or contains, a verified employer
        # (handles "Tibicle" alone matching "Tibicle LLP").
        if any(phrase.lower() in v or v.split()[0] in phrase.lower() for v in verified_lower):
            continue
        issues.append(f"Mentions an employer-like name not in the verified profile: {phrase!r}")
    return issues


def check_unsupported_numeric_claims(text: str, evidence_blob: str) -> list[str]:
    """Flag a quantified figure (percentage or 2+ digit number) in generated
    text that doesn't appear anywhere in the candidate's real evidence --
    e.g. an invented "reduced costs by 30%" the profile never substantiates.
    Numbers already present in the evidence blob (dates, counts the
    candidate's own resume states) are not flagged."""
    issues = []
    for m in _NUMBER_CLAIM_RE.finditer(text):
        value = m.group(1)
        if value not in evidence_blob:
            issues.append(f"Unsupported quantified claim: {value!r} does not appear in the candidate's evidence.")
    return issues



# Phase 6.1: candidate-facing documents (resume, cover letter) must present
# the candidate's strengths, not narrate their own disqualification --
# experience-gap/fit reasoning belongs in the internal job analysis
# (job_analysis.py's candidate_gaps/application_strategy fields), never in
# text the employer actually reads. This is a phrase-level heuristic, not
# semantic understanding: it catches the concrete, observed failure mode
# (the model explicitly telling the employer "I only have X years against
# your Y-year requirement") without policing ordinary honest phrasing
# elsewhere (e.g. application_answers.py deliberately does NOT use this
# check -- a direct interview-style question like "why should we hire you"
# may legitimately warrant an honest, candidate-volunteered caveat).
_GAP_LANGUAGE_PATTERNS = [
    re.compile(r"\bbelow the\b", re.I),
    re.compile(r"\bfewer years\b", re.I),
    re.compile(r"\bunder-?qualified\b", re.I),
    re.compile(r"\bstretch (?:role|application|fit)\b", re.I),
    re.compile(r"\bdoes(?:n't| not) meet\b", re.I),
    re.compile(r"\bfalls? short\b", re.I),
    re.compile(r"\bshort of the\b", re.I),
    re.compile(r"\bexperience gap\b", re.I),
    re.compile(r"\bgap in (?:my |the )?experience\b", re.I),
    re.compile(r"\bnot eligible\b", re.I),
    re.compile(r"\bI only have\b", re.I),
    re.compile(r"\bI want to be transparent\b", re.I),
    re.compile(r"\byears? short\b", re.I),
    re.compile(r"\bdo(?:n't| not) (?:currently )?meet the (?:required|stated|minimum)\b", re.I),
    re.compile(r"\black(?:ing)? (?:the )?(?:required|stated|necessary) (?:experience|years|qualifications?)\b", re.I),
]


def check_experience_gap_language(text: str, max_mentions: int = 0) -> list[str]:
    """Flag explicit self-disqualifying/gap-acknowledging language beyond
    `max_mentions` occurrences. Pass 0 for content that must never mention a
    gap at all (the resume); pass a small number like 1 for content allowed
    a single strategic acknowledgment (the cover letter)."""
    hits = [m.group(0) for pattern in _GAP_LANGUAGE_PATTERNS for m in pattern.finditer(text)]
    if len(hits) > max_mentions:
        return [f"Explicit experience-gap language found ({len(hits)} mention(s), max {max_mentions} allowed): {hits}"]
    return []


def validate_content(
    text: str, *, candidate_years: float, verified_employers: list[str], evidence_blob: str,
    target_company: str | None = None, max_gap_mentions: int | None = None,
) -> dict[str, Any]:
    """Run every deterministic check and return {"status": "OK"|"NEEDS_REVIEW", "issues": [...]}.
    `target_company` is the job's own company -- see check_unverified_employers().
    `max_gap_mentions`: pass 0/1/etc. to also run check_experience_gap_language();
    leave as None (default) to skip that check -- e.g. application_answers.py
    intentionally never passes this, since an honest, directly-asked-for
    caveat in an interview-style answer is not the same failure mode."""
    issues: list[str] = []
    issues += check_years_of_experience_claims(text, candidate_years)
    issues += check_unverified_employers(text, verified_employers, target_company)
    issues += check_unsupported_numeric_claims(text, evidence_blob)
    if max_gap_mentions is not None:
        issues += check_experience_gap_language(text, max_gap_mentions)
    return {"status": "NEEDS_REVIEW" if issues else "OK", "issues": issues}
