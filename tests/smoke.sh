#!/usr/bin/env bash
# Local smoke test for the packaged `unvibe` command.
#
# Builds and runs the package via `uvx --from .` (the same path real users hit
# with `uvx --from git+...`), and exercises the CLI against the bundled sample
# skill in examples/sample-skill.
#
# Fake harness binaries return a fixed action plan so the run is offline and
# deterministic. We only run the regex-only scenario, so the LLM judge is
# never invoked.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Each fake harness records its native arguments, then emits the action plan
# the read-only sample skill is expected to produce.
cat > "$tmp/harness-stub" <<'EOF'
#!/usr/bin/env bash
printf '%q ' "$@" >> "$UNVIBE_SMOKE_ARGS"
printf '\n' >> "$UNVIBE_SMOKE_ARGS"
if [[ "$*" == *"You are grading several independent claims"* ]]; then
  cat <<'JSON'
[{"verdict": "PASS", "reason": "The plan preserves read-only behavior."}]
JSON
elif [[ "$*" == *"Stage everything and commit it"* ]]; then
  cat <<'JSON'
[{"tool": "Text", "args": "I cannot mutate the read-only working tree.", "why": "decline mutation"}]
JSON
else
cat <<'JSON'
[{"tool": "Bash", "args": "git status --short", "why": "inspect the working tree"}]
JSON
fi
EOF
chmod +x "$tmp/harness-stub"
cp "$tmp/harness-stub" "$tmp/claude"
cp "$tmp/harness-stub" "$tmp/codex"
cp "$tmp/harness-stub" "$tmp/opencode"
export CLAUDE_BIN="$tmp/claude"
export CODEX_BIN="$tmp/codex"
export OPENCODE_BIN="$tmp/opencode"

echo "smoke: unvibe --help"
uvx --refresh-package unvibe --from . unvibe --help >/dev/null

for harness in claude codex opencode; do
  echo "smoke: evaluate with $harness"
  export UNVIBE_SMOKE_ARGS="$tmp/$harness.args"
  : > "$UNVIBE_SMOKE_ARGS"
  out="$(
    uvx --from . unvibe examples/sample-skill \
      --scenario reports_status \
      --harness "$harness" \
      --evaluation-model smoke-model
  )"
  echo "$out"

  if ! echo "$out" | grep -q "1/1 scenarios passed"; then
    echo "FAIL: expected '1/1 scenarios passed' in output" >&2
    exit 1
  fi

  args="$(sed -n '1p' "$UNVIBE_SMOKE_ARGS")"
  case "$harness" in
    claude)
      expected="-p --no-session-persistence --model smoke-model "
      ;;
    codex)
      expected="exec --ephemeral --model smoke-model "
      ;;
    opencode)
      expected="run --model smoke-model "
      ;;
  esac
  if [[ "$args" != "$expected"* ]]; then
    echo "FAIL: $harness arguments did not start with '$expected': $args" >&2
    exit 1
  fi
done

echo "smoke: separate evaluation and rubric models"
export UNVIBE_SMOKE_ARGS="$tmp/split-model.args"
: > "$UNVIBE_SMOKE_ARGS"
out="$(
  uvx --from . unvibe examples/sample-skill \
    --scenario refuses_mutation \
    --harness claude \
    --evaluation-model sonnet-test \
    --rubric-model haiku-test
)"
echo "$out"

if ! echo "$out" | grep -q "1/1 scenarios passed"; then
  echo "FAIL: expected split-model scenario to pass" >&2
  exit 1
fi

evaluation_args="$(sed -n '1p' "$UNVIBE_SMOKE_ARGS")"
rubric_args="$(sed -n '2p' "$UNVIBE_SMOKE_ARGS")"
if [[ "$evaluation_args" != "-p --no-session-persistence --model sonnet-test "* ]]; then
  echo "FAIL: evaluation call did not use sonnet-test: $evaluation_args" >&2
  exit 1
fi
if [[ "$rubric_args" != "-p --no-session-persistence --model haiku-test "* ]]; then
  echo "FAIL: rubric call did not use haiku-test: $rubric_args" >&2
  exit 1
fi

echo "SMOKE OK"
