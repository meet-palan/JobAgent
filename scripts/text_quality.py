"""Deterministic, conservative checks for LLM-generation text ARTIFACTS --
mechanical defects in how output was assembled (missing/repeated whitespace),
never a judgment on writing quality or style. A clean result never certifies
"well written," only "free of the specific mechanical defects this module
knows how to check for."

Two separate concerns, deliberately kept apart:

- normalize_whitespace() is a SAFE auto-fix: it only ever collapses
  whitespace that is already there (repeated spaces/tabs, excess blank
  lines, trailing spaces). It never inserts a character into a word and
  never touches URLs, emails, or punctuation spacing -- there is nothing
  here for it to get wrong.

- find_whitespace_issues() is a best-effort DETECTOR, not a fixer. Missing
  space after terminal punctuation (",;:!?" followed immediately by a
  letter) is checked with reasonably high precision, excluding URLs/emails.
  Missing space between two ordinary lowercase words (e.g.
  "requirementgathering") has NO reliable general solution without a
  dictionary -- English has no structural signal at the boundary. The long
  fused-word check below is intentionally a weak, best-effort heuristic
  (conservative threshold, chosen to avoid flagging real long words like
  "internationalization") and is documented as such rather than presented
  as a complete solution. The real fix for that failure mode is preventing
  it at the source (see llm_provider.ClaudeProvider.complete()'s content-
  block joining) -- this detector exists as a regression canary and a
  second line of defense, not the primary guarantee.
"""

from __future__ import annotations

import re

_URL_OR_EMAIL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b)")


def _mask_safe_spans(text: str) -> str:
    """Replace URL/email spans with same-length placeholder runs so the
    punctuation-spacing check below never fires inside one (a URL's dots and
    an email's @ are not missing-space mistakes)."""
    return _URL_OR_EMAIL_RE.sub(lambda m: "#" * len(m.group(0)), text)


def normalize_whitespace(text: str) -> str:
    """Safe, lossless-to-meaning whitespace cleanup: collapse runs of spaces/
    tabs to one space, collapse 3+ newlines to a single paragraph break,
    strip trailing whitespace per line, strip the whole text. Never inserts
    anything into a word; only ever removes whitespace that was already
    redundant."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


_REPEATED_WHITESPACE_RE = re.compile(r"[ \t]{2,}|\n{3,}")
# A safe-context list so "e.g.available", "Inc.based", "3.5" etc. don't get
# flagged as missing-space mistakes. Matched against the text immediately
# preceding the period being evaluated (not just the last contiguous letter
# run -- "e.g" itself contains a period, so a naive "word right before the
# dot" capture would only see "g", not "e.g").
_ABBREVIATION_TAIL_RE = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|mr|mrs|ms|dr|inc|ltd|llp|llc|corp|st|jr|sr)\.$", re.I
)
_MISSING_SPACE_AFTER_PUNCT_RE = re.compile(r"[A-Za-z][,:;!?][A-Za-z]")
_MISSING_SPACE_AFTER_PERIOD_RE = re.compile(r"[A-Za-z]+\.([A-Z][a-z])")
_LONG_FUSED_WORD_RE = re.compile(r"\b[a-z]{22,}\b")


def find_whitespace_issues(text: str) -> list[str]:
    """Return a list of human-readable descriptions of suspected whitespace
    artifacts, or an empty list if none are found. See module docstring for
    what this does and does not reliably catch."""
    issues: list[str] = []

    for m in _REPEATED_WHITESPACE_RE.finditer(text):
        snippet = text[max(0, m.start() - 15):m.end() + 15]
        issues.append(f"Repeated whitespace near: {snippet!r}")

    masked = _mask_safe_spans(text)

    for m in _MISSING_SPACE_AFTER_PUNCT_RE.finditer(masked):
        snippet = text[max(0, m.start() - 15):m.end() + 15]
        issues.append(f"Missing space after punctuation near: {snippet!r}")

    for m in _MISSING_SPACE_AFTER_PERIOD_RE.finditer(masked):
        preceding = masked[:m.start(1)]
        if _ABBREVIATION_TAIL_RE.search(preceding):
            continue
        snippet = text[max(0, m.start() - 15):m.end() + 15]
        issues.append(f"Missing space after period near: {snippet!r}")

    for m in _LONG_FUSED_WORD_RE.finditer(text):
        issues.append(
            f"Unusually long unbroken lowercase word (possible missing space, best-effort check): {m.group(0)!r}"
        )

    return issues
