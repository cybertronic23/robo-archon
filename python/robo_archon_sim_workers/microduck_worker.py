#!/usr/bin/env python3
"""Continuous official-policy MuJoCo worker. NDJSON commands; physics owns an independent lease."""

import argparse
import contextlib
import importlib.metadata
import importlib.util
import json
import math
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
        )
    metadata = policy.ort_session.get_modelmeta().custom_metadata_map
    if policy.ort_session.get_inputs()[0].shape != [
        1,
        61,
    ] or policy.ort_session.get_outputs()[0].shape != [1, 14]:
        raise ValueError("official policy shape mismatch")
    if metadata["joint_names"].split(",") != names or metadata["action_scale"] != "1.0":
        raise ValueError("official policy joint/action metadata mismatch")
    data.qpos[:7] = [0, 0, 0.125, 1, 0, 0, 0]
    data.qpos[policy.joint_qpos_indices] = policy.default_pose
    controller.reset(data.qpos)
    policy.set_position_targets(policy.default_pose)
    mujoco.mj_forward(model, data)
    viewer = None
    if args.viewer:
        import mujoco.viewer

        viewer = mujoco.viewer.launch_passive(model, data)
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

    def reply(request, **fields):
        print(
            json.dumps({"protocol": PROTOCOL, "id": request.get("id"), **fields}),
            flush=True,
        )

    def stop(why):
        nonlocal command, state, reason
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
            "policy_id": policy_id,
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
            policy.set_vel_cmd(*command) if not np.allclose(
                policy.vel_cmd, command, atol=1e-8
            ) else None
        if fault:
            controller.q_target[:] = data.qpos[policy.joint_qpos_indices]
        else:
            policy.apply_action(policy.infer())
        for _ in range(4):
            controller.update()
            mujoco.mj_step(model, data)
        obs = snapshot()
        fault = safety_fault(
            fault,
            all(np.isfinite(v).all() for v in (data.qpos, data.qvel, data.ctrl)),
            obs["tilt_deg"],
            obs["xyz"][2],
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
            metrics["sum_yaw_rate"] += obs["yaw_rate"]
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
