"""Discover job postings, normalize them, score them, and save the results.

Sources (configured in data/discovery_sources.json):
  - Greenhouse boards-api.greenhouse.io -- public, unauthenticated JSON API.
  - Lever api.lever.co -- public, unauthenticated JSON API.
  - Ashby api.ashbyhq.com/posting-api -- public, unauthenticated job-board API.
  - SmartRecruiters api.smartrecruiters.com -- public postings API, where a
    company has that public feed enabled (many do not; see README note in
    data/discovery_sources.json).
  - A company's own "cxs" career-site API (e.g. Workday) -- the same data the
    company's public career page itself fetches to render job listings.

This script does NOT log in anywhere, does NOT touch sites whose Terms of
Service or robots.txt prohibit automated access (e.g. LinkedIn, Naukri,
Indeed, Wellfound -- see the "_unavailable_sources" note in
data/discovery_sources.json for the specific reason each was excluded), and
does NOT submit applications. It only discovers, normalizes, deduplicates,
scores (via scripts/score_job.py), and saves postings for a human to review.

Discovery is optimized for HIGH RECALL: it does not reject a posting for
missing salary/experience/location/description/posting-date, and it does not
apply the matching engine's score threshold. Suitability is decided entirely
by scripts/score_job.py, after discovery.

Pipeline:
  1. Expand the candidate's own target/secondary titles (data/candidate_profile.json)
     with curated, closely-related title variants for the same role family
     (see ROLE_FAMILY_TITLE_VARIANTS) -- broadening the search without pulling
     in unrelated roles.
  2. Fetch raw postings from each configured source (paginating where the
     source supports it, up to MAX_PAGES_PER_SOURCE / MAX_JOBS_PER_SOURCE),
     filtered to the expanded titles and the candidate's preferred locations.
     A missing location is never treated as irrelevant -- it's unknown.
  3. Normalize every posting into one common schema (see JOB_SCHEMA_FIELDS).
  4. Deduplicate using a confidence-tiered identity check -- exact
     (source, source_job_id), then canonical application URL, then
     (company, title, location, required_skills) -- against both this run's
     results and data/jobs.json. A rediscovered job updates last_seen_at
     instead of being re-added; a genuinely new job gets first_seen_at set.
  5. Score each new unique job with scripts/score_job.py.
  6. Save every job (existing + new) to data/jobs.json, preserving history.
     Save jobs scoring AUTO_APPLY/REVIEW to jobs/shortlisted/<id>.md as well.
  7. Print per-source statistics (attempted/found/parsed/malformed/duplicates/
     new_unique/errors/status).

Usage:
    python scripts/discover_jobs.py
    python scripts/discover_jobs.py --max-jobs-per-source 50 --sources data/discovery_sources.json
    python scripts/discover_jobs.py --dry-run   # discover/score but don't write anything
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_job as scorer  # noqa: E402  -- reuse the matching engine

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES_PATH = REPO_ROOT / "data" / "discovery_sources.json"
DEFAULT_PROFILE_PATH = REPO_ROOT / "data" / "candidate_profile.json"
DEFAULT_JOBS_PATH = REPO_ROOT / "data" / "jobs.json"
SHORTLIST_DIR = REPO_ROOT / "jobs" / "shortlisted"

USER_AGENT = "JobAgent-personal-discovery/0.2 (personal job search tool, low volume)"
SHORTLIST_THRESHOLD = 80

# Safety limits -- configurable via CLI flags, see main(). One broken/huge
# source can never run indefinitely or dominate a run.
DEFAULT_MAX_PAGES_PER_SOURCE = 5
DEFAULT_MAX_JOBS_PER_SOURCE = 200
DEFAULT_REQUEST_DELAY_SECONDS = 0.4
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
WORKDAY_SEARCH_PAGE_SIZE = 20

TODAY = date.today().isoformat()

JOB_SCHEMA_FIELDS = [
    "id", "company", "title", "location", "work_mode", "industry", "company_size",
    "salary_min", "salary_max", "salary_currency",
    "min_experience_years", "max_experience_years",
    "required_skills", "preferred_skills", "description",
    "application_url", "source_url", "source_job_id", "linkedin_url",
    "source", "posted_date", "date_discovered", "discovered_at",
]


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def http_get_json(url: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Exceptions treated as "this source is unavailable right now" rather than a
# crash: network errors, HTTP errors, and timeouts (urllib raises
# socket.timeout / TimeoutError, both are OSError subclasses like URLError).
SOURCE_UNAVAILABLE_EXCEPTIONS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError)


# --------------------------------------------------------------------------
# Text extraction helpers
# --------------------------------------------------------------------------

def strip_html(html: str | None) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html or "")
    text = re.sub(r"<li[^>]*>", "\n", text)
    text = re.sub(r"</(p|li|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


_EXP_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*years?", re.I)
_EXP_PLUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+\s*years?", re.I)
_EXP_MIN_RE = re.compile(r"minimum\s*(\d+(?:\.\d+)?)\s*years?", re.I)


def parse_experience_years(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = _EXP_RANGE_RE.search(text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _EXP_PLUS_RE.search(text)
    if m:
        return float(m.group(1)), None
    m = _EXP_MIN_RE.search(text)
    if m:
        return float(m.group(1)), None
    return None, None


_SALARY_RANGE_RE = re.compile(r"\$\s*([\d,.]+)\s*([kKmM]?)\s*[-–]\s*\$?\s*([\d,.]+)\s*([kKmM]?)")


def parse_salary_range(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse a human-readable salary range like '$211.4K - $290.6K' into (min, max, currency).

    Never invents a value -- returns (None, None, None) if nothing matches.
    """
    if not text:
        return None, None, None
    m = _SALARY_RANGE_RE.search(text)
    if not m:
        return None, None, None

    def to_num(num_str: str, suffix: str) -> float:
        n = float(num_str.replace(",", ""))
        if suffix.lower() == "k":
            n *= 1_000
        elif suffix.lower() == "m":
            n *= 1_000_000
        return n

    lo = to_num(m.group(1), m.group(2))
    hi = to_num(m.group(3), m.group(4) or m.group(2))
    return lo, hi, "USD"


