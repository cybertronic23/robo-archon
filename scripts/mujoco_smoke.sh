#!/usr/bin/env bash
# Headless MuJoCo smoke: connect → short instruction → shutdown (no viewer).
# Requires: .venv-mujoco (or ROBO_ARCHON_PYTHON) with mujoco installed, and a built CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -z "${ROBO_ARCHON_PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv-mujoco/bin/python" ]]; then
    export ROBO_ARCHON_PYTHON="$ROOT/.venv-mujoco/bin/python"
  fi
fi

if [[ -z "${ROBO_ARCHON_PYTHON:-}" ]] || ! "$ROBO_ARCHON_PYTHON" -c "import mujoco" 2>/dev/null; then
  echo "skip: mujoco Python env not ready (set ROBO_ARCHON_PYTHON or create .venv-mujoco)" >&2
  exit 0
fi

# Prefer EGL on Linux CI (no display).
if [[ "$(uname -s)" == "Linux" ]] && [[ -z "${MUJOCO_GL:-}" ]]; then
  export MUJOCO_GL=egl
fi

BIN="${ROBO_ARCHON_BIN:-}"
if [[ -z "$BIN" ]]; then
  cargo build -q -p robo-archon-cli
  BIN="$ROOT/target/debug/robo-archon"
fi

EPISODE_ROOT="${TMPDIR:-/tmp}/robo-archon-mujoco-smoke-$$"
mkdir -p "$EPISODE_ROOT"
trap 'rm -rf "$EPISODE_ROOT"' EXIT

echo "[mujoco_smoke] running short headless turn…"
"$BIN" \
  --backend mujoco \
  --model builtin:diff_car \
  --policy instruction \
  --instruction "向前走一点" \
  --step-ms 0 \
  --episode-dir "$EPISODE_ROOT" \
  --save-frames

# Expect at least one episode artifact.
if ! ls "$EPISODE_ROOT"/ep-*/episode.json >/dev/null 2>&1 \
  && ! ls "$EPISODE_ROOT"/ep-*.json >/dev/null 2>&1; then
  echo "fail: no episode output under $EPISODE_ROOT" >&2
  ls -la "$EPISODE_ROOT" >&2 || true
  exit 1
fi

echo "[mujoco_smoke] ok"
