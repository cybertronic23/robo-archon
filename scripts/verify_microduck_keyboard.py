#!/usr/bin/env python3
"""Exercise the real CLI keyboard/Executive/worker chain through a Unix PTY."""

import argparse
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            str(ROOT / "target/debug/robo-archon"),
            "--robot",
            "microduck",
            "--backend",
            "mujoco",
            "--skill-keyboard",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=ROOT,
        start_new_session=True,
    )
    os.close(slave)
    buffer = b""
    outcomes = []

    def wait_for(predicate, seconds=30):
        nonlocal buffer
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                buffer += os.read(master, 65536)
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode(errors="replace").strip()
                    if predicate(line):
                        return line
            assert process.poll() is None, "CLI exited prematurely"
        raise AssertionError("keyboard result timeout")

    def result():
        line = wait_for(lambda line: line.startswith('{"skill_id":'))
        value = json.loads(line)
        assert (
            value["observation"]["fault"] is None
            and value["observation"]["stop_confirmed"] is True
        ), value
        outcomes.append(value)
        return value

    try:
        wait_for(lambda line: "Commands expire after 2 seconds." in line)
        # Delayed user input is intentional: exercise standing -> motion, not only startup.
        time.sleep(1)
        for key, direction in [("w", 0), ("a", 1), ("d", -1), ("w", 0)]:
            os.write(master, key.encode())
            value = result()
            assert value["status"] == "succeeded", value
            motion = value["observation"]["motion"]
            if direction:
                assert motion["sum_yaw_rate"] / motion["samples"] * direction > 0.15, (
                    value
                )
            else:
                start = motion["start_xyz"]
                end = value["observation"]["xyz"]
                assert (
                    sum((a - b) ** 2 for a, b in zip(start[:2], end[:2])) ** 0.5 > 0.05
                ), value
            time.sleep(0.3)
        os.write(master, b"w")
        time.sleep(0.4)
        os.write(master, b"x")
        cancelled = result()
        assert cancelled["status"] == "cancelled", cancelled
        stopped = result()
        assert (
            stopped["skill_id"] == "microduck.stop" and stopped["status"] == "succeeded"
        ), stopped
        os.write(master, b"q")
        assert process.wait(timeout=5) == 0
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "acceptance": "passed",
                    "keys": "w a d w, w interrupted by x, q",
                    "native_viewer": False,
                    "results": outcomes,
                },
                indent=2,
            )
            + "\n"
        )
        print(f"Keyboard/Executive acceptance passed: {args.report}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


if __name__ == "__main__":
    main()
