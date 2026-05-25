# unvibe

Tiny pseudo-evals for `SKILL.md`.

`unvibe` pokes a skill with scenario prompts and asks Claude what tools it
would call. It does not execute those tools. The result is a lightweight smoke
test for skill drift: useful, imperfect, and intentionally small.

## Usage

```bash
bin/unvibe path/to/skill-dir
bin/unvibe path/to/skill-dir --scenario happy_path
bin/unvibe path/to/skill-dir --verbose
```

Each skill directory must contain:

```text
SKILL.md
EVALUATION.yaml
```

`unvibe` uses `claude -p` by default. Set `CLAUDE_BIN` to use a different
Claude executable.

## EVALUATION.yaml

```yaml
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
