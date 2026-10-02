# M2g.2 + M2f.3 — continuous skills and Microduck official-policy execution

Status: M2g.2 and the M2f.3 official velstand play scope accepted for the documented presets. Repeated stop/restart, two-second keyboard commands, cancellation and native macOS viewer startup/rendering passed. This is nominal flat-floor acceptance, not arbitrary velocity tracking or full upstream feature parity.

Implemented scope: trusted `SkillRunner`/`RunnerRegistry`, per-robot Executive execution, pinned external Microduck package, continuous MuJoCo worker, bounded official velstand commands, reports and terminal keyboard play. M2g.1 metadata discovery remains separate from execution readiness. Custom weights and LLM tool routing belong to M2g.3; Go2 follows Microduck.

## Separation and contracts

`SkillRegistry.prepare` validates manifest/body/backend/policy/parameters. `RunnerRegistry` contains host-registered implementations; manifests cannot name shell commands or load arbitrary code. `Executive.run_skill` acquires existing shared resources, owns timeout/cancellation/event preemption, and invokes start/poll/stop. The Microduck bridge owns only its versioned adapter and NDJSON process channel. A new runner can implement this interface without changing the generic registry or Executive.

`whole_body` currently acquires Base + Arm + Gripper in the same Executive used by trajectories. Unknown resources fail closed. A general resource hierarchy and cross-process/multiple robot orchestration remain future work. The old arm trajectory path is unchanged; Franka's manifest is still descriptive until its runner is implemented.

Protocol `archon.continuous.v1` uses correlated request IDs and hello/command/poll/observe/stop/shutdown operations. The worker advances real physics independently of host requests: policy nominally 50 Hz, physics 200 Hz (four steps per tick). Commands contain body-frame vx/vy/yaw_rate, wall-clock duration and a lease ≤600 ms. Poll renews the lease; observe does not. Expiry and input EOF force zero twist. Worker initialization uses the official pose once; command transitions never reset position or policy history.

The installed policy has 61 observations and 14 joint-offset actions. Names/action scaling and Python/runtime versions are checked before startup. Source commits, weights SHA-256 and licenses are locked in `robots/microduck.lock.json`. Package tree integrity is checked at every launch. Upstream model licensing remains separate from Apache code/policy licensing; downloaded meshes and weights stay outside Git.

## Completion and safety behavior

Runner checks measured upright entry (tilt ≤15°, trunk height ≥0.10 m). Runtime handles cancellation, deadline, worker errors and preempting events, always attempts stop, then releases shared locks. A normal result requires duration completion and measured settling after zero twist: planar speed <0.015 m/s and absolute yaw rate <0.08 rad/s for three successive samples. Cleanup has a separate four-second ceiling, so reported elapsed time can exceed the call deadline. Unconfirmed stop or worker fault returns failure.

Worker latches a fault for tilt >45°, trunk height <0.075 m or non-finite state. Fallen state rejects further motion, holds current joint targets and requires an explicit process restart; this is not a recovery skill. Non-finite physics terminates the worker. Controlled shutdown is bounded; parent failure is additionally covered by lease/EOF. This is simulation behavior, not a hardware emergency-stop implementation.

`microduck.stand`, `microduck.walk`, `microduck.stop` share velstand; stand/stop supply zero twist. Walk parameters are bounded by vx ±0.40 m/s, vy ±0.20 m/s, yaw ±1.0 rad/s; supported envelope does not mean accurate tracking or acceptance of every combination. Tested keyboard presets are forward vx=0.4 and arc turns vx=0.3,yaw=±1.0. Diagnostics reproduced weak turn initiation at the old vx=0.2 preset and weak straight restarts at vx=0.3 even without Archon I/O. These are policy response limitations. The new forward preset is inside the pinned official training config's ±0.4 range. CLI reports these explicit presets; user-supplied parameters are never remapped. The underlying low-command stationarity remains a policy limitation; M2g.4 now rejects duration-complete calls without measured directional motion rather than reporting success. Arbitrary commands are bounded inputs, not motion guarantees. Results retain measured pose, velocity, tilt, simulation time and motion samples; a completed interval does not prove a requested distance was achieved.

## Install and play

From repository root, with Python 3.12 and uv:

```sh
uv venv --python 3.12 .venv-microduck
uv pip install --python .venv-microduck/bin/python -r python/requirements-microduck-probe.txt
cargo run -p robo-archon-cli -- --install-robot microduck
cargo run -p robo-archon-cli -- --doctor-robot microduck
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco --skill-keyboard --viewer
```

Default runtime uses `.venv-microduck/bin/python`; macOS viewer selects its `mjpython` and supplies the base interpreter library directory to support uv virtual environments. Override with `ROBO_ARCHON_PYTHON` when needed. Keyboard requires a TTY: w forward (vx=0.4); a/d arc turn (vx=0.3,yaw=±1); x cancel/stop; q or Ctrl-C cancel/exit. Each command expires after two seconds; policy remains active while idle. Keys are processed sequentially, with x/q interrupting the active call. Viewer rendering needs a graphical session.

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
.venv-microduck/bin/python scripts/verify_microduck_keyboard.py \
  --report tmp-episodes/microduck-keyboard-acceptance.json
.venv-microduck/bin/python scripts/probe_microduck_presets.py \
  --report tmp-episodes/microduck-preset-diagnostics.json
cargo test --workspace --offline
```

## Acceptance limits

Initial lifecycle evidence is recorded in `docs/validation/m2g-2-microduck.json`; final preset/keyboard/viewer acceptance and deterministic diagnostics are in `docs/validation/m2g-2-microduck-presets.json`. CPU tests cover continuous standing/forward/turn command execution with separate measured motion gates, measured stop, repeated commands in one worker, command expiry, independent lease expiry, invalid command rejection, disconnect exit and Archon cancellation. Runtime tests cover timeout/start failure/cancellation cleanup and conflict with trajectory locks. Fall detection/latching has threshold checks plus a physical push test that confirms fault latching and rejection of further commands. Native macOS viewer startup, bounded motion, shutdown and rendered frames passed; terminal controls were checked end to end through a PTY. Manual mouse interaction and native-viewer keyboard play are not separately accepted. Varied starting poses, long-duration reliability, hardware transfer and other official tricks remain unverified. The catalog is therefore `controlled`, not `task_verified`.

## Final nominal preset gates

The continuous acceptance script now fails on a missed gate: each six-second forward interval must move >0.10 m in XY; each four-second arc turn must have mean signed yaw rate >0.15 rad/s in the requested direction; trunk height must remain >0.10 m and tilt <15°. It tests two complete cycles with measured stops between calls in one worker. The PTY test additionally verifies the actual two-second keyboard presets, >0.05 m forward motion, both turn directions, x cancellation, confirmed stop and q shutdown. Command duration uses wall time; every result retains actual simulation time. These are demonstration gates, not requested-speed tracking tolerances.

The final continuous report measured approximately 0.70–0.71 m forward displacement per six-second interval and +0.46 / -0.58–0.59 rad/s mean yaw during arc turns. Two-second CLI turn calls and viewer-enabled rendering also passed. See the final evidence JSON for raw observations and exact simulation times. The previous lifecycle report is retained as historical evidence, including its failed old turn presets.
