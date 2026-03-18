"""Tests for strip_thinking_tags() -- narration stripping (Phase 6) coverage."""

from __future__ import annotations

import pytest

from researchclaw.utils.thinking_tags import strip_thinking_tags


class TestNarrationStripping:
    """Phase 6: conversational meta-commentary lines must be removed."""

    NARRATION_OPENERS = [
        "Now let me analyse the data.",
        "Let me now summarize the findings.",
        "I'll now write the abstract.",
        "Let me also add some context.",
        "Good. Let me proceed.",
        "Good, the data is clear.",
        "Here's my analysis of the results.",
        "Here's the summary:",
        "Now I need to address the second point.",
        "The plan is to run the experiment first.",
        "The analysis shows a strong correlation.",
        "The next step is validation.",
        "This is a research note for the appendix.",
        "This is the third continuation of the draft.",
    ]

    @pytest.mark.parametrize("line", NARRATION_OPENERS)
    def test_narration_line_removed(self, line: str) -> None:
        """Each known narration opener must be stripped when it appears alone."""
        result = strip_thinking_tags(line)
        # After stripping a pure narration line the result should be empty
        # or contain only whitespace.
        assert result.strip() == "", (
            f"Narration opener not stripped: {line!r} -> {result!r}"
        )

    def test_narration_line_removed_from_multiline(self) -> None:
        """Narration line inside a real text block is stripped, content preserved."""
        text = (
            "## Introduction\n"
            "Now let me provide context for this section.\n"
            "Dark matter constitutes roughly 27% of the universe's energy budget.\n"
        )
        result = strip_thinking_tags(text)
        assert "Dark matter" in result
        assert "Now let me" not in result

    def test_legitimate_sentence_starting_with_here_preserved(self) -> None:
        """'Here we show ...' in a scientific context must NOT be stripped.

        The regex anchors on specific openers; a sentence that merely contains
        'Here' in an unexpected position should not be affected.
        """
        # "Here we show" does not match the opener list
        text = "Here we show that the proposed method achieves 95% accuracy."
        result = strip_thinking_tags(text)
        assert "Here we show" in result

    def test_paper_content_with_the_analysis_not_stripped(self) -> None:
        """'The analysis of...' at line start is a known opener and IS stripped.

        This tests that the regex is intentionally aggressive on these phrases.
        """
        text = "The analysis shows a strong correlation between X and Y.\n"
        result = strip_thinking_tags(text)
        # "The analysis" IS in the regex -- it should be stripped
        assert "The analysis" not in result

    def test_narration_mid_document_stripped_content_preserved(self) -> None:
        """Narration interspersed across a multi-section document is removed."""
        text = (
            "## Methods\n"
            "We use a Bayesian framework.\n"
            "Now I need to describe the prior distributions.\n"
            "The prior is log-normal with mu=0, sigma=1.\n"
            "Let me also mention the likelihood.\n"
            "The likelihood is Gaussian.\n"
        )
        result = strip_thinking_tags(text)
        assert "Bayesian" in result
        assert "log-normal" in result
        assert "Gaussian" in result
        assert "Now I need to" not in result
        assert "Let me also" not in result

    def test_empty_string_unchanged(self) -> None:
        assert strip_thinking_tags("") == ""

    def test_no_narration_text_unchanged(self) -> None:
        text = "The experiment yielded a p-value of 0.03 (two-tailed t-test)."
        result = strip_thinking_tags(text)
        assert "p-value of 0.03" in result


class TestNarrationDoesNotObliterateLegitimateContent:
    """Regression guard: narration stripping must not remove scientific prose."""

    def test_abstract_survives(self) -> None:
        abstract = (
            "We present a novel approach to quantum error correction using "
            "topological codes. Our method reduces logical error rates by 40% "
            "compared to the surface code baseline."
        )
        assert strip_thinking_tags(abstract).strip() == abstract.strip()

    def test_numbered_list_survives(self) -> None:
        text = "1. Collect data\n2. Run analysis\n3. Report findings\n"
        result = strip_thinking_tags(text)
        assert "Collect data" in result
        assert "Report findings" in result
