#!/usr/bin/env python3
"""Continuous official-policy MuJoCo worker. NDJSON commands; physics owns an independent lease."""

import argparse
import contextlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from install_microduck import verify  # noqa: E402 — repository bootstrap path

PROTOCOL = "archon.continuous.v1"


def read_commands(inbox):
    for line in sys.stdin:
        try:
            inbox.put(json.loads(line))
        except Exception as error:
            inbox.put({"op": "invalid", "error": str(error)})
    inbox.put({"op": "disconnect"})


def safety_fault(previous, finite, tilt_deg, height):
    """Faults latch until the simulation is explicitly restarted."""
    if not finite:
        return "non_finite_state"
    if previous:
        return previous
    if tilt_deg > 45 or height < 0.075:
        return "fallen"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--viewer", action="store_true")
    parser.add_argument("--policy-package", type=Path)
    parser.add_argument("--available-policy", type=Path, action="append", default=[])
    parser.add_argument("--record-dir", type=Path)
    args = parser.parse_args()
    package = args.package.resolve()
    lock = json.loads((ROOT / "robots/microduck.lock.json").read_text())
    verify(package, lock)
    for name, version in lock["runtime_versions"].items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"{name} must be {version}")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Microduck worker requires Python 3.12")
    policy_id, policy_path, provenance = (
        "velstand",
        package / "velstand.onnx",
        lock["policy"],
    )
    if args.policy_package:
        from policy_packages import verify_installed

        provenance, policy_path = verify_installed(args.policy_package)
        policy_id = provenance["id"]
    from official_behaviors import DEFAULT as BEHAVIOR_DIR, verify as verify_behaviors

    behavior_lock = verify_behaviors() if BEHAVIOR_DIR.exists() else None
    behavior_kwargs = {}
    if behavior_lock:
        for name, entry in behavior_lock["behaviors"].items():
            key = {
                "sitstand": "sitstand_onnx_path",
                "ground_pick": "ground_pick_onnx_path",
            }.get(name, name + "_onnx_path")
            behavior_kwargs[key] = str(BEHAVIOR_DIR / entry["file"])
        behavior_kwargs.update(kick_duration=0.5, roulade_duration=1.0)
    sys.path.insert(0, str(package))
    import mujoco
    import numpy as np

    spec = importlib.util.spec_from_file_location(
        "microduck_official_infer", package / "official_infer.py"
    )
    official = importlib.util.module_from_spec(spec)
    # Upstream diagnostics must not corrupt the NDJSON channel.
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(official)
        bam = official.load_bam_model(200, 7.4, None)
        model, data, controller, names = official.load_mujoco_with_bam(
            str(package / "model/scene.xml"), bam, 0.005, 0.1, 6.0
        )
        policy = official.PolicyInference(
            model,
            data,
            walking_onnx_path=str(policy_path),
            bam_ctrl=controller,
            new_cmd_obs=True,
            use_projected_gravity=True,
            **behavior_kwargs,
        )
    metadata = policy.ort_session.get_modelmeta().custom_metadata_map
    if policy.ort_session.get_inputs()[0].shape != [
        1,
        61,
    ] or policy.ort_session.get_outputs()[0].shape != [1, 14]:
        raise ValueError("official policy shape mismatch")
    if metadata["joint_names"].split(",") != names or metadata["action_scale"] != "1.0":
        raise ValueError("official policy joint/action metadata mismatch")
    # Preload host-selected, verified compatible sessions. No model I/O in a control handoff.
    import onnxruntime as ort

    sessions = {policy_id: (policy.ort_session, provenance)}
    if policy_id != "velstand":
        sessions["velstand"] = (
            ort.InferenceSession(
                str(package / "velstand.onnx"), providers=["CPUExecutionProvider"]
            ),
            lock["policy"],
        )
    for directory in args.available_policy:
        from policy_packages import verify_installed

        manifest, weight = verify_installed(directory)
        if manifest["id"] not in sessions:
            sessions[manifest["id"]] = (
                ort.InferenceSession(str(weight), providers=["CPUExecutionProvider"]),
                manifest,
            )
    initial_yaw = float(os.environ.get("ROBO_ARCHON_MICRODUCK_INITIAL_YAW", "0"))
    if not math.isfinite(initial_yaw) or abs(initial_yaw) > math.pi:
        raise ValueError("initial heading must be finite and within +/-pi")
    data.qpos[:7] = [
        0,
        0,
        0.125,
        math.cos(initial_yaw / 2),
        0,
        0,
        math.sin(initial_yaw / 2),
    ]
    data.qpos[policy.joint_qpos_indices] = policy.default_pose
    controller.reset(data.qpos)
    policy.set_position_targets(policy.default_pose)
    mujoco.mj_forward(model, data)
    viewer = None
    if args.viewer:
        import mujoco.viewer

        viewer = mujoco.viewer.launch_passive(model, data)
        # Keep long walking showcases in frame while preserving user orbit/zoom.
        with viewer.lock():
            viewer.cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
            viewer.cam.trackbodyid = mujoco.mj_name2id(
                model, mujoco.mjtObj.mjOBJ_BODY, "trunk_base"
            )
    renderer = None
    if args.record_dir:
        args.record_dir.mkdir(parents=True, exist_ok=False)
        model.vis.global_.offwidth = 480
        model.vis.global_.offheight = 360
        renderer = mujoco.Renderer(model, height=360, width=480)
        camera = mujoco.MjvCamera()
        camera.distance, camera.azimuth, camera.elevation = 0.85, 140, -18
    inbox = queue.Queue()
    threading.Thread(target=read_commands, args=(inbox,), daemon=True).start()
    command = [0.0, 0.0, 0.0]
    lease_until = 0.0
    duration_until = 0.0
    state = "idle"
    fault = None
    reason = "standing"
    disconnected_until = None
    tick = 0
    metrics = None
    active_behavior = None
    behavior_elapsed = 0.0
    episode_id = 0

    def reply(request, **fields):
        print(
            json.dumps({"protocol": PROTOCOL, "id": request.get("id"), **fields}),
            flush=True,
        )

    def stop(why):
        nonlocal command, state, reason, active_behavior, policy_id, provenance
        if active_behavior:
            policy.sit_mode = False
            policy.ground_pick_mode = False
            policy.behavior_mode = None
            policy.current_policy = "walking"
            policy.ort_session = sessions["velstand"][0]
            policy.walking_session = policy.ort_session
            policy_id, provenance = "velstand", lock["policy"]
            active_behavior = None
            with contextlib.redirect_stdout(sys.stderr):
                policy.set_vel_cmd(0, 0, 0)
        command = [0.0, 0.0, 0.0]
        state = "fault" if fault else "idle"
        reason = why

    def snapshot():
        gravity = policy.get_projected_gravity()
        tilt = float(np.degrees(np.arccos(np.clip(-gravity[2], -1, 1))))
        quat = data.qpos[3:7]
        yaw = math.atan2(
            2 * (quat[0] * quat[3] + quat[1] * quat[2]),
            1 - 2 * (quat[2] ** 2 + quat[3] ** 2),
        )
        velocity = policy.quat_rotate_inverse(quat, data.qvel[:3])
        return {
            "episode_id": episode_id,
            "policy_id": policy_id,
            "active_behavior": active_behavior,
            "joint_positions": data.qpos[policy.joint_qpos_indices].tolist(),
            "policy_provenance": provenance,
            "state": state,
            "reason": reason,
            "fault": fault,
            "simulation_time": float(data.time),
            "xyz": data.qpos[:3].tolist(),
            "yaw": yaw,
            "tilt_deg": tilt,
            "body_velocity": velocity.tolist(),
            "yaw_rate": float(policy.get_base_ang_vel()[2]),
            "command": command,
            "motion": metrics,
        }

    while True:
        started = time.monotonic()
        if viewer and not viewer.is_running():
            stop("viewer_closed")
            break
        while not inbox.empty():
            request = inbox.get()
            try:
                op = request.get("op")
                if op != "disconnect" and request.get("protocol") != PROTOCOL:
                    raise ValueError("protocol mismatch")
                if op == "hello":
                    if request.get("protocol") != PROTOCOL:
                        raise ValueError("protocol mismatch")
                    reply(request, ok=True, observation=snapshot())
                elif op == "select_policy":
                    target = request.get("policy_id")
                    obs = snapshot()
                    if target not in sessions:
                        raise ValueError("policy was not preloaded by host")
                    if (
                        fault
                        or state != "idle"
                        or obs["tilt_deg"] > 15
                        or obs["xyz"][2] < 0.1
                        or math.hypot(*obs["body_velocity"][:2]) >= 0.015
                        or abs(obs["yaw_rate"]) >= 0.08
                    ):
                        raise ValueError(
                            "policy handoff requires measured settled upright state"
                        )
                    policy.ort_session, provenance = sessions[target]
                    policy.walking_session = policy.ort_session
                    policy.input_name = policy.ort_session.get_inputs()[0].name
                    policy.output_name = policy.ort_session.get_outputs()[0].name
                    # Preserve measured pose, actuator state and action history across the handoff.
                    policy_id = target
                    reply(request, ok=True, observation=snapshot())
                elif op == "behavior":
                    name = request.get("name")
                    if not behavior_lock or name not in behavior_lock["behaviors"]:
                        raise ValueError("official behavior not installed")
                    obs = snapshot()
                    if (
                        fault
                        or state != "idle"
                        or obs["tilt_deg"] > 15
                        or obs["xyz"][2] < 0.1
                        or math.hypot(*obs["body_velocity"][:2]) >= 0.015
                        or abs(obs["yaw_rate"]) >= 0.08
                    ):
                        raise ValueError(
                            "behavior requires measured settled upright state"
                        )
                    if request.get("lease_ms") != 600:
                        raise ValueError("behavior requires 600ms lease")
                    # Always enter official behaviors from the pinned official walking session.
                    policy.walking_session = sessions["velstand"][0]
                    policy.ort_session = policy.walking_session
                    policy.current_policy = "walking"
                    with contextlib.redirect_stdout(sys.stderr):
                        policy.set_vel_cmd(0, 0, 0)
                    with contextlib.redirect_stdout(sys.stderr):
                        if name == "sitstand":
                            policy.toggle_sit()
                        elif name == "ground_pick":
                            policy.trigger_ground_pick()
                        else:
                            policy.trigger_behavior(name)
                    active_behavior, behavior_elapsed = name, 0.0
                    policy_id = "official." + name
                    entry = behavior_lock["behaviors"][name]
                    provenance = {
                        **entry,
                        "revision": behavior_lock["revision"],
                        "license": behavior_lock["license"],
                    }
                    state, reason = "running", "behavior"
                    lease_until = started + 0.6
                    duration_until = started + 9.0
                    metrics = {
                        "start_xyz": data.qpos[:3].tolist(),
                        "min_height": float(data.qpos[2]),
                        "max_tilt_deg": 0.0,
                        "samples": 0,
                        "sum_vx": 0.0,
                        "sum_vy": 0.0,
                        "sum_yaw_rate": 0.0,
                        "behavior": name,
                        "executed_policy_id": policy_id,
                        "executed_policy_provenance": provenance,
                        "joint_min": data.qpos[policy.joint_qpos_indices].tolist(),
                        "joint_max": data.qpos[policy.joint_qpos_indices].tolist(),
                    }
                    reply(request, ok=True, observation=snapshot())
                elif op == "command":
                    if fault:
                        raise ValueError(
                            "fallen/faulted robot requires explicit simulation restart"
                        )
                    twist = request["twist"]
                    duration = request["duration_ms"]
                    lease = request["lease_ms"]
                    if len(twist) != 3 or any(
                        isinstance(v, bool)
                        or not isinstance(v, (int, float))
                        or not math.isfinite(v)
                        for v in twist
                    ):
                        raise ValueError("twist requires three finite numbers")
                    if any(abs(v) > limit for v, limit in zip(twist, [0.4, 0.2, 1.0])):
                        raise ValueError("twist outside verified command envelope")
                    if (
                        type(duration) is not int
                        or not 1 <= duration <= 10000
                        or type(lease) is not int
                        or not 100 <= lease <= 600
                    ):
                        raise ValueError("invalid duration/lease")
                    if state == "running":
                        raise ValueError("another command is running; stop it first")
                    command = list(twist)
                    state, reason = "running", "command"
                    duration_until = started + duration / 1000
                    lease_until = started + lease / 1000
                    metrics = {
                        "start_xyz": data.qpos[:3].tolist(),
                        "min_height": float(data.qpos[2]),
                        "max_tilt_deg": 0.0,
                        "samples": 0,
                        "sum_vx": 0.0,
                        "sum_vy": 0.0,
                        "sum_yaw_rate": 0.0,
                    }
                    reply(request, ok=True, observation=snapshot())
                elif op == "poll":
                    if state == "running":
                        lease_until = started + 0.6
                    reply(request, ok=True, observation=snapshot())
                elif op == "observe":
                    reply(request, ok=True, observation=snapshot())
                elif op == "stop":
                    stop("explicit_stop")
                    reply(request, ok=True, observation=snapshot())
                elif op == "reset":
                    if state == "running":
                        raise ValueError("stop before explicit simulation reset")
                    stop("explicit_reset")
                    mujoco.mj_resetData(model, data)
                    data.qpos[:7] = [
                        0,
                        0,
                        0.125,
                        math.cos(initial_yaw / 2),
                        0,
                        0,
                        math.sin(initial_yaw / 2),
                    ]
                    data.qpos[policy.joint_qpos_indices] = policy.default_pose
                    policy.current_policy = "walking"
                    policy.ort_session = sessions["velstand"][0]
                    policy.walking_session = policy.ort_session
                    policy_id, provenance = "velstand", lock["policy"]
                    policy.last_action[:] = 0
                    if policy.action_buffer is not None:
                        policy.action_buffer[:] = 0
                    policy.head_offset[:] = 0
                    policy.body_cmd[:] = 0
                    with contextlib.redirect_stdout(sys.stderr):
                        policy.set_vel_cmd(0, 0, 0)
                    controller.reset(data.qpos)
                    policy.set_position_targets(policy.default_pose)
                    mujoco.mj_forward(model, data)
                    fault, state, reason, metrics = None, "idle", "explicit_reset", None
                    episode_id += 1
                    reply(request, ok=True, observation=snapshot())
                elif op == "shutdown":
                    stop("shutdown")
                    reply(request, ok=True, observation=snapshot())
                    if renderer:
                        renderer.close()
                    if viewer:
                        viewer.close()
                    return
                elif op == "disconnect":
                    stop("disconnected")
                    # Keep physics alive briefly to settle, then terminate the orphan.
                    disconnected_until = started + 1.0
                else:
                    raise ValueError("unknown command")
            except Exception as error:
                stop("invalid_command")
                reply(request, ok=False, error=str(error), observation=snapshot())
        now = time.monotonic()
        if state == "running" and now >= duration_until:
            stop("duration_complete")
        elif state == "running" and now >= lease_until:
            stop("lease_expired")
        with contextlib.redirect_stdout(sys.stderr):
            policy.set_vel_cmd(*command) if not active_behavior and not np.allclose(
                policy.vel_cmd, command, atol=1e-8
            ) else None
        if active_behavior:
            behavior_elapsed += 0.02
            with contextlib.redirect_stdout(sys.stderr):
                if (
                    active_behavior == "sitstand"
                    and policy.sit_mode
                    and behavior_elapsed >= 2.0
                ):
                    policy.toggle_sit()
                policy.update_ground_pick_phase(0.02)
                policy.update_behavior(0.02)
        if fault:
            controller.q_target[:] = data.qpos[policy.joint_qpos_indices]
        else:
            # Compatible exports may use different tensor names. Refresh on every session change.
            policy.input_name = policy.ort_session.get_inputs()[0].name
            policy.output_name = policy.ort_session.get_outputs()[0].name
            policy.apply_action(policy.infer())
        for _ in range(4):
            controller.update()
            mujoco.mj_step(model, data)
        obs = snapshot()
        # Expected low posture is limited to a timed, pinned official behavior.
        # All non-finite states remain fatal, and ordinary walking retains original gates.
        allowed_low = active_behavior in ("sitstand", "roulade")
        allowed_roll = active_behavior == "roulade"
        fault = safety_fault(
            fault,
            all(np.isfinite(v).all() for v in (data.qpos, data.qvel, data.ctrl)),
            0.0 if allowed_roll else obs["tilt_deg"],
            0.1 if allowed_low and obs["xyz"][2] >= 0.035 else obs["xyz"][2],
        )
        if fault:
            stop(fault)
            if fault == "non_finite_state":
                raise RuntimeError("non-finite physics state; worker terminated")
        if metrics and state == "running":
            metrics["min_height"] = min(metrics["min_height"], obs["xyz"][2])
            metrics["max_tilt_deg"] = max(metrics["max_tilt_deg"], obs["tilt_deg"])
            metrics["samples"] += 1
            metrics["sum_vx"] += obs["body_velocity"][0]
            metrics["sum_vy"] += obs["body_velocity"][1]
            metrics["sum_yaw_rate"] += obs["yaw_rate"]
            if "joint_min" in metrics:
                metrics["joint_min"] = np.minimum(
                    metrics["joint_min"], obs["joint_positions"]
                ).tolist()
                metrics["joint_max"] = np.maximum(
                    metrics["joint_max"], obs["joint_positions"]
                ).tolist()
        if active_behavior and not fault:
            done_at = {
                "sitstand": 5.0,
                "ground_pick": 3.8,
                "kick_left": 1.5,
                "kick_right": 1.5,
                "roulade": 2.5,
            }[active_behavior]
            if (
                behavior_elapsed >= done_at
                and obs["xyz"][2] >= 0.1
                and obs["tilt_deg"] <= 15
            ):
                stop("duration_complete")
            elif behavior_elapsed >= done_at + 2.0:
                fault = "behavior_return_not_upright"
                stop(fault)
        if viewer:
            viewer.sync()
        if renderer and tick % 5 == 0:
            from PIL import Image

            camera.lookat[:] = data.qpos[:3]
            camera.lookat[2] = 0.13
            renderer.update_scene(data, camera=camera)
            Image.fromarray(renderer.render()).save(
                args.record_dir / f"{tick // 5:06d}.png"
            )
        tick += 1
        if disconnected_until and now >= disconnected_until:
            break
        time.sleep(max(0.0, 0.02 - (time.monotonic() - started)))
    if renderer:
        renderer.close()
    if viewer:
        viewer.close()


if __name__ == "__main__":
    main()
