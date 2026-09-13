"""Score a job posting against the candidate's actual, evidence-backed profile,
then separately decide whether it's suitable to auto-apply to.

This is a PERSONAL matching engine (not a general-purpose one). Two problems
from the previous version drove this redesign:

  1. Transferable-but-generic capabilities (requirement gathering, client POC,
     BRD/SOW, stakeholder management) could carry an entirely unrelated role
     (e.g. "Product Counsel") to a very high score, because nothing gated
     capability/evidence credit on whether the JOB'S ROLE FAMILY actually
     needed those things.
  2. A high overall score alone made a job "auto-apply eligible" even when
     the role was clearly senior (title said "Senior"/"Lead"/etc.) -- score
     and apply-worthiness are different questions and must be computed
     independently.

Pipeline, in order:
  A. Classify the job's ROLE FAMILY (PRODUCT, BUSINESS_ANALYSIS,
     PROJECT_PROGRAM, OTHER_TECH, SALES, LEGAL, MARKETING, HR, UNKNOWN).
     The candidate's target role families are derived from their own
     target/secondary titles (data/candidate_profile.json) -- not hardcoded.
  B. Classify the job's CAREER LEVEL (INTERN..EXECUTIVE) from title wording
     first (seniority keywords), years second, never years alone.
  C. Score nine components (weights sum to 100):
        role_family_score        20
        capability_score         20  -- dampened hard if role family mismatches
        evidence_score           15  -- dampened hard if role family mismatches
        title_alignment_score    10
        experience_fit_score     10
        career_level_fit         10
        location_fit              8
        industry_fit               4
        salary_fit                 3
  D. Independently compute `application_decision` in {AUTO_APPLY, REVIEW, SKIP}
     using the rules in `decide_application()` -- a high score can still be
     SKIP (unrelated family, clearly senior) or REVIEW (good capability match
     but an imperfect but-not-huge experience gap).

Evidence rule (unchanged from before): "practical" capability strength
(projects, coursework, tool use, certifications) is real signal, but is never
scored as if it were "professional" (paid employment). See
data/candidate_profile.json's `capabilities` block for what backs each area.

Job schema (fields not present are treated as unknown/unspecified):
{
  "id": "optional string", "title": "string", "company": "string",
  "industry": "string", "company_size": "startup | mid-size | large enterprise | MNC",
  "location": "string", "work_mode": "remote | hybrid | onsite",
  "required_skills": ["..."], "nice_to_have_skills": ["..."], "description": "string",
  "min_experience_years": number | null, "max_experience_years": number | null,
  "salary_min": number | null, "salary_max": number | null, "application_url": "string"
}

Usage:
    python scripts/score_job.py --job path/to/job.json
    python scripts/score_job.py --job-id JOB123 [--jobs data/jobs.json]
    python scripts/score_job.py --job path/to/job.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = REPO_ROOT / "data" / "candidate_profile.json"
DEFAULT_JOBS_PATH = REPO_ROOT / "data" / "jobs.json"

# --------------------------------------------------------------------------
# Weights (sum to 100)
# --------------------------------------------------------------------------

WEIGHT_ROLE_FAMILY = 20
WEIGHT_CAPABILITY = 20
WEIGHT_EVIDENCE = 15
WEIGHT_TITLE = 10
WEIGHT_EXPERIENCE = 10
WEIGHT_CAREER_LEVEL = 10
WEIGHT_LOCATION = 8
WEIGHT_INDUSTRY = 4
WEIGHT_SALARY = 3

EVIDENCE_REQUIRED_WEIGHT = 12
EVIDENCE_NICE_TO_HAVE_WEIGHT = 3
WORK_MODE_WEIGHT = 4
LOCATION_SUBWEIGHT = 4
INDUSTRY_SUBWEIGHT = 3
COMPANY_SIZE_SUBWEIGHT = 1

SECONDARY_TITLE_FRACTION = 0.7
RELATED_FAMILY_TITLE_FRACTION = 0.5

CAPABILITY_STRENGTH_SCORE = {"professional": 1.0, "practical": 0.65, "none": 0.0}

# A job whose role family isn't one the candidate targets gets capability and
# evidence credit multiplied by this -- generic transferable skills can still
# nudge the score, but can never make an unrelated role look like a strong match.
FAMILY_MISMATCH_DAMPENING = 0.2
FAMILY_UNKNOWN_DAMPENING = 0.6

AUTO_APPLY_MIN_SCORE = 85
REVIEW_MIN_SCORE = 75
AUTO_APPLY_MAX_EXPERIENCE_GAP = 1.0
REVIEW_MAX_EXPERIENCE_GAP = 2.0

ENTRY_CAREER_LEVELS = {"ENTRY_LEVEL", "JUNIOR"}
SENIOR_PLUS_CAREER_LEVELS = {"SENIOR", "LEAD", "MANAGER", "DIRECTOR", "EXECUTIVE"}


# --------------------------------------------------------------------------
# Text / word helpers
# --------------------------------------------------------------------------

def normalize(text: str | None) -> str:
    return (text or "").strip().lower()


def titles_match(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def word_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


_WORD_VARIANTS = {
    "manage": {"manager", "managers", "management", "managing"},
    "analy": {"analyst", "analysts", "analysis", "analytics"},
    "coordinat": {"coordinator", "coordinators", "coordination", "coordinating"},
    "associat": {"associate", "associates", "association"},
    "own": {"owner", "owners", "ownership"},
}
_VARIANT_LOOKUP = {variant: canon for canon, variants in _WORD_VARIANTS.items() for variant in variants}


def normalize_word(word: str) -> str:
    return _VARIANT_LOOKUP.get(word, word)


def title_words(title: str) -> set[str]:
    return {normalize_word(w) for w in word_tokens(title)}


_STOPWORDS = {
    "the", "and", "of", "with", "for", "in", "to", "is", "are", "on", "as", "an", "or",
    "at", "by", "be", "this", "that", "will", "into", "from", "using", "your", "you",
    "our", "other", "such", "than", "then", "have", "has", "not", "but", "all", "can",
}


def significant_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOPWORDS]


def contains_word(text: str, phrase: str) -> bool:
    """Word-boundary substring check -- plain `phrase in text` would let
    "intern" match inside "internal", or "sales" match inside "salesforce"."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


