# M2g.3 — compatible ONNX policy packages and dynamic LLM skills

Status: implemented and accepted with an official-weight custom-package fixture and a local mock HTTP model endpoint. The user has no self-trained checkpoint yet. This acceptance does not demonstrate a newly trained behavior or genuine cloud model reasoning; those require a real compatible checkpoint / configured model provider.

## Scope and separation

A data-only policy package contains `policy.json` and a self-contained ONNX weight. The installer copies only those files, validates before and after copying, writes an install receipt, and refuses to overwrite a different installed package. Policy IDs cannot replace the reserved official `velstand` ID. Source/license/checksum are preserved; packages remain in ignored `python/policies/external/`. No user script, shell command, remote download or pickle loader is introduced.

Current compatible adapter: `microduck.twist.v1` on MuJoCo, with embedded normalization, 50 Hz policy rate, float32 `[1,61] → [1,14]`, the existing observation/action contract IDs, exact joint order, named observation and command layouts, action scale 1, and matching nominal joint pose. Startup checks the runtime, model assets, receipt, weight SHA-256, ONNX metadata and finite smoke inference. Changing observations, action semantics, normalization, nominal pose or controller requires a new trusted adapter; matching tensor dimensions alone is insufficient. Structural readiness is not acceptance of a new behavior.

The installed package inventory provides policy profiles to a runtime-only body catalog overlay. Users reference policy IDs in their own skill manifests; they do not edit core code or the repository's catalog. `--validate-skill-call` reads installed metadata without loading ONNX; `--list-skill-tools` performs readiness checks. Metadata validation, dependency readiness and measured behavior acceptance remain distinct.

The generic SkillRegistry stays independent of ONNX and LLM providers. The runner uses the host-selected policy package; the worker revalidates it and reports the loaded policy ID, digest and source. One invocation initializes one simulation episode and uses one policy. Multiple-policy hot switching/composition within one episode is deferred to M2g.4.

## Dynamic model invocation

`--skill-instruction` builds function definitions from the current user's registry, filtering for body/platform compatibility, installed policy readiness, the trusted runner/config/resources, and supported parameter types/ranges/budgets. Descriptive Franka manifests, unavailable policies and unsupported adapters are not offered. Tool names are resolved back to registered skills; there is no Microduck action list in the model prompt.

A bounded Chat Completions tool request selects at most one function call, or returns an explanation without motion. The model controls only declared skill parameters. The host selects the manifest's time budget and validates unknown tools, malformed/parallel calls, parameter types/ranges, policy binding and resources before creating the worker. Execution still passes through Executive and measured stop handling. No rule-based fallback silently converts an unsupported request into motion.

The raw SkillResult is saved before the optional follow-up model request. The follow-up sends it as a correlated tool result and requests a short measured explanation. Feedback cannot trigger another action; feedback transport failures are recorded without losing physical evidence. A model explanation is untrusted text; the raw result remains the source of truth. Calls are non-streaming and bounded; API credentials travel via curl's stdin configuration, not its argument list or reports.

The adapter accepts a configurable Chat Completions base URL (include `/v1` if the provider requires it), model and API key. The existing default provider URL is retained; select a model supported by your provider. This is not an OpenAI Responses/Realtime adapter or a multi-turn robot chat UI.

## Reproduce the custom package acceptance

After installing the pinned Microduck environment and assets per M2g.2:

```sh
.venv-microduck/bin/python scripts/make_policy_fixture.py
cargo build -p robo-archon-cli
cargo run -p robo-archon-cli -- --install-policy tmp-episodes/m2g3-fixture/policy
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco \
  --skills-dir tmp-episodes/m2g3-fixture/skills --list-skill-tools
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco \
  --skills-dir tmp-episodes/m2g3-fixture/skills \
  --run-skill tmp-episodes/m2g3-fixture/call.json \
  --skill-report tmp-episodes/m2g3-fixture/result.json
.venv-microduck/bin/python scripts/verify_policy_packages.py
.venv-microduck/bin/python scripts/verify_skill_agent.py
```

`make_policy_fixture.py` explicitly copies OFFICIAL weights to a separate `example.velstand` package and registers `example.walk`. It does not train or claim a new skill. The generated manifest is the complete current schema/example. For your own trained export, supply your own checkpoint, provenance/license and SHA-256, satisfy the adapter metadata/normalization contracts, use a new policy ID, install it, and reference that ID from a new Skill binding. Custom skills must declare vx/vy/yaw_rate/duration_ms and the trusted adapter's whole-body resource. Unsupported new action types are refused until their adapter exists.

For real model use, configure credentials through environment variables, then:

```sh
cargo run -p robo-archon-cli -- --robot microduck --backend mujoco \
  --skills-dir tmp-episodes/m2g3-fixture/skills \
  --skill-instruction '使用已注册技能短暂向前移动，然后报告实际结果' \
  --llm-base-url "$LLM_BASE_URL" --llm-model "$LLM_MODEL" \
  --skill-report tmp-episodes/m2g3-fixture/model-result.json --viewer
```

`DEEPSEEK_API_KEY` is read by the existing CLI; `OPENAI_API_KEY` is a fallback. Custom `--policy-packages PATH` selects another local install directory. This milestone installs local directories only; it does not fetch arbitrary URLs or train policies.

## Acceptance and limits

Evidence: `docs/validation/m2g-3-policy-packages.json`. Checks include clean/idempotent installation, path/contract/rate/normalization/unknown-field rejection, actual ONNX joint-order mismatch with an updated valid checksum, installed tampering, overwrite refusal, dynamic custom skill discovery, measured custom-package execution, local HTTP planning/tool feedback, refusal and invalid/unknown call rejection without worker startup, and preservation of physical evidence when feedback HTTP fails. Rust tests preserve existing behavior and test dynamic tool names, host budgets and parallel-call rejection.

No real custom-trained behavior, live cloud API inference, multi-tool composition, hot policy switching, Go2, different simulator or hardware transfer is accepted here. Earlier Microduck arbitrary-speed/low-command limitations still apply. Scope follows the user's explicit request to validate the custom package mechanism first.
