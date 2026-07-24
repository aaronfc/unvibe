"""
unvibe — Tiny pseudo-evals for SKILL.md files.

Usage:
    unvibe setup
    unvibe <skill-dir> --harness <name> --evaluation-model <model>
        --rubric-model <model> [--effort <level>]
    unvibe --create <skill-dir> --harness <name>
        --evaluation-model <model> --rubric-model <model>

Normal mode reads SKILL.md and EVALUATIONS.yaml from <skill-dir>. For each scenario:
  1. Asks the selected coding-agent harness what tool calls it would make.
  2. Checks must_include / must_not_include regex patterns.
  3. Optionally asks an LLM judge to score rubric items.

Create mode reads SKILL.md and writes a first-pass EVALUATIONS.yaml.

Exits 0 if all scenarios pass, 1 otherwise, or 130 when interrupted.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Any

import yaml

DEFAULT_EFFORT = "medium"
DEFAULT_EVALUATIONS_FILENAME = "EVALUATIONS.yaml"
LEGACY_EVALUATION_FILENAME = "EVALUATION.yaml"
DEFAULT_HARNESS_TIMEOUT_SECONDS = 300
HARNESS_ERROR_PREFIX = "__error__:"
SUPPORTED_HARNESSES = ("claude", "codex", "opencode")
MODEL_SUGGESTIONS = {
    "claude": ("opus", "haiku"),
    "codex": ("gpt-5.6-sol", "gpt-5.6-luna"),
}
HARNESS_BINS = {
    "claude": os.environ.get("CLAUDE_BIN", "claude"),
    "codex": os.environ.get("CODEX_BIN", "codex"),
    "opencode": os.environ.get("OPENCODE_BIN", "opencode"),
}
HARNESS_BIN_ENV = {
    "claude": "CLAUDE_BIN",
    "codex": "CODEX_BIN",
    "opencode": "OPENCODE_BIN",
}
HARNESS_COMMANDS = {
    "claude": ("-p", "--no-session-persistence"),
    "codex": ("exec", "--ephemeral"),
    "opencode": ("run",),
}
_HARNESS_PROCESSES: set[subprocess.Popen[str]] = set()
_HARNESS_PROCESSES_LOCK = Lock()
_INTERRUPTED = Event()


@dataclass
class ScenarioResult:
    id: str
    plan: list[dict[str, Any]] | None = None
    raw: str = ""
    must_include: list[dict[str, Any]] = field(default_factory=list)
    must_not_include: list[dict[str, Any]] = field(default_factory=list)
    rubric: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def failure(
        cls, scenario_id: str, error: str, *, raw: str = ""
    ) -> "ScenarioResult":
        return cls(id=scenario_id, raw=raw, error=error)


NARRATE_INSTRUCTIONS = """\
You are evaluating a skill. Below in the <SKILL> block is the skill's full
instructions. In the <USER> block is a user message.

Your task: imagine you are the agent receiving the user's message with this
skill loaded in your context. Output a JSON array describing EVERY tool call
you would make, in order. Each entry must have:

  {"tool": "<tool name e.g. Bash, Edit, Read>",
   "args": "<the args you would pass, as a single string>",
   "why":  "<one short sentence>"}

CRITICAL:
- Do NOT execute ANY tools yourself. Do not call Bash, Read, Edit, etc.
- Output ONLY the JSON array. No prose, no markdown fences, no explanation.
- If the user message asks you to refuse something (e.g., merging a PR that
  the skill forbids), output the JSON array of actions you WOULD take —
  which may be a short list ending with a text response, not tool calls.
  Represent a text reply to the user as: {"tool": "Text", "args": "<reply>",
  "why": "<why>"}.
"""

JUDGE_INSTRUCTIONS = """\
You are grading several independent claims about an agent's planned actions.

Below in <PLAN> is the agent's JSON action plan. Below in <CLAIMS> is a
numbered list of claims about what the plan should satisfy. Grade EACH claim
on its own merits, based ONLY on the plan.

Output a JSON array with one entry per claim, in the same order as the
input, and the SAME LENGTH as the input. Each entry must be:

  {"verdict": "PASS" | "FAIL", "reason": "<one sentence why>"}

No prose, no markdown fences, no other text — only the JSON array.
"""

CREATE_INSTRUCTIONS = """\
You are generating an EVALUATIONS.yaml file for unvibe, a tiny pseudo-eval
runner for SKILL.md files.

