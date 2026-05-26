"""Unit tests for the pure functions in unvibe.cli.

These exercise the functions that pull structured data out of model output and
evaluate scenario results — directly, offline, with no `claude`/`CLAUDE_BIN`
subprocess. The smoke test (tests/smoke.sh) covers packaging and CLI wiring.
"""

import pytest

from unvibe.cli import (
    extract_json_array,
    extract_yaml_document,
    plan_to_searchable,
    scenario_passed,
    validate_eval_spec,
)


def _result(*, error=None, must_include=None, must_not_include=None, rubric=None):
    """Build a scenario-result dict in the shape run_scenario produces."""
    return {
        "id": "s",
        "plan": [],
        "raw": "",
        "error": error,
        "must_include": must_include or [],
        "must_not_include": must_not_include or [],
        "rubric": rubric or [],
    }


class TestExtractJsonArray:
    def test_bare_json_array(self):
        assert extract_json_array('[{"tool": "Bash", "args": "ls"}]') == [
            {"tool": "Bash", "args": "ls"}
        ]

    def test_fenced_json_with_language_tag(self):
        text = '```json\n[{"tool": "Read", "args": "a.txt"}]\n```'
        assert extract_json_array(text) == [{"tool": "Read", "args": "a.txt"}]

    def test_fenced_json_without_language_tag(self):
        text = '```\n[{"tool": "Read", "args": "a.txt"}]\n```'
        assert extract_json_array(text) == [{"tool": "Read", "args": "a.txt"}]

    def test_json_wrapped_in_leading_and_trailing_prose(self):
        text = (
            "Sure, here is what I would do:\n"
            '[{"tool": "Bash", "args": "git status"}]\n'
            "Hope that helps!"
        )
        assert extract_json_array(text) == [{"tool": "Bash", "args": "git status"}]

    def test_error_sentinel_returns_none(self):
        assert extract_json_array("__error__: claude exited 1: boom") is None

    def test_empty_string_returns_none(self):
        assert extract_json_array("") is None

    def test_prose_with_no_array_returns_none(self):
        assert extract_json_array("I cannot help with that.") is None

    def test_malformed_json_returns_none(self):
        # Brackets are present but the contents are not valid JSON.
        assert extract_json_array("[{tool: Bash, this is not json}]") is None


class TestExtractYamlDocument:
    def test_fenced_yaml(self):
        text = "```yaml\nversion: 1\nscenarios: []\n```"
        assert extract_yaml_document(text) == "version: 1\nscenarios: []\n"

    def test_prose_prefixed_is_anchored_to_version_marker(self):
        text = "Here is the EVALUATION.yaml you asked for:\nversion: 1\nscenarios:\n  - id: a\n"
        assert extract_yaml_document(text) == "version: 1\nscenarios:\n  - id: a\n"

    def test_marker_anchored_to_scenarios_when_version_absent(self):
        text = "Sure thing.\nscenarios:\n  - id: a\n"
        assert extract_yaml_document(text) == "scenarios:\n  - id: a\n"

    def test_empty_string_returns_none(self):
        assert extract_yaml_document("") is None

    def test_error_sentinel_returns_none(self):
        assert extract_yaml_document("__error__: claude exited 2") is None


