# RoboArchon

Rust embodied Agent OS (Physical AI). Workspace focused on the embodied plane.

## Build & Run

```bash
cargo build
cargo run -p robo-archon-cli -- --backend sim --step-ms 0
# MuJoCo (needs .venv-mujoco):
export ROBO_ARCHON_PYTHON="$(pwd)/.venv-mujoco/bin/python"
cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:diff_car \
  --policy instruction --viewer --tui --step-ms 0
```

Binary name: `robo-archon`.

## Project Structure

```
crates/
  robo-archon-embodied/     — Types, Policy/SafetyGate/RobotBackend traits
  robo-archon-runtime/      — Executive, EventBus, locks, Arbiter
  robo-archon-kinetic/      — Chronos interpolation
  robo-archon-policy/       — Mock / ColorBlob / Instruction / LlmPolicy
  robo-archon-perception/   — Camera, detectors, Observation enrich
  robo-archon-ros2/         — Topic contract (/robo_archon/arm/…)
  robo-archon-sim/          — In-process SimBackend
  robo-archon-sim-bridge/   — NDJSON bridge to Python MuJoCo workers
  robo-archon-cli/          — Binary `robo-archon`
python/robo_archon_sim_workers/  — MuJoCo / mock workers
```

Digital-plane crates (`archon-core/llm/tools/cli`) remain in-tree but are not workspace members; iterate them in the separate `archon` repo later.

## Key Conventions

- Tools/policies implement traits in `robo_archon_embodied`.
- LLM/strategies only propose; Safety + Executive authorize motion.
- Env: prefer `ROBO_ARCHON_*` (`ARCHON_*` still accepted as compat for Python/mjpython).

See `ARCHITECTURE.md` and `docs/embodied-getting-started.md`.