unvibe evaluates a skill by loading SKILL.md, showing the agent a user message,
and asking it to output the JSON action plan it WOULD take. unvibe then checks:

- must_include: Python regexes that must appear in flattened planned tool calls.
- must_not_include: Python regexes that must not appear in planned tool calls.
- rubric: natural-language claims judged against the planned action plan.

The flattened plan looks like:
  [0] Bash: git status --short
  [1] Read: path/to/file

Generate a detailed first-pass EVALUATIONS.yaml for the skill below.

Required shape:

version: 1
scenarios:
  - id: short_snake_case_id
    user_message: |
      A realistic message the user might send when invoking this skill.
    must_include:
      - 'ToolName: .*important action from the skill'
    must_not_include:
      - 'ToolName: .*forbidden action from the skill'
    rubric:
      - 'The plan follows an important semantic rule from the skill.'

Rules:
- Output ONLY YAML. No prose. No markdown fences.
- Include top-level `version: 1` and `scenarios`.
- Create 4 to 8 high-signal scenarios.
- Use snake_case scenario ids.
- Every scenario MUST use `user_message`, not `prompt`, `input`, or `query`.
- Do not require the agent to read SKILL.md; unvibe already provides it.
- Include happy paths, edge cases, skip/refusal behavior when relevant, and
  anti-regressions for dangerous or explicitly forbidden actions.
- Prefer concrete `must_include` / `must_not_include` regexes for tool usage.
- `must_include` / `must_not_include` match planned tool calls only; normal
  text replies are excluded.
- Use `rubric` for semantic claims, prose style, refusal quality, and final
  answer content.
- If the skill's primary output is a direct answer to the user, do not require
  Write/Edit tool calls unless the user message explicitly asks to create or
  modify a file.
- For "draft/write this content" scenarios without a target file path, assume
  the agent replies with Text. Text is excluded from regex matching, so cover
  those scenarios with `rubric` instead of Write/Edit assertions.
- Use tool-call regexes only for concrete file, shell, network, browser, PR,
  issue, or other tool workflows.
- Do not put prose-quality phrases in `must_not_include` unless the skill is
  expected to write or edit that prose through a tool call.
- Do not assert exact commands unless the skill makes them explicit.
- Remember regexes are case-insensitive Python regex strings.
- Escape backslashes correctly for YAML double-quoted strings, or use single
  quotes when easier.
- Do not invent external systems, repo names, or file paths unless the skill
  itself names them.
"""

REPAIR_CREATE_INSTRUCTIONS = """\
Repair this generated EVALUATIONS.yaml so it validates for unvibe.

Output ONLY YAML. No prose. No markdown fences.

Every scenario must have:
- id: non-empty string
- user_message: non-empty string
- at least one assertion in must_include, must_not_include, or rubric

Keep the same intent, but fix the schema and any invalid regexes.
"""


def green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def build_harness_command(
    harness: str,
    prompt: str,
    model: str | None = None,
    effort: str = DEFAULT_EFFORT,
) -> list[str]:
    """Build the native noninteractive command for a supported harness."""
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(f"unsupported harness: {harness}")
    if not model:
        raise ValueError("model is required")

    command = [HARNESS_BINS[harness], *HARNESS_COMMANDS[harness]]
    command.extend(["--model", model])
    if harness == "claude":
        command.extend(["--effort", effort])
    elif harness == "codex":
        command.extend(["-c", f'model_reasoning_effort="{effort}"'])
    else:
        command.extend(["--variant", effort])
    command.append(prompt)
    return command


def _terminate_harness_process(
    process: subprocess.Popen[str], *, force: bool = False
) -> None:
    """Stop a harness and any child processes it spawned."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(process.pid, sig)
        elif force:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _stop_harness_processes(*, force: bool = False) -> None:
    """Prevent new harness calls and stop all active ones."""
    _INTERRUPTED.set()
    with _HARNESS_PROCESSES_LOCK:
        processes = list(_HARNESS_PROCESSES)
    for process in processes:
        _terminate_harness_process(process, force=force)


