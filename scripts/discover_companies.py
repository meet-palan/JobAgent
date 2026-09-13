"""Discover and verify additional Greenhouse/Lever/Ashby company job boards.

This is the "company discovery" half of expanding coverage, kept separate from
scripts/discover_jobs.py (which discovers/scores individual JOB POSTINGS from
an already-trusted company list). This script instead grows that trusted list:
it takes a maintained, curated seed file of CANDIDATE company identifiers
(data/discovery_candidates.json -- real companies guessed to run their careers
page on a given ATS), and for each one makes a single, real, public,
unauthenticated HTTP request to that ATS's job-board API to confirm the board
actually exists before it is ever considered "active."

A candidate is never added to data/discovery_sources.json on a guess -- only
after a live request succeeds (HTTP 200, parseable job list). A candidate
whose board 404s, times out, or errors is reported as unavailable/not-found
and dropped; it is never treated as verified. This mirrors the existing
guarantee already documented at the top of data/discovery_sources.json
("Every company below was live-verified... before being added").

Beyond existence, each verified board is also checked for search relevance:
does it currently list at least one posting matching the candidate's target
role families (Product / Business Analysis / Project-Program, expanded the
same way scripts/discover_jobs.py already does via expand_target_titles /
title_is_relevant -- reused here, not reimplemented)? A company can run a
perfectly real Greenhouse/Lever/Ashby board and simply have zero relevant
openings right now (e.g. an infra company only hiring engineers); such a
company is reported but not promoted, so the maintained list stays useful
rather than bloating with permanently-irrelevant boards. This is a
company-selection heuristic only -- it never touches job-level filtering or
scoring, both of which remain exactly as scripts/discover_jobs.py and
scripts/score_job.py already implement them.

Usage:
    python scripts/discover_companies.py                  # report only, no writes
    python scripts/discover_companies.py --apply           # also merge newly
                                                             # verified+relevant
                                                             # companies into
                                                             # data/discovery_sources.json
    python scripts/discover_companies.py --max-companies-per-source 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_jobs as dj  # noqa: E402 -- reuse fetch_*, title relevance, exceptions

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANDIDATES_PATH = REPO_ROOT / "data" / "discovery_candidates.json"
DEFAULT_SOURCES_PATH = REPO_ROOT / "data" / "discovery_sources.json"
DEFAULT_PROFILE_PATH = REPO_ROOT / "data" / "candidate_profile.json"

DEFAULT_MAX_COMPANIES_PER_SOURCE = 60
DEFAULT_MAX_PAGES_PER_COMPANY = 1  # Greenhouse/Lever/Ashby's public list endpoints
# are not paginated -- each returns the company's complete board in one
# response (confirmed against the live APIs). This constant exists so the
# config shape matches Workday's (which does paginate, unchanged in
# discover_jobs.py) and so a future paginated connector has a home for it --
# it is not silently ignored, verify_* below asserts exactly one request.
# Reuse discover_jobs.py's request-politeness defaults rather than redefining
# the same numbers here -- one company-verification request per candidate
# should behave identically to a job-fetch request against the same ATS.
DEFAULT_REQUEST_DELAY_SECONDS = dj.DEFAULT_REQUEST_DELAY_SECONDS
DEFAULT_REQUEST_TIMEOUT_SECONDS = dj.DEFAULT_REQUEST_TIMEOUT_SECONDS

SOURCE_FIELD = {
    "greenhouse": "title",
    "lever": "text",
    "ashby": "title",
}


def verify_greenhouse(token: str, timeout: int) -> dict[str, Any]:
    try:
        postings = dj.fetch_greenhouse_jobs(token, timeout=timeout)
    except dj.SOURCE_UNAVAILABLE_EXCEPTIONS as e:
        return {"exists": False, "error": str(e), "postings": []}
    if not isinstance(postings, list):
        return {"exists": False, "error": "unexpected response shape", "postings": []}
    return {"exists": True, "error": None, "postings": postings}


def verify_lever(slug: str, timeout: int) -> dict[str, Any]:
    try:
        postings = dj.fetch_lever_jobs(slug, timeout=timeout)
    except dj.SOURCE_UNAVAILABLE_EXCEPTIONS as e:
        return {"exists": False, "error": str(e), "postings": []}
    if not isinstance(postings, list):
        return {"exists": False, "error": "unexpected response shape", "postings": []}
    return {"exists": True, "error": None, "postings": postings}


def verify_ashby(board: str, timeout: int) -> dict[str, Any]:
    try:
        postings = dj.fetch_ashby_jobs(board, timeout=timeout)
    except dj.SOURCE_UNAVAILABLE_EXCEPTIONS as e:
        return {"exists": False, "error": str(e), "postings": []}
    if not isinstance(postings, list):
        return {"exists": False, "error": "unexpected response shape", "postings": []}
    return {"exists": True, "error": None, "postings": postings}


VERIFIERS = {"greenhouse": verify_greenhouse, "lever": verify_lever, "ashby": verify_ashby}


def count_relevant(postings: list[dict], title_field: str, keywords: list[str]) -> int:
    return sum(1 for p in postings if dj.title_is_relevant(p.get(title_field) or "", keywords))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        content = f.read().strip()
    return json.loads(content) if content else default


def already_active(sources: dict, connector: str, identifier: str) -> bool:
    key = {"greenhouse": "greenhouse_boards", "lever": "lever_companies", "ashby": "ashby_boards"}[connector]
    return identifier in sources.get(key, [])


def check_connector(
    connector: str, candidates: list[str], sources: dict, keywords: list[str], config: dict,
) -> dict[str, Any]:
    result = {
        "connector": connector,
        "candidates_total": len(candidates),
        "already_active": 0,
        "checked": 0,
        "verified_relevant": [],
        "verified_no_relevant_match": [],
        "not_found_or_unavailable": [],
    }
    verifier = VERIFIERS[connector]
    field = SOURCE_FIELD[connector]
    for identifier in candidates:
        if already_active(sources, connector, identifier):
            result["already_active"] += 1
            continue
        if result["checked"] >= config["max_companies_per_source"]:
            break
        outcome = verifier(identifier, config["timeout"])
        result["checked"] += 1
        if not outcome["exists"]:
            result["not_found_or_unavailable"].append({"id": identifier, "reason": outcome["error"]})
        else:
            relevant = count_relevant(outcome["postings"], field, keywords)
            entry = {"id": identifier, "total_jobs": len(outcome["postings"]), "relevant_jobs": relevant}
            if relevant > 0:
                result["verified_relevant"].append(entry)
            else:
                result["verified_no_relevant_match"].append(entry)
        time.sleep(config["delay"])
    return result


def apply_results(sources: dict, results: list[dict[str, Any]]) -> dict:
    key_by_connector = {"greenhouse": "greenhouse_boards", "lever": "lever_companies", "ashby": "ashby_boards"}
    updated = dict(sources)
    for result in results:
        key = key_by_connector[result["connector"]]
        existing = list(updated.get(key, []))
        for entry in result["verified_relevant"]:
            if entry["id"] not in existing:
                existing.append(entry["id"])
        updated[key] = existing
    return updated


def format_report(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append(r["connector"].upper())
        lines.append(f"  Candidates in seed list: {r['candidates_total']} (already active: {r['already_active']})")
        lines.append(f"  Companies queried:       {r['checked']}")
        lines.append(f"  Verified (board exists): {len(r['verified_relevant']) + len(r['verified_no_relevant_match'])}")
        lines.append(f"    - with a relevant opening now: {len(r['verified_relevant'])}")
        lines.append(f"    - board exists, no relevant match right now: {len(r['verified_no_relevant_match'])}")
        lines.append(f"  Not found / unavailable: {len(r['not_found_or_unavailable'])}")
        if r["verified_relevant"]:
            lines.append("  New companies to add (board exists + relevant opening found):")
            for entry in r["verified_relevant"]:
                lines.append(f"    - {entry['id']}  ({entry['relevant_jobs']} relevant / {entry['total_jobs']} total jobs)")
        if r["not_found_or_unavailable"]:
            lines.append("  Not found / unavailable:")
            for entry in r["not_found_or_unavailable"]:
                lines.append(f"    - {entry['id']}: {entry['reason']}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES_PATH)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_PATH)
    parser.add_argument("--max-companies-per-source", type=int, default=DEFAULT_MAX_COMPANIES_PER_SOURCE)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    parser.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--apply", action="store_true", help="Write newly verified+relevant companies into --sources")
    args = parser.parse_args(argv)

    candidates = load_json(args.candidates, {})
    sources = load_json(args.sources, {})
    candidate_profile = load_json(args.profile, {})
    keywords = dj.expand_target_titles(
        candidate_profile.get("target_titles", []), candidate_profile.get("secondary_titles", [])
    )

    config = {
        "max_companies_per_source": args.max_companies_per_source,
        "delay": args.request_delay,
        "timeout": args.request_timeout,
    }

    results = []
    for connector, key in (("greenhouse", "greenhouse_boards"), ("lever", "lever_companies"), ("ashby", "ashby_boards")):
        results.append(check_connector(connector, candidates.get(key, []), sources, keywords, config))

    print(format_report(results))

    if args.apply:
        updated = apply_results(sources, results)
        with args.sources.open("w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2, ensure_ascii=False)
            f.write("\n")
        added = sum(len(r["verified_relevant"]) for r in results)
        print(f"Applied: {added} new companies written to {args.sources}")
    else:
        print("Dry run (no --apply) -- data/discovery_sources.json was not modified.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
