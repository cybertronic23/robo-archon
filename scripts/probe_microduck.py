#!/usr/bin/env python3
"""Pinned official-policy feasibility probe. See docs/specs/m2f-3-microduck.md."""

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import mujoco
import numpy as np


def main():
    p = argparse.ArgumentParser(
        description="Rehearse official Microduck ONNX in CPU MuJoCo; this is a feasibility probe, not an Archon backend."
    )
    p.add_argument(
        "--source", type=Path, required=True, help="Pinned microduck_rl checkout"
    )
    p.add_argument(
        "--policies",
        type=Path,
        required=True,
        help="Directory containing velstand.onnx",
    )
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--video", type=Path)
    p.add_argument("--preview", type=Path)
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--forward-speed", type=float, default=0.3)
    p.add_argument("--turn-speed", type=float, default=0.2)
    p.add_argument("--yaw-rate", type=float, default=1.0)
    a = p.parse_args()
    if a.preview and not a.video:
        p.error("--preview requires --video")
    root = a.source.resolve()

    def revision(path):
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip()

    model_revision = revision(root)
    if model_revision != "8d0db74916a4f833d1d9b95d6a1d7f4d13b9d5ec":
        p.error("Use the microduck_rl revision pinned in docs/specs/m2f-3-microduck.md")
    weights = (a.policies / "velstand.onnx").resolve()
    weights_sha = hashlib.sha256(weights.read_bytes()).hexdigest()
    if (
        weights_sha
        != "1c659be55da94bc5753b707de5c6a3e7c49931e05ca3b6991615cef1a8ba9a45"
    ):
        p.error("velstand.onnx checksum differs from the pinned official weights")
    from bam import model as bam_module

    bam_root = Path(bam_module.__file__).resolve().parents[1]
    bam_revision = revision(bam_root)
    if bam_revision != "62bd8ce12154340be97e06f7f41a0ca8f116d967":
        p.error("Use the BAM revision pinned in docs/specs/m2f-3-microduck.md")
    spec = importlib.util.spec_from_file_location(
        "official_infer", root / "scripts/infer_policy.py"
    )
    official = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(official)
    bam = official.load_bam_model(200, 7.4, None)
    m, d, c, names = official.load_mujoco_with_bam(
        str(root / official.MICRODUCK_XML), bam, 0.005, 0.1, 6.0
    )
    policy = official.PolicyInference(
        m,
        d,
        walking_onnx_path=str(weights),
        bam_ctrl=c,
        new_cmd_obs=True,
        use_projected_gravity=True,
    )
    jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "trunk_base_freejoint")
    adr = m.jnt_qposadr[jid]
    d.qpos[adr : adr + 7] = [0, 0, 0.125, 1, 0, 0, 0]
    d.qpos[policy.joint_qpos_indices] = policy.default_pose
    c.reset(d.qpos)
    policy.set_position_targets(policy.default_pose)
    mujoco.mj_forward(m, d)
    renderer = None
    proc = None
    if a.video:

        m.vis.global_.offwidth = 960
        m.vis.global_.offheight = 720
        renderer = mujoco.Renderer(m, height=720, width=960)
        camera = mujoco.MjvCamera()
        camera.distance = 0.85
        camera.azimuth = 140
        camera.elevation = -18
        a.video.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [
                a.ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgb24",
                "-video_size",
                "960x720",
                "-framerate",
                "25",
                "-i",
                "-",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(a.video),
            ],
            stdin=subprocess.PIPE,
        )
    phases = [
        ("stand", 3, (0, 0, 0)),
        ("forward", 6, (a.forward_speed, 0, 0)),
        ("turn", 4, (a.turn_speed, 0, a.yaw_rate)),
        ("stop", 3, (0, 0, 0)),
    ]
    report = []
    start = time.perf_counter()
    tick = 0
    for name, seconds, cmd in phases:
        policy.set_vel_cmd(*cmd)
        before = d.qpos[adr : adr + 7].copy()
        heights = []
        tilts = []
        speeds = []
        yaws = []
        for i in range(int(seconds * 50)):
            policy.apply_action(policy.infer())
            for _ in range(4):
                c.update()
                mujoco.mj_step(m, d)
            heights.append(float(d.qpos[adr + 2]))
            g = policy.get_projected_gravity()
            tilts.append(float(np.degrees(np.arccos(np.clip(-g[2], -1, 1)))))
            speeds.append(
                float(
                    policy.quat_rotate_inverse(d.qpos[adr + 3 : adr + 7], d.qvel[:3])[0]
                )
            )
            yaws.append(float(policy.get_base_ang_vel()[2]))
            if not all(np.isfinite(v).all() for v in (d.qpos, d.qvel, d.ctrl)):
                raise RuntimeError("Non-finite simulation state")
            if renderer and tick % 2 == 0:
                camera.lookat[:] = d.qpos[adr : adr + 3]
                camera.lookat[2] = 0.13
                renderer.update_scene(d, camera=camera)
                frame = renderer.render()
                proc.stdin.write(frame.tobytes())
                if a.preview and tick == 450:
                    from PIL import Image

                    a.preview.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(frame).save(a.preview)
            tick += 1
        end = d.qpos[adr : adr + 7].copy()
        row = {
            "phase": name,
            "seconds": seconds,
            "command": cmd,
            "start_xyz": before[:3].tolist(),
            "end_xyz": end[:3].tolist(),
            "xy_displacement_m": float(np.linalg.norm(end[:2] - before[:2])),
            "min_trunk_height_m": min(heights),
            "max_tilt_deg": max(tilts),
            "mean_forward_speed_m_s": float(np.mean(speeds[50:])),
            "mean_yaw_rate_rad_s": float(np.mean(yaws[50:])),
        }
        report.append(row)
        print(json.dumps(row), flush=True)
    if renderer:
        renderer.close()
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError("Video encoding failed")
    result = {
        "model_commit": "8d0db74916a4f833d1d9b95d6a1d7f4d13b9d5ec",
        "policy_revision": "d5a8b55033e157f1af2ed6bd5c1e435b770a8ee0",
        "bam_revision": bam_revision,
        "weights_sha256": weights_sha,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "actuator_delay_ticks": 0,
        "scope": "single nominal flat-floor smoke rollout; not Archon integration or real-hardware validation",
        "policy": "velstand.onnx",
        "mujoco": mujoco.__version__,
        "simulation_seconds": 16,
        "wall_seconds": time.perf_counter() - start,
        "phases": report,
    }
    a.report.parent.mkdir(parents=True, exist_ok=True)

    checks = {
        "upright": all(
            row["max_tilt_deg"] < 30 and row["min_trunk_height_m"] > 0.08
            for row in report
        ),
        "forward_progress": report[1]["xy_displacement_m"] > 0.2,
        "turn_response": report[2]["mean_yaw_rate_rad_s"] > 0.1,
        "settled_stop": abs(report[3]["mean_forward_speed_m_s"]) < 0.01,
    }
    result["smoke_checks"] = checks
    result["smoke_passed"] = all(checks.values())
    a.report.write_text(json.dumps(result, indent=2) + "\n")
    if not result["smoke_passed"]:
        raise SystemExit("Official-policy locomotion smoke checks failed; see report")


if __name__ == "__main__":
    main()