def classify_work_mode(text: str | None) -> str | None:
    t = (text or "").lower()
    if "hybrid" in t:
        return "hybrid"
    if re.search(r"\bremote\b", t):
        return "remote"
    if "onsite" in t or "on-site" in t or "based at" in t or "based in" in t:
        return "onsite"
    return None


def make_id(*parts: str) -> str:
    slug = "-".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in parts if p)
    return slug[:150]


def word_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


# A small, explicit set of word-form variants for the vocabulary that shows up
# in role titles. Deliberately not a general stemmer (e.g. a blind prefix cut)
# because that also collapses unrelated words like "product"/"production".
_WORD_VARIANTS = {
    "manage": {"manager", "managers", "management", "managing"},
    "analy": {"analyst", "analysts", "analysis", "analytics"},
    "coordinat": {"coordinator", "coordinators", "coordination", "coordinating"},
    "associat": {"associate", "associates", "association"},
}
_VARIANT_LOOKUP = {variant: canon for canon, variants in _WORD_VARIANTS.items() for variant in variants}


def normalize_word(word: str) -> str:
    return _VARIANT_LOOKUP.get(word, word)


def title_is_relevant(title: str, keywords: list[str]) -> bool:
    """True if `title` contains every (normalized) word of at least one keyword phrase.

    Deliberately stricter than "shares any word" -- across a large company's
    full job catalog, single generic words like "manager" or "product" match
    hundreds of unrelated roles (e.g. "Manager - Accounts"). Requiring the
    full keyword phrase (all its words, order-independent) cuts that noise
    while still matching real variants like "Program/Project Management Lead".
    """
    title_words = {normalize_word(w) for w in word_tokens(title)}
    for kw in keywords:
        kw_words = {normalize_word(w) for w in word_tokens(kw)}
        if kw_words and kw_words.issubset(title_words):
            return True
    return False


def location_is_relevant(location: str, preferred_locations: list[str]) -> bool:
    """True unless `location` is stated AND clearly outside the preferred list.

    High-recall rule: a MISSING location is unknown, not irrelevant, so it
    always passes. A stated "remote" location always passes too, since remote
    work is an explicit preference. Discovery should not silently drop a job
    just because its location field is blank or informally worded.
    """
    if not preferred_locations:
        return True
    loc = (location or "").strip().lower()
    if not loc:
        return True
    if "remote" in loc or "work from home" in loc or "wfh" in loc:
        return True
    return any(pref.lower() in loc for pref in preferred_locations)


# --------------------------------------------------------------------------
# Search / title expansion
# --------------------------------------------------------------------------

# Curated, closely-related title variants per role family. NOT a general
# synonym expander -- only added when the candidate's own target/secondary
# titles already belong to that family (see expand_target_titles), and never
# expanded into unrelated families like engineering, sales, or marketing.
ROLE_FAMILY_TITLE_VARIANTS: dict[str, list[str]] = {
    "product": [
        "Product Manager", "Associate Product Manager", "APM", "Product Analyst",
        "Product Management Analyst", "Product Specialist", "Product Operations",
        "Product Operations Analyst",
    ],
    "business_analysis": [
        "Business Analyst", "Business Analysis", "Business Systems Analyst",
        "Functional Analyst", "Business Operations Analyst",
    ],
    "project": [
        "Project Manager", "Project Coordinator", "Project Analyst", "PMO Analyst",
        "Project Management Analyst", "Program Coordinator", "Program Analyst",
        "Program Manager", "Technical Program Manager",
    ],
}

_FAMILY_TRIGGER_WORDS: dict[str, set[str]] = {
    "product": {"product"},
    "business_analysis": {"business", "analyst", "analysis"},
    "project": {"project", "program", "coordinator"},
}

