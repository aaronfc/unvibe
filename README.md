# unvibe

Tiny pseudo-evals for `SKILL.md`.

`unvibe` pokes a skill with scenario prompts and asks Claude what tools it
would call. It does not execute those tools. The result is a lightweight smoke
test for skill drift: useful, imperfect, and intentionally small.

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
unvibe path/to/skill-dir
unvibe path/to/skill-dir --scenario happy_path
unvibe path/to/skill-dir --verbose
unvibe path/to/skill-dir --parallel 5
unvibe --create path/to/skill-dir
```

Each skill directory must contain:

```text
SKILL.md
EVALUATION.yaml
```

`unvibe` uses `claude -p` by default. Set `CLAUDE_BIN` to use a different
Claude executable.

## Creating EVALUATION.yaml

Use `--create` to generate a first-pass eval file from an existing `SKILL.md`:

```bash
bin/unvibe --create path/to/skill-dir
```

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

`tests/smoke.sh` builds and runs the packaged command against that example
using a stubbed `CLAUDE_BIN`, so it stays offline and deterministic:

```bash
tests/smoke.sh
```
