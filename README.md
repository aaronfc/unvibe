![unvibe logo](https://github.com/user-attachments/assets/dc7558be-ae4d-4c84-a908-f7996ce630af)

# unvibe

Tiny literal pseudo-evals for instruction documents.

`unvibe` pokes a `SKILL.md`, `AGENTS.md`, or `CLAUDE.md` with scenario prompts
and asks Claude what tools it would call. It does not execute those tools. The
result is a lightweight smoke test for instruction drift: useful, imperfect,
and intentionally small.

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
unvibe path/to/AGENTS.md
unvibe path/to/CLAUDE.md
unvibe path/to/skill-dir --scenario happy_path
unvibe path/to/skill-dir --verbose
unvibe path/to/skill-dir --parallel 5
unvibe path/to/CLAUDE.md --evaluation CLAUDE.EVALUATION.yaml
unvibe --create path/to/AGENTS.md
```

The target can be an explicit file named `SKILL.md`, `AGENTS.md`, or
`CLAUDE.md`. For backward compatibility, it can also be a directory containing
exactly one of those filenames. If a directory contains several supported
files, name the intended file explicitly.

By default, the evaluation lives beside the selected instruction document:

```text
AGENTS.md
EVALUATION.yaml
```

Use `--evaluation <path>` when several instruction documents and suites share
a directory.

`unvibe` uses `claude -p` by default. Set `CLAUDE_BIN` to use a different
Claude executable.

## Literal mode and its limits

`unvibe` evaluates the selected file's text literally. It does not reproduce
native parent chains, overrides, imports, target-agent loading, or other
effective-context behavior. In particular, `@path` imports in `CLAUDE.md` are
not expanded; the CLI warns when it finds them.

Running an `AGENTS.md` through Claude is a format-neutral pseudo-eval, not a
claim that Claude and Codex interpret or execute the document identically.
Native effective-context execution and non-Claude backends remain out of scope
for this slice and are tracked in [#7](https://github.com/aaronfc/unvibe/issues/7).

## Creating EVALUATION.yaml

Use `--create` to generate a first-pass eval file from an existing instruction
document:

```bash
bin/unvibe --create path/to/AGENTS.md
```

This writes `path/to/EVALUATION.yaml`. If that file already exists, `unvibe`
exits without changing it. Use `--force` to replace it, and combine
`--evaluation` with `--create` to choose another output path:

```bash
unvibe --create path/to/AGENTS.md --force
unvibe --create path/to/CLAUDE.md --evaluation CLAUDE.EVALUATION.yaml
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

A backward-compatible `SKILL.md` example lives in
[`examples/sample-skill`](examples/sample-skill).

The pure functions in `unvibe.cli` (response parsing, spec validation, plan
flattening, pass/fail evaluation) have fast, offline unit tests. From a source
checkout:

```bash
uv run pytest
```

`tests/smoke.sh` builds and runs the packaged command against that example
using a stubbed `CLAUDE_BIN`, so it stays offline and deterministic:

```bash
tests/smoke.sh
```
