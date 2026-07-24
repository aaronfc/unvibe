![unvibe logo](https://github.com/user-attachments/assets/dc7558be-ae4d-4c84-a908-f7996ce630af)

# unvibe

Tiny pseudo-evals for `SKILL.md`.

`unvibe` pokes a skill with scenario prompts and asks a coding-agent harness
what tools it would call. It does not execute those planned tools. The result
is a lightweight smoke test for skill drift: useful, imperfect, and
intentionally small.

## Install

Run it without installing anything:

```bash
uvx --from git+https://github.com/aaronfc/unvibe.git unvibe path/to/skill-dir
```

Or install it as a persistent tool:

```bash
uv tool install git+https://github.com/aaronfc/unvibe.git
unvibe path/to/skill-dir
```

From a local checkout, `uv run unvibe ...` (or the `bin/unvibe` wrapper) runs
the same command against your working tree.

## Usage

```bash
unvibe setup
unvibe path/to/skill-dir
unvibe path/to/skill-dir --scenario happy_path
unvibe path/to/skill-dir --verbose
unvibe path/to/skill-dir --parallel 5
unvibe path/to/skill-dir \
  --harness codex \
  --evaluation-model gpt-5.6-sol \
  --rubric-model gpt-5.6-luna
unvibe --create path/to/skill-dir
```

Each skill directory must contain:

```text
SKILL.md
EVALUATION.yaml
```

## Runtime configuration

`unvibe` supports the native Claude Code, Codex, and OpenCode harnesses. Every
run requires an explicit harness, evaluation model, and rubric model. There
are no implicit harness or model defaults.

Configuration uses this precedence:

```text
CLI parameter > environment variable > user config
```

Choose the three required values interactively and save them to
`~/.config/unvibe/config.yaml`:

```bash
unvibe setup
```

Or configure them without prompts:

```bash
unvibe setup \
  --harness claude \
  --evaluation-model opus \
  --rubric-model haiku
```

The saved file has this shape:

```yaml
version: 1
harness: claude
evaluation_model: opus
rubric_model: haiku
effort: medium
```

Set `UNVIBE_CONFIG` or pass `--config` to use a different file. Environment
configuration is also supported:

```bash
export UNVIBE_HARNESS=codex
export UNVIBE_EVALUATION_MODEL=gpt-5.6-sol
export UNVIBE_RUBRIC_MODEL=gpt-5.6-luna
export UNVIBE_EFFORT=medium
unvibe path/to/skill-dir
```

Suggested starting pairs are intentionally guidance, not defaults:

| Harness | Evaluation | Rubric |
|---|---|---|
| Claude Code | `opus` | `haiku` |
| Codex | `gpt-5.6-sol` | `gpt-5.6-luna` |

OpenCode models use its `provider/model` format and must also be chosen
explicitly.

Effort is optional and defaults to `medium`. Configure it with `--effort`,
`UNVIBE_EFFORT`, or the user config. It maps to the harness-native control:

```text
claude   -> --effort
codex    -> model_reasoning_effort
opencode -> --variant
```

Use `CLAUDE_BIN`, `CODEX_BIN`, or `OPENCODE_BIN` to override the corresponding
executable.

## Creating EVALUATION.yaml

Use `--create` to generate a first-pass eval file from an existing `SKILL.md`:

```bash
bin/unvibe --create path/to/skill-dir
```

This uses the same required runtime configuration as a normal evaluation.
This writes `path/to/skill-dir/EVALUATION.yaml`. If that file already exists,
`unvibe` exits without changing it. Use `--force` to replace it:

```bash
unvibe --create path/to/skill-dir --force
```

The generated file is a starting point. Read it before trusting it.

## EVALUATION.yaml

```yaml
version: 1
scenarios:
  - id: happy_path
    user_message: |
      Verify PR #123 with evals and update the PR description.
    must_include:
      - "ssh .*eval-runner\\.sh.*--label[= ]before"
      - "ssh .*eval-runner\\.sh.*--label[= ]after"
    must_not_include:
      - "gh pr merge"
    rubric:
      - "The plan preserves the before log before running the after pass."
```

Assertions:

- `must_include`: case-insensitive Python regexes that must appear in the
  planned tool calls.
- `must_not_include`: case-insensitive Python regexes that must not appear in
  the planned tool calls.
- `rubric`: optional natural-language claims judged against the planned tool
  calls.

Exit code is `0` when every scenario passes and `1` otherwise.

## Development

A runnable example lives in [`examples/sample-skill`](examples/sample-skill).

The pure functions in `unvibe.cli` (response parsing, spec validation, plan
flattening, pass/fail evaluation) have fast, offline unit tests. From a source
checkout:

```bash
uv run pytest
```

`tests/smoke.sh` builds and runs the packaged command against that example
using stubbed harness binaries, so it stays offline and deterministic:

```bash
tests/smoke.sh
```
