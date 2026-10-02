# M2g.2 + M2f.3 — continuous skills and Microduck official-policy execution

Status: execution foundation implemented; complete playable-motion acceptance remains open because turn-after-stop tracking is weak.

Implemented scope: trusted `SkillRunner`/`RunnerRegistry`, per-robot Executive execution, pinned external Microduck package, continuous MuJoCo worker, bounded official velstand commands, reports and terminal keyboard play. M2g.1 metadata discovery remains separate from execution readiness. Custom weights and LLM tool routing belong to M2g.3; Go2 follows Microduck.

## Separation and contracts

`SkillRegistry.prepare` validates manifest/body/backend/policy/parameters. `RunnerRegistry` contains host-registered implementations; manifests cannot name shell commands or load arbitrary code. `Executive.run_skill` acquires existing shared resources, owns timeout/cancellation/event preemption, and invokes start/poll/stop. The Microduck bridge owns only its versioned adapter and NDJSON process channel. A new runner can implement this interface without changing the generic registry or Executive.

`whole_body` currently acquires Base + Arm + Gripper in the same Executive used by trajectories. Unknown resources fail closed. A general resource hierarchy and cross-process/multiple robot orchestration remain future work. The old arm trajectory path is unchanged; Franka's manifest is still descriptive until its runner is implemented.

Protocol `archon.continuous.v1` uses correlated request IDs and hello/command/poll/observe/stop/shutdown operations. The worker advances real physics independently of host requests: policy nominally 50 Hz, physics 200 Hz (four steps per tick). Commands contain body-frame vx/vy/yaw_rate, wall-clock duration and a lease ≤600 ms. Poll renews the lease; observe does not. Expiry and input EOF force zero twist. Worker initialization uses the official pose once; command transitions never reset position or policy history.

The installed policy has 61 observations and 14 joint-offset actions. Names/action scaling and Python/runtime versions are checked before startup. Source commits, weights SHA-256 and licenses are locked in `robots/microduck.lock.json`. Package tree integrity is checked at every launch. Upstream model licensing remains separate from Apache code/policy licensing; downloaded meshes and weights stay outside Git.

## Completion and safety behavior

Runner checks measured upright entry (tilt ≤15°, trunk height ≥0.10 m). Runtime handles cancellation, deadline, worker errors and preempting events, always attempts stop, then releases shared locks. A normal result requires duration completion and measured settling after zero twist: planar speed <0.015 m/s and absolute yaw rate <0.08 rad/s for three successive samples. Cleanup has a separate four-second ceiling, so reported elapsed time can exceed the call deadline. Unconfirmed stop or worker fault returns failure.

Worker latches a fault for tilt >45°, trunk height <0.075 m or non-finite state. Fallen state rejects further motion, holds current joint targets and requires an explicit process restart; this is not a recovery skill. Non-finite physics terminates the worker. Controlled shutdown is bounded; parent failure is additionally covered by lease/EOF. This is simulation behavior, not a hardware emergency-stop implementation.

`microduck.stand`, `microduck.walk`, `microduck.stop` share velstand; stand/stop supply zero twist. Walk parameters are bounded by vx ±0.30 m/s, vy ±0.20 m/s, yaw ±1.0 rad/s; supported envelope does not mean accurate tracking or acceptance of every combination. The demonstrated forward/turn presets remain vx=0.3 and vx=0.2,yaw=1.0. Low-command stationarity is still unresolved. Repeated stop-to-motion transitions also showed weak motion at the nominal presets; acceptance records each motion gate separately and does not treat lifecycle success as tracking success. Results retain measured pose, velocity, tilt, simulation time and motion samples; a completed interval does not prove a requested distance was achieved.

## Install and play

From repository root, with Python 3.12 and uv:

```sh
uv venv --python 3.12 .venv-microduck
uv pip install --python .venv-microduck/bin/python -r python/requirements-microduck-probe.txt
cargo run -p robo-archon-cli -- --install-robot microduck
cargo run -p robo-archon-cli -- --doctor-robot microduck
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco --skill-keyboard --viewer
```

Default runtime uses `.venv-microduck/bin/python`; macOS viewer selects its `mjpython`. Override with `ROBO_ARCHON_PYTHON` when needed. Keyboard requires a TTY: w forward; a/d turn; x cancel/stop; q or Ctrl-C cancel/exit. Each command expires after two seconds; policy remains active while idle. Keys are processed sequentially, with x/q interrupting the active call. Viewer rendering needs a graphical session.

One-shot execution, including headless CPU use:

```sh
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco \
  --run-skill skills/calls/microduck-forward.json \
  --skill-report tmp-episodes/microduck-result.json
```

Add `--auto-stop-ms 1000` to exercise cancellation (a cancelled one-shot returns nonzero). `--skill-record-dir tmp-episodes/new-frames` records local PNG frames; directory must not already exist. Public previews may be optimized GIFs; videos and downloaded assets are never committed.

Reproduce the continuous worker checks with:

```sh
.venv-microduck/bin/python scripts/verify_microduck_runtime.py \
  --report tmp-episodes/microduck-worker-acceptance.json
.venv-microduck/bin/python scripts/verify_microduck_fall.py
cargo test --workspace --offline
```

## Acceptance limits

Evidence is recorded in `docs/validation/m2g-2-microduck.json`. CPU tests cover continuous standing/forward/turn command execution with separate measured motion gates, measured stop, repeated commands in one worker, command expiry, independent lease expiry, invalid command rejection, disconnect exit and Archon cancellation. Runtime tests cover timeout/start failure/cancellation cleanup and conflict with trajectory locks. Fall detection/latching has threshold checks plus a physical push test that confirms fault latching and rejection of further commands. Varied starting poses, long-duration reliability, native GUI visual acceptance, hardware transfer and other official tricks remain unverified. The catalog is therefore `controlled`, not `task_verified`.