def call_harness(
    prompt: str,
    harness: str,
    model: str,
    effort: str = DEFAULT_EFFORT,
    timeout: int = DEFAULT_HARNESS_TIMEOUT_SECONDS,
) -> str:
    """Invoke a coding-agent harness and return its assistant text."""
    command = build_harness_command(harness, prompt, model, effort)
    try:
        with _HARNESS_PROCESSES_LOCK:
            if _INTERRUPTED.is_set():
                return f"{HARNESS_ERROR_PREFIX} interrupted"
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            _HARNESS_PROCESSES.add(process)
    except FileNotFoundError:
        binary = HARNESS_BINS[harness]
        sys.exit(
            f"error: '{binary}' not on PATH. Set ${HARNESS_BIN_ENV[harness]}."
        )
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_harness_process(process, force=True)
            process.communicate()
            return f"{HARNESS_ERROR_PREFIX} {harness} timed out after {timeout}s"
        except KeyboardInterrupt:
            _terminate_harness_process(process)
            process.wait()
            raise
    finally:
        with _HARNESS_PROCESSES_LOCK:
            _HARNESS_PROCESSES.discard(process)

    if _INTERRUPTED.is_set():
        return f"{HARNESS_ERROR_PREFIX} interrupted"
    if process.returncode != 0:
        return (
            f"{HARNESS_ERROR_PREFIX} {harness} exited {process.returncode}: "
            f"{stderr.strip()}"
        )
    return stdout.strip()


