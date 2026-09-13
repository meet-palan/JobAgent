# JobAgent

A personal job-search and job-application assistant. It keeps track of your
profile, the jobs you discover, the applications you submit, and the
supporting documents (resumes, cover letters, answers to application
questions) generated along the way.

## Status

Early scaffolding only. There is no job scraping, browser automation, or
application-submission logic yet — this repository currently just defines
the folder structure and data files the agent will use once that logic is
built.

## Structure

```
JobAgent/
├── CLAUDE.md              Guidance for Claude Code when working in this repo
├── README.md               This file
├── .gitignore
│
├── profile/                 Your information, kept up to date by hand
│   ├── resume/               Source resume file(s) (gitignored — personal)
│   ├── profile.md            Basic profile: name, contact, target roles
│   ├── skills.md              Skills inventory
│   └── preferences.md         Job search preferences (location, salary, etc.)
│
├── jobs/                     Jobs move through these stages as folders of notes
│   ├── discovered/            Newly found postings, not yet reviewed
│   ├── shortlisted/           Postings worth applying to
│   ├── rejected/               Postings passed on or that rejected you
│   └── applied/                 Postings you've applied to
│
├── applications/             Generated application material
│   ├── resumes/                Tailored resume versions per application
│   ├── cover_letters/           Tailored cover letters per application
│   ├── answers/                  Saved answers to recurring application questions
│   └── pending/                   Applications drafted but not yet submitted
│
├── data/                      Structured data the agent reads/writes
│   ├── jobs.json                Tracked job postings
│   └── applications.json         Tracked applications and their status
│
├── scripts/                  Automation scripts (none yet)
├── tests/                     Tests for scripts/agent logic (none yet)
└── docs/                      Additional documentation
```

## Next steps

- Fill in `profile/profile.md`, `profile/skills.md`, and `profile/preferences.md`.
- Drop your resume into `profile/resume/`.
- Decide on job sources and build discovery scripts under `scripts/`.
