"""Tests for scripts/text_quality.py -- deterministic whitespace-artifact
detection and safe normalization. See that module's docstring for exactly
what find_whitespace_issues() does and does not reliably catch (pure
alphabetic word concatenation below ~22 characters has no general solution
without a dictionary -- documented there and in the Phase 6.1 report, not
hidden).

Run directly:
    python tests/text_quality.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import text_quality as tq  # noqa: E402


class NormalizeWhitespaceTestCase(unittest.TestCase):
    def test_collapses_repeated_spaces(self):
        self.assertEqual(tq.normalize_whitespace("hello   world"), "hello world")

    def test_collapses_excess_blank_lines(self):
        self.assertEqual(tq.normalize_whitespace("para one\n\n\n\npara two"), "para one\n\npara two")

    def test_strips_trailing_whitespace_per_line_and_overall(self):
        self.assertEqual(tq.normalize_whitespace("hello world  \nnext line  "), "hello world\nnext line")

    def test_never_touches_a_single_correctly_spaced_sentence(self):
        text = "I led requirement gathering and stakeholder alignment at Tibicle LLP."
        self.assertEqual(tq.normalize_whitespace(text), text)

    def test_never_inserts_characters_into_words(self):
        # normalize_whitespace only ever removes whitespace -- it must never
        # add anything, so an already-concatenated word stays exactly as-is
        # (fixing concatenation is not this function's job -- see module docstring).
        text = "requirementgathering"
        self.assertEqual(tq.normalize_whitespace(text), text)


class FindWhitespaceIssuesTestCase(unittest.TestCase):
    def test_detects_repeated_whitespace(self):
        issues = tq.find_whitespace_issues("This  has   double spaces.")
        self.assertTrue(any("Repeated whitespace" in i for i in issues))

    def test_detects_missing_space_after_colon(self):
        # The exact artifact observed live: "Note:candidate's"
        issues = tq.find_whitespace_issues("Note:candidate's strengths are clear.")
        self.assertTrue(any("Missing space after punctuation" in i for i in issues))

    def test_detects_missing_space_after_period_between_sentences(self):
        issues = tq.find_whitespace_issues("Delivered on time.Next quarter looks strong.")
        self.assertTrue(any("Missing space after period" in i for i in issues))

    def test_does_not_flag_known_abbreviations_before_period(self):
        issues = tq.find_whitespace_issues("Career Essentials in Business Analysis, e.g.Coursework from Microsoft.")
        self.assertEqual(issues, [])

    def test_does_not_flag_urls(self):
        issues = tq.find_whitespace_issues("Visit https://example.com/Portfolio for more, thanks.")
        self.assertEqual(issues, [])

    def test_does_not_flag_emails(self):
        issues = tq.find_whitespace_issues("Contact meet.palan@example.com for details, please.")
        self.assertEqual(issues, [])

    def test_does_not_flag_decimal_numbers(self):
        issues = tq.find_whitespace_issues("Graduated with a CGPA of 9.44, among other achievements.")
        self.assertEqual(issues, [])

    def test_does_not_flag_normal_long_real_words(self):
        issues = tq.find_whitespace_issues("Familiar with internationalization and characterization of data.")
        self.assertEqual(issues, [])

    def test_detects_a_long_fused_word_as_best_effort(self):
        issues = tq.find_whitespace_issues("I focused on leadingcommunicationacrossteams during the rollout.")
        self.assertTrue(any("Unusually long unbroken" in i for i in issues))

    def test_normal_well_formed_paragraph_has_no_issues(self):
        text = (
            "I led requirement gathering and stakeholder alignment at Tibicle LLP, producing BRD/SOW "
            "documentation to guide project scope. I also facilitated daily Agile scrum meetings."
        )
        self.assertEqual(tq.find_whitespace_issues(text), [])

    def test_known_limitation_short_concatenations_not_reliably_caught(self):
        # Documented, honest limitation: without a dictionary, a shorter
        # fused word below the conservative length threshold cannot be
        # reliably distinguished from a real word. This test exists to make
        # the limitation explicit and regression-visible, not to assert
        # desired-but-unachievable behavior.
        issues = tq.find_whitespace_issues("I led requirementgathering and managementapp work.")
        self.assertEqual(issues, [], "documents that short fused words are a known gap -- see module docstring")


if __name__ == "__main__":
    unittest.main()
