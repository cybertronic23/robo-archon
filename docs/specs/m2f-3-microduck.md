# M2f.3 — Microduck first: official-policy simulation

Status: CPU feasibility and Archon official-policy continuous integration implemented. See [M2g.2 execution spec](m2g-2-continuous-skills.md). Physical push/fall rejection is checked; the updated nominal play presets pass repeated stop/restart acceptance. M2g.4 adds explicit simulation reset and five official motions, each with standing/walking restart acceptance. Trained recovery, arbitrary velocity tracking, roller-body variants and hardware transfer are not accepted.
User scope: Microduck before Go2, simulation only, official pretrained policies as the baseline. No real robot is available. NVIDIA cloud compute may be used if training becomes necessary; it is not needed for this CPU inference probe.

## Upstream components

- [microduck](https://github.com/pollen-robotics/microduck): Rust robot runtime. Its [simulation guide](https://github.com/pollen-robotics/microduck/blob/main/docs/robot/simulation.md) documents `robotd --sim` and `scripts/duck-sim`, which connect real daemons to a MuJoCo body.
- [microduck_rl](https://github.com/pollen-robotics/microduck_rl): MJCF models, CPU policy rehearsal, MuJoCo Warp/mjlab training, and `duck-body` simulator.
- [microduck-policies](https://huggingface.co/pollen-robotics/microduck-policies): public official ONNX weights and schema-2 manifest. The pinned manifest selects `velstand.onnx` as the default walk slot; it also holds the standing pose at zero command.
- [BAM](https://github.com/Rhoban/bam): XL330 M6 voltage/friction actuator model. Retain it for the baseline; XML position actuators are a diagnostic fallback, not equivalent acceptance evidence.

Pin these sources together; floating latest dependencies are not a supported reproduction recipe:

| Component | Tested revision/version |
| --- | --- |
| microduck_rl | `8d0db74916a4f833d1d9b95d6a1d7f4d13b9d5ec` |
| official policy repository | `d5a8b55033e157f1af2ed6bd5c1e435b770a8ee0` |
| velstand.onnx SHA-256 | `1c659be55da94bc5753b707de5c6a3e7c49931e05ca3b6991615cef1a8ba9a45` |
| BAM (from upstream uv.lock) | `62bd8ce12154340be97e06f7f41a0ca8f116d967` |
| Python / MuJoCo / ONNX Runtime | `3.12.12` / `3.10.0` / `1.24.4` |

Upstream code and official policy metadata specify Apache-2.0. The RL README separately identifies the 3D model files as CC BY-SA-NC. Model meshes must retain their own license/attribution; do not relabel them under Archon's license. The probe downloads them externally and does not vendor meshes or weights.

## Verified feasibility and limits

The probe imports upstream `scripts/infer_policy.py` and uses its observation construction, action application, and BAM setup unchanged. It initializes the official HOME pose once, then advances real MuJoCo physics; it does not teleport the robot to implement walking. Scene: `scene.xml` / `robot_groundcontact.xml`. Physics: 200 Hz, policy: 50 Hz. BAM: 7.4 V, firmware kp 200, voltage sag gain 0.1, floor 6 V, no current limiter. Actuator delay is zero for this nominal probe; training samples delays and noise that are not reproduced here.

One continuous 16-second flat-floor rollout on macOS ARM64:

| Phase | Command | Observed result |
| --- | --- | --- |
| Stand, 3 s | zero twist | trunk remained upright |
| Forward, 6 s | vx 0.30 m/s | approximately 0.392 m net XY displacement; mean body-forward speed after the first second 0.079 m/s |
| Turn, 4 s | vx 0.20 m/s, yaw 1.0 rad/s | approximately 0.207 m net XY displacement; mean yaw rate after the first second 0.351 rad/s |
| Stop, 3 s | zero twist | mean body-forward speed after the first second approximately -0.00038 m/s |

Maximum trunk tilt was approximately 4.18 degrees; minimum trunk height approximately 0.113 m. The rendered video was visually inspected. These results establish a playable locomotion baseline, not accurate velocity tracking, long-duration reliability, rough-terrain support, or hardware transfer.

A separate probe at vx 0.15 m/s moved only approximately 5 mm in 6 seconds, with mean body-forward speed approximately 0.00009 m/s. This low-command behavior is unresolved: a small command can settle into a stationary state. Do not claim that the requested speed is the achieved speed or silently remap it. The first user-facing demo should expose measured motion and use the demonstrated command presets.

Evidence: `docs/validation/m2f-3-microduck-probe.json`, `docs/validation/m2f-3-microduck-low-command.json`, and `docs/media/microduck-official-policy-probe.gif`.

## Reproduce the probe

From the Archon repository root, with `uv`, Git, curl, and optionally ffmpeg available:

```sh
uv venv --python 3.12 .venv-microduck-probe
uv pip install --python .venv-microduck-probe/bin/python -r python/requirements-microduck-probe.txt
mkdir -p third_party/microduck

git clone https://github.com/pollen-robotics/microduck_rl.git third_party/microduck/rl
git -C third_party/microduck/rl checkout --detach 8d0db74916a4f833d1d9b95d6a1d7f4d13b9d5ec

git clone https://github.com/Rhoban/bam.git third_party/microduck/bam
git -C third_party/microduck/bam checkout --detach 62bd8ce12154340be97e06f7f41a0ca8f116d967

mkdir -p third_party/microduck/policies
curl -L --fail https://huggingface.co/pollen-robotics/microduck-policies/resolve/d5a8b55033e157f1af2ed6bd5c1e435b770a8ee0/velstand.onnx -o third_party/microduck/policies/velstand.onnx

PYTHONPATH="$PWD/third_party/microduck/bam" .venv-microduck-probe/bin/python scripts/probe_microduck.py \
  --source third_party/microduck/rl --policies third_party/microduck/policies \
  --report tmp-episodes/microduck-probe.json
```

Add `--video tmp-episodes/microduck-probe.mp4 --preview tmp-episodes/microduck-probe.png` for rendering. The probe checks Git revisions and the weight checksum, writes metrics, and fails when nominal upright/forward/turn/stop smoke gates are not met. Those gates are coarse feasibility gates, not speed-accuracy acceptance. To reproduce the low-command limitation, add `--forward-speed 0.15 --turn-speed 0.1 --yaw-rate 0.5`; this is expected to fail the motion gates.

For interactive play, use the same environment, BAM path, and pinned upstream checkout:

```sh
cd third_party/microduck/rl
PYTHONPATH="../bam" ../../../.venv-microduck-probe/bin/mjpython scripts/infer_policy.py \
  --walking ../policies/velstand.onnx --new-cmd-obs --lin-vel-x 0.3
```

On macOS, the native MuJoCo viewer uses `mjpython`; on Linux use the environment's Python. This interactive command is upstream's entry point; the automated rollout and offscreen rendering were tested here, while manual keyboard play has not been acceptance-tested. Keyboard control is read from the terminal, which must be a TTY. No NVIDIA GPU or complete CUDA training dependency stack is required for this route.

## Archon integration scope and follow-up

1. Register the Microduck body package with explicit model, policy, source revision, checksums, licenses, and optional CAD/printing links. The MuJoCo binding is now `controlled`, with pinned installer and runtime checks; it is not promoted to `task_verified`.
2. Add a continuous locomotion worker. It owns physics and 50 Hz policy inference independently of LLM calls. Body-level commands: desired body-frame twist `(vx, vy, yaw_rate)`, stand/stop, reset; expose observed base pose, velocity, gravity/tilt, and simulation time. The MuJoCo binding owns MJCF and BAM details.
3. Give every motion command an expiry. Stop on expiry, disconnect, explicit cancellation, or fall. Reset is a visible episode restart; do not describe a reset as a trained recovery behavior.
4. Provide a keyboard demo with forward/turn presets and an obvious stop action, then Agent-issued bounded commands. Include recording and replayable metrics. A `duck-body` + official Rust `robotd` route is supported upstream but has not been locally reproduced in this probe; evaluate it separately if runtime parity is desired.
5. Verify longer runs and varied initial states, command expiry/cancellation, falls, installation from a clean environment, and rendering. Publish command and measured velocity separately. Only then promote the binding to verified in the catalog.
6. Add official sit/stand, ground-pick, and other tricks as separately verified capabilities. The official manifest includes them, but this probe does not establish that they work locally. Go2 follows completion of the Microduck integration.

Agent/body contracts remain independent of the simulator. Other backends can provide their own model and policy bindings under the same body package; this milestone only verifies MuJoCo. Retraining on NVIDIA cloud is deferred until an actual policy-quality need is established.

The reusable Skill/Runner mechanism is tracked separately as M2g; Microduck exercises it in M2g.2/M2f.3. See [the extensible skill design](../skills-design.md). Registration alone never promotes a robot binding. Current supported commands and remaining validation limits are recorded in the M2g.2 spec.
