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
uvx --from git+https://github.com/aaronfc/unvibe.git unvibe setup
uvx --from git+https://github.com/aaronfc/unvibe.git unvibe path/to/skill-dir
```

Or install it as a persistent tool:

```bash
uv tool install git+https://github.com/aaronfc/unvibe.git
unvibe setup
unvibe path/to/skill-dir
```

From a local checkout, `uv run unvibe ...` (or the `bin/unvibe` wrapper) runs
the same command against your working tree.

Before running an evaluation, install and authenticate at least one supported
harness: Claude Code, Codex, or OpenCode. `unvibe` invokes that harness's local
CLI; it does not manage the harness installation or login.

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
run requires a configured harness, evaluation model, and rubric model. Normal
runs have no implicit harness or model defaults.

### First-time setup

Choose the three required values interactively:

```bash
unvibe setup
```

After you choose Claude or Codex, setup still asks for both models and shows
the suggested pair as defaults. Press Enter to accept each default, or type a
different model. OpenCode models remain explicit because they are
provider-specific. Setup saves the result to
`~/.config/unvibe/config.yaml`, or to `$XDG_CONFIG_HOME/unvibe/config.yaml`
when `XDG_CONFIG_HOME` is set. Running `unvibe setup` again ignores the saved
answers, repeats the setup process, and overwrites that file.

To configure without prompts:

```bash
unvibe setup \
  --harness claude \
  --evaluation-model opus \
  --rubric-model haiku \
  --effort medium
```

You can also write the YAML file directly:

```yaml
version: 1
harness: claude
evaluation_model: opus
rubric_model: haiku
effort: medium
```

Set `UNVIBE_CONFIG` or pass `--config` to use a different file. The config
path itself uses this precedence:

```text
--config > UNVIBE_CONFIG > XDG/default config path
```

### Environment setup

All runtime choices can instead come from the environment:

```bash
export UNVIBE_HARNESS=codex
export UNVIBE_EVALUATION_MODEL=gpt-5.6-sol
export UNVIBE_RUBRIC_MODEL=gpt-5.6-luna
export UNVIBE_EFFORT=medium
unvibe path/to/skill-dir
```

For each runtime value, configuration uses this precedence:

```text
CLI parameter > environment variable > user config
```

### Configuration reference

| Purpose | CLI | Environment | YAML key | Required | Accepted value |
|---|---|---|---|---|---|
| Harness | `--harness` | `UNVIBE_HARNESS` | `harness` | Yes | `claude`, `codex`, or `opencode` |
| Evaluation model | `--evaluation-model` | `UNVIBE_EVALUATION_MODEL` | `evaluation_model` | Yes | Model value accepted by the selected harness |
| Rubric model | `--rubric-model` | `UNVIBE_RUBRIC_MODEL` | `rubric_model` | Yes | Model value accepted by the selected harness |
| Effort | `--effort` | `UNVIBE_EFFORT` | `effort` | No | Harness-specific value; defaults to `medium` |
| Config path | `--config` | `UNVIBE_CONFIG` | — | No | Filesystem path to a version 1 YAML config |

Model and effort values are passed to the selected harness:

| Harness | Model format | Suggested evaluation/rubric pair | Effort mapping and values |
|---|---|---|---|
| Claude Code | Alias such as `opus`, `sonnet`, or `haiku`, or a full model ID accepted by `claude --model` | `opus` / `haiku` | `claude --effort`; commonly `low`, `medium`, `high`, `xhigh`, or `max` |
| Codex | Model slug accepted by `codex --model`, such as `gpt-5.6-sol` | `gpt-5.6-sol` / `gpt-5.6-luna` | `model_reasoning_effort`; `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`, subject to model/account support |
| OpenCode | Required `provider/model` form accepted by `opencode --model` | Provider-specific | `opencode --variant`; values are provider/model-specific |

The suggested pairs are defaults only in the interactive setup prompts; normal
runs never select models implicitly. Model availability can vary by harness
version and account. `unvibe` passes model and effort strings through; the
selected harness reports unsupported values.

Use `CLAUDE_BIN`, `CODEX_BIN`, or `OPENCODE_BIN` to override the corresponding
executable. Each value is a filesystem path or command name for that harness
binary.

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
