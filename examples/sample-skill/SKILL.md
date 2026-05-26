---
name: git-status-reporter
description: Report the state of the working tree without changing it.
---

# git-status-reporter

Use this skill when the user asks for the current state of a git working tree.

## What to do

1. Run `git status --short` to see staged, unstaged, and untracked changes.
2. Summarize the result for the user in plain language.

## Rules

- This skill is read-only. NEVER stage, commit, push, reset, or force-push.
- Do not run `git add`, `git commit`, `git push`, or any `--force` flag.
- If the user asks you to mutate the tree, decline and explain that this
  skill only reports status.
