# JobAgent

A personal job-search assistant: it discovers job postings from public ATS
APIs, scores them against a candidate's real profile, and surfaces the good
ones on a local dashboard. Everything is deterministic, dependency-free
Python — no browser automation, no LLM calls, no automated application
submission.

## Current phase

**Phase 5 — complete.** Company discovery, job discovery/normalization,
deduplication with history tracking, the V2 matching engine, and a read-only
dashboard are all implemented, tested (181 tests), and validated against
live data (203 unique jobs at last run). See
`docs/JobAgent_Algorithm_Reference.pdf` for full algorithm detail and
`docs/JobAgent_Project_Analysis.pdf` for a structural walkthrough.

## Architecture

```
STAGE 0                 STAGE 1                  STAGE 2                V2 MATCHING
Company Discovery  -->  Job Discovery/       -->  Dedup + History  -->  + Decision      -->  Dashboard
(discover_             Normalization             (identity-tiered      (score_job.py)      (dashboard.py,
 companies.py)          (discover_jobs.py)        dedup, first/           role family,        read-only)
                                                    last_seen_at)          career level,
                                                                           score, AUTO_APPLY/
                                                                           REVIEW/SKIP)
```

Each stage is a small, single-purpose script; state hands off between them
through `data/*.json`, never through shared in-memory objects.

## Directory structure

```
JobAgent/
├── CLAUDE.md                 Guidance for Claude Code working in this repo
├── README.md                  This file
│
├── profile/                    Candidate's own information, kept up to date by hand
│   ├── resume/                  Source resume file (gitignored — personal)
│   ├── profile.md                Contact info, target roles, experience, education
│   ├── skills.md                  Skills inventory & certifications
│   └── preferences.md              Location/salary/industry/work-mode preferences
│
├── jobs/                        A posting moves between these 4 folders as its status changes
│   ├── discovered/                Newly found, not yet reviewed
│   ├── shortlisted/                AUTO_APPLY or REVIEW -- worth a look (auto-written)
│   ├── rejected/                    Passed on, or an explicit employer rejection
│   └── applied/                      Already applied to
│
├── applications/                Generated application material (per-application, not per-job)
│   ├── resumes/ · cover_letters/ · answers/ · pending/
│
├── data/                        Structured, machine-readable source of truth
│   ├── jobs.json                  All tracked postings (the schema below)
│   ├── applications.json           Tracked applications & status
│   ├── candidate_profile.json      Structured mirror of profile/*.md
│   ├── discovery_sources.json      ATS companies discovery actually queries (verified)
│   └── discovery_candidates.json   Unverified seed list -- only discover_companies.py reads it
│
├── scripts/
│   ├── discover_companies.py      Stage 0 -- verify & promote new ATS company boards
│   ├── discover_jobs.py           Stage 1+2 -- fetch, normalize, dedupe, persist, score
│   └── score_job.py                Stage 3 -- the V2 matching/decision engine (STABLE, see CLAUDE.md)
│
├── dashboard.py                 Local read-only HTTP server + JSON API over data/jobs.json
├── dashboard/index.html          The dashboard's single-page UI
│
├── tests/                        One test file per script, plain unittest, no network calls
└── docs/                          Generated reference docs (algorithm, structure)
```

## Discovery sources

Public, unauthenticated ATS/career-site APIs only:

| Connector | How | Status |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` -- one request, full board | 33 companies active |
| Lever | `api.lever.co/v0/postings/{slug}` -- one request, full board | 3 companies active |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}` -- one request, full board | 16 companies active |
| Workday | `cxs` career-site API -- paginated search + per-job detail fetch | 1 company (Accenture) |
| SmartRecruiters | Paginated postings API | Implemented, 0 companies (see limitations) |

`scripts/discover_companies.py` grows the Greenhouse/Lever/Ashby list: it
takes a curated candidate list (`data/discovery_candidates.json`), makes one
real request per candidate to confirm the board exists, and only promotes a
candidate into `data/discovery_sources.json` if it's both real and currently
carries a relevant-looking opening. LinkedIn, Naukri, Indeed, and Wellfound
are deliberately excluded — see `_unavailable_sources` in
`data/discovery_sources.json` for the specific access-control reason each
was ruled out.

## Normalized job schema

Every connector converges on one schema before anything downstream sees it:

