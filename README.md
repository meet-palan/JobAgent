# JobAgent

A personal job-search assistant: it discovers job postings from public ATS
APIs, scores them against a candidate's real profile, and surfaces the good
ones on a local dashboard. Phase 5 (discovery through matching) is
deterministic, dependency-free Python -- no LLM calls there at all. Phase 6
adds an LLM-assisted application-intelligence layer on top, still with no
browser automation and no automated application submission.

## Current phase

**Phase 5 — complete and frozen.** Company discovery, job discovery/
normalization, deduplication with history tracking, the V2 matching engine,
and a read-only dashboard are all implemented, tested (190 tests), and
validated against live data (203 unique jobs at last run). A pre-Phase-6
safety audit tightened the AUTO_APPLY experience gate (see "AUTO_APPLY /
REVIEW / SKIP" below) — see `docs/JobAgent_Algorithm_Reference.pdf` for full
algorithm detail and `docs/JobAgent_Project_Analysis.pdf` for a structural
walkthrough.

**Phase 6 — Application Intelligence — complete and frozen.** Turns an
AUTO_APPLY/REVIEW job into a structured job analysis, a tailored resume, a
cover letter when useful, and application-question answers -- see
"Application Intelligence (Phase 6)" below. Still does not submit anything
anywhere.

**Phase 6.1 — Human Application Review — complete.** A review gate on top of
Phase 6: a human records a quality verdict (APPROVE / NEEDS_CHANGES / REJECT)
on each generated package before anything could ever move toward submission
in a later phase -- see "Human Application Review (Phase 6.1)" below.

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
│   ├── resumes/ · cover_letters/ · answers/
│   ├── pending/<job_id>/          Phase 6 output: analysis/resume/cover_letter/answers/application_package.json
│   └── review/<job_id>/            Phase 6.1 output: review.json (human verdict, gitignored)
│
├── data/                        Structured, machine-readable source of truth
│   ├── jobs.json                  All tracked postings (the schema below)
│   ├── applications.json           Tracked applications & status
│   ├── candidate_profile.json      Structured mirror of profile/*.md
│   ├── discovery_sources.json      ATS companies discovery actually queries (verified)
│   ├── discovery_candidates.json   Unverified seed list -- only discover_companies.py reads it
│   └── application_intelligence/   Phase 6 job-analysis cache, gitignored (keyed per job id)
│
├── scripts/
│   ├── discover_companies.py      Stage 0 -- verify & promote new ATS company boards
│   ├── discover_jobs.py           Stage 1+2 -- fetch, normalize, dedupe, persist, score
│   ├── score_job.py                Stage 3 -- the V2 matching/decision engine (STABLE, see CLAUDE.md)
│   ├── candidate_context.py        Phase 6 -- profile loading, evidence blob, verified facts
│   ├── llm_provider.py              Phase 6 -- LLMProvider interface (ClaudeProvider/MockProvider)
│   ├── prompts.py                    Phase 6 -- versioned prompt templates
│   ├── job_analysis.py                Phase 6 -- the one cached structured job analysis
│   ├── resume_tailoring.py             Phase 6 -- resume content + verbatim-history rendering
│   ├── cover_letter.py                  Phase 6 -- cover letter drafting
│   ├── application_answers.py            Phase 6 -- Q&A, deterministic where possible
│   ├── claim_validation.py                Phase 6 -- deterministic fact-checking of LLM output
│   ├── text_quality.py                     Phase 6.1 -- whitespace-artifact detection/normalization
│   ├── application_package.py               Phase 6 -- package assembly + readiness check
│   ├── run_phase6.py                         Phase 6 -- orchestrator / CLI entry point
│   ├── review.py                              Phase 6.1 -- review record model + workflow
│   └── review_application.py                   Phase 6.1 -- human review CLI
│
├── dashboard.py                 Local read-only HTTP server + JSON API over data/jobs.json
│                                  (+ read-only Phase 6/6.1 status via /api/intelligence,/api/review)
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
has a stated minimum experience that does not exceed the candidate's actual
professional experience becomes **AUTO_APPLY**; everything else that clears
the SKIP gates is **REVIEW**. See `docs/JobAgent_Algorithm_Reference.pdf`
Section 6 for the exact gate order.

**Safety note (post-Phase-5 fix):** the experience gate previously allowed up
to a 1-year gap, which let a candidate with 1 year of professional experience
reach AUTO_APPLY against a job requiring 2 years. Experience *scoring* still
gives partial credit to a slightly underqualified candidate (that's what can
route a job to REVIEW), but that credit can never by itself unlock
AUTO_APPLY — an unstated requirement is still never treated as a rejection,
and this stays fully independent of `score_job.py`'s stable 9-component
scoring, which was not changed.

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
python tests/score_job.py           # 76 tests -- the V2 matching engine
python tests/discover_jobs.py       # 58 tests -- fetch/normalize/dedupe/persist
python tests/discover_companies.py  # 23 tests -- company verification
python tests/dashboard.py           # 43 tests -- filter/sort/payload logic + Phase 6/6.1 status visibility
python tests/phase6.py              # 53 tests -- application intelligence, all LLM calls mocked
python tests/text_quality.py        # 16 tests -- whitespace-artifact detection/normalization
python tests/review.py              # 27 tests -- Phase 6.1 human review workflow
```

Plain `unittest` (filenames don't follow pytest's default discovery pattern
-- use `pytest --import-mode=importlib tests/<name>.py` if you prefer
pytest). No network calls in any suite (connectors are tested against
fixture payloads; Phase 6/6.1 tests use `llm_provider.MockProvider`, never a
live model).

## Application Intelligence (Phase 6)

For each AUTO_APPLY/REVIEW job (SKIP jobs are excluded by default -- no
point spending an LLM call on a job the candidate isn't applying to; a
specific SKIP job can still be run via `--job-id` for debugging),
`scripts/run_phase6.py` produces one structured job analysis, a tailored
resume, a cover letter when useful, and answers to a standard set of
application questions -- never submitting anything anywhere.

```
python scripts/run_phase6.py --max-jobs 3              # process a few AUTO_APPLY/REVIEW jobs
python scripts/run_phase6.py --job-id <id>              # process one specific job (any decision)
python scripts/run_phase6.py --provider mock --mock-response '{...}'   # offline dry run, no API key needed
```

Requires `ANTHROPIC_API_KEY` in the environment for real runs (`--provider
claude`, the default) -- credentials are never hardcoded or written into
generated job data.

**Architecture**: `scripts/candidate_context.py` (profile loading + evidence
blob), `scripts/prompts.py` (versioned prompt templates), `scripts/llm_provider.py`
(provider interface — `ClaudeProvider` / `MockProvider`), `scripts/job_analysis.py`
(the one cached LLM call every other module reuses), `scripts/resume_tailoring.py`,
`scripts/cover_letter.py`, `scripts/application_answers.py`, `scripts/claim_validation.py`
(deterministic fact-checking of generated content), `scripts/application_package.py`
(assembly + readiness check).

**Truthfulness**: employment dates and education are copied verbatim from
`profile.md` -- the LLM never even sees them as editable. Every LLM-drafted
sentence (resume highlights, cover letter, open-ended answers) is checked by
`claim_validation.py` for overclaimed years of experience, unverified
employer names, and unsupported quantified claims before being trusted; a
flagged artifact is marked `NEEDS_REVIEW`, never silently accepted.

**Caching**: an analysis (and the resume/cover-letter/answers built from it)
is cached to `applications/pending/<job_id>/`, keyed by a fingerprint of the
job's id+description, the candidate profile's hash, and the prompt version.
Re-running Phase 6 on an unchanged job makes zero LLM calls; a changed job
description, an edited candidate profile, or a bumped prompt version each
invalidate only the affected job.

**Generation quality (Phase 6.1 fix)**: a real smoke test surfaced two defect
classes -- word-concatenation artifacts (e.g. "requirementgathering") traced
to `ClaudeProvider.complete()` joining multiple response text blocks with an
empty string instead of a space (fixed at the source), and resumes/cover
letters explicitly narrating the candidate's own experience gap. Fixed via
`scripts/text_quality.py` (safe whitespace normalization + a conservative,
documented-as-imperfect artifact detector) and
`claim_validation.check_experience_gap_language()` (a resume may never
mention a gap; a cover letter may acknowledge one at most once, never
repeatedly or defensively) -- both prompts were also updated accordingly.

**Status**: architecture, caching, and 53 tests (all LLM calls mocked) are in
place and passing. Validated twice against real data: first a mocked dry run
(one live REVIEW job plus a constructed AUTO_APPLY fixture), then a **live
run against the real Claude API** with the same two fixtures. The live run
surfaced and fixed two real bugs: (1) `max_tokens` was too low to leave room
for the model's internal reasoning plus a full structured response, causing
truncated/empty output on the larger prompts -- fixed by raising the budget
and detecting `stop_reason: max_tokens` explicitly rather than failing with a
confusing "invalid JSON" error; (2) `claim_validation` flagged the job's own
company name (e.g. "interest in the role at Acme") as a fabricated past
employer -- fixed by exempting the target company from that check. Both
fixes are covered by new tests. The live run's actual generated content
(job analysis, resume, cover letter, answers) was manually inspected and
found truthful -- e.g. one interview-style application ANSWER (not the
resume) proactively disclosed the candidate's real experience gap ("my total
professional experience is 1.0 years, which is below the 3.0+ years this
role requires") rather than glossing over it; that kind of direct disclosure
remains appropriate for a directly-asked question, which is why
`application_answers.py` deliberately does not use the Phase 6.1
gap-language restriction the resume and cover letter now do (see
"Generation quality (Phase 6.1 fix)" above).

## Human Application Review (Phase 6.1)

A mandatory human gate on top of Phase 6, before anything could ever move
toward submission in a later phase. No LLM calls, no browser automation --
it only reads a package Phase 6 already wrote and records a human's verdict.

```
python scripts/review_application.py --list-pending                        # what needs a look
python scripts/review_application.py --job-id <id>                          # show the package + current review
python scripts/review_application.py --job-id <id> --approve --notes "Looks good"
python scripts/review_application.py --job-id <id> --needs-changes --resume-quality 2 \
    --issue "Summary reads too generic" --notes "Regenerate resume"
python scripts/review_application.py --job-id <id> --reject --notes "Not a genuine fit"
```

**Review record** (`applications/review/<job_id>/review.json`): `review_status`
(`PENDING` / `APPROVED` / `NEEDS_CHANGES` / `REJECTED`), six optional 1-5
quality scores (`analysis_quality`, `resume_quality`, `cover_letter_quality`,
`answers_quality`, `truthfulness`, `overall_quality` -- a reviewer is never
forced to score every component), a free-text `issues` list, `needs_regeneration`,
`review_notes`, and `reviewed_at`. `scripts/review.py` validates every field
before writing -- an invalid status or an out-of-range score raises rather
than silently corrupting the file.

**Scope rule**: only jobs Phase 6 actually processes (`AUTO_APPLY`/`REVIEW`,
same rule as `run_phase6.select_jobs()`) that already have a generated
package appear in `--list-pending` -- a `REVIEW` job with no package yet
just hasn't been through Phase 6, and `SKIP` jobs never enter review at all.

**Dashboard**: read-only visibility only (`review_status`, `overall_quality`,
`needs_regeneration` per job, plus a "View Review Notes" panel that fetches
`/api/review/<job_id>`) -- the dashboard never writes a review or calls the
review CLI itself; all mutation happens through the CLI above.

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
- Resumes/cover letters render as `.txt`, not `.pdf` — a deliberate content-
  vs-typesetting simplification; say the word if you want real PDF output.
- `check_unverified_employers`'s proper-noun heuristic is best-effort, not
  exhaustive — it catches obvious fabrications, not every possible one.
- No application submission, browser automation, or CAPTCHA/anti-bot handling
  anywhere — those are explicitly future phases, not built here.
