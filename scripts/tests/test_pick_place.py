"""Physical task acceptance: real CLI/NDJSON/Executive, not mocked success."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python/robo_archon_sim_workers"))
from mujoco_worker import MujocoSession
from pick_place import measured_verdict


def session(robot="franka_panda", seed=1):
    folder = "franka_emika_panda" if robot == "franka_panda" else robot
    profile = json.loads((ROOT / f"robots/profiles/{robot}.json").read_text())
    s = MujocoSession()
    s.load(
        dict(
            model_path=str(ROOT / f"python/models/external/{folder}/scene.xml"),
            render=False,
            arm_profile=profile,
            task="pick_place",
            seed=seed,
        )
    )
    s._poll_estop = lambda: False
    return s


class PhysicalTaskTests(unittest.TestCase):
    def cli(self, robot, seed, backend="mujoco", extra=(), expect_success=True):
        binary = Path(
            os.environ.get("ROBO_ARCHON_TEST_BINARY", ROOT / "target/debug/robo-archon")
        )
        self.assertTrue(binary.is_file(), "build robo-archon-cli before task tests")
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, ROBO_ARCHON_PYTHON=sys.executable)
            command = [
                str(binary),
                "--backend",
                backend,
                "--robot",
                robot,
                "--demo",
                "pick-place",
                "--seed",
                str(seed),
                "--save-frames",
                "false",
                "--episode-dir",
                temp,
                *extra,
            ]
            run = subprocess.run(
                command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180
            )
            self.assertEqual(
                run.returncode == 0, expect_success, run.stdout + "\n" + run.stderr
            )
            paths = list(Path(temp).glob("*/episode.json"))
            self.assertEqual(len(paths), 1, run.stderr)
            episode = json.loads(paths[0].read_text())
            final = episode["events"][-1]
            self.assertEqual(final["kind"], "task_result")
            self.assertEqual(final["payload"]["success"], expect_success)
            self.assertEqual(episode["backend"], backend)
            self.assertEqual(episode["task_id"], "pick_place.v1")
            self.assertTrue(episode["ended_us"])
            if expect_success:
                evaluation = next(
                    e["payload"]
                    for e in episode["events"]
                    if e["kind"] == "task_evaluation"
                )
                for key in ("grasp_seen", "lifted", "released", "success"):
                    self.assertTrue(evaluation[key], evaluation)
                self.assertLess(evaluation["speed"], 0.05)
                distance = np.linalg.norm(
                    np.array(evaluation["object_position"][:2])
                    - evaluation["goal_position"][:2]
                )
                self.assertLess(distance, 0.025)
                self.assertEqual(
                    len([e for e in episode["events"] if e["kind"] == "task_phase"]), 11
                )
            if expect_success and os.environ.get("ROBO_ARCHON_ACCEPTANCE_DIR"):
                output = Path(os.environ["ROBO_ARCHON_ACCEPTANCE_DIR"])
                output.mkdir(parents=True, exist_ok=True)
                (output / f"{robot}-{backend}-seed{seed}.json").write_text(
                    json.dumps(episode, indent=2) + "\n"
                )
            return episode

    def test_mujoco_matrix(self):
        for robot in ("franka_panda", "so101"):
            for seed in range(3):
                with self.subTest(robot=robot, seed=seed):
                    self.cli(robot, seed)

    @unittest.skipUnless(
        os.environ.get("ROBO_ARCHON_TEST_MANISKILL") == "1",
        "second platform runs in the ManiSkill environment/job",
    )
    def test_maniskill_same_task(self):
        for seed in range(3):
            with self.subTest(seed=seed):
                self.cli("franka_panda", seed, "maniskill")

    def test_unreachable_and_collision_do_not_mutate_scene(self):
        for robot in ("franka_panda", "so101"):
            with self.subTest(robot=robot):
                s = session(robot)
                qpos = s.data.qpos.copy()
                ctrl = s.data.ctrl.copy()
                time = s.data.time
                for target in ([10, 10, 10], [float("nan"), 0, 0]):
                    with self.assertRaisesRegex(ValueError, "unreachable|invalid"):
                        s.solve_ik(target)
                    np.testing.assert_array_equal(s.data.qpos, qpos)
                    np.testing.assert_array_equal(s.data.ctrl, ctrl)
                # Downward TCP below floor is kinematically reachable but unsafe.
                x = 0.45 if robot == "franka_panda" else 0.2
                with self.assertRaisesRegex(ValueError, "collision"):
                    s.solve_ik([x, 0, 0.001])
                np.testing.assert_array_equal(s.data.qpos, qpos)
                self.assertEqual(s.data.time, time)
                s.apply_estop()
                np.testing.assert_allclose(
                    s.data.ctrl[s.actuator_ids], s._joint_positions()
                )
                reset = s.reset()
                self.assertFalse(reset["annotations"][0]["payload"]["grasp_seen"])

    def test_task_success_requires_every_measured_condition(self):
        s = session()
        c = s.task_config
        good = dict(
            config=c, pos=c["goal"], speed=0.0, openness=1.0, peak=0.15, grasp_seen=True
        )
        self.assertTrue(measured_verdict(**good)["success"])
        for change in (
            dict(grasp_seen=False),
            dict(peak=0.03),
            dict(pos=c["start"]),
            dict(openness=0.5),
            dict(speed=0.1),
        ):
            self.assertFalse(measured_verdict(**dict(good, **change))["success"])
        self.assertFalse(s.observation()["annotations"][0]["payload"]["success"])

    def test_actuator_failure_stops_before_lift_and_records_failure(self):
        ep = self.cli(
            "franka_panda",
            1,
            extra=(
                "--worker",
                str(ROOT / "scripts/tests/fixtures/open_gripper_worker.py"),
            ),
            expect_success=False,
        )
        self.assertIn("physical grasp failed", ep["events"][-1]["payload"]["error"])
        phases = [
            e["payload"]["name"] for e in ep["events"] if e["kind"] == "task_phase"
        ]
        self.assertNotIn("lift", phases)

    def test_stop_does_not_resume_next_phase(self):
        for backend in (
            ("mujoco", "maniskill")
            if os.environ.get("ROBO_ARCHON_TEST_MANISKILL") == "1"
            else ("mujoco",)
        ):
            ep = self.cli(
                "franka_panda",
                1,
                backend,
                extra=("--auto-stop-ms", "20", "--step-ms", "2"),
                expect_success=False,
            )
            phases = [
                e["payload"]["name"] for e in ep["events"] if e["kind"] == "task_phase"
            ]
            self.assertEqual(phases, ["approach"])
            self.assertIn("cancel", ep["events"][-1]["payload"]["error"].lower())


if __name__ == "__main__":
    unittest.main()