def harness_error_message(text: str) -> str | None:
    """Return the readable message from an explicit harness error."""
    if not text.startswith(HARNESS_ERROR_PREFIX):
        return None
    return text.removeprefix(HARNESS_ERROR_PREFIX).strip()


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Pull a JSON array out of the model's response.

    Handles bare JSON, fenced JSON, and JSON with leading/trailing prose.
    """
    if not text or text.startswith(HARNESS_ERROR_PREFIX):
        return None
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def extract_yaml_document(text: str) -> str | None:
    """Pull a YAML document out of a model response."""
    if not text or text.startswith(HARNESS_ERROR_PREFIX):
        return None
    fence = re.search(r"```(?:ya?ml)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    text = text.strip()
    for marker in ("version:", "scenarios:"):
        match = re.search(rf"(?m)^{marker}", text)
        if match:
            return text[match.start() :].strip() + "\n"
    return text + "\n" if text else None


def validate_eval_spec(eval_spec: Any) -> None:
    """Validate the small subset of EVALUATIONS.yaml that unvibe understands."""
    if not isinstance(eval_spec, dict):
        raise ValueError("expected top-level YAML mapping")

    scenarios = eval_spec.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("expected non-empty `scenarios` list")

    for index, scenario in enumerate(scenarios):
        prefix = f"scenario {index + 1}"
        if not isinstance(scenario, dict):
            raise ValueError(f"{prefix}: expected mapping")
        if not isinstance(scenario.get("id"), str) or not scenario["id"].strip():
            raise ValueError(f"{prefix}: expected non-empty string `id`")
        if (
            not isinstance(scenario.get("user_message"), str)
            or not scenario["user_message"].strip()
        ):
            raise ValueError(
                f"{prefix} ({scenario.get('id')}): "
                "expected non-empty string `user_message`"
            )

        has_assertion = False
        for key in ("must_include", "must_not_include", "rubric"):
            items = scenario.get(key, [])
            if items is None:
                continue
            if not isinstance(items, list):
                raise ValueError(
                    f"{prefix} ({scenario['id']}): `{key}` must be a list"
                )
            if items:
                has_assertion = True
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(
                        f"{prefix} ({scenario['id']}): "
                        f"`{key}` entries must be non-empty strings"
                    )
                if key in ("must_include", "must_not_include"):
                    try:
                        re.compile(item, re.IGNORECASE)
                    except re.error as exc:
                        raise ValueError(
                            f"{prefix} ({scenario['id']}): "
                            f"invalid regex in `{key}`: {item!r}: {exc}"
                        ) from exc
        if not has_assertion:
            raise ValueError(
                f"{prefix} ({scenario['id']}): expected at least one assertion"
            )


def create_evaluation_yaml(
    skill_md: str,
    harness: str,
    evaluation_model: str,
    effort: str = DEFAULT_EFFORT,
) -> str:
    """Ask a coding-agent harness to generate a first-pass EVALUATIONS.yaml."""
    prompt = CREATE_INSTRUCTIONS + f"\n\n<SKILL>\n{skill_md}\n</SKILL>\n"
    last_error = "unknown error"

    for _ in range(2):
        raw = call_harness(
            prompt,
            harness,
            evaluation_model,
            effort,
            timeout=DEFAULT_HARNESS_TIMEOUT_SECONDS,
        )
        invocation_error = harness_error_message(raw)
        yaml_text = None
        if invocation_error:
            last_error = invocation_error
        else:
            yaml_text = extract_yaml_document(raw)
            if yaml_text is None:
                last_error = f"could not parse YAML from {harness}'s response"
            else:
                try:
                    eval_spec = yaml.safe_load(yaml_text)
                    validate_eval_spec(eval_spec)
                    return yaml_text
                except yaml.YAMLError as exc:
                    last_error = f"generated YAML is invalid: {exc}"
                except ValueError as exc:
                    last_error = (
                        "generated EVALUATIONS.yaml did not validate: "
                        f"{exc}"
                    )

        prompt = (
            REPAIR_CREATE_INSTRUCTIONS
            + f"\n\n<ERROR>\n{last_error}\n</ERROR>\n"
            + f"\n<GENERATED_EVALUATION>\n{yaml_text or raw}\n</GENERATED_EVALUATION>\n"
        )

    sys.exit(f"error: {last_error}")


def plan_to_searchable(
    plan: list[dict[str, Any]], include_text: bool = False
) -> str:
    """Flatten the action plan into a single string for regex matching.

    By default, "Text" entries (the agent's reply to the user) are excluded:
    must_include / must_not_include patterns are about ACTIONS the agent
    would take, not what it would say. An agent that refuses to merge while
    quoting "gh pr merge" in its reply is not violating a no-merge rule —
    it's citing it. For assertions about text content, use `rubric`.
    """
    parts = []
    for i, entry in enumerate(plan):
        tool = entry.get("tool", "")
        if not include_text and tool.lower() == "text":
            continue
        args = entry.get("args", "")
        if not isinstance(args, str):
            args = json.dumps(args)
        parts.append(f"[{i}] {tool}: {args}")
    return "\n".join(parts)


def run_scenario(
    skill_md: str,
    scenario: dict[str, Any],
    harness: str,
    evaluation_model: str,
    rubric_model: str,
    effort: str = DEFAULT_EFFORT,
) -> ScenarioResult:
    """Run one scenario; return its structured result."""
    user_msg = scenario["user_message"]
    prompt = (
        NARRATE_INSTRUCTIONS
        + f"\n\n<SKILL>\n{skill_md}\n</SKILL>\n\n<USER>\n{user_msg}\n</USER>\n"
    )
    raw = call_harness(prompt, harness, evaluation_model, effort)
    plan = extract_json_array(raw)

    result = ScenarioResult(id=scenario["id"], plan=plan, raw=raw)

    invocation_error = harness_error_message(raw)
    if invocation_error:
        result.error = invocation_error
        return result

    if plan is None:
        result.error = (
            f"Could not parse action plan from {harness}'s response"
        )
        return result

    searchable = plan_to_searchable(plan)

    for pat in scenario.get("must_include", []):
        ok = bool(re.search(pat, searchable, re.IGNORECASE))
        result.must_include.append({"pattern": pat, "passed": ok})

    for pat in scenario.get("must_not_include", []):
        match = re.search(pat, searchable, re.IGNORECASE)
        ok = match is None
        result.must_not_include.append(
            {
                "pattern": pat,
                "passed": ok,
                "matched_line": searchable[
                    max(0, match.start() - 20) : match.end() + 60
                ]
                if match
                else None,
            }
        )

    claims = scenario.get("rubric", [])
    if claims:
        plan_json = json.dumps(plan, indent=2)
        claims_block = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(claims))
        judge_prompt = (
            JUDGE_INSTRUCTIONS
            + f"\n\n<PLAN>\n{plan_json}\n</PLAN>\n\n<CLAIMS>\n{claims_block}\n</CLAIMS>\n"
        )
        raw_verdicts = call_harness(
            judge_prompt, harness, rubric_model, effort
        )
        parsed = extract_json_array(raw_verdicts)

        invocation_error = harness_error_message(raw_verdicts)
        if invocation_error:
            result.error = f"judge: {invocation_error}"
            return result

        if not isinstance(parsed, list) or len(parsed) != len(claims):
            err = f"judge returned malformed response (got {type(parsed).__name__}, expected list of {len(claims)}): {raw_verdicts[:200]}"
            for claim in claims:
                result.rubric.append(
                    {"claim": claim, "passed": False, "verdict": f"FAIL: {err}"}
                )
        else:
            for claim, entry in zip(claims, parsed):
                verdict_str = str(entry.get("verdict", "")).strip().upper()
                reason = str(entry.get("reason", "")).strip()
                passed = verdict_str == "PASS"
                result.rubric.append(
                    {"claim": claim, "passed": passed, "verdict": f"{verdict_str}: {reason}"}
                )

    return result


def scenario_passed(result: ScenarioResult) -> bool:
    if result.error:
        return False
    for group in (
        result.must_include,
        result.must_not_include,
        result.rubric,
    ):
        if any(not item["passed"] for item in group):
            return False
    return True


def print_report(
    skill_name: str, results: list[ScenarioResult], verbose: bool
):
    passed_count = sum(1 for r in results if scenario_passed(r))
    print(f"\n{skill_name} — {len(results)} scenario(s)\n")

    for r in results:
        mark = green("✓") if scenario_passed(r) else red("✗")
        print(f"  {r.id:<60} {mark}")

        if r.error:
            print(f"    {red('error:')} {r.error}")
            if verbose and r.raw:
                print(f"    {dim('raw:')} {r.raw[:500]}")
            continue

        for group_name, items in [
            ("must_include", r.must_include),
            ("must_not_include", r.must_not_include),
            ("rubric", r.rubric),
        ]:
            if not items:
                continue
            n_pass = sum(1 for it in items if it["passed"])
            n_total = len(items)
            group_mark = green("✓") if n_pass == n_total else red("✗")
            print(f"    {group_name:<16} ({n_pass}/{n_total} {group_mark})")
            for it in items:
                if it["passed"] and not verbose:
                    continue
                sym = green("✓") if it["passed"] else red("✗")
                key = it.get("pattern") or it.get("claim")
                print(f"      {sym} {key}")
                if not it["passed"] and group_name == "must_not_include":
                    print(f"        {dim('matched:')} {it['matched_line']}")
                if not it["passed"] and group_name == "rubric":
                    print(f"        {dim('verdict:')} {it['verdict']}")

        if verbose and r.plan:
            print(f"    {dim('plan:')}")
            for entry in r.plan:
                print(f"      {dim('-')} {entry.get('tool')}: {entry.get('args', '')[:120]}")

    print(f"\n  {passed_count}/{len(results)} scenarios passed\n")


def default_config_path() -> Path:
    """Return the user configuration path, respecting XDG_CONFIG_HOME."""
    explicit_path = os.environ.get("UNVIBE_CONFIG")
    if explicit_path:
        return Path(explicit_path).expanduser()

    config_root = os.environ.get("XDG_CONFIG_HOME")
    if config_root:
        return Path(config_root).expanduser() / "unvibe" / "config.yaml"
    return Path.home() / ".config" / "unvibe" / "config.yaml"


def resolve_config_path(cli_path: str | None) -> Path:
    return Path(cli_path).expanduser() if cli_path else default_config_path()


def load_user_config(path: Path) -> dict[str, Any]:
    """Load and validate the small unvibe user configuration file."""
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    if loaded.get("version", 1) != 1:
        raise ValueError(f"{path} has an unsupported version")

    for key in ("harness", "evaluation_model", "rubric_model", "effort"):
        value = loaded.get(key)
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError(f"{path}: `{key}` must be a non-empty string")
    return loaded


def add_runtime_options(ap: argparse.ArgumentParser) -> None:
    ap.add_argument(
        "--harness",
        choices=SUPPORTED_HARNESSES,
        help="Coding-agent harness",
    )
    ap.add_argument(
        "--evaluation-model",
        help="Model used to generate scenario plans",
    )
    ap.add_argument(
        "--rubric-model",
        help="Model used to judge rubric claims",
    )
    ap.add_argument(
        "--effort",
        help=f"Harness reasoning effort (default: {DEFAULT_EFFORT})",
    )
    ap.add_argument(
        "--config",
        help="User config file (default: ~/.config/unvibe/config.yaml)",
    )


def resolve_runtime_configuration(
    args: argparse.Namespace,
    ap: argparse.ArgumentParser,
    *,
    require_choices: bool,
    include_saved_config: bool = True,
) -> argparse.Namespace:
    config_path = resolve_config_path(args.config)
    config = {}
    if include_saved_config:
        try:
            config = load_user_config(config_path)
        except ValueError as exc:
            ap.error(str(exc))

    args.config_path = config_path
    args.harness = (
        args.harness
        or os.environ.get("UNVIBE_HARNESS")
        or config.get("harness")
    )
    args.evaluation_model = (
        args.evaluation_model
        or os.environ.get("UNVIBE_EVALUATION_MODEL")
        or config.get("evaluation_model")
    )
    args.rubric_model = (
        args.rubric_model
        or os.environ.get("UNVIBE_RUBRIC_MODEL")
        or config.get("rubric_model")
    )
    args.effort = (
        args.effort
        or os.environ.get("UNVIBE_EFFORT")
        or config.get("effort")
        or DEFAULT_EFFORT
    )

    if args.harness and args.harness not in SUPPORTED_HARNESSES:
        choices = ", ".join(SUPPORTED_HARNESSES)
        ap.error(
            f"harness must be one of: {choices} (got {args.harness!r})"
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.effort):
        ap.error(
            "effort must contain only letters, numbers, dots, "
            "underscores, or hyphens"
        )

    if require_choices:
        missing = []
        if not args.harness:
            missing.append("harness")
        if not args.evaluation_model:
            missing.append("evaluation model")
        if not args.rubric_model:
            missing.append("rubric model")
        if missing:
            ap.error(missing_configuration_message(missing, config_path))
    return args


def missing_configuration_message(
    missing: list[str], config_path: Path
) -> str:
    missing_text = ", ".join(missing)
    return f"""\
missing required configuration: {missing_text}

Choose all three values with command-line parameters:
  unvibe <skill-dir> --harness claude \\
    --evaluation-model opus --rubric-model haiku

Or save your choices to {config_path}:
  unvibe setup --config {config_path}

Or set all three environment variables:
  UNVIBE_HARNESS
  UNVIBE_EVALUATION_MODEL
  UNVIBE_RUBRIC_MODEL

Suggested starting pairs (suggestions, not defaults):
  Claude/Anthropic: evaluation=opus, rubric=haiku
  Codex/OpenAI: evaluation=gpt-5.6-sol, rubric=gpt-5.6-luna

Optional effort can be set with --effort, UNVIBE_EFFORT, or setup.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments and resolve CLI-over-env-over-file config."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "setup":
        ap = argparse.ArgumentParser(
            prog="unvibe setup",
            description="Save explicit unvibe runtime choices.",
        )
        add_runtime_options(ap)
        args = ap.parse_args(raw_args[1:])
        args.command = "setup"
        return resolve_runtime_configuration(
            args,
            ap,
            require_choices=False,
            include_saved_config=False,
        )

    ap = argparse.ArgumentParser(prog="unvibe", description=__doc__)
    ap.add_argument("skill_dir", help="Path to skill directory")
    ap.add_argument(
        "--create",
        action="store_true",
        help="Generate EVALUATIONS.yaml from SKILL.md",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite EVALUATIONS.yaml when used with --create",
    )
    ap.add_argument("--scenario", help="Run only this scenario id")
    add_runtime_options(ap)
    ap.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show passing assertions + full plan",
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Concurrent scenarios (default 3)",
    )
    args = ap.parse_args(raw_args)
    args.command = "run"
    return resolve_runtime_configuration(args, ap, require_choices=True)


