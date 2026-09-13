"""Lightweight local, read-only dashboard for data/jobs.json.

Serves one static HTML/JS page (dashboard/index.html) plus a tiny JSON API
(/api/jobs) that filters and sorts jobs already scored by
scripts/score_job.py via scripts/discover_jobs.py. Reads data/jobs.json
fresh on every request -- no caching, no second database.

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


def build_payload(jobs: list[dict], **filter_kwargs) -> dict:
    """Build the full /api/jobs response: today/all/decision counts (always
    computed over the FULL unfiltered `jobs`, so the stat tiles never shift
    just because a filter is active), the role-family/source dropdown
    options, and the actually filtered+sorted job list. Every field on each
    job dict is passed through unchanged -- this never projects a job down
    to a summary shape, so the dashboard's detail panel always has everything."""
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
    return {"today": today, "counts": counts, "role_families": role_families, "sources": sources, "jobs": filtered}


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