# Location searches to run in addition to the candidate's stated preferred
# locations -- covers how remote/India-remote roles are actually worded.
REMOTE_PSEUDO_LOCATIONS = ["Remote", "Remote India", "Work From Home India", "Hybrid India"]


def expand_target_titles(target_titles: list[str], secondary_titles: list[str]) -> list[str]:
    """Broaden the candidate's own titles with curated same-family variants.

    e.g. "Associate Product Manager" pulls in "APM"/"Product Analyst"/
    "Product Operations Analyst", but a profile with no project/program title
    never gets "PMO Analyst" added, and nothing here ever adds an unrelated
    family like "Software Engineer" or "Marketing Manager".
    """
    base = [*target_titles, *secondary_titles]
    base_words: set[str] = set()
    for t in base:
        base_words |= word_tokens(t)

    expanded = list(base)
    seen_lower = {t.lower() for t in base}
    for family, trigger_words in _FAMILY_TRIGGER_WORDS.items():
        if base_words & trigger_words:
            for variant in ROLE_FAMILY_TITLE_VARIANTS[family]:
                if variant.lower() not in seen_lower:
                    expanded.append(variant)
                    seen_lower.add(variant.lower())
    return expanded


# --------------------------------------------------------------------------
# URL normalization (for cross-source duplicate detection)
# --------------------------------------------------------------------------

def canonical_application_url(url: str | None) -> str | None:
    """Normalize a URL for identity comparison: lowercase host, no scheme,
    no trailing slash, no query string (tracking/referral params like
    ?gh_src=, ?lever-source=, ?utm_* differ per source for the same job).
    """
    if not url:
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


# --------------------------------------------------------------------------
# Greenhouse connector
# --------------------------------------------------------------------------

