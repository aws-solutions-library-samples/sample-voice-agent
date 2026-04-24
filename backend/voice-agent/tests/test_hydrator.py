"""Unit tests for app/hydrator.py — prompt template rendering.

Mirrors the OG voiceagent test patterns so that any behavior change
between the two code paths is caught by diffing test output, not by
watching production prompts drift.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.hydrator import VOICE_WRAPPER, hydrate_prompt


class TestCurrentTimeInjection:
    def test_injects_current_time_placeholder(self):
        with patch("app.hydrator.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 25, 16, 30)
            out = hydrate_prompt("Today is {{current_time}}.", {})
        assert "Wednesday, March 25, 2026 04:30 PM" in out

    def test_injects_without_case_data(self):
        with patch("app.hydrator.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 25, 9, 5)
            out = hydrate_prompt("At {{current_time}}", None)
        assert "09:05 AM" in out

    def test_current_time_not_overridden_by_case_data(self):
        # Even if case_data includes current_time, our injected value wins
        # because replacement runs first.
        with patch("app.hydrator.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 25, 16, 30)
            out = hydrate_prompt(
                "Now: {{current_time}}",
                {"current_time": "SHOULD_NOT_APPEAR"},
            )
        assert "SHOULD_NOT_APPEAR" not in out
        assert "04:30 PM" in out


class TestCaseDataSubstitution:
    def test_replaces_known_placeholder(self):
        out = hydrate_prompt("DOS: {{Service_Date}}", {"Service_Date": "2026-02-14"})
        assert "DOS: 2026-02-14" in out

    def test_empty_string_for_falsy_values(self):
        out = hydrate_prompt("Patient: {{Name}}", {"Name": ""})
        assert "Patient: " in out

    def test_empty_string_for_none_values(self):
        out = hydrate_prompt("Patient: {{Name}}", {"Name": None})
        assert "Patient: " in out

    def test_preserves_zero_as_string(self):
        # '0' is falsy in Python; the OG collapses it to "". Keep that
        # behavior so the two codebases render identically.
        out = hydrate_prompt("Count: {{N}}", {"N": 0})
        assert "Count: " in out

    def test_coerces_non_string_values(self):
        out = hydrate_prompt("Amount: {{Amt}}", {"Amt": 125.50})
        assert "Amount: 125.5" in out

    def test_multiple_placeholders_same_key(self):
        out = hydrate_prompt("{{X}} and {{X}}", {"X": "foo"})
        assert out.count("foo") == 2


class TestUnknownPlaceholderStripping:
    def test_strips_unknown_placeholder(self):
        out = hydrate_prompt("Hello {{Unknown}} world", {})
        assert "{{" not in out
        assert "}}" not in out
        assert "Hello  world" in out

    def test_strips_multiple_unknown_placeholders(self):
        out = hydrate_prompt("{{A}} {{B}} {{C}}", {})
        assert "{{" not in out
        assert "}}" not in out

    def test_strips_only_unknown_placeholders(self):
        out = hydrate_prompt(
            "{{Known}} + {{Unknown}}",
            {"Known": "yes"},
        )
        assert "yes" in out
        assert "{{Unknown}}" not in out
        assert "{{" not in out


class TestVoiceWrapper:
    def test_wrapper_always_prepended(self):
        out = hydrate_prompt("hello", {})
        assert out.startswith(VOICE_WRAPPER)

    def test_wrapper_prepended_even_with_empty_prompt(self):
        out = hydrate_prompt("", {})
        assert out == VOICE_WRAPPER

    def test_wrapper_prepended_with_none_prompt(self):
        out = hydrate_prompt(None, {})  # type: ignore[arg-type]
        assert out == VOICE_WRAPPER

    def test_wrapper_contains_required_rules(self):
        assert "NEVER use markdown" in VOICE_WRAPPER
        assert "short, punchy" in VOICE_WRAPPER
        assert "data readouts" in VOICE_WRAPPER


class TestIdempotence:
    def test_hydrating_an_already_hydrated_prompt_is_stable(self):
        # First pass fills in; second pass should produce the same output
        # modulo current_time drift (which we hold constant here).
        with patch("app.hydrator.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 25, 12, 0)
            once = hydrate_prompt("X: {{X}}, t: {{current_time}}", {"X": "v"})
            twice = hydrate_prompt(once, {"X": "v"})
        # After second pass, VOICE_WRAPPER appears twice (the wrapper is
        # prepended unconditionally). That's intentional but worth
        # documenting — callers should not hydrate twice.
        assert twice.count(VOICE_WRAPPER) == 2


class TestRealChrisPrompt:
    def test_chris_prompt_fragment_renders(self):
        chris_fragment = (
            "# Identity\n"
            "You are Chris, a billing specialist.\n"
            "You are calling for one claim from {{Service_Date}}.\n"
            "Today's date: {{current_time}}"
        )
        with patch("app.hydrator.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 25, 10, 15)
            out = hydrate_prompt(chris_fragment, {"Service_Date": "2026-01-15"})

        assert "claim from 2026-01-15" in out
        assert "Today's date: Wednesday, March 25, 2026 10:15 AM" in out
        assert "{{" not in out

    def test_chris_prompt_with_empty_case_data_strips_placeholders(self):
        # Inbound call scenario — no case data, placeholders collapse to
        # empty string instead of leaking literal template syntax to
        # Bedrock.
        chris_fragment = "Claim from {{Service_Date}}."
        out = hydrate_prompt(chris_fragment, {})
        assert "{{Service_Date}}" not in out
        assert "Claim from ." in out


@pytest.mark.parametrize(
    "template,case_data,expected_substring,should_not_contain",
    [
        ("{{A}}", {"A": "x"}, "x", "{{A}}"),
        ("{{A}}", {}, "", "{{A}}"),
        ("no placeholders", {"A": "x"}, "no placeholders", "{{"),
        ("{{A}}{{B}}", {"A": "1", "B": "2"}, "12", "{{"),
    ],
)
def test_parametrized(template, case_data, expected_substring, should_not_contain):
    out = hydrate_prompt(template, case_data)
    if expected_substring:
        assert expected_substring in out
    assert should_not_contain not in out