# --------------------------------------------------------------------------
# A. Role family classification
# --------------------------------------------------------------------------

ROLE_FAMILIES = [
    "PRODUCT", "BUSINESS_ANALYSIS", "PROJECT_PROGRAM", "OTHER_TECH",
    "SALES", "LEGAL", "MARKETING", "HR", "UNKNOWN",
]

# Checked FIRST, as plain substrings of "title description" -- these are
# unambiguous enough that they must win even when a generic word like
# "Manager" also appears (e.g. "Product Counsel Associate Manager" is LEGAL,
# not PRODUCT, despite containing "product" and "manager").
_LEGAL_PHRASES = ("counsel", "attorney", "paralegal")
_HR_PHRASES = ("recruiter", "recruitment", "hr business partner", "human resources", "talent acquisition")
_MARKETING_PHRASES = ("marketing",)
_SALES_PHRASES = ("sales", "account executive", "business development")
_OTHER_TECH_PHRASES = (
    "software engineer", "data scientist", "devops", "qa engineer", "quality assurance",
    "site reliability", "sre", "backend engineer", "frontend engineer", "full stack",
    "machine learning engineer",
)


def _match_priority_phrases(text: str) -> str | None:
    if any(contains_word(text, p) for p in _LEGAL_PHRASES):
        return "LEGAL"
    if any(contains_word(text, p) for p in _HR_PHRASES):
        return "HR"
    if any(contains_word(text, p) for p in _MARKETING_PHRASES):
        return "MARKETING"
    if any(contains_word(text, p) for p in _SALES_PHRASES):
        return "SALES"
    if any(contains_word(text, p) for p in _OTHER_TECH_PHRASES):
        return "OTHER_TECH"
    return None


def _match_title_word_patterns(title: str) -> str:
    words = title_words(title)
    if "product" in words and ("analy" in words or "manage" in words or "own" in words):
        return "PRODUCT"
    if "business" in words and "analy" in words:
        return "BUSINESS_ANALYSIS"
    if "functional" in words and "analy" in words:
        return "BUSINESS_ANALYSIS"
    if "systems" in words and "analy" in words:
        return "BUSINESS_ANALYSIS"
    if ("project" in words or "program" in words) and ("manage" in words or "coordinat" in words):
        return "PROJECT_PROGRAM"
    return "UNKNOWN"


def classify_role_family(title: str, description: str = "") -> str:
    """Classifies from the TITLE alone whenever it's unambiguous. Description
    text is only consulted as a last resort for a genuinely unclear title --
    otherwise an incidental word in a long description (e.g. a "Product
    Management Analyst" posting that happens to mention "Marketing Operations"
    or "data scientists" among the people it collaborates with) can wrongly
    override a perfectly clear title signal.
    """
    title_text = f" {normalize(title)} "

    family = _match_priority_phrases(title_text)
    if family:
        return family

    family = _match_title_word_patterns(title)
    if family != "UNKNOWN":
        return family

    if description:
        combined = f" {normalize(title)} {normalize(description)[:300]} "
        family = _match_priority_phrases(combined)
        if family:
            return family

    return "UNKNOWN"


