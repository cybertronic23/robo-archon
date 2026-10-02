#!/usr/bin/env python3
"""Deterministic official-policy stop/restart diagnostics, with no host/lease effects.
Each case starts in the official pose, then stands 3 s, moves 6 s, stops 2 s,
and restarts for 6 s. This isolates policy command sensitivity from Archon I/O.
"""

import sys
import importlib.util
import json
import contextlib
import argparse

from pathlib import Path
import numpy as np
import mujoco

sys.dont_write_bytecode = True

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path, required=True)
args = parser.parse_args()
package = root / "python/models/external/microduck"
sys.path.insert(0, str(root / "scripts"))
from install_microduck import verify  # noqa: E402 — repository bootstrap path

verify(package, json.loads((root / "robots/microduck.lock.json").read_text()))
sys.path.insert(0, str(package))
spec = importlib.util.spec_from_file_location("official", package / "official_infer.py")
o = importlib.util.module_from_spec(spec)
with contextlib.redirect_stdout(sys.stderr):
    spec.loader.exec_module(o)
results = []
for vx, vy, yaw in [(0.2, 0, 1), (0.3, 0, 1), (0.3, 0, -1), (0.3, 0, 0), (0.4, 0, 0)]:
    with contextlib.redirect_stdout(sys.stderr):
        bam = o.load_bam_model(200, 7.4, None)
        m, d, c, n = o.load_mujoco_with_bam(
            str(package / "model/scene.xml"), bam, 0.005, 0.1, 6
        )
        p = o.PolicyInference(
            m,
            d,
            walking_onnx_path=str(package / "velstand.onnx"),
            bam_ctrl=c,
            new_cmd_obs=True,
            use_projected_gravity=True,
        )
    d.qpos[:7] = [0, 0, 0.125, 1, 0, 0, 0]
    d.qpos[p.joint_qpos_indices] = p.default_pose
    c.reset(d.qpos)
    p.set_position_targets(p.default_pose)
    mujoco.mj_forward(m, d)
    phases = []
    for label, cmd, seconds in [
        ("stand", (0, 0, 0), 3),
        ("move", (vx, vy, yaw), 6),
        ("stop", (0, 0, 0), 2),
        ("restart", (vx, vy, yaw), 6),
    ]:
        with contextlib.redirect_stdout(sys.stderr):
            p.set_vel_cmd(*cmd)
        before = d.qpos[:3].copy()
        vel = []
        yr = []
        tilt = []
        for tick in range(seconds * 50):
            p.apply_action(p.infer())
            for _ in range(4):
                c.update()
                mujoco.mj_step(m, d)
            vel.append(float(p.quat_rotate_inverse(d.qpos[3:7], d.qvel[:3])[0]))
            yr.append(float(p.get_base_ang_vel()[2]))
            tilt.append(
                float(
                    np.degrees(np.arccos(np.clip(-p.get_projected_gravity()[2], -1, 1)))
                )
            )
        phases.append(
            dict(
                label=label,
                xy=float(np.linalg.norm(d.qpos[:2] - before[:2])),
                vx=float(np.mean(vel[50:])),
                yaw=float(np.mean(yr[50:])),
                tilt=max(tilt),
            )
        )
    r = dict(cmd=[vx, vy, yaw], phases=phases)
    results.append(r)
    print(json.dumps(r), flush=True)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(results, indent=2) + "\n")