def prompt_for_choice(label: str, default: str | None = None) -> str:
    hint = f" (default: {default})" if default else ""
    while True:
        try:
            value = input(f"{label}{hint}: ").strip()
        except EOFError:
            sys.exit(
                "error: setup needs an interactive terminal or explicit "
                "--harness, --evaluation-model, and --rubric-model values"
            )
        if value:
            return value
        if default:
            return default
        print(f"{label} is required.", file=sys.stderr)


def run_setup(args: argparse.Namespace) -> None:
    if not args.harness:
        args.harness = prompt_for_choice(
            "Harness (claude, codex, opencode)"
        )
        if args.harness not in SUPPORTED_HARNESSES:
            choices = ", ".join(SUPPORTED_HARNESSES)
            sys.exit(f"error: harness must be one of: {choices}")

    suggestions = MODEL_SUGGESTIONS.get(args.harness, (None, None))
    if not args.evaluation_model:
        args.evaluation_model = prompt_for_choice(
            "Evaluation model", suggestions[0]
        )
    if not args.rubric_model:
        args.rubric_model = prompt_for_choice(
            "Rubric model", suggestions[1]
        )

    config = {
        "version": 1,
        "harness": args.harness,
        "evaluation_model": args.evaluation_model,
        "rubric_model": args.rubric_model,
        "effort": args.effort,
    }
    try:
        args.config_path.parent.mkdir(parents=True, exist_ok=True)
        args.config_path.write_text(
            yaml.safe_dump(config, sort_keys=False)
        )
    except OSError as exc:
        sys.exit(f"error: could not write {args.config_path}: {exc}")

    print(f"saved configuration to {args.config_path}")
    print(f"  harness: {args.harness}")
    print(f"  evaluation model: {args.evaluation_model}")
    print(f"  rubric model: {args.rubric_model}")
    print(f"  effort: {args.effort}")