def candidate_target_role_families(candidate: dict[str, Any]) -> set[str]:
    """Derived from the candidate's own target/secondary titles -- not hardcoded."""
    titles = [*candidate.get("target_titles", []), *candidate.get("secondary_titles", [])]
    return {classify_role_family(t) for t in titles} - {"UNKNOWN"}


ROLE_FAMILY_CAPABILITY_NEEDS = {
    "PRODUCT": {"primary": ["product_management", "analytics"], "secondary": ["stakeholder_communication", "requirements_gathering", "documentation"]},
    "BUSINESS_ANALYSIS": {"primary": ["business_analysis", "requirements_gathering", "documentation"], "secondary": ["stakeholder_communication", "analytics"]},
    "PROJECT_PROGRAM": {"primary": ["project_management", "stakeholder_communication"], "secondary": ["agile_scrum", "documentation"]},
    "OTHER_TECH": {"primary": ["software_development"], "secondary": ["ai_tools"]},
    "SALES": {"primary": [], "secondary": ["stakeholder_communication"]},
    "LEGAL": {"primary": [], "secondary": []},
    "MARKETING": {"primary": [], "secondary": ["stakeholder_communication"]},
    "HR": {"primary": [], "secondary": []},
    "UNKNOWN": {"primary": ["business_analysis", "project_management"], "secondary": ["stakeholder_communication", "documentation"]},
}


