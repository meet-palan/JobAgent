# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A personal job-search and job-application assistant. It is organized around
a pipeline: **discover jobs → normalize → deduplicate → score/decide →
review on a dashboard**. `profile/` holds the candidate's own information;
`jobs/` and `applications/` hold human-readable working state; `data/` holds
the structured (machine-readable) version of that state.

## Current phase: Phase 5 (complete)

Job discovery, deduplication, V2 matching/scoring, and a read-only dashboard
are fully implemented, tested, and validated against live data. There is
**no browser automation, no application-submission automation, no resume/
cover-letter generation, and no LLM call anywhere in the discovery or
matching pipeline** — every stage (fetch, normalize, dedupe, score, decide)
is deterministic, dependency-free Python. Do not add any of the above unless
explicitly asked; see "Before adding new capabilities" below.

The static candidate profile (`profile/*.md` + `data/candidate_profile.json`)
is the only candidate the system currently serves. The long-term direction is
a public platform matching many candidates against a shared job universe —
see `README.md` → "Future direction" — but that is out of scope until
explicitly started; nothing in the current code should be changed to
special-case this one candidate in a way that would make that harder later.

## Architecture

```
Stage 0: Company discovery   (scripts/discover_companies.py)
Stage 1: Job discovery/normalization + Stage 2: Dedup/history (scripts/discover_jobs.py)
Stage 3: V2 matching + decision (scripts/score_job.py)
              ↓
        dashboard.py (read-only)
```

Full algorithm detail lives in `docs/JobAgent_Algorithm_Reference.pdf`.

## Source-of-truth files

- `data/jobs.json` and `data/applications.json` are the sources of truth for
  structured state. Files under `jobs/*/` and `applications/*/` are
  human-readable/working artifacts (notes, drafts, generated documents) tied
  to entries in those JSON files.
- `data/candidate_profile.json` is the structured mirror of `profile/*.md`,
  read by both `discover_jobs.py` and `discover_companies.py`. Keep it in
  sync by hand when the `profile/*.md` files change.
- `data/discovery_sources.json` is the list of ATS companies discovery
  actually queries. `data/discovery_candidates.json` is a separate,
  unverified seed list that only `discover_companies.py` reads — a candidate
  is promoted into `discovery_sources.json` only after a live request
  confirms its board exists.

## Coding rules

- Keep scripts under `scripts/` small and single-purpose; add tests for any
  non-trivial script under `tests/`. Run the full suite before and after any
  change (`python tests/<name>.py` for each file — plain `unittest`, no test
  runner is installed).
- **No fabrication, ever.** A company, a job, an API field, or a test
  expectation is never invented or guessed into a "verified"/"passing" state.
  A board is only "verified" after a real HTTP request confirms it; a
  candidate that 404s or errors is dropped and reported, never added.
  Missing job data (salary, experience, location, posting date, skills)
  stays `null`/empty — never inferred or defaulted to make a record look
  more complete.
- **Discovery is high recall, matching is precision.** Nothing in Stage 0/1/2
  ever rejects a posting for missing fields or a low score — only
  `score_job.py`'s `application_decision` does that. Company selection
  (Stage 0) may prioritize candidates likely to have relevant roles, but that
  is a maintenance heuristic, not a job-level filter.
- **Reuse the normalized job schema** (see below) — every connector
  (Greenhouse/Lever/Ashby/SmartRecruiters/Workday) converges on it; do not
  introduce a second job shape.
- **Access rules**: only public, unauthenticated ATS/career-site APIs are
  queried. Never bypass a CAPTCHA, login wall, rate limit, or robots.txt
  restriction. If a source is blocked or its ToS prohibits automated access
  (see `_unavailable_sources` in `data/discovery_sources.json` for
  LinkedIn/Naukri/Indeed/Wellfound), it stays excluded — document why rather
  than working around it.

## Normalized job schema

Every discovered posting, regardless of source, has this shape (fields the
source genuinely doesn't expose stay `null`/empty, never guessed):

```
id, company, title, location, work_mode, industry, company_size,
salary_min/max/currency, min/max_experience_years,
required_skills[], preferred_skills[], description,
application_url, source_url, source_job_id, source,
posted_date, date_discovered, discovered_at,
first_seen_at, last_seen_at,           # set by dedup/history, Stage 2
score, role_family, career_level,       # set by V2 matching, Stage 3
application_decision, decision_reason, score_breakdown, ...
```

## Matching rules (`scripts/score_job.py`) — treat as STABLE

This is the V2 matching engine. **Do not change its weights, thresholds,
career-level/role-family rules, capability/evidence logic, experience rules,
or the AUTO_APPLY/REVIEW/SKIP decision gates without the user explicitly
asking for a matching-behavior change** — and even then, update
`tests/score_job.py` first and confirm the new behavior against it. The 76
existing tests encode real product decisions (e.g. a high score must never
override the seniority gate; project/internship evidence must never be
scored as professional experience; **AUTO_APPLY requires the job's stated
minimum experience to be no greater than the candidate's actual professional
experience** — a gap of exactly 1 year previously passed this gate and let a
1-year candidate reach AUTO_APPLY against a 2-year requirement, fixed
post-Phase-5) — treat a test failure here as a regression to fix, not a test
to weaken.

Safe changes: comments/docstrings that don't alter control flow, formatting
that doesn't touch logic. Anything else needs a matching test update first.

## Testing requirements

Before committing any change, run all four suites and confirm counts:
`tests/score_job.py` (76), `tests/discover_jobs.py` (58),
`tests/discover_companies.py` (23), `tests/dashboard.py` (33) — 190 total.
Add tests for new behavior; never delete or weaken a test just to make it
pass. `discover_jobs.py`/`discover_companies.py` tests use fixture payloads
and mocks — no real network calls in the suite.

## Current limitations

- SmartRecruiters connector is implemented and tested but dormant — no
  configured company currently has its public postings feed enabled.
- Lever has 3 active companies; broader Lever company discovery hasn't found
  additional working boards yet (guessed candidates 404'd).
- Single static candidate only; no multi-user support, accounts, or auth.

## Before adding new capabilities

Before adding scraping of a new kind of site, browser automation, LLM calls,
resume/cover-letter generation, or auto-submission features, confirm scope
with the user — these are higher-risk, harder-to-reverse capabilities (they
touch external sites and/or submit real applications on the user's behalf).
