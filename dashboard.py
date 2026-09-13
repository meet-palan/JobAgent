"""Lightweight local, read-only dashboard for data/jobs.json.

Serves one static HTML/JS page (dashboard/index.html) plus a tiny JSON API
(/api/jobs) that filters and sorts jobs already scored by
scripts/score_job.py via scripts/discover_jobs.py. Reads data/jobs.json
fresh on every request -- no caching, no second database. It also exposes
/api/intelligence/<job_id>, a read-only view of whatever Phase 6
(scripts/run_phase6.py) has already written to applications/pending/<job_id>/,
and /api/review/<job_id>, a read-only view of whatever a human reviewer has
recorded via scripts/review_application.py (Phase 6.1) to applications/review/<job_id>/
-- both routes only read a file that may or may not exist; neither invokes
Phase 6, an LLM, or the review CLI itself.

This is display-only: it never calls an LLM, never re-runs the matching
engine, never re-runs discovery, and never writes to data/jobs.json.

Usage:
    python dashboard.py
    python dashboard.py --port 8420 --no-browser
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent
JOBS_PATH = REPO_ROOT / "data" / "jobs.json"
STATIC_DIR = REPO_ROOT / "dashboard"
PENDING_DIR = REPO_ROOT / "applications" / "pending"
REVIEW_DIR = REPO_ROOT / "applications" / "review"
DEFAULT_PORT = 8420


# --------------------------------------------------------------------------
# Pure data functions -- these are what tests/dashboard.py exercises. No
# network, no file writes, no matching logic: just reading and filtering
# the job schema scripts/discover_jobs.py already produces.
# --------------------------------------------------------------------------

def today_str() -> str:
    """Actual current local date, in the same YYYY-MM-DD form discover_jobs.py
    stamps discovered_at/last_seen_at with. Never hardcoded/assumed."""
    return date.today().isoformat()


def load_intelligence_status(job_id: str, pending_dir: Path = PENDING_DIR) -> str:
    """Read-only visibility into Phase 6 (scripts/run_phase6.py), which
    writes applications/pending/<job_id>/application_package.json. This is a
    plain file check -- it never triggers Phase 6, never calls an LLM, and
    never writes anything; a job that hasn't been processed yet simply has
    no such file. Mirrors application_package.py's own status vocabulary so
    the dashboard and the intelligence layer never drift into two different
    sets of state names."""
    path = pending_dir / job_id / "application_package.json"
    if not path.exists():
        return "NOT_PROCESSED"
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "NOT_PROCESSED"
    if package.get("ready_for_browser_automation"):
        return "READY_FOR_AUTOMATION"
    if package.get("requires_human_review"):
        return "READY_FOR_REVIEW"
    for status in (package.get("analysis_status"), package.get("resume_status"), package.get("cover_letter_status")):
        if status == "FAILED":
            return "VALIDATION_FAILED"
        if status == "NEEDS_REVIEW":
            return "NEEDS_USER_INPUT"
    return "PROCESSING"


def load_application_package(job_id: str, pending_dir: Path = PENDING_DIR) -> dict | None:
    """Full read-only package content for the detail-panel 'View Application
    Package' action -- same file load_intelligence_status summarizes."""
    path = pending_dir / job_id / "application_package.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_review(job_id: str, review_dir: Path = REVIEW_DIR) -> dict | None:
    """Full read-only Phase 6.1 review record, if a human has ever reviewed
    this job -- written by scripts/review_application.py, never by the
    dashboard. Returns None (not an error) for a job nobody has reviewed
    yet; that is the normal, expected state for anything still PENDING."""
    path = review_dir / job_id / "review.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_review_status(job_id: str, review_dir: Path = REVIEW_DIR) -> str:
    """PENDING/APPROVED/NEEDS_CHANGES/REJECTED, or "NOT_REVIEWED" if no
    review record exists at all yet (distinct from an explicit PENDING
    record a reviewer has started but not finished)."""
    review = load_review(job_id, review_dir)
    if review is None:
        return "NOT_REVIEWED"
    return review.get("review_status", "NOT_REVIEWED")


def load_jobs(path: Path = JOBS_PATH) -> list[dict]:
    """Read data/jobs.json fresh. Tolerant, never crashes:
    - a missing file, empty file, or invalid JSON returns [].
    - the top-level value must be a list; anything else returns [].
    - a malformed entry (not a dict -- e.g. a stray string or null) is
      dropped rather than crashing the whole read.
    A dict entry with missing/sparse fields is NOT considered malformed --
    it's kept as-is; the UI shows "Not provided" for whatever is missing.
    """
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8").strip()
        data = json.loads(content) if content else []
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [j for j in data if isinstance(j, dict)]


def is_today_job(job: dict, today: str) -> bool:
    """A job counts as "today" if it was last_seen or first discovered
    today. Missing fields simply don't match -- never assumed."""
    return job.get("last_seen_at") == today or job.get("discovered_at") == today


