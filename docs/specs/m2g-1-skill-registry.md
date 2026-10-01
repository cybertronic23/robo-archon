# M2g.1 — Skill package registry

Status: implemented and covered by registry tests and CLI validation checks. Execution is out of scope.
Design: [AgentOS skill architecture](../skills-design.md).

## Implemented contract

- Independent `robo-archon-skills` crate depends on embodied body metadata, serde, JSON and anyhow. No MuJoCo, ONNX Runtime, LLM client or Python dependency.
- Packages are immediate subdirectories of a caller-selected root, each containing `skill.json`. Sorted loading produces deterministic discovery. No code execution or downloads.
- Strict schema version 1; unknown fields rejected; no implicit replacement of duplicate IDs/tool names or duplicate body/backend targets.
- Parameter vocabulary: bounded numbers, integers, booleans and strings with finite choices. Registration validates defaults and bounds. Calls reject unknown/missing/invalid parameters and apply valid defaults.
- Each binding names body, backend, runner, optional body policy ID, object config and nonempty unique resources. Runner configuration cannot be overridden through call parameters.
- Prepared calls check a controlled body binding and exact existing policy contracts, where a policy is declared. No installed asset or runner readiness claim.
- Calls have an explicit timeout within the skill budget. Preparation does not start a timer or stop a robot.
- Provider-neutral tool descriptions use schemas generated from the same parameter definitions. Compatibility filtering is metadata-only; do not pass this list directly as executable tools before M2g.3 readiness filtering.

## CLI

```sh
cargo run --locked -p robo-archon-cli -- --list-skills
cargo run --locked -p robo-archon-cli -- --inspect-skill microduck.walk
cargo run --locked -p robo-archon-cli -- --skills-dir /path/to/user/skills --list-skills
```

`franka.home` is a manifest example with a compatible existing policy reference; its runner has not been implemented. `microduck.walk` is a draft manifest and remains blocked by the planned body binding. Neither is currently executable as a Skill.

To validate metadata without running a robot:

```sh
mkdir -p tmp-episodes
cat > tmp-episodes/home-call.json <<'JSON'
{"skill_id":"franka.home","parameters":{},"timeout_ms":3000}
JSON
cargo run --locked -p robo-archon-cli -- \
  --robot franka_panda --backend mujoco --validate-skill-call tmp-episodes/home-call.json
```

Successful output explicitly includes `validation_only: true` and `execution_supported: false`. A call to Microduck currently fails compatibility validation. There is no `--run-skill` command yet.

## Acceptance

1. Built-in example manifests load; a separate user directory loads without core changes.
2. Duplicate IDs/tool names, malformed schema, invalid defaults/ranges and duplicate target bindings fail registration.
3. Calls cannot inject runner config, coerce types, exceed bounds/budgets or select an incompatible body/platform/policy.
4. Defaults and generated input schema agree on required fields; discovery is deterministic.
5. CLI list/inspect/validate returns before backend creation and never executes a policy.
6. Existing workspace tests continue to pass.

Follow-up: M2g.2 runtime/Runner and M2f.3 actual Microduck worker. M2g.3 custom ONNX and LLM tool calling. This milestone deliberately does not invent runnable commands for those phases.
