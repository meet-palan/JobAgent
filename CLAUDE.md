# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A personal job-search and job-application assistant. It is organized around
a simple pipeline: discover jobs → shortlist → apply → track outcome. The
`profile/` folder holds the user's own information; `jobs/` and
`applications/` hold working state; `data/` holds the structured
(machine-readable) version of that state.

## Current state

Scaffolding only. No scraping, no browser automation, no automated
application submission exists yet. Do not add these unless explicitly asked.

## Conventions

- `data/jobs.json` and `data/applications.json` are the sources of truth for
  structured state. Files under `jobs/*/` and `applications/*/` are
  human-readable/working artifacts (notes, drafts, generated documents) tied
  to entries in those JSON files.
- Job postings move between `jobs/discovered/`, `jobs/shortlisted/`,
  `jobs/rejected/`, and `jobs/applied/` as their status changes — treat these
  as the four possible states of a job, not independent categories.
- Generated, tailored application documents live under `applications/`,
  named per-application (not per-job), since one job may get multiple drafts.
- `profile/resume/` and generated resumes/cover letters may contain personal
  information and are gitignored — don't remove them from `.gitignore`
  without being asked.
- Keep scripts under `scripts/` small and single-purpose; add tests for any
  non-trivial script under `tests/`.

## Before adding new capabilities

This repo is intentionally minimal right now. Before adding scraping,
browser automation, or auto-submission features, confirm scope with the
user — these are higher-risk, harder-to-reverse capabilities (they touch
external sites and submit real applications on the user's behalf).
