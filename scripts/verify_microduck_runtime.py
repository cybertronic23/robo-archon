#!/usr/bin/env python3
"""CPU integration acceptance: one worker, continuous physics, bounded commands and watchdog.
Run with the pinned Microduck Python environment; output is a local validation report.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package", type=Path, default=ROOT / "python/models/external/microduck"
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    script = ROOT / "python/robo_archon_sim_workers/microduck_worker.py"
    spec = importlib.util.spec_from_file_location("worker", script)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    assert worker.safety_fault(None, True, 46, 0.12) == "fallen"
    assert worker.safety_fault("fallen", True, 0, 0.12) == "fallen"
    assert worker.safety_fault(None, False, 0, 0.12) == "non_finite_state"
    assert worker.safety_fault("fallen", False, 0, 0.12) == "non_finite_state"
    assert worker.safety_fault(None, True, 0, 0.07) == "fallen"
    assert worker.safety_fault(None, True, 0, 0.12) is None
    process = subprocess.Popen(
        [sys.executable, "-u", str(script), "--package", str(args.package)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    sequence = 0

    def exchange(op, expect_ok=True, **fields):
        nonlocal sequence
        sequence += 1
        request = {"op": op, "id": sequence, "protocol": worker.PROTOCOL, **fields}
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 30)[0], "worker response timeout"
        response = json.loads(process.stdout.readline())
        assert response["id"] == sequence and response["ok"] == expect_ok, response
        return response["observation"]

    observations = []
    try:
        first = exchange("hello")
        invalid = exchange(
            "command",
            expect_ok=False,
            twist=[0.31, 0, 0],
            duration_ms=1000,
            lease_ms=600,
        )
        assert invalid["command"] == [0, 0, 0]
        exchange("command", twist=[0.3, 0, 0], duration_ms=3000, lease_ms=200)
        time.sleep(0.5)
        expired = exchange("observe")
        assert expired["reason"] == "lease_expired" and expired["command"] == [
            0,
            0,
            0,
        ], expired
        time.sleep(1)
        # Repeat stand/forward/turn in the same process: policy state never resets between skills.
        for cycle in range(2):
            for label, twist, duration in [
                ("stand", [0, 0, 0], 1000),
                ("forward", [0.3, 0, 0], 6000),
                ("turn", [0.2, 0, 1], 4000),
            ]:
                start = exchange(
                    "command", twist=twist, duration_ms=duration, lease_ms=600
                )
                while True:
                    time.sleep(0.1)
                    end = exchange("poll")
                    assert end["fault"] is None, end
                    if end["state"] != "running":
                        break
                assert end["reason"] == "duration_complete", end
                displacement = (
                    sum((a - b) ** 2 for a, b in zip(end["xyz"][:2], start["xyz"][:2]))
                    ** 0.5
                )
                mean_yaw = end["motion"]["sum_yaw_rate"] / end["motion"]["samples"]
                motion_gate = (
                    displacement > 0.1
                    if label == "forward"
                    else mean_yaw > 0.15
                    if label == "turn"
                    else None
                )
                exchange("stop")
                stable = 0
                deadline = time.monotonic() + 4
                while stable < 3:
                    time.sleep(0.1)
                    settled = exchange("observe")
                    vx, vy = settled["body_velocity"][:2]
                    stable = (
                        stable + 1
                        if (vx * vx + vy * vy) ** 0.5 < 0.015
                        and abs(settled["yaw_rate"]) < 0.08
                        else 0
                    )
                    assert time.monotonic() < deadline and settled["fault"] is None, (
                        settled
                    )
                observations.append(
                    {
                        "cycle": cycle,
                        "command": label,
                        "start": start,
                        "end": end,
                        "settled": settled,
                        "displacement_xy": displacement,
                        "motion_gate_passed": motion_gate,
                    }
                )
        exchange("command", twist=[0.3, 0, 0], duration_ms=3000, lease_ms=600)
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        report = {
            "acceptance": "protocol/continuous-lifecycle passed; motion gates reported separately",
            "first": first,
            "watchdog": expired,
            "invalid_command": invalid,
            "rollouts": observations,
            "disconnect_exit": 0,
            "fault_detection": "threshold/latching unit checks; no induced physical fall rollout",
            "duration_clock": "wall clock; measured simulation time retained",
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Continuous worker acceptance passed: {args.report}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