def fetch_greenhouse_jobs(board_token: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    data = http_get_json(url, timeout=timeout)
    return data.get("jobs", [])


def normalize_greenhouse_job(raw: dict, board_token: str) -> dict:
    title = raw.get("title", "")
    location = (raw.get("location") or {}).get("name", "")
    content = strip_html(raw.get("content", ""))
    min_exp, max_exp = parse_experience_years(content)
    absolute_url = raw.get("absolute_url")
    updated_at = raw.get("updated_at") or raw.get("first_published")
    return {
        "id": make_id("greenhouse", board_token, title, str(raw.get("id", ""))),
        "company": board_token,
        "title": title,
        "location": location,
        "work_mode": classify_work_mode(content) or classify_work_mode(location),
        "industry": None,
        "company_size": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        # Greenhouse job content varies too much per company to reliably split
        # into atomic must-have/good-to-have skills; left empty rather than guessed.
        "required_skills": [],
        "preferred_skills": [],
        "description": content[:4000],
        "application_url": absolute_url,
        "source_url": absolute_url,
        "source_job_id": str(raw.get("id", "")) or None,
        "linkedin_url": None,
        "posted_date": (updated_at or "")[:10] or None,
        "source": f"greenhouse:{board_token}",
        "date_discovered": TODAY,
        "discovered_at": TODAY,
    }


# --------------------------------------------------------------------------
# Lever connector
# --------------------------------------------------------------------------

def fetch_lever_jobs(company_slug: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    return http_get_json(url, timeout=timeout)


def _lever_list_text(raw: dict, *keywords: str) -> str | None:
    for lst in raw.get("lists", []):
        label = (lst.get("text") or "").lower()
        if any(k in label for k in keywords):
            return strip_html(lst.get("content", ""))
    return None


def _bullets(text: str | None) -> list[str]:
    if not text:
        return []
    lines = [ln.strip("-• \t") for ln in text.splitlines()]
    return [ln for ln in lines if ln]


def _is_experience_line(line: str) -> bool:
    return bool(_EXP_RANGE_RE.search(line) or _EXP_PLUS_RE.search(line) or _EXP_MIN_RE.search(line))


def normalize_lever_job(raw: dict, company_slug: str) -> dict:
    title = raw.get("text", "")
    location = (raw.get("categories") or {}).get("location", "")
    description = strip_html(raw.get("descriptionPlain") or raw.get("description", ""))
    skills_text = _lever_list_text(raw, "skill")
    requirements_text = _lever_list_text(raw, "experience", "what you will need", "requirement", "qualification")
    min_exp, max_exp = parse_experience_years(" ".join(filter(None, [description, skills_text, requirements_text])))

    required: list[str] = []
    preferred: list[str] = []
    for bullet in _bullets(skills_text) + _bullets(requirements_text):
        if _is_experience_line(bullet) or re.match(r"education|pedigree", bullet, re.I):
            continue
        bucket = preferred if re.search(r"\bplus\b|nice to have|preferred", bullet, re.I) else required
        bucket.append(bullet)

    hosted_url = raw.get("hostedUrl")
    created_at = raw.get("createdAt")  # epoch millis
    posted_date = None
    if isinstance(created_at, (int, float)):
        posted_date = date.fromtimestamp(created_at / 1000).isoformat()

    return {
        "id": make_id("lever", company_slug, title, str(raw.get("id", ""))),
        "company": company_slug,
        "title": title,
        "location": location,
        "work_mode": classify_work_mode(description) or classify_work_mode(location),
        "industry": None,
        "company_size": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        "required_skills": required,
        "preferred_skills": preferred,
        "description": description[:4000],
        "application_url": hosted_url or raw.get("applyUrl"),
        "source_url": hosted_url,
        "source_job_id": raw.get("id"),
        "linkedin_url": None,
        "posted_date": posted_date,
        "source": f"lever:{company_slug}",
        "date_discovered": TODAY,
        "discovered_at": TODAY,
    }


# --------------------------------------------------------------------------
# Ashby connector (public, unauthenticated job-board API)
# --------------------------------------------------------------------------

def fetch_ashby_jobs(board_name: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true"
    data = http_get_json(url, timeout=timeout)
    return data.get("jobs", [])


def normalize_ashby_job(raw: dict, board_name: str) -> dict:
    title = raw.get("title", "")
    location = raw.get("location") or ""
    if raw.get("isRemote") and "remote" not in location.lower():
        location = f"{location} (Remote)".strip() if location else "Remote"
    description = strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlain") or "")
    min_exp, max_exp = parse_experience_years(description)
    compensation = raw.get("compensation") or {}
    salary_min, salary_max, salary_currency = parse_salary_range(
        compensation.get("scrapeableCompensationSalarySummary")
    )
    published_at = raw.get("publishedAt")
    job_url = raw.get("jobUrl")

    return {
        "id": make_id("ashby", board_name, title, str(raw.get("id", ""))),
        "company": board_name,
        "title": title,
        "location": location,
        "work_mode": classify_work_mode(raw.get("workplaceType"))
        or classify_work_mode(description)
        or classify_work_mode(location),
        "industry": None,
        "company_size": None,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "min_experience_years": min_exp,
        "max_experience_years": max_exp,
        # Ashby's public API doesn't structure requirements into discrete
        # skill bullets the way Lever's "lists" do -- left empty rather than guessed.
        "required_skills": [],
        "preferred_skills": [],
        "description": description[:4000],
        "application_url": raw.get("applyUrl") or job_url,
        "source_url": job_url,
        "source_job_id": raw.get("id"),
        "linkedin_url": None,
        "posted_date": (published_at or "")[:10] or None,
        "source": f"ashby:{board_name}",
        "date_discovered": TODAY,
        "discovered_at": TODAY,
    }


# --------------------------------------------------------------------------
# SmartRecruiters connector (public postings API where a company has it
# enabled -- many companies on lower SmartRecruiters plans do not expose it,
# so this is included for completeness but data/discovery_sources.json ships
# with no companies configured until a live one is verified; see its
# "_smartrecruiters_note").
# --------------------------------------------------------------------------

def fetch_smartrecruiters_page(company: str, offset: int, limit: int, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> dict:
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?offset={offset}&limit={limit}"
    return http_get_json(url, timeout=timeout)


def normalize_smartrecruiters_job(raw: dict, company: str) -> dict:
    title = raw.get("name", "")
    location_info = raw.get("location") or {}
    location = location_info.get("fullLocation") or location_info.get("city") or ""
    work_mode = None
    if location_info.get("remote"):
        work_mode = "remote"
    elif location_info.get("hybrid"):
        work_mode = "hybrid"
    experience_level = (raw.get("experienceLevel") or {}).get("label")
    posting_id = raw.get("id")
    apply_url = f"https://jobs.smartrecruiters.com/{company}/{posting_id}" if posting_id else None

    return {
        "id": make_id("smartrecruiters", company, title, str(posting_id or "")),
        "company": company,
        "title": title,
        "location": location,
        "work_mode": work_mode or classify_work_mode(location),
        "industry": (raw.get("industry") or {}).get("label"),
        "company_size": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "min_experience_years": None,
        "max_experience_years": None,
        "required_skills": [],
        "preferred_skills": [],
        # The postings LIST endpoint doesn't include the full job ad body --
        # that needs a second per-posting detail call this connector doesn't
        # make (no verified live company currently justifies the extra
        # requests). Left as the one non-empty fact we do have, rather than fabricated.
        "description": f"Experience level: {experience_level}" if experience_level else "",
        "application_url": apply_url,
        "source_url": apply_url,
        "source_job_id": posting_id,
        "linkedin_url": None,
        "posted_date": (raw.get("releasedDate") or "")[:10] or None,
        "source": f"smartrecruiters:{company}",
        "date_discovered": TODAY,
        "discovered_at": TODAY,
    }


# --------------------------------------------------------------------------
# Workday ("cxs" career-site API) connector
# --------------------------------------------------------------------------

def fetch_workday_search(
    cxs_base_url: str, search_text: str, limit: int = WORKDAY_SEARCH_PAGE_SIZE, offset: int = 0,
    timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> list[dict]:
    data = http_post_json(
        f"{cxs_base_url}/jobs", {"limit": limit, "offset": offset, "searchText": search_text}, timeout=timeout
    )
    return data.get("jobPostings", [])


def fetch_workday_detail(cxs_base_url: str, external_path: str, timeout: int = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> dict:
    return http_get_json(f"{cxs_base_url}{external_path}", timeout=timeout)


def _workday_field(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*:\s*(.+)", text)
    return m.group(1).split("\n")[0].strip() if m else None


def normalize_workday_job(detail: dict, company_name: str, source_key: str) -> dict:
    info = detail.get("jobPostingInfo", {})
    title = info.get("title", "")
    location = info.get("location", "")
    text = strip_html(info.get("jobDescription", ""))

    must_have = _workday_field(text, "Must have skills")
    good_to_have = _workday_field(text, "Good to have skills")
    required = [s.strip() for s in (must_have or "").split(",") if s.strip()]
    preferred = [s.strip() for s in (good_to_have or "").split(",") if s.strip() and s.strip().upper() != "NA"]

    min_exp, _ = parse_experience_years(text)
    external_url = info.get("externalUrl")

    return {
        "id": make_id("workday", company_name, title, info.get("jobReqId") or info.get("jobPostingId", "")),
        "company": company_name,
        "title": title,
        "location": location,
        "work_mode": classify_work_mode(text),
        "industry": None,
        "company_size": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "min_experience_years": min_exp,
        "max_experience_years": None,
        "required_skills": required,
        "preferred_skills": preferred,
        "description": text[:4000],
        "application_url": external_url,
        "source_url": external_url,
        "source_job_id": info.get("jobReqId") or info.get("jobPostingId"),
        "linkedin_url": None,
        # Workday's search facet returns a relative freshness string, not a
        # date -- not parsed into posted_date since it can't be done without guessing.
        "posted_date": None,
        "source": source_key,
        "date_discovered": TODAY,
        "discovered_at": TODAY,
    }


# --------------------------------------------------------------------------
# Normalization / deduplication / scoring / persistence
# --------------------------------------------------------------------------

def dedupe_key(job: dict) -> tuple:
    skills_key = tuple(sorted(s.strip().lower() for s in job.get("required_skills", [])))
    return (
        (job.get("company") or "").strip().lower(),
        (job.get("title") or "").strip().lower(),
        (job.get("location") or "").strip().lower(),
        skills_key,
    )


def job_identity_keys(job: dict) -> list[tuple]:
    """Confidence-tiered identity keys for this job, strongest first:
      1. (source, source_job_id) -- exact source match.
      2. canonical application URL -- catches the same posting reachable via
         a different source's copy of the same link.
      3. (company, title, location, required_skills) -- the existing
         dedupe_key(); a same-titled-and-located posting with DIFFERENT
         required skills is deliberately NOT treated as the same job here
         (e.g. two "Business Analyst" reqs at the same company/city for
         different clients/skillsets are real, separate openings).
    Any single matching key is enough to call two postings the same job.
    """
    keys: list[tuple] = []
    source, source_job_id = job.get("source"), job.get("source_job_id")
    if source and source_job_id:
        keys.append(("source_id", source, str(source_job_id)))
    canon_url = canonical_application_url(job.get("application_url"))
    if canon_url:
        keys.append(("url", canon_url))
    keys.append(("company_title_location_skills", dedupe_key(job)))
    return keys


def build_identity_index(jobs: list[dict]) -> dict[tuple, dict]:
    index: dict[tuple, dict] = {}
    for job in jobs:
        for key in job_identity_keys(job):
            index[key] = job
    return index


def register_job(index: dict[tuple, dict], job: dict) -> None:
    for key in job_identity_keys(job):
        index[key] = job


def find_duplicate(index: dict[tuple, dict], job: dict) -> dict | None:
    for key in job_identity_keys(job):
        existing = index.get(key)
        if existing is not None:
            return existing
    return None


def load_existing_jobs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        content = f.read().strip()
    return json.loads(content) if content else []


def save_jobs(path: Path, jobs: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)
        f.write("\n")


def backfill_history_fields(job: dict) -> None:
    """Every job must carry first_seen_at/last_seen_at. For jobs saved before
    this field existed, backfill both from date_discovered (never invented --
    it's the closest real fact we have) instead of leaving them missing.
    """
    if not job.get("first_seen_at"):
        job["first_seen_at"] = job.get("date_discovered") or TODAY
    if not job.get("last_seen_at"):
        job["last_seen_at"] = job.get("date_discovered") or TODAY


def score_and_annotate(job: dict, candidate: dict) -> dict:
    scorer_input = dict(job)
    scorer_input["nice_to_have_skills"] = job.get("preferred_skills", [])
    result = scorer.score_job(scorer_input, candidate)
    job["score"] = result["overall_match_score"]
    job["role_family"] = result["role_family"]
    job["career_level"] = result["career_level"]
    job["application_decision"] = result["application_decision"]
    job["decision_reason"] = result["decision_reason"]
    job["score_breakdown"] = result["breakdown"]
    job["experience_gap_years"] = result["experience_gap_years"]
    job["relevant_evidence"] = result["relevant_evidence"]
    job["transferable_evidence"] = result["transferable_evidence"]
    job["missing_requirements"] = result["missing_requirements"]
    job["experience_gaps"] = result["experience_gaps"]
    job["risks"] = result["risks"]
    # Shortlisted = worth a human look or better (AUTO_APPLY or REVIEW).
    # Jobs the engine decided to SKIP stay "discovered" -- not moved to
    # jobs/rejected/, since that folder means an explicit human/employer
    # rejection, not an algorithmic low score.
    job["status"] = "shortlisted" if result["application_decision"] in ("AUTO_APPLY", "REVIEW") else "discovered"
    return job


def write_shortlist_file(job: dict) -> Path:
    SHORTLIST_DIR.mkdir(parents=True, exist_ok=True)
    path = SHORTLIST_DIR / f"{job['id']}.md"
    exp = "not specified"
    if job.get("min_experience_years") is not None:
        exp = f"{job['min_experience_years']}"
        if job.get("max_experience_years") is not None:
            exp += f"-{job['max_experience_years']}"
        exp += "+ years" if job.get("max_experience_years") is None else " years"

    def section(title: str, items: list[str]) -> list[str]:
        return ["", f"## {title}", *([f"- {i}" for i in items] or ["- (none)"])]

    lines = [
        f"# {job['title']} — {job['company']}",
        "",
        f"- Match: {job['score']}/100",
        f"- Role family: {job.get('role_family')}   Career level: {job.get('career_level')}",
        f"- Decision: {job['application_decision']} -- {job['decision_reason']}",
        f"- Location: {job['location']} ({job.get('work_mode') or 'work mode not specified'})",
        f"- Experience required: {exp} (gap: {job.get('experience_gap_years', 0):g} years)",
        f"- Source: {job['source']}",
        f"- Discovered: {job['date_discovered']}",
        f"- Apply: {job['application_url']}",
        "",
        "## Score breakdown",
        *[f"- {k}: {v}" for k, v in job["score_breakdown"].items()],
    ]
    lines += section("Relevant evidence", job["relevant_evidence"])
    lines += section("Transferable evidence", job["transferable_evidence"])
    lines += section("Missing requirements", job["missing_requirements"])
    lines += section("Experience gaps", job["experience_gaps"])
    lines += section("Risks", job["risks"])
    lines += ["", "## Description", job.get("description", "")]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Per-source statistics
# --------------------------------------------------------------------------

def new_stats(name: str) -> dict:
    return {
        "source": name, "attempted": 0, "found": 0, "parsed": 0,
        "malformed": 0, "rejected": 0, "duplicates": 0, "new_unique": 0, "errors": 0,
        "status": "ok",
    }


def format_stats_table(stats_list: list[dict]) -> str:
    header = (
        f"{'SOURCE':<28}{'FOUND':>7}{'PARSED':>8}{'MALFORMED':>11}"
        f"{'REJECTED':>10}{'DUPLICATES':>12}{'NEW':>6}  STATUS"
    )
    lines = [header]
    for s in stats_list:
        lines.append(
            f"{s['source']:<28}{s['found']:>7}{s['parsed']:>8}{s['malformed']:>11}"
            f"{s.get('rejected', 0):>10}{s['duplicates']:>12}{s['new_unique']:>6}  {s['status']}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Collectors -- each appends normalized jobs (tagged with their source stats
# dict via "_source_stats", stripped before scoring) to raw_jobs, and its own
# stats dict to stats_list. A source that errors is marked "unavailable" /
# "error" and the run continues with the remaining sources.
# --------------------------------------------------------------------------

def collect_greenhouse(sources: dict, keywords: list[str], preferred_locations: list[str], config: dict,
                        raw_jobs: list[dict], stats_list: list[dict]) -> None:
    for token in sources.get("greenhouse_boards", []):
        stats = new_stats(f"greenhouse:{token}")
        stats_list.append(stats)
        stats["attempted"] += 1
        try:
            postings = fetch_greenhouse_jobs(token, timeout=config["timeout"])
        except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
            stats["status"], stats["errors"] = "unavailable", 1
            print(f"warning: greenhouse:{token} unavailable: {e}", file=sys.stderr)
            continue
        stats["found"] = len(postings)
        added = 0
        for raw in postings:
            if added >= config["max_jobs_per_source"]:
                break
            title = raw.get("title") or ""
            if not title or raw.get("id") is None:
                stats["malformed"] += 1
                continue
            location = (raw.get("location") or {}).get("name", "")
            if title_is_relevant(title, keywords) and location_is_relevant(location, preferred_locations):
                job = normalize_greenhouse_job(raw, token)
                job["_source_stats"] = stats
                raw_jobs.append(job)
                stats["parsed"] += 1
                added += 1
            else:
                stats["rejected"] += 1
        time.sleep(config["delay"])


def collect_lever(sources: dict, keywords: list[str], preferred_locations: list[str], config: dict,
                   raw_jobs: list[dict], stats_list: list[dict]) -> None:
    for slug in sources.get("lever_companies", []):
        stats = new_stats(f"lever:{slug}")
        stats_list.append(stats)
        stats["attempted"] += 1
        try:
            postings = fetch_lever_jobs(slug, timeout=config["timeout"])
        except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
            stats["status"], stats["errors"] = "unavailable", 1
            print(f"warning: lever:{slug} unavailable: {e}", file=sys.stderr)
            continue
        stats["found"] = len(postings)
        added = 0
        for raw in postings:
            if added >= config["max_jobs_per_source"]:
                break
            title = raw.get("text") or ""
            if not title or raw.get("id") is None:
                stats["malformed"] += 1
                continue
            location = (raw.get("categories") or {}).get("location", "")
            if title_is_relevant(title, keywords) and location_is_relevant(location, preferred_locations):
                job = normalize_lever_job(raw, slug)
                job["_source_stats"] = stats
                raw_jobs.append(job)
                stats["parsed"] += 1
                added += 1
            else:
                stats["rejected"] += 1
        time.sleep(config["delay"])


def collect_ashby(sources: dict, keywords: list[str], preferred_locations: list[str], config: dict,
                   raw_jobs: list[dict], stats_list: list[dict]) -> None:
    for board in sources.get("ashby_boards", []):
        stats = new_stats(f"ashby:{board}")
        stats_list.append(stats)
        stats["attempted"] += 1
        try:
            postings = fetch_ashby_jobs(board, timeout=config["timeout"])
        except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
            stats["status"], stats["errors"] = "unavailable", 1
            print(f"warning: ashby:{board} unavailable: {e}", file=sys.stderr)
            continue
        stats["found"] = len(postings)
        added = 0
        for raw in postings:
            if added >= config["max_jobs_per_source"]:
                break
            title = raw.get("title") or ""
            if not title or raw.get("id") is None:
                stats["malformed"] += 1
                continue
            if not raw.get("isListed", True):
                stats["rejected"] += 1
                continue
            location = raw.get("location") or ""
            if title_is_relevant(title, keywords) and location_is_relevant(location, preferred_locations):
                job = normalize_ashby_job(raw, board)
                job["_source_stats"] = stats
                raw_jobs.append(job)
                stats["parsed"] += 1
                added += 1
            else:
                stats["rejected"] += 1
        time.sleep(config["delay"])


def collect_smartrecruiters(sources: dict, keywords: list[str], preferred_locations: list[str], config: dict,
                             raw_jobs: list[dict], stats_list: list[dict]) -> None:
    page_size = 100
    for company in sources.get("smartrecruiters_companies", []):
        stats = new_stats(f"smartrecruiters:{company}")
        stats_list.append(stats)
        added = 0
        page = 0
        while page < config["max_pages_per_source"] and added < config["max_jobs_per_source"]:
            stats["attempted"] += 1
            try:
                data = fetch_smartrecruiters_page(company, offset=page * page_size, limit=page_size, timeout=config["timeout"])
            except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
                stats["status"], stats["errors"] = "unavailable", stats["errors"] + 1
                print(f"warning: smartrecruiters:{company} unavailable: {e}", file=sys.stderr)
                break
            postings = data.get("content", [])
            stats["found"] += len(postings)
            for raw in postings:
                if added >= config["max_jobs_per_source"]:
                    break
                title = raw.get("name") or ""
                if not title or raw.get("id") is None:
                    stats["malformed"] += 1
                    continue
                location = (raw.get("location") or {}).get("fullLocation", "")
                if title_is_relevant(title, keywords) and location_is_relevant(location, preferred_locations):
                    job = normalize_smartrecruiters_job(raw, company)
                    job["_source_stats"] = stats
                    raw_jobs.append(job)
                    stats["parsed"] += 1
                    added += 1
                else:
                    stats["rejected"] += 1
            time.sleep(config["delay"])
            page += 1
            if len(postings) < page_size:
                break  # no more pages


def collect_workday(sources: dict, keywords: list[str], preferred_locations: list[str], config: dict,
                     raw_jobs: list[dict], stats_list: list[dict]) -> None:
    for wd in sources.get("workday", []):
        cxs = wd["cxs_base_url"]
        name = wd["name"]
        stats = new_stats(f"workday:{name.lower()}")
        stats_list.append(stats)
        seen_paths: dict[str, dict] = {}
        for kw in keywords:
            page = 0
            while page < config["max_pages_per_source"]:
                stats["attempted"] += 1
                try:
                    postings = fetch_workday_search(
                        cxs, kw, limit=WORKDAY_SEARCH_PAGE_SIZE, offset=page * WORKDAY_SEARCH_PAGE_SIZE,
                        timeout=config["timeout"],
                    )
                except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
                    stats["status"], stats["errors"] = "unavailable", stats["errors"] + 1
                    print(f"warning: workday:{name} search {kw!r} unavailable: {e}", file=sys.stderr)
                    break
                time.sleep(config["delay"])
                stats["found"] += len(postings)
                for p in postings:
                    path = p.get("externalPath")
                    title = p.get("title", "")
                    bullet_fields = p.get("bulletFields") or []
                    loc_str = bullet_fields[1] if len(bullet_fields) > 1 else ""
                    if not path:
                        stats["malformed"] += 1
                        continue
                    if path in seen_paths:
                        continue
                    if not title_is_relevant(title, keywords):
                        stats["rejected"] += 1
                        continue
                    if not location_is_relevant(loc_str, preferred_locations):
                        stats["rejected"] += 1
                        continue
                    seen_paths[path] = {}
                page += 1
                if len(postings) < WORKDAY_SEARCH_PAGE_SIZE:
                    break

        added = 0
        for path in seen_paths:
            if added >= config["max_jobs_per_source"]:
                break
            try:
                detail = fetch_workday_detail(cxs, path, timeout=config["timeout"])
            except SOURCE_UNAVAILABLE_EXCEPTIONS as e:
                stats["errors"] += 1
                print(f"warning: workday:{name} detail {path!r} unavailable: {e}", file=sys.stderr)
                continue
            job = normalize_workday_job(detail, name, f"workday:{name.lower()}")
            job["_source_stats"] = stats
            raw_jobs.append(job)
            stats["parsed"] += 1
            added += 1
            time.sleep(config["delay"])


# --------------------------------------------------------------------------
# Run orchestration
# --------------------------------------------------------------------------

def run_discovery(sources: dict, candidate: dict, jobs_path: Path, config: dict, dry_run: bool) -> list[dict]:
    keywords = expand_target_titles(candidate.get("target_titles", []), candidate.get("secondary_titles", []))
    preferred_locations = [*candidate.get("preferred_locations", []), *REMOTE_PSEUDO_LOCATIONS]

    raw_jobs: list[dict] = []
    stats_list: list[dict] = []
    collect_lever(sources, keywords, preferred_locations, config, raw_jobs, stats_list)
    collect_greenhouse(sources, keywords, preferred_locations, config, raw_jobs, stats_list)
    collect_ashby(sources, keywords, preferred_locations, config, raw_jobs, stats_list)
    collect_smartrecruiters(sources, keywords, preferred_locations, config, raw_jobs, stats_list)
    collect_workday(sources, keywords, preferred_locations, config, raw_jobs, stats_list)

    existing = load_existing_jobs(jobs_path)
    for job in existing:
        backfill_history_fields(job)
    index = build_identity_index(existing)

    unique_new: list[dict] = []
    total_duplicates = 0
    rediscovered = 0
    for job in raw_jobs:
        stats = job.pop("_source_stats", None)
        dup = find_duplicate(index, job)
        if dup is not None:
            total_duplicates += 1
            if stats is not None:
                stats["duplicates"] += 1
            if dup.get("last_seen_at") != TODAY:
                dup["last_seen_at"] = TODAY
                rediscovered += 1
            continue
        job["first_seen_at"] = TODAY
        job["last_seen_at"] = TODAY
        register_job(index, job)
        unique_new.append(job)
        if stats is not None:
            stats["new_unique"] += 1

    for job in unique_new:
        score_and_annotate(job, candidate)

    shortlisted = [j for j in unique_new if j["status"] == "shortlisted"]

    if not dry_run:
        save_jobs(jobs_path, existing + unique_new)
        for job in shortlisted:
            write_shortlist_file(job)

    print(format_stats_table(stats_list))
    print()
    print(f"Fetched {len(raw_jobs)} relevant posting(s); {total_duplicates} duplicate(s) "
          f"({rediscovered} rediscovered from a prior run, last_seen_at updated); "
          f"{len(unique_new)} new unique job(s){' (dry run, nothing saved)' if dry_run else f' saved to {jobs_path}'}.")
    print(f"{len(shortlisted)} job(s) are AUTO_APPLY or REVIEW "
          f"{'would be' if dry_run else 'were'} written to {SHORTLIST_DIR}.")
    for job in sorted(unique_new, key=lambda j: -j["score"]):
        print(f"  [{job['score']:3d}] {job['application_decision']:11s} {job['title']} @ {job['company']} ({job['location']})")

    return unique_new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS_PATH)
    parser.add_argument("--max-jobs-per-source", type=int, default=DEFAULT_MAX_JOBS_PER_SOURCE,
                         help="Cap on new postings fetched per source this run")
    parser.add_argument("--max-pages-per-source", type=int, default=DEFAULT_MAX_PAGES_PER_SOURCE,
                         help="Cap on pages fetched per source/query this run")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true", help="Discover and score but do not write any files")
    args = parser.parse_args(argv)

    with args.sources.open(encoding="utf-8") as f:
        sources = json.load(f)
    with args.profile.open(encoding="utf-8") as f:
        candidate = json.load(f)

    config = {
        "max_jobs_per_source": args.max_jobs_per_source,
        "max_pages_per_source": args.max_pages_per_source,
        "delay": args.request_delay,
        "timeout": args.request_timeout,
    }

    run_discovery(sources, candidate, args.jobs, config, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