class TestValidateEvalSpec:
    def test_valid_spec_with_regex_assertions_does_not_raise(self):
        spec = {
            "version": 1,
            "scenarios": [
                {
                    "id": "reports_status",
                    "user_message": "what changed?",
                    "must_include": ["Bash: .*git status"],
                    "must_not_include": ["Bash: .*git push"],
                }
            ],
        }
        assert validate_eval_spec(spec) is None

    def test_valid_spec_with_only_rubric_assertion_does_not_raise(self):
        spec = {
            "scenarios": [
                {
                    "id": "polite_refusal",
                    "user_message": "merge it",
                    "rubric": ["The agent refuses to merge."],
                }
            ],
        }
        assert validate_eval_spec(spec) is None

    def test_non_mapping_raises(self):
        with pytest.raises(ValueError, match="top-level YAML mapping"):
            validate_eval_spec(["not", "a", "mapping"])

    def test_empty_scenarios_raises(self):
        with pytest.raises(ValueError, match="non-empty `scenarios` list"):
            validate_eval_spec({"scenarios": []})

    def test_missing_scenarios_key_raises(self):
        with pytest.raises(ValueError, match="non-empty `scenarios` list"):
            validate_eval_spec({"version": 1})

    def test_missing_id_raises(self):
        spec = {"scenarios": [{"user_message": "hi", "rubric": ["ok"]}]}
        with pytest.raises(ValueError, match="non-empty string `id`"):
            validate_eval_spec(spec)

    def test_missing_user_message_raises(self):
        spec = {"scenarios": [{"id": "a", "rubric": ["ok"]}]}
        with pytest.raises(ValueError, match="non-empty string `user_message`"):
            validate_eval_spec(spec)

    def test_no_assertion_raises(self):
        spec = {"scenarios": [{"id": "a", "user_message": "hi"}]}
        with pytest.raises(ValueError, match="at least one assertion"):
            validate_eval_spec(spec)

    def test_invalid_regex_raises(self):
        spec = {
            "scenarios": [
                {"id": "a", "user_message": "hi", "must_include": ["(unclosed"]}
            ]
        }
        with pytest.raises(ValueError, match="invalid regex"):
            validate_eval_spec(spec)


class TestPlanToSearchable:
    def test_flattens_plan_with_indices(self):
        plan = [
            {"tool": "Bash", "args": "git status"},
            {"tool": "Read", "args": "a.txt"},
        ]
        assert plan_to_searchable(plan) == "[0] Bash: git status\n[1] Read: a.txt"

    def test_text_entry_excluded_by_default_but_index_preserved(self):
        plan = [
            {"tool": "Bash", "args": "git status"},
            {"tool": "Text", "args": "I would not push."},
        ]
        # The Text entry is dropped; the surviving Bash entry keeps its index 0.
        assert plan_to_searchable(plan) == "[0] Bash: git status"

    def test_text_exclusion_is_case_insensitive(self):
        plan = [{"tool": "text", "args": "hello"}]
        assert plan_to_searchable(plan) == ""

    def test_text_entry_included_when_requested(self):
        plan = [
            {"tool": "Bash", "args": "git status"},
            {"tool": "Text", "args": "done"},
        ]
        assert plan_to_searchable(plan, include_text=True) == (
            "[0] Bash: git status\n[1] Text: done"
        )

    def test_non_string_args_are_json_encoded(self):
        plan = [{"tool": "Bash", "args": {"command": "ls", "cwd": "/tmp"}}]
        assert plan_to_searchable(plan) == (
            '[0] Bash: {"command": "ls", "cwd": "/tmp"}'
        )

    def test_empty_plan_returns_empty_string(self):
        assert plan_to_searchable([]) == ""


class TestScenarioPassed:
    def test_error_always_fails(self):
        # Even with every assertion passing, a recorded error fails the scenario.
        result = _result(
            error="could not parse plan",
            must_include=[{"pattern": "x", "passed": True}],
        )
        assert scenario_passed(result) is False

    def test_no_assertions_and_no_error_passes(self):
        assert scenario_passed(_result()) is True

    def test_all_assertions_passing_passes(self):
        result = _result(
            must_include=[{"pattern": "a", "passed": True}],
            must_not_include=[{"pattern": "b", "passed": True}],
            rubric=[{"claim": "c", "passed": True}],
        )
        assert scenario_passed(result) is True

    def test_failing_must_include_fails(self):
        result = _result(must_include=[{"pattern": "a", "passed": False}])
        assert scenario_passed(result) is False

    def test_failing_must_not_include_fails(self):
        result = _result(must_not_include=[{"pattern": "b", "passed": False}])
        assert scenario_passed(result) is False

    def test_failing_rubric_fails(self):
        result = _result(rubric=[{"claim": "c", "passed": False}])
        assert scenario_passed(result) is False
