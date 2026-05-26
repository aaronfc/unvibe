#!/usr/bin/env bash
# Local smoke test for the packaged `unvibe` command.
#
# Builds and runs the package via `uvx --from .` (the same path real users hit
# with `uvx --from git+...`), and exercises the CLI against the bundled sample
# skill in examples/sample-skill.
#
# A fake CLAUDE_BIN returns a fixed action plan so the run is offline and
# deterministic — no network, no real Claude call. We only run the regex-only
# scenario so the LLM judge is never invoked.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Fake `claude -p` that always emits the action plan the read-only sample skill
# is expected to produce.
cat > "$tmp/claude" <<'EOF'
#!/usr/bin/env bash
cat <<'JSON'
[{"tool": "Bash", "args": "git status --short", "why": "inspect the working tree"}]
JSON
EOF
chmod +x "$tmp/claude"
export CLAUDE_BIN="$tmp/claude"

echo "smoke: unvibe --help"
uvx --from . unvibe --help >/dev/null

echo "smoke: evaluate examples/sample-skill --scenario reports_status"
out="$(uvx --from . unvibe examples/sample-skill --scenario reports_status)"
echo "$out"

if ! echo "$out" | grep -q "1/1 scenarios passed"; then
  echo "FAIL: expected '1/1 scenarios passed' in output" >&2
  exit 1
fi

echo "SMOKE OK"
