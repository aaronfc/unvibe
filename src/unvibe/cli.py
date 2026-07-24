"""
unvibe — Tiny pseudo-evals for SKILL.md files.

Usage:
    unvibe <skill-dir> [--harness <name>] [--evaluation-model <model>]
    unvibe --create <skill-dir> [--harness <name>] [--evaluation-model <model>]

Normal mode reads SKILL.md and EVALUATION.yaml from <skill-dir>. For each scenario:
  1. Asks the selected coding-agent harness what tool calls it would make.
  2. Checks must_include / must_not_include regex patterns.
  3. Optionally asks an LLM judge to score rubric items.

Create mode reads SKILL.md and writes a first-pass EVALUATION.yaml.

Exits 0 if all scenarios pass, 1 otherwise.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

DEFAULT_HARNESS = "claude"
SUPPORTED_HARNESSES = ("claude", "codex", "opencode")
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
You are generating an EVALUATION.yaml file for unvibe, a tiny pseudo-eval
runner for SKILL.md files.

unvibe evaluates a skill by loading SKILL.md, showing the agent a user message,
and asking it to output the JSON action plan it WOULD take. unvibe then checks:

- must_include: Python regexes that must appear in flattened planned tool calls.
- must_not_include: Python regexes that must not appear in planned tool calls.
- rubric: natural-language claims judged against the planned action plan.

The flattened plan looks like:
  [0] Bash: git status --short
  [1] Read: path/to/file

Generate a detailed first-pass EVALUATION.yaml for the skill below.

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
Repair this generated EVALUATION.yaml so it validates for unvibe.

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
    harness: str, prompt: str, model: str | None = None
) -> list[str]:
    """Build the native noninteractive command for a supported harness."""
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError(f"unsupported harness: {harness}")

    command = [HARNESS_BINS[harness], *HARNESS_COMMANDS[harness]]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    return command


def call_harness(
    prompt: str,
    harness: str = DEFAULT_HARNESS,
    model: str | None = None,
    timeout: int = 180,
) -> str:
    """Invoke a coding-agent harness and return its assistant text."""
    command = build_harness_command(harness, prompt, model)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        binary = HARNESS_BINS[harness]
        sys.exit(
            f"error: '{binary}' not on PATH. Set ${HARNESS_BIN_ENV[harness]}."
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return (
            f"__error__: {harness} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def call_claude(prompt: str, timeout: int = 180) -> str:
    """Backward-compatible wrapper for the original Claude-only API."""
    return call_harness(prompt, "claude", timeout=timeout)


def extract_json_array(text: str) -> list[dict[str, Any]] | None:
    """Pull a JSON array out of the model's response.

    Handles bare JSON, fenced JSON, and JSON with leading/trailing prose.
    """
    if not text or text.startswith("__error__"):
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
    if not text or text.startswith("__error__"):
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
    """Validate the small subset of EVALUATION.yaml that unvibe understands."""
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
    harness: str = DEFAULT_HARNESS,
    evaluation_model: str | None = None,
) -> str:
    """Ask a coding-agent harness to generate a first-pass EVALUATION.yaml."""
    prompt = CREATE_INSTRUCTIONS + f"\n\n<SKILL>\n{skill_md}\n</SKILL>\n"
    last_error = "unknown error"

    for _ in range(2):
        raw = call_harness(prompt, harness, evaluation_model, timeout=300)
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
                last_error = f"generated EVALUATION.yaml did not validate: {exc}"

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
    harness: str = DEFAULT_HARNESS,
    evaluation_model: str | None = None,
    rubric_model: str | None = None,
) -> dict[str, Any]:
    """Run one scenario; return a result dict."""
    user_msg = scenario["user_message"]
    prompt = (
        NARRATE_INSTRUCTIONS
        + f"\n\n<SKILL>\n{skill_md}\n</SKILL>\n\n<USER>\n{user_msg}\n</USER>\n"
    )
    raw = call_harness(prompt, harness, evaluation_model)
    plan = extract_json_array(raw)

    result: dict[str, Any] = {
        "id": scenario["id"],
        "plan": plan,
        "raw": raw,
        "must_include": [],
        "must_not_include": [],
        "rubric": [],
        "error": None,
    }

    if plan is None:
        result["error"] = "Could not parse action plan from Claude's response"
        return result

    searchable = plan_to_searchable(plan)

    for pat in scenario.get("must_include", []):
        ok = bool(re.search(pat, searchable, re.IGNORECASE))
        result["must_include"].append({"pattern": pat, "passed": ok})

    for pat in scenario.get("must_not_include", []):
        match = re.search(pat, searchable, re.IGNORECASE)
        ok = match is None
        result["must_not_include"].append(
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
        judge_model = rubric_model if rubric_model is not None else evaluation_model
        raw_verdicts = call_harness(judge_prompt, harness, judge_model)
        parsed = extract_json_array(raw_verdicts)

        if not isinstance(parsed, list) or len(parsed) != len(claims):
            err = f"judge returned malformed response (got {type(parsed).__name__}, expected list of {len(claims)}): {raw_verdicts[:200]}"
            for claim in claims:
                result["rubric"].append(
                    {"claim": claim, "passed": False, "verdict": f"FAIL: {err}"}
                )
        else:
            for claim, entry in zip(claims, parsed):
                verdict_str = str(entry.get("verdict", "")).strip().upper()
                reason = str(entry.get("reason", "")).strip()
                passed = verdict_str == "PASS"
                result["rubric"].append(
                    {"claim": claim, "passed": passed, "verdict": f"{verdict_str}: {reason}"}
                )

    return result


def scenario_passed(result: dict[str, Any]) -> bool:
    if result["error"]:
        return False
    for group in ("must_include", "must_not_include", "rubric"):
        if any(not item["passed"] for item in result[group]):
            return False
    return True


def print_report(skill_name: str, results: list[dict[str, Any]], verbose: bool):
    passed_count = sum(1 for r in results if scenario_passed(r))
    print(f"\n{skill_name} — {len(results)} scenario(s)\n")

    for r in results:
        mark = green("✓") if scenario_passed(r) else red("✗")
        print(f"  {r['id']:<60} {mark}")

        if r["error"]:
            print(f"    {red('error:')} {r['error']}")
            if verbose and r["raw"]:
                print(f"    {dim('raw:')} {r['raw'][:500]}")
            continue

        for group_name, items in [
            ("must_include", r["must_include"]),
            ("must_not_include", r["must_not_include"]),
            ("rubric", r["rubric"]),
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

        if verbose and r["plan"]:
            print(f"    {dim('plan:')}")
            for entry in r["plan"]:
                print(f"      {dim('-')} {entry.get('tool')}: {entry.get('args', '')[:120]}")

    print(f"\n  {passed_count}/{len(results)} scenarios passed\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments and resolve CLI-over-environment configuration."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("skill_dir", help="Path to skill directory")
    ap.add_argument(
        "--create",
        action="store_true",
        help="Generate EVALUATION.yaml from SKILL.md",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite EVALUATION.yaml when used with --create",
    )
    ap.add_argument("--scenario", help="Run only this scenario id")
    ap.add_argument(
        "--harness",
        choices=SUPPORTED_HARNESSES,
        help="Coding-agent harness (default: $UNVIBE_HARNESS or claude)",
    )
    ap.add_argument(
        "--evaluation-model",
        "--model",
        dest="evaluation_model",
        help=(
            "Scenario model (default: $UNVIBE_EVALUATION_MODEL, "
            "$UNVIBE_MODEL, or harness native default)"
        ),
    )
    ap.add_argument(
        "--rubric-model",
        help=(
            "Rubric judge model (default: $UNVIBE_RUBRIC_MODEL or "
            "evaluation model)"
        ),
    )
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
    args = ap.parse_args(argv)

    args.harness = (
        args.harness or os.environ.get("UNVIBE_HARNESS") or DEFAULT_HARNESS
    )
    if args.harness not in SUPPORTED_HARNESSES:
        choices = ", ".join(SUPPORTED_HARNESSES)
        ap.error(
            f"$UNVIBE_HARNESS must be one of: {choices} "
            f"(got {args.harness!r})"
        )
    if args.evaluation_model is None:
        args.evaluation_model = (
            os.environ.get("UNVIBE_EVALUATION_MODEL")
            or os.environ.get("UNVIBE_MODEL")
            or None
        )
    if args.rubric_model is None:
        args.rubric_model = (
            os.environ.get("UNVIBE_RUBRIC_MODEL") or args.evaluation_model
        )
    return args


def main(argv: list[str] | None = None):
    args = parse_args(argv)

    skill_dir = Path(args.skill_dir).resolve()
    skill_md_path = skill_dir / "SKILL.md"
    eval_path = skill_dir / "EVALUATION.yaml"
    if not skill_md_path.exists():
        sys.exit(f"error: {skill_md_path} not found")

    if args.create:
        if eval_path.exists() and not args.force:
            sys.exit(
                f"error: {eval_path} already exists. Use --force to overwrite it."
            )
        skill_md = skill_md_path.read_text()
        eval_path.write_text(
            create_evaluation_yaml(skill_md, args.harness, args.evaluation_model)
        )
        print(f"created {eval_path}")
        return

    if not eval_path.exists():
        sys.exit(f"error: {eval_path} not found")

    skill_md = skill_md_path.read_text()
    eval_spec = yaml.safe_load(eval_path.read_text())
    scenarios = eval_spec.get("scenarios", [])
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            sys.exit(f"error: no scenario with id '{args.scenario}'")

    print(f"Running {len(scenarios)} scenario(s) against {skill_dir.name}", file=sys.stderr)

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futures = {
            ex.submit(
                run_scenario,
                skill_md,
                s,
                args.harness,
                args.evaluation_model,
                args.rubric_model,
            ): s
            for s in scenarios
        }
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                results.append(fut.result())
                print(f"  ✓ {s['id']} done", file=sys.stderr)
            except Exception as e:
                results.append({"id": s["id"], "error": str(e), "plan": None, "raw": "",
                                "must_include": [], "must_not_include": [], "rubric": []})
                print(f"  ✗ {s['id']} crashed: {e}", file=sys.stderr)

    results.sort(key=lambda r: [s["id"] for s in scenarios].index(r["id"]))
    print_report(skill_dir.name, results, args.verbose)
    sys.exit(0 if all(scenario_passed(r) for r in results) else 1)


if __name__ == "__main__":
    main()
