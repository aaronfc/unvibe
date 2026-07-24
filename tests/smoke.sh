#!/usr/bin/env bash
# Local smoke test for the packaged `unvibe` command.
#
# Builds and runs the package via `uvx --from .` (the same path real users hit
# with `uvx --from git+...`), and exercises the CLI against the bundled sample
# skill in examples/sample-skill.
#
# Fake harness binaries return fixed plans and rubric verdicts so the run is
# offline and deterministic.
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

unset UNVIBE_HARNESS
unset UNVIBE_MODEL
unset UNVIBE_EVALUATION_MODEL
unset UNVIBE_RUBRIC_MODEL
unset UNVIBE_EFFORT

echo "smoke: missing configuration fails with guidance"
export UNVIBE_CONFIG="$tmp/missing-config.yaml"
if uvx --from . unvibe examples/sample-skill >"$tmp/missing.out" 2>&1; then
  echo "FAIL: unconfigured run should fail" >&2
  exit 1
fi
for required_text in \
  "missing required configuration" \
  "unvibe setup" \
  "UNVIBE_EVALUATION_MODEL" \
  "evaluation=gpt-5.6-sol, rubric=gpt-5.6-luna"
do
  if ! grep -q "$required_text" "$tmp/missing.out"; then
    echo "FAIL: missing-config output lacks '$required_text'" >&2
    exit 1
  fi
done

echo "smoke: setup writes explicit runtime choices"
export UNVIBE_CONFIG="$tmp/config.yaml"
uvx --from . unvibe setup \
  --harness claude \
  --evaluation-model opus \
  --rubric-model haiku >/dev/null

export UNVIBE_SMOKE_ARGS="$tmp/setup-config.args"
: > "$UNVIBE_SMOKE_ARGS"
out="$(
  uvx --from . unvibe examples/sample-skill \
    --scenario refuses_mutation
)"
echo "$out"

if ! echo "$out" | grep -q "1/1 scenarios passed"; then
  echo "FAIL: expected configured split-model scenario to pass" >&2
  exit 1
fi

evaluation_args="$(sed -n '1p' "$UNVIBE_SMOKE_ARGS")"
rubric_args="$(sed -n '2p' "$UNVIBE_SMOKE_ARGS")"
if [[ "$evaluation_args" != "-p --no-session-persistence --model opus --effort medium "* ]]; then
  echo "FAIL: configured evaluation call did not use opus: $evaluation_args" >&2
  exit 1
fi
if [[ "$rubric_args" != "-p --no-session-persistence --model haiku --effort medium "* ]]; then
  echo "FAIL: configured rubric call did not use haiku: $rubric_args" >&2
  exit 1
fi

for harness in claude codex opencode; do
  echo "smoke: evaluate with $harness"
  export UNVIBE_SMOKE_ARGS="$tmp/$harness.args"
  : > "$UNVIBE_SMOKE_ARGS"
  out="$(
    uvx --from . unvibe examples/sample-skill \
      --scenario reports_status \
      --harness "$harness" \
      --evaluation-model smoke-model \
      --rubric-model smoke-rubric-model \
      --effort high
  )"
  echo "$out"

  if ! echo "$out" | grep -q "1/1 scenarios passed"; then
    echo "FAIL: expected '1/1 scenarios passed' in output" >&2
    exit 1
  fi

  args="$(sed -n '1p' "$UNVIBE_SMOKE_ARGS")"
  case "$harness" in
    claude)
      expected="-p --no-session-persistence --model smoke-model --effort high "
      ;;
    codex)
      expected="exec --ephemeral --model smoke-model -c model_reasoning_effort=\\\"high\\\" "
      ;;
    opencode)
      expected="run --model smoke-model --variant high "
      ;;
  esac
  if [[ "$args" != "$expected"* ]]; then
    echo "FAIL: $harness arguments did not start with '$expected': $args" >&2
    exit 1
  fi
done

echo "SMOKE OK"
