#!/usr/bin/env python3
"""Accept pinned official motions in one Archon episode; no ball/object task claim."""

import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--report",
        type=Path,
        default=ROOT / "tmp-episodes/m2g4-behavior-acceptance.json",
    )
    p.add_argument("--record-dir", type=Path)
    args = p.parse_args()
    directory = ROOT / "tmp-episodes/m2g4-behavior-tests"
    directory.mkdir(parents=True, exist_ok=True)
    names = ["sitstand", "ground_pick", "kick_left", "kick_right", "roulade"]
    steps = []
    for name in names:
        steps.append(
            {"skill_id": "microduck." + name, "parameters": {}, "timeout_ms": 10000}
        )
        steps.append(
            {
                "skill_id": "microduck.walk",
                "parameters": {"vx": 0.4, "duration_ms": 1000},
                "timeout_ms": 4000,
            }
        )
    plan = directory / "plan.json"
    plan.write_text(
        json.dumps({"schema_version": 1, "timeout_ms": 70000, "steps": steps})
    )
    report = directory / "result.json"
    command = [
        str(ROOT / "target/debug/robo-archon"),
        "--robot",
        "microduck",
        "--backend",
        "mujoco",
        "--run-skill-sequence",
        str(plan),
        "--skill-report",
        str(report),
    ]
    if args.record_dir:
        command += ["--skill-record-dir", str(args.record_dir)]
    output = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=100
    )
    assert output.returncode == 0, output.stderr[-2500:]
    result = json.loads(report.read_text())
    assert result["status"] == "succeeded" and len(result["steps"]) == 10
    last_time = -1
    for index, step in enumerate(result["steps"]):
        obs = step["observation"]
        assert (
            obs["fault"] is None
            and obs["stop_confirmed"]
            and obs["xyz"][2] >= 0.1
            and obs["tilt_deg"] < 15
        )
        assert obs["simulation_time"] > last_time
        last_time = obs["simulation_time"]
        if index % 2 == 0:
            name = names[index // 2]
            motion = obs["motion"]
            assert motion["executed_policy_id"] == "official." + name
            assert (
                max(b - a for a, b in zip(motion["joint_min"], motion["joint_max"]))
                > 0.2
            )
            if name == "sitstand":
                assert motion["min_height"] < 0.08
            elif name == "ground_pick":
                assert motion["max_tilt_deg"] > 15
            elif name == "roulade":
                assert motion["max_tilt_deg"] > 120
        else:
            motion = obs["motion"]
            delta = (
                sum((obs["xyz"][j] - motion["start_xyz"][j]) ** 2 for j in (0, 1))
                ** 0.5
            )
            assert delta > 0.02, (index, delta)
    evidence = {
        "accepted": True,
        "scope": "five official motions with walking restart after each, same episode, flat floor; kicks in air, peck without object",
        "result": result,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "accepted": True,
                "steps": len(result["steps"]),
                "elapsed_ms": result["elapsed_ms"],
                "report": str(args.report),
            }
        )
    )


if __name__ == "__main__":
    main()
