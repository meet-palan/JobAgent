"""LLM provider abstraction for Phase 6 (Application Intelligence).

Phase 5 (discovery/normalization/dedup/matching) is deliberately LLM-free.
Phase 6 is the first place in this codebase that calls a language model, and
only where semantic reasoning earns its cost: understanding a job posting,
positioning the candidate truthfully, drafting resume/cover-letter/answer
text. Everything upstream of Phase 6 (which jobs even reach this module) is
still decided by scripts/score_job.py alone -- see job_analysis.py.

The rest of the intelligence layer depends on the LLMProvider interface
below, never on a specific vendor SDK -- swapping providers, or running
fully offline in tests, never touches job_analysis.py/resume_tailoring.py/etc.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any


class LLMProviderError(Exception):
    """Raised for any provider failure: missing credentials, network error,
    malformed response. Callers must treat this as a FAILED state, never as
    an empty-but-valid result -- see job_analysis.analyze_job()'s error
    handling, which never marks a job COMPLETED on this exception."""


class LLMProvider(ABC):
    @abstractmethod
    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        """Return the model's plain-text completion, or raise LLMProviderError."""
        raise NotImplementedError


class ClaudeProvider(LLMProvider):
    """Calls the real Claude API. Requires the `anthropic` package and an
    ANTHROPIC_API_KEY -- credentials are read from the environment only,
    never hardcoded or persisted into job data."""

    def __init__(self, model: str = "claude-sonnet-5", api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        if not self.api_key:
            raise LLMProviderError(
                "ANTHROPIC_API_KEY is not set -- cannot call Claude. "
                "Set the environment variable or pass api_key= explicitly."
            )
        try:
            import anthropic
        except ImportError as e:
            raise LLMProviderError("The 'anthropic' package is not installed (pip install anthropic).") from e

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            response = client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            # Some models spend part of max_tokens on internal reasoning
            # ("thinking" content blocks) before producing the actual text --
            # if that leaves no room to finish, the API reports
            # stop_reason="max_tokens" with a truncated (possibly empty)
            # text block. A truncated response is never usable JSON, so this
            # is raised explicitly here as a clear, actionable FAILED reason
            # rather than surfacing downstream as a confusing "invalid JSON"
            # error once job_analysis.py/etc. try to parse it.
            if response.stop_reason == "max_tokens":
                raise LLMProviderError(
                    f"Response was truncated at max_tokens={max_tokens} before completing "
                    f"(stop_reason=max_tokens) -- increase max_tokens for this call."
                )
            # Join with a space, not "": a response can legitimately contain
            # more than one "text" content block (e.g. interleaved with
            # "thinking" blocks), and joining them with "" glues the end of
            # one directly onto the start of the next with zero separator --
            # the confirmed cause of word-concatenation artifacts observed
            # live ("requirementgathering", "Icurrently", etc., always at
            # exactly one point in the text: a block boundary). A stray
            # extra space at a boundary that already had trailing/leading
            # whitespace is harmless and cleaned up by
            # text_quality.normalize_whitespace() downstream; losing a real
            # word boundary is not recoverable after the fact.
            text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
            return " ".join(text_blocks)
        except LLMProviderError:
            raise
        except Exception as e:  # noqa: BLE001 -- any SDK/network failure becomes one clear error type
            raise LLMProviderError(f"Claude API call failed: {e}") from e


def parse_json_response(text: str) -> Any:
    """Best-effort JSON parse of an LLM response: strips a leading/trailing
    markdown code fence (```json ... ``` or ``` ... ```) if present -- a
    common way models wrap structured output despite being asked for JSON
    only -- then parses. Still raises json.JSONDecodeError on genuinely
    malformed or truncated output; every caller already treats that as a
    FAILED state (see job_analysis.py, resume_tailoring.py, cover_letter.py,
    application_answers.py), this only avoids failing on well-formed JSON
    that's merely fenced.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\s*$", "", stripped)
    return json.loads(stripped)


class MockProvider(LLMProvider):
    """Deterministic, no-network provider for tests and offline dry-runs.

    Pass `response` for a fixed reply to every call, or `responses` (a list)
    to return a different canned reply per call in order. Pass `fail=True`
    to simulate a provider failure. Every call is recorded in `.calls` so
    tests can assert on what was actually asked (e.g. that a prompt embeds
    only verified candidate facts).
    """

    def __init__(self, response: str | None = None, responses: list[str] | None = None, fail: bool = False):
        self.response = response
        self.responses = list(responses) if responses else None
        self.fail = fail
        self.calls: list[dict] = []

    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        if self.fail:
            raise LLMProviderError("MockProvider configured to fail")
        if self.responses is not None:
            if not self.responses:
                raise LLMProviderError("MockProvider ran out of queued responses")
            return self.responses.pop(0)
        if self.response is not None:
            return self.response
        raise LLMProviderError("MockProvider has no response configured")