def evaluation_path_for_run(skill_dir: Path) -> Path:
    """Select the plural eval file, falling back to the legacy singular name."""
    eval_path = skill_dir / DEFAULT_EVALUATIONS_FILENAME
    if eval_path.exists():
        return eval_path

    legacy_path = skill_dir / LEGACY_EVALUATION_FILENAME
    if legacy_path.exists():
        print(
            f"warning: {LEGACY_EVALUATION_FILENAME} is deprecated; "
            f"rename it to {DEFAULT_EVALUATIONS_FILENAME}",
            file=sys.stderr,
        )
        return legacy_path

    return eval_path


def _main(argv: list[str] | None = None):
    args = parse_args(argv)
    if args.command == "setup":
        run_setup(args)
        return

    skill_dir = Path(args.skill_dir).resolve()
    skill_md_path = skill_dir / "SKILL.md"
    eval_path = skill_dir / DEFAULT_EVALUATIONS_FILENAME
    if not skill_md_path.exists():
        sys.exit(f"error: {skill_md_path} not found")

    if args.create:
        if eval_path.exists() and not args.force:
            sys.exit(
                f"error: {eval_path} already exists. Use --force to overwrite it."
            )
        skill_md = skill_md_path.read_text()
        eval_path.write_text(
            create_evaluation_yaml(
                skill_md,
                args.harness,
                args.evaluation_model,
                args.effort,
            )
        )
        print(f"created {eval_path}")
        return

    eval_path = evaluation_path_for_run(skill_dir)
    if not eval_path.exists():
        sys.exit(f"error: {eval_path} not found")

    skill_md = skill_md_path.read_text()
    eval_spec = yaml.safe_load(eval_path.read_text())
    scenarios = eval_spec.get("scenarios", [])
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            sys.exit(f"error: no scenario with id '{args.scenario}'")

    print(
        f"Running {len(scenarios)} scenario(s) against {skill_dir.name} "
        f"with {args.harness} "
        f"(evaluation={args.evaluation_model}, "
        f"rubric={args.rubric_model}, effort={args.effort})",
        file=sys.stderr,
    )

    _INTERRUPTED.clear()
    results: list[ScenarioResult] = []
    executor = ThreadPoolExecutor(max_workers=args.parallel)
    futures = {}
    try:
        for scenario in scenarios:
            future = executor.submit(
                run_scenario,
                skill_md,
                scenario,
                args.harness,
                args.evaluation_model,
                args.rubric_model,
                args.effort,
            )
            futures[future] = scenario
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results.append(fut.result())
                print(f"  ✓ {s['id']} done", file=sys.stderr)
            except Exception as e:
                results.append(ScenarioResult.failure(s["id"], str(e)))
                print(f"  ✗ {s['id']} crashed: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        for future in futures:
            future.cancel()
        _stop_harness_processes()
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            _stop_harness_processes(force=True)
            executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown()

    results.sort(key=lambda r: [s["id"] for s in scenarios].index(r.id))
    print_report(skill_dir.name, results, args.verbose)
    sys.exit(0 if all(scenario_passed(r) for r in results) else 1)


def main(argv: list[str] | None = None):
    try:
        _main(argv)
    except KeyboardInterrupt:
        _stop_harness_processes()
        print("\nInterrupted. Stopped running scenarios.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