```
id, company, title, location, work_mode, industry, company_size,
salary_min/max/currency, min/max_experience_years,
required_skills[], preferred_skills[], description,
application_url, source_url, source_job_id, source,
posted_date, date_discovered, discovered_at,
first_seen_at, last_seen_at,                     # Stage 2
score, role_family, career_level,
application_decision, decision_reason, score_breakdown,
relevant_evidence, transferable_evidence, missing_requirements,
experience_gaps, risks, status
```

A field the source genuinely doesn't expose stays `null`/empty — never
inferred or guessed.

## Deduplication strategy

A confidence-tiered identity check, any one tier matching is enough:

1. exact `(source, source_job_id)` match,
2. same canonical application URL (tracking params/host-case/trailing-slash
   normalized away),
3. `(company, title, location, sorted(required_skills))` — deliberately does
   **not** collapse two same-titled postings at the same company/location if
   their required skills differ (two real, separate reqs).

Re-discovering an existing job only refreshes `last_seen_at`; `first_seen_at`
is set once and never touched again. Full detail in
`docs/JobAgent_Algorithm_Reference.pdf`, Section 4.

## V2 matching overview

`scripts/score_job.py` classifies each posting's role family (Product /
Business Analysis / Project-Program / etc.) and career level from its title,
then scores nine weighted components (role family, capability relevance,
evidence, title alignment, experience fit, career-level fit, location, industry,
salary — summing to 100). It is a **personal** matcher, not general-purpose:
practical/project evidence is tracked separately from professional
(paid-work) evidence and is never scored as if it were the latter.

## AUTO_APPLY / REVIEW / SKIP

The numeric score and the application decision are computed independently.
A senior-level title, a role-family mismatch, a >2-year experience gap, or a
critical missing requirement (avoided industry, below-minimum salary, near-
total skills gap) forces **SKIP** regardless of score. Only a posting that
clears every gate **and** scores ≥85 **and** reads entry/junior level **and**
has ≤1 year experience gap becomes **AUTO_APPLY**; everything else that
clears the SKIP gates is **REVIEW**. See `docs/JobAgent_Algorithm_Reference.pdf`
Section 6 for the exact gate order.

## Dashboard

```
python dashboard.py                  # http://127.0.0.1:8420, opens a browser tab
python dashboard.py --port 8421 --no-browser
```

Read-only: it never calls an LLM, never re-scores, never re-runs discovery,
and never writes to `data/jobs.json`. Defaults to Today's Jobs; toggle to
All Jobs, search by title/company/description/skills, filter by decision/
role family/location/source/minimum score, sort by score/newest/company/
title, and open a job's full detail panel (every schema field, "Not
provided" for anything missing).

## Running discovery

```
python scripts/discover_jobs.py                                    # full run, writes data/jobs.json
python scripts/discover_jobs.py --dry-run                           # discover + score, write nothing
python scripts/discover_companies.py                                 # dry-run report of new candidate boards
python scripts/discover_companies.py --apply                          # promote verified+relevant ones
```

Both scripts expose `--request-delay`, `--request-timeout`, and per-source
job/page/company caps — see each script's `--help` for the full list. Safe
to run daily: re-running is idempotent (proven live — see Phase 5 report),
never duplicates a job, and only refreshes `last_seen_at` on unchanged ones.

## Running tests

```
python tests/score_job.py           # 67 tests -- the V2 matching engine
python tests/discover_jobs.py       # 58 tests -- fetch/normalize/dedupe/persist
python tests/discover_companies.py  # 23 tests -- company verification
python tests/dashboard.py           # 33 tests -- filter/sort/payload logic
```

Plain `unittest`, no test runner installed, no network calls in any suite
(connectors are tested against fixture payloads).

## How the static candidate profile works

**Current**: one static candidate, defined by hand in `profile/*.md` and
mirrored into `data/candidate_profile.json`. Every discovery/matching
function takes this as a plain `candidate: dict` parameter — nothing in
`scripts/` hardcodes the candidate's name, titles, or preferences.

**Future**: a public platform where many candidates each submit their own
profile (education, experience, projects, preferences) and get matched
against a shared job universe — conceptually `Candidate → Candidate Profile
→ Job Matcher → Personalized Jobs`. Not implemented yet; the current
architecture is written so that swapping the static profile for a real
`Candidate` object later shouldn't require rewriting the discovery or
matching layers.

## Important limitations

- SmartRecruiters: connector built and tested, but no configured company
  currently has its public postings feed enabled (platform default is off).
- Lever: 3 active companies; further Lever company discovery hasn't found
  additional working boards yet.
- Single static candidate only — no accounts, no auth, no multi-user support.
- No resume tailoring, cover-letter generation, or application submission —
  those are explicitly future phases, not built here.
