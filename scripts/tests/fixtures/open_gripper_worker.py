"""Fault injection: simulated gripper actuator stuck open; real MuJoCo physics."""

from pathlib import Path
import sys

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "python/robo_archon_sim_workers")
)
from mujoco_worker import MujocoSession, main

original = MujocoSession.command


def stuck_open(self, msg):
    return original(self, dict(msg, gripper_open=1.0))


MujocoSession.command = stuck_open
if __name__ == "__main__":
    raise SystemExit(main())