def matches_filters(
    job: dict, decision: str | None = None, role_family: str | None = None,
    location: str | None = None, source: str | None = None,
    min_score: float | None = None, search: str | None = None,
) -> bool:
    """True if `job` satisfies every given filter (an unset/"All" filter
    always passes). `search` matches title, company, description, and both
    skill lists -- a job with no score never satisfies a min_score filter,
    since an unknown score can't be shown to meet a stated minimum."""
    if decision and decision != "All" and job.get("application_decision") != decision:
        return False
    if role_family and role_family != "All" and job.get("role_family") != role_family:
        return False
    if location and location.lower() not in (job.get("location") or "").lower():
        return False
    if source and source.lower() not in (job.get("source") or "").lower():
        return False
    if min_score is not None:
        score = job.get("score")
        if score is None or score < min_score:
            return False
    if search:
        skills = [*(job.get("required_skills") or []), *(job.get("preferred_skills") or [])]
        text_fields = " ".join(str(job.get(f) or "") for f in ("title", "company", "description"))
        haystack = (text_fields + " " + " ".join(skills)).lower()
        if search.lower() not in haystack:
            return False
    return True


SORT_KEYS = {
    "score": lambda j: j.get("score") if j.get("score") is not None else -1,
    "newest": lambda j: (j.get("last_seen_at") or "", j.get("discovered_at") or "", j.get("first_seen_at") or ""),
    "company": lambda j: (j.get("company") or "").lower(),
    "title": lambda j: (j.get("title") or "").lower(),
}
SORT_DESCENDING = {"score", "newest"}


def filter_and_sort(
    jobs: list[dict], *, view: str = "all", decision: str | None = None, role_family: str | None = None,
    location: str | None = None, source: str | None = None, min_score: float | None = None,
    search: str | None = None, sort: str = "score", today: str | None = None,
) -> list[dict]:
    """Apply the Today/All view, every matches_filters() filter, and one of
    SORT_KEYS, in that order. `today` is injectable for tests; the running
    dashboard always uses the real current date via today_str()."""
    today = today or today_str()
    result = jobs
    if view == "today":
        result = [j for j in result if is_today_job(j, today)]
    result = [j for j in result if matches_filters(j, decision, role_family, location, source, min_score, search)]
    key_fn = SORT_KEYS.get(sort, SORT_KEYS["score"])
    return sorted(result, key=key_fn, reverse=(sort in SORT_DESCENDING))


def build_payload(
    jobs: list[dict], *, pending_dir: Path = PENDING_DIR, review_dir: Path = REVIEW_DIR, **filter_kwargs
) -> dict:
    """Build the full /api/jobs response: today/all/decision counts (always
    computed over the FULL unfiltered `jobs`, so the stat tiles never shift
    just because a filter is active), the role-family/source dropdown
    options, and the actually filtered+sorted job list. Every field on each
    job dict is passed through unchanged -- this never projects a job down
    to a summary shape, so the dashboard's detail panel always has everything.

    Each returned job also gets `intelligence_status` (Phase 6 visibility --
    see load_intelligence_status()) and `review_status`/`review_overall_quality`/
    `review_needs_regeneration` (Phase 6.1 visibility -- see load_review())
    added via a shallow copy, so the original job dicts from data/jobs.json
    are never mutated."""
    today = today_str()
    counts = {
        "today": sum(1 for j in jobs if is_today_job(j, today)),
        "all": len(jobs),
        "AUTO_APPLY": sum(1 for j in jobs if j.get("application_decision") == "AUTO_APPLY"),
        "REVIEW": sum(1 for j in jobs if j.get("application_decision") == "REVIEW"),
        "SKIP": sum(1 for j in jobs if j.get("application_decision") == "SKIP"),
    }
    role_families = sorted({j["role_family"] for j in jobs if j.get("role_family")})
    sources = sorted({j["source"] for j in jobs if j.get("source")})
    filtered = filter_and_sort(jobs, today=today, **filter_kwargs)

    def annotate(job: dict) -> dict:
        if not job.get("id"):
            return dict(job)
        review = load_review(job["id"], review_dir)
        return {
            **job,
            "intelligence_status": load_intelligence_status(job["id"], pending_dir),
            "review_status": (review or {}).get("review_status", "NOT_REVIEWED"),
            "review_overall_quality": (review or {}).get("overall_quality"),
            "review_needs_regeneration": (review or {}).get("needs_regeneration", False),
        }

    annotated = [annotate(j) for j in filtered]
    return {"today": today, "counts": counts, "role_families": role_families, "sources": sources, "jobs": annotated}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002 -- keep console quiet
        pass

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/jobs":
            qs = parse_qs(parsed.query)

            def one(name: str, default: str | None = None) -> str | None:
                values = qs.get(name)
                return values[0] if values else default

            min_score_raw = one("min_score")
            min_score = float(min_score_raw) if min_score_raw not in (None, "") else None
            jobs = load_jobs()
            payload = build_payload(
                jobs, view=one("view", "all"), decision=one("decision"), role_family=one("role_family"),
                location=one("location"), source=one("source"), min_score=min_score,
                search=one("search"), sort=one("sort", "score"),
            )
            self._send_json(payload)
            return
        if parsed.path.startswith("/api/intelligence/"):
            job_id = parsed.path[len("/api/intelligence/"):]
            package = load_application_package(job_id)
            if package is None:
                self.send_error(404, "No Phase 6 application package for this job yet")
                return
            self._send_json(package)
            return
        if parsed.path.startswith("/api/review/"):
            job_id = parsed.path[len("/api/review/"):]
            review = load_review(job_id)
            if review is None:
                self.send_error(404, "No Phase 6.1 review record for this job yet")
                return
            self._send_json(review)
            return
        self.send_error(404, "Not found")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"JobAgent dashboard running at {url}")
    print(f"Reading jobs from {JOBS_PATH}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
