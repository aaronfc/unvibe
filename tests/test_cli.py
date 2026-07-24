"""Unit tests for the pure functions in unvibe.cli.

These exercise the functions that pull structured data out of model output and
evaluate scenario results — directly and offline. The smoke test
(tests/smoke.sh) covers packaging and CLI wiring.
"""

import subprocess

import pytest
import yaml

from unvibe import cli
from unvibe.cli import (
    build_harness_command,
    call_harness,
    extract_json_array,
    extract_yaml_document,
    main,
    parse_args,
    plan_to_searchable,
    run_scenario,
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


class TestHarnessCommands:
    def test_requires_explicit_model(self):
        with pytest.raises(ValueError, match="model is required"):
            build_harness_command("claude", "hello")

    @pytest.mark.parametrize(
        ("harness", "expected"),
        [
            (
                "claude",
                [
                    "test-claude",
                    "-p",
                    "--no-session-persistence",
                    "--model",
                    "test-model",
                    "--effort",
                    "high",
                    "hello",
                ],
            ),
            (
                "codex",
                [
                    "test-codex",
                    "exec",
                    "--ephemeral",
                    "--model",
                    "test-model",
                    "-c",
                    'model_reasoning_effort="high"',
                    "hello",
                ],
            ),
            (
                "opencode",
                [
                    "test-opencode",
                    "run",
                    "--model",
                    "test-model",
                    "--variant",
                    "high",
                    "hello",
                ],
            ),
        ],
    )
    def test_passes_explicit_model_to_selected_harness(
        self, monkeypatch, harness, expected
    ):
        monkeypatch.setitem(cli.HARNESS_BINS, harness, f"test-{harness}")

        assert (
            build_harness_command(harness, "hello", "test-model", "high")
            == expected
        )

    def test_call_harness_runs_selected_adapter(self, monkeypatch):
        monkeypatch.setitem(cli.HARNESS_BINS, "codex", "test-codex")
        observed = {}

        def run(command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, stdout="answer\n", stderr="")

        monkeypatch.setattr(cli.subprocess, "run", run)

        assert (
            call_harness(
                "hello",
                "codex",
                "gpt-test",
                effort="xhigh",
                timeout=12,
            )
            == "answer"
        )
        assert observed == {
            "command": [
                "test-codex",
                "exec",
                "--ephemeral",
                "--model",
                "gpt-test",
                "-c",
                'model_reasoning_effort="xhigh"',
                "hello",
            ],
            "kwargs": {
                "capture_output": True,
                "text": True,
                "timeout": 12,
                "check": False,
            },
        }


class TestRuntimeConfiguration:
    @pytest.fixture(autouse=True)
    def clear_runtime_environment(self, monkeypatch, tmp_path):
        for name in (
            "UNVIBE_HARNESS",
            "UNVIBE_MODEL",
            "UNVIBE_EVALUATION_MODEL",
            "UNVIBE_RUBRIC_MODEL",
            "UNVIBE_EFFORT",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(
            "UNVIBE_CONFIG", str(tmp_path / "missing-config.yaml")
        )

    def test_missing_configuration_explains_every_configuration_path(
        self, capsys
    ):
        with pytest.raises(SystemExit):
            parse_args(["skill"])

        error = capsys.readouterr().err
        assert "--harness" in error
        assert "--evaluation-model" in error
        assert "--rubric-model" in error
        assert "UNVIBE_HARNESS" in error
        assert "UNVIBE_EVALUATION_MODEL" in error
        assert "UNVIBE_RUBRIC_MODEL" in error
        assert "unvibe setup" in error
        assert "opus" in error
        assert "haiku" in error
        assert "gpt-5.6-sol" in error
        assert "gpt-5.6-luna" in error

    def test_cli_configures_required_values_and_default_effort(self):
        args = parse_args(
            [
                "skill",
                "--harness",
                "claude",
                "--evaluation-model",
                "opus",
                "--rubric-model",
                "haiku",
            ]
        )

        assert args.harness == "claude"
        assert args.evaluation_model == "opus"
        assert args.rubric_model == "haiku"
        assert args.effort == "medium"

    def test_environment_configures_all_runtime_values(self, monkeypatch):
        monkeypatch.setenv("UNVIBE_HARNESS", "opencode")
        monkeypatch.setenv("UNVIBE_EVALUATION_MODEL", "provider/evaluator")
        monkeypatch.setenv("UNVIBE_RUBRIC_MODEL", "provider/judge")
        monkeypatch.setenv("UNVIBE_EFFORT", "high")

        args = parse_args(["skill"])

        assert args.harness == "opencode"
        assert args.evaluation_model == "provider/evaluator"
        assert args.rubric_model == "provider/judge"
        assert args.effort == "high"

    def test_user_config_supplies_all_runtime_values(
        self, monkeypatch, tmp_path
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "version: 1\n"
            "harness: codex\n"
            "evaluation_model: gpt-5.6-sol\n"
            "rubric_model: gpt-5.6-luna\n"
            "effort: xhigh\n"
        )
        monkeypatch.setenv("UNVIBE_CONFIG", str(config_path))

        args = parse_args(["skill"])

        assert args.harness == "codex"
        assert args.evaluation_model == "gpt-5.6-sol"
        assert args.rubric_model == "gpt-5.6-luna"
        assert args.effort == "xhigh"

    def test_cli_overrides_environment_and_config(
        self, monkeypatch, tmp_path
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "harness: claude\n"
            "evaluation_model: config-evaluator\n"
            "rubric_model: config-judge\n"
            "effort: low\n"
        )
        monkeypatch.setenv("UNVIBE_CONFIG", str(config_path))
        monkeypatch.setenv("UNVIBE_HARNESS", "codex")
        monkeypatch.setenv("UNVIBE_EVALUATION_MODEL", "environment-evaluator")
        monkeypatch.setenv("UNVIBE_RUBRIC_MODEL", "environment-judge")
        monkeypatch.setenv("UNVIBE_EFFORT", "high")

        args = parse_args(
            [
                "skill",
                "--harness",
                "opencode",
                "--evaluation-model",
                "cli-evaluator",
                "--rubric-model",
                "cli-judge",
                "--effort",
                "max",
            ]
        )

        assert args.harness == "opencode"
        assert args.evaluation_model == "cli-evaluator"
        assert args.rubric_model == "cli-judge"
        assert args.effort == "max"

    def test_legacy_model_flag_is_rejected(self, capsys):
        with pytest.raises(SystemExit):
            parse_args(
                [
                    "skill",
                    "--harness",
                    "claude",
                    "--evaluation-model",
                    "opus",
                    "--rubric-model",
                    "haiku",
                    "--model",
                    "legacy-model",
                ]
            )

        assert "unrecognized arguments: --model" in capsys.readouterr().err

    def test_legacy_model_environment_is_ignored(
        self, monkeypatch, capsys
    ):
        monkeypatch.setenv("UNVIBE_HARNESS", "claude")
        monkeypatch.setenv("UNVIBE_MODEL", "provider/legacy-model")

        with pytest.raises(SystemExit):
            parse_args(["skill"])

        error = capsys.readouterr().err
        assert "evaluation model" in error
        assert "rubric model" in error

    def test_setup_writes_explicit_configuration(self, tmp_path):
        config_path = tmp_path / "config.yaml"

        main(
            [
                "setup",
                "--config",
                str(config_path),
                "--harness",
                "claude",
                "--evaluation-model",
                "opus",
                "--rubric-model",
                "haiku",
                "--effort",
                "high",
            ]
        )

        assert yaml.safe_load(config_path.read_text()) == {
            "version": 1,
            "harness": "claude",
            "evaluation_model": "opus",
            "rubric_model": "haiku",
            "effort": "high",
        }

    def test_setup_prompts_for_each_required_choice(
        self, monkeypatch, tmp_path
    ):
        config_path = tmp_path / "config.yaml"
        answers = iter(
            ["codex", "gpt-5.6-sol", "gpt-5.6-luna"]
        )
        monkeypatch.setattr(
            "builtins.input", lambda prompt: next(answers)
        )

        main(["setup", "--config", str(config_path)])

        assert yaml.safe_load(config_path.read_text()) == {
            "version": 1,
            "harness": "codex",
            "evaluation_model": "gpt-5.6-sol",
            "rubric_model": "gpt-5.6-luna",
            "effort": "medium",
        }


class TestScenarioModels:
    def test_uses_evaluation_model_for_plan_and_rubric_model_for_judge(
        self, monkeypatch
    ):
        calls = []

        def call(prompt, harness, model, effort, timeout=180):
            calls.append((harness, model, effort))
            if len(calls) == 1:
                return '[{"tool": "Read", "args": "README.md"}]'
            return '[{"verdict": "PASS", "reason": "The plan reads it."}]'

        monkeypatch.setattr(cli, "call_harness", call)

        result = run_scenario(
            "skill instructions",
            {
                "id": "reads_docs",
                "user_message": "Read the docs",
                "rubric": ["The plan reads the documentation."],
            },
            "claude",
            "sonnet",
            "haiku",
            "high",
        )

        assert calls == [
            ("claude", "sonnet", "high"),
            ("claude", "haiku", "high"),
        ]
        assert result["rubric"][0]["passed"] is True


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