def score_role_family(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    family = classify_role_family(job.get("title", ""), job.get("description", ""))
    target_families = candidate_target_role_families(candidate)

    if family in target_families:
        return {"score": WEIGHT_ROLE_FAMILY, "family": family, "is_mismatch": False,
                "status": f"'{family}' is one of your target role families"}
    if family == "UNKNOWN":
        return {"score": round(WEIGHT_ROLE_FAMILY * 0.35), "family": family, "is_mismatch": False,
                "status": "role family could not be reliably classified from the title"}
    return {"score": 0, "family": family, "is_mismatch": True,
            "status": f"'{family}' is not one of your target role families ({', '.join(sorted(target_families)) or 'none set'})"}


def family_dampening_factor(role_family_result: dict[str, Any]) -> float:
    if role_family_result["is_mismatch"]:
        return FAMILY_MISMATCH_DAMPENING
    if role_family_result["family"] == "UNKNOWN":
        return FAMILY_UNKNOWN_DAMPENING
    return 1.0


# --------------------------------------------------------------------------
# B. Career level classification
# --------------------------------------------------------------------------

CAREER_LEVELS = ["INTERN", "ENTRY_LEVEL", "JUNIOR", "MID_LEVEL", "SENIOR", "LEAD", "MANAGER", "DIRECTOR", "EXECUTIVE", "UNKNOWN"]

_EXEC_MARKERS = ("chief", "ceo", "cto", "coo", "vice president", "vp")
_DIRECTOR_MARKERS = ("director", "head of")
_LEAD_MARKERS = ("lead", "group product", "group manager")
_SENIOR_MARKERS = ("senior", "sr", "principal", "staff")
_INTERN_MARKERS = ("intern", "internship")
_ENTRY_MARKERS = ("entry level", "entry-level", "fresher", "graduate trainee", "trainee")
_JUNIOR_MARKERS = ("associate", "junior", "jr")
# Family-defining phrases where "manager"/"coordinator" names the FUNCTION, not a
# people-management seniority level -- so a bare "manager" elsewhere (e.g.
# "Manager - Accounts") still counts as a MANAGER-level title.
_FAMILY_TITLE_PHRASES = (
    "product manager", "project manager", "program manager", "product owner",
    "product analyst", "product management", "program management", "project management",
    "business analyst", "project coordinator", "program coordinator",
)


def classify_career_level(title: str, min_experience_years: float | None = None) -> str:
    t = f" {normalize(title)} "

    if any(contains_word(t, m) for m in _EXEC_MARKERS):
        return "EXECUTIVE"
    if contains_word(t, "associate director"):
        return "DIRECTOR"
    if any(contains_word(t, m) for m in _DIRECTOR_MARKERS):
        return "DIRECTOR"
    if any(contains_word(t, m) for m in _SENIOR_MARKERS):
        return "SENIOR"
    if any(contains_word(t, m) for m in _LEAD_MARKERS):
        return "LEAD"
    if any(contains_word(t, m) for m in _INTERN_MARKERS):
        return "INTERN"
    if any(contains_word(t, m) for m in _ENTRY_MARKERS):
        return "ENTRY_LEVEL"
    if any(contains_word(t, m) for m in _JUNIOR_MARKERS):
        return "JUNIOR"
    if contains_word(t, "manager") and not any(contains_word(t, p) for p in _FAMILY_TITLE_PHRASES):
        return "MANAGER"

    if min_experience_years is not None:
        if min_experience_years < 1:
            return "ENTRY_LEVEL"
        if min_experience_years <= 2:
            return "JUNIOR"
        if min_experience_years <= 4:
            return "MID_LEVEL"
        if min_experience_years <= 7:
            return "SENIOR"
        return "LEAD"

    return "UNKNOWN"


def score_career_level_fit(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    level = classify_career_level(job.get("title", ""), job.get("min_experience_years"))
    if level in ENTRY_CAREER_LEVELS:
        score, status = WEIGHT_CAREER_LEVEL, "matches your entry-level/junior target"
    elif level == "MID_LEVEL":
        score, status = round(WEIGHT_CAREER_LEVEL * 0.6), "mid-level -- only a fit if capability evidence is unusually strong"
    elif level == "UNKNOWN":
        score, status = round(WEIGHT_CAREER_LEVEL * 0.5), "career level could not be reliably classified"
    else:
        score, status = round(WEIGHT_CAREER_LEVEL * 0.1), f"{level.title()} is above your current target level"
    return {"score": score, "level": level, "status": status}


# --------------------------------------------------------------------------
# Capability relevance (dampened by role-family mismatch)
# --------------------------------------------------------------------------

def score_capability_relevance(job: dict[str, Any], role_family: str, candidate: dict[str, Any]) -> dict[str, Any]:
    needs = ROLE_FAMILY_CAPABILITY_NEEDS.get(role_family, ROLE_FAMILY_CAPABILITY_NEEDS["UNKNOWN"])
    capabilities = candidate.get("capabilities", {})

    def strength_of(key: str) -> float:
        return CAPABILITY_STRENGTH_SCORE.get(capabilities.get(key, {}).get("strength", "none"), 0.0)

    primary, secondary = needs["primary"], needs["secondary"]
    total_weight = len(primary) * 2 + len(secondary)
    weighted_sum = sum(strength_of(k) * 2 for k in primary) + sum(strength_of(k) for k in secondary)
    fraction = weighted_sum / total_weight if total_weight else 0.0
    raw_score = WEIGHT_CAPABILITY * fraction

    matched, practical_only, gaps = [], [], []
    for key in [*primary, *secondary]:
        s = capabilities.get(key, {}).get("strength", "none")
        (matched if s == "professional" else practical_only if s == "practical" else gaps).append(key)

    return {
        "raw_score": raw_score,
        "professional_matches": matched,
        "practical_matches": practical_only,
        "capability_gaps": gaps,
        "primary_capabilities": primary,
        "needed_capabilities": [*primary, *secondary],
    }


# --------------------------------------------------------------------------
# Relevant evidence (dampened by role-family mismatch)
# --------------------------------------------------------------------------

def build_candidate_evidence_blob(candidate: dict[str, Any]) -> str:
    parts = list(candidate.get("skills", []))
    for cap in candidate.get("capabilities", {}).values():
        parts.extend(cap.get("evidence", []))
    return " ".join(parts).lower()


def phrase_coverage(phrase: str, blob: str) -> float:
    words = significant_words(phrase)
    if not words:
        return 1.0
    matched = [w for w in words if w in blob]
    return len(matched) / len(words)


def score_relevant_evidence(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    required = job.get("required_skills") or []
    nice = job.get("nice_to_have_skills") or []
    blob = build_candidate_evidence_blob(candidate)

    if required:
        fractions = [phrase_coverage(p, blob) for p in required]
        required_raw = EVIDENCE_REQUIRED_WEIGHT * (sum(fractions) / len(fractions))
    else:
        required_raw = EVIDENCE_REQUIRED_WEIGHT

    if nice:
        nice_fractions = [phrase_coverage(p, blob) for p in nice]
        nice_raw = EVIDENCE_NICE_TO_HAVE_WEIGHT * (sum(nice_fractions) / len(nice_fractions))
    else:
        nice_raw = EVIDENCE_NICE_TO_HAVE_WEIGHT

    strong = [p for p in required if phrase_coverage(p, blob) >= 0.6]
    weak = [p for p in required if phrase_coverage(p, blob) < 0.3]
    partial = [p for p in required if p not in strong and p not in weak]
    nice_have = [p for p in nice if phrase_coverage(p, blob) >= 0.6]

    return {
        "raw_score": required_raw + nice_raw,
        "strong_matches": strong, "partial_matches": partial, "weak_matches": weak,
        "nice_to_have_matches": nice_have, "required_count": len(required),
    }


# --------------------------------------------------------------------------
# Title alignment (10 pts) -- literal title text, family-independent
# --------------------------------------------------------------------------

def score_title_alignment(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    job_title = job.get("title", "")

    for t in candidate.get("target_titles", []):
        if titles_match(job_title, t):
            return {"score": WEIGHT_TITLE, "detail": f"Title matches target title '{t}'"}
    for t in candidate.get("secondary_titles", []):
        if titles_match(job_title, t):
            return {"score": round(WEIGHT_TITLE * SECONDARY_TITLE_FRACTION), "detail": f"Title matches secondary title '{t}'"}

    family = classify_role_family(job_title)
    if family in candidate_target_role_families(candidate):
        return {"score": round(WEIGHT_TITLE * RELATED_FAMILY_TITLE_FRACTION),
                "detail": f"Title is a '{family.replace('_', ' ').title()}'-family role, not an exact/secondary title match"}

    known_words: set[str] = set()
    for t in [*candidate.get("target_titles", []), *candidate.get("secondary_titles", [])]:
        known_words |= title_words(t)
    jt_words = title_words(job_title)
    overlap = jt_words & known_words
    if overlap and jt_words:
        fraction = len(overlap) / len(jt_words)
        return {"score": round(WEIGHT_TITLE * 0.3 * fraction), "detail": f"Partial title-word overlap: {sorted(overlap)}"}

    return {"score": 0, "detail": f"Title '{job_title}' is not recognized as a related role"}


# --------------------------------------------------------------------------
# Experience fit (10 pts) -- gradient, senior-conservative
# --------------------------------------------------------------------------

def score_experience_fit(job: dict[str, Any], candidate: dict[str, Any], career_level: str) -> dict[str, Any]:
    min_req = job.get("min_experience_years")
    max_req = job.get("max_experience_years")
    years = candidate.get("total_experience_years", 0)
    senior = career_level in SENIOR_PLUS_CAREER_LEVELS

    if min_req is None and max_req is None:
        return {"score": WEIGHT_EXPERIENCE, "status": "not specified", "gap": 0.0,
                "detail": "Job does not state an experience requirement"}

    if min_req is not None and years < min_req:
        gap = round(min_req - years, 2)
        if gap <= 1:
            fraction = 0.85
        elif gap <= 2:
            fraction = 0.6
        elif gap <= 3:
            fraction = 0.4
        elif gap <= 5:
            fraction = 0.2
        else:
            fraction = 0.05
        if senior:
            fraction *= 0.4
        return {
            "score": round(WEIGHT_EXPERIENCE * fraction), "status": "underqualified", "gap": gap,
            "detail": f"Requires {min_req:g}+ years; you have {years:g} (gap: {gap:g} years)"
            + (" -- senior-level role, scored conservatively" if senior else ""),
        }

    if max_req is not None and years > max_req:
        over = years - max_req
        fraction = max(0.7, 1 - over * 0.05)
        return {"score": round(WEIGHT_EXPERIENCE * fraction), "status": "overqualified", "gap": 0.0,
                "detail": f"Requires up to {max_req:g} years; you have {years:g}"}

    return {"score": WEIGHT_EXPERIENCE, "status": "meets requirement", "gap": 0.0,
            "detail": f"Your {years:g} years fits the stated range"}


# --------------------------------------------------------------------------
# Location / work mode (unknown never penalized)
# --------------------------------------------------------------------------

def score_location_work_mode(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    job_work_mode = normalize(job.get("work_mode"))
    job_location = (job.get("location") or "").strip()
    preferred_modes = {normalize(m) for m in candidate.get("preferred_work_modes", [])}
    preferred_locations = candidate.get("preferred_locations", [])

    if not job_work_mode:
        work_mode_score, work_mode_status = WORK_MODE_WEIGHT, "not specified"
    elif not preferred_modes or job_work_mode in preferred_modes:
        work_mode_score, work_mode_status = WORK_MODE_WEIGHT, "matches preference"
    else:
        work_mode_score, work_mode_status = 0, "not in preferred work modes"

    if job_work_mode == "remote":
        location_score, location_status = LOCATION_SUBWEIGHT, "remote (location not applicable)"
    elif not job_location:
        location_score, location_status = LOCATION_SUBWEIGHT, "not specified"
    elif not preferred_locations or any(titles_match(job_location, loc) for loc in preferred_locations):
        location_score, location_status = LOCATION_SUBWEIGHT, "matches preference"
    else:
        location_score, location_status = 0, "outside preferred locations"

    return {
        "score": work_mode_score + location_score,
        "work_mode_status": work_mode_status, "location_status": location_status,
        "detail": f"work_mode={job.get('work_mode')!r}, location={job_location!r}",
    }


# --------------------------------------------------------------------------
# Industry / domain (unknown never penalized)
# --------------------------------------------------------------------------

def score_industry_domain(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    job_industry = (job.get("industry") or "").strip()
    job_size = normalize(job.get("company_size"))
    preferred_industries = candidate.get("preferred_industries", [])
    avoid_industries = candidate.get("avoid_industries", [])
    preferred_sizes = {normalize(s) for s in candidate.get("preferred_company_sizes", [])}

    is_avoided = bool(job_industry) and any(normalize(job_industry) == normalize(a) for a in avoid_industries)
    if is_avoided:
        industry_score, industry_status = 0, "avoided industry"
    elif not job_industry:
        industry_score, industry_status = INDUSTRY_SUBWEIGHT, "not specified"
    elif not preferred_industries or any(titles_match(job_industry, p) for p in preferred_industries):
        industry_score, industry_status = INDUSTRY_SUBWEIGHT, "preferred"
    else:
        industry_score, industry_status = round(INDUSTRY_SUBWEIGHT * 0.5), "not in preferred industries"

    if not job_size:
        size_score, size_status = COMPANY_SIZE_SUBWEIGHT, "not specified"
    elif not preferred_sizes or job_size in preferred_sizes:
        size_score, size_status = COMPANY_SIZE_SUBWEIGHT, "preferred"
    else:
        size_score, size_status = round(COMPANY_SIZE_SUBWEIGHT * 0.5), "not a preferred size"

    return {"score": industry_score + size_score, "industry_status": industry_status,
            "company_size_status": size_status, "is_avoided_industry": is_avoided}


# --------------------------------------------------------------------------
# Salary (unknown never penalized)
# --------------------------------------------------------------------------

def score_salary(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    salary_min = job.get("salary_min")
    salary_max = job.get("salary_max")
    cand_min = candidate.get("min_acceptable_salary_inr_per_year", 0)
    cand_target = candidate.get("target_salary_min_inr_per_year", cand_min)

    if salary_min is None and salary_max is None:
        return {"score": WEIGHT_SALARY, "status": "not specified", "detail": "Job does not list a salary (not held against it)"}

    effective_max = salary_max if salary_max is not None else salary_min

    if effective_max >= cand_target:
        return {"score": WEIGHT_SALARY, "status": "meets or exceeds target", "detail": f"Job offers up to {effective_max}"}
    if effective_max >= cand_min:
        span = max(cand_target - cand_min, 1)
        fraction = (effective_max - cand_min) / span
        return {"score": round(WEIGHT_SALARY * (0.5 + 0.5 * fraction)), "status": "between minimum and target",
                "detail": f"Job offers up to {effective_max}, between your minimum {cand_min} and target {cand_target}"}
    return {"score": 0, "status": "below minimum acceptable",
            "detail": f"Job offers up to {effective_max}, below your minimum acceptable {cand_min}"}


# --------------------------------------------------------------------------
# D. Application decision -- independent of the score's magnitude
# --------------------------------------------------------------------------

def decide_application(
    total_score: int, role_family_result: dict, career_level_result: dict,
    experience_result: dict, critical_missing: bool, appears_legitimate: bool,
) -> dict[str, Any]:
    level = career_level_result["level"]
    gap = experience_result.get("gap", 0) or 0
    is_senior_plus = level in SENIOR_PLUS_CAREER_LEVELS

    # SKIP conditions are hard disqualifiers, checked first, regardless of score.
    if is_senior_plus:
        return {"decision": "SKIP", "reason": f"Career level is {level} -- above your target (entry-level/junior)."}
    if role_family_result["is_mismatch"]:
        return {"decision": "SKIP", "reason": f"Role family mismatch: {role_family_result['status']}."}
    if gap > 2:
        return {"decision": "SKIP", "reason": f"Experience gap of {gap:g} years exceeds the 2-year SKIP threshold."}
    if critical_missing:
        return {"decision": "SKIP", "reason": "A critical requirement (avoided industry, below-minimum salary, or near-total skills mismatch) is missing."}
    if total_score < REVIEW_MIN_SCORE:
        return {"decision": "SKIP", "reason": f"Score {total_score} is below the {REVIEW_MIN_SCORE} review threshold."}

    # AUTO_APPLY: every condition must hold.
    if (
        total_score >= AUTO_APPLY_MIN_SCORE
        and not role_family_result["is_mismatch"]
        and level in ENTRY_CAREER_LEVELS
        and gap <= AUTO_APPLY_MAX_EXPERIENCE_GAP
        and not critical_missing
        and appears_legitimate
    ):
        return {"decision": "AUTO_APPLY", "reason": f"Score {total_score} >= {AUTO_APPLY_MIN_SCORE}, target role family, {level} level, and only a {gap:g}-year experience gap."}

    # Otherwise, anything that survived the SKIP gate is worth a human look.
    reasons = []
    if total_score < AUTO_APPLY_MIN_SCORE:
        reasons.append(f"score {total_score} is below the {AUTO_APPLY_MIN_SCORE} auto-apply bar")
    if level == "MID_LEVEL":
        reasons.append("career level is mid-level")
    if gap > AUTO_APPLY_MAX_EXPERIENCE_GAP:
        reasons.append(f"experience gap is {gap:g} years")
    if not appears_legitimate:
        reasons.append("listing legitimacy could not be fully confirmed")
    return {"decision": "REVIEW", "reason": "Worth a human look: " + "; ".join(reasons) + "."}


# --------------------------------------------------------------------------
# Top-level scoring
# --------------------------------------------------------------------------

def score_job(job: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    role_family_result = score_role_family(job, candidate)
    family = role_family_result["family"]
    dampening = family_dampening_factor(role_family_result)

    capability = score_capability_relevance(job, family, candidate)
    evidence = score_relevant_evidence(job, candidate)
    title = score_title_alignment(job, candidate)
    career_level_result = score_career_level_fit(job, candidate)
    experience = score_experience_fit(job, candidate, career_level_result["level"])
    location = score_location_work_mode(job, candidate)
    industry = score_industry_domain(job, candidate)
    salary = score_salary(job, candidate)

    capability_score = round(capability["raw_score"] * dampening)
    evidence_score = round(evidence["raw_score"] * dampening)

    total = max(0, min(100, round(
        role_family_result["score"] + capability_score + evidence_score + title["score"]
        + experience["score"] + career_level_result["score"] + location["score"]
        + industry["score"] + salary["score"]
    )))

    critical_missing = (
        industry["is_avoided_industry"]
        or salary["status"] == "below minimum acceptable"
        or (evidence["required_count"] > 0 and len(evidence["weak_matches"]) >= max(1, evidence["required_count"] // 2)
            and evidence["raw_score"] < EVIDENCE_REQUIRED_WEIGHT * 0.3)
    )
    application_url = job.get("application_url") or ""
    appears_legitimate = bool(application_url) and application_url.startswith("http") and len(job.get("description") or "") > 50

    decision = decide_application(total, role_family_result, career_level_result, experience, critical_missing, appears_legitimate)

    # --- Explanation assembly ---
    capabilities = candidate.get("capabilities", {})
    needed = set(capability["needed_capabilities"])

    relevant_evidence: list[str] = []
    transferable_evidence: list[str] = []
    for key in capability["professional_matches"]:
        for ev in capabilities.get(key, {}).get("evidence", [])[:1]:
            relevant_evidence.append(ev)
    for key in capability["practical_matches"]:
        for ev in capabilities.get(key, {}).get("evidence", [])[:1]:
            relevant_evidence.append(f"(project/practical, not professional) {ev}")
    # Generic capabilities the candidate has that this family doesn't primarily need.
    for key, cap in capabilities.items():
        if key in needed or cap.get("strength", "none") == "none":
            continue
        if key in ("stakeholder_communication", "documentation", "requirements_gathering", "client_facing"):
            for ev in cap.get("evidence", [])[:1]:
                transferable_evidence.append(ev)

    relevant_evidence.extend(evidence["strong_matches"])

    missing_requirements: list[str] = []
    for key in capability["capability_gaps"]:
        if key in capability["primary_capabilities"]:
            missing_requirements.append(f"No demonstrated experience in '{key.replace('_', ' ')}', which this role family primarily needs")
    if evidence["weak_matches"]:
        missing_requirements.append("Little to no evidence for: " + "; ".join(evidence["weak_matches"]))
    if role_family_result["is_mismatch"]:
        missing_requirements.append(role_family_result["status"])
    if location["work_mode_status"] == "not in preferred work modes":
        missing_requirements.append(f"Work mode {job.get('work_mode')!r} is not in your preferred work modes")
    if location["location_status"] == "outside preferred locations":
        missing_requirements.append(f"Location {job.get('location')!r} is outside your preferred locations")
    if industry["is_avoided_industry"]:
        missing_requirements.append(f"Industry {job.get('industry')!r} is on your avoid list")
    if salary["status"] == "below minimum acceptable":
        missing_requirements.append(salary["detail"])

    experience_gaps: list[str] = []
    if experience["status"] == "underqualified":
        experience_gaps.append(experience["detail"])

    risks: list[str] = []
    if evidence["partial_matches"]:
        risks.append("Only partial evidence for: " + "; ".join(evidence["partial_matches"]))
    if career_level_result["level"] == "MID_LEVEL":
        risks.append("Career level reads as mid-level, above your primary target")
    if role_family_result["family"] == "UNKNOWN":
        risks.append("Could not reliably classify this job's role family from its title")
    if experience["status"] == "overqualified":
        risks.append(experience["detail"])
    if not appears_legitimate:
        risks.append("Listing is missing an application URL or has an unusually thin description")

    return {
        "job_id": job.get("id"),
        "job_title": job.get("title"),
        "overall_match_score": total,
        "application_decision": decision["decision"],
        "decision_reason": decision["reason"],
        "role_family": family,
        "career_level": career_level_result["level"],
        "breakdown": {
            "role_family_score": role_family_result["score"],
            "capability_score": capability_score,
            "evidence_score": evidence_score,
            "title_alignment_score": title["score"],
            "experience_fit_score": experience["score"],
            "career_level_fit": career_level_result["score"],
            "location_fit": location["score"],
            "industry_fit": industry["score"],
            "salary_fit": salary["score"],
        },
        "experience_gap_years": experience.get("gap", 0.0),
        "candidate_experience_years": candidate.get("total_experience_years", 0),
        "job_min_experience_years": job.get("min_experience_years"),
        "job_max_experience_years": job.get("max_experience_years"),
        "relevant_evidence": relevant_evidence,
        "transferable_evidence": transferable_evidence,
        "missing_requirements": missing_requirements,
        "experience_gaps": experience_gaps,
        "risks": risks,
    }


# --------------------------------------------------------------------------
# Auto-apply precondition checks (logic only -- no submission wired up)
# --------------------------------------------------------------------------

def verify_auto_apply_preconditions(job: dict[str, Any], score_result: dict[str, Any], applications: list[dict[str, Any]]) -> dict[str, Any]:
    """Checks that must pass before this job could ever be auto-applied to.

    Does NOT apply to anything -- only evaluates conditions for a future apply step.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    job_url = job.get("application_url") or ""
    already_applied = any(a.get("application_url") == job_url or a.get("job_id") == job.get("id") for a in applications)
    checks["not_already_applied"] = not already_applied
    if already_applied:
        reasons.append("A record for this job already exists in applications.json")

    checks["valid_url"] = bool(job_url) and job_url.startswith("http")
    if not checks["valid_url"]:
        reasons.append("application_url is missing or not a valid http(s) URL")

    checks["identifiable"] = bool((job.get("company") or "").strip()) and bool((job.get("title") or "").strip())
    if not checks["identifiable"]:
        reasons.append("Company or title is missing")

    checks["appears_legitimate"] = bool(job_url) and len((job.get("description") or "")) > 50
    if not checks["appears_legitimate"]:
        reasons.append("Listing has no application URL or an unusually thin description")

    checks["decision_is_auto_apply"] = score_result.get("application_decision") == "AUTO_APPLY"
    if not checks["decision_is_auto_apply"]:
        reasons.append(f"Scoring decision is {score_result.get('application_decision')}, not AUTO_APPLY")

    return {"eligible": all(checks.values()), "checks": checks, "reasons": reasons}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def find_job_by_id(jobs_path: Path, job_id: str) -> dict[str, Any]:
    jobs = load_json(jobs_path)
    for job in jobs:
        if job.get("id") == job_id:
            return job
    raise SystemExit(f"No job with id {job_id!r} found in {jobs_path}")


def format_report(result: dict[str, Any]) -> str:
    lines = [
        f"Job: {result['job_title']!r} (id={result['job_id']})",
        f"Role family: {result['role_family']}   Career level: {result['career_level']}",
        f"Match: {result['overall_match_score']}/100",
        f"Experience: candidate {result['candidate_experience_years']:g}y vs job "
        f"{result['job_min_experience_years']}-{result['job_max_experience_years']}y "
        f"(gap {result['experience_gap_years']:g}y)",
        f"Decision: {result['application_decision']}",
        f"Reason: {result['decision_reason']}",
        "",
        "Breakdown:",
    ]
    for key, value in result["breakdown"].items():
        lines.append(f"  {key}: {value}")

    def section(title: str, items: list[str]) -> None:
        lines.append("")
        lines.append(f"{title}:")
        if items:
            for item in items:
                lines.append(f"  - {item}")
        else:
            lines.append("  (none)")

    section("Relevant evidence", result["relevant_evidence"])
    section("Transferable evidence", result["transferable_evidence"])
    section("Missing requirements", result["missing_requirements"])
    section("Experience gaps", result["experience_gaps"])
    section("Risks", result["risks"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    job_group = parser.add_mutually_exclusive_group(required=True)
    job_group.add_argument("--job", type=Path, help="Path to a single job JSON file")
    job_group.add_argument("--job-id", type=str, help="Id of a job to look up in --jobs")
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS_PATH, help="Path to data/jobs.json (used with --job-id)")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH, help="Path to candidate profile JSON")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a formatted report")
    args = parser.parse_args(argv)

    candidate = load_json(args.profile)
    job = load_json(args.job) if args.job else find_job_by_id(args.jobs, args.job_id)

    result = score_job(job, candidate)
    print(json.dumps(result, indent=2) if args.json else format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
