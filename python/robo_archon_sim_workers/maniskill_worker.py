#!/usr/bin/env python3
"""ManiSkill/SAPIEN CPU Panda binding, sharing the measured pick_place.v1 contract.

MuJoCo is used only as a locked kinematic reference (IK / conservative path check).
All execution, object movement, grasp contacts and task measurements use SAPIEN.
"""

from pathlib import Path
import contextlib
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import PROTOCOL_VERSION, now_us, read_msg, write_msg, write_error


class ManiSkillSession:
    def load(self, hello):
        # Library initialization can print diagnostics; stdout belongs to NDJSON.
        import json
        import numpy as np
        import sapien
        import torch
        import mujoco
        from mani_skill.envs.sapien_env import BaseEnv
        from mani_skill.utils.structs.pose import Pose
        from mani_skill.utils.building.ground import build_ground
        from mani_skill.sensors.camera import CameraConfig
        from mani_skill.utils import sapien_utils
        from pick_place import scene_model, CartesianIK, CollisionGuard, TaskEvaluator
        from mujoco_worker import MujocoSession

        if sys.platform == "darwin" and not (
            hello.get("render", False) or hello.get("video_out")
        ):
            from mani_skill.render import utils as render_utils

            original = render_utils.can_render
            # Upstream 3.0.1 reports can_render(None)=True on Darwin.
            render_utils.can_render = lambda device: device is not None and original(
                device
            )
        profile = hello.get("arm_profile")
        if (
            not profile
            or profile["robot_id"] != "franka_panda"
            or hello.get("task") != "pick_place"
        ):
            raise ValueError(
                "ManiSkill binding requires --robot franka_panda --demo pick-place"
            )
        if hello.get("viewer"):
            raise ValueError(
                "ManiSkill CPU binding has no interactive viewer; use --record-video"
            )
        from mani_skill import PACKAGE_ASSET_DIR
        from asset_integrity import validate_runtime_asset

        self.runtime_asset = validate_runtime_asset(
            Path(__file__).resolve().parents[2] / "robots/runtime-assets.lock.json",
            PACKAGE_ASSET_DIR,
        )
        self.profile = profile
        self.joint_names = profile["joint_names"]
        self.dof = len(self.joint_names)
        self.model_path = Path(hello["model_path"])
        # Same checksum, identity, actuator and limit validation as the MuJoCo binding.
        self.reference = MujocoSession()
        self.reference.load(
            dict(
                hello,
                render=False,
                viewer=False,
                record_dir=None,
                video_out=None,
                media_root=None,
            )
        )
        self.model = self.reference.model
        self.data = self.reference.data
        self.ik = self.reference.ik
        self.guard = self.reference.collision_guard
        self.config = self.reference.task_config
        self.seed = hello.get("seed", 0)
        config = self.config
        home = profile["home"]
        want_render = hello.get("render", False) or bool(hello.get("video_out"))

        class SharedPickPlaceEnv(BaseEnv):
            SUPPORTED_ROBOTS = ["panda"]

            def _setup_scene(env):
                if not want_render:
                    # ManiSkill 3.0.1 unconditionally forces a renderer on macOS.
                    # Respect our explicit headless setting before building native systems.
                    env._render_device = None
                    env.backend.render_device = None
                    env.backend.render_backend = "none"
                super()._setup_scene()

            def _load_agent(env, options):
                super()._load_agent(options, sapien.Pose())

            def _load_scene(env, options):
                env.floor = build_ground(env.scene, floor_width=4, name="task_floor")
                material = sapien.physx.PhysxMaterial(
                    static_friction=3, dynamic_friction=3, restitution=0
                )
                builder = env.scene.create_actor_builder()
                builder.add_box_collision(
                    half_size=[0.02] * 3, material=material, density=625
                )
                if want_render:
                    builder.add_box_visual(
                        half_size=[0.02] * 3,
                        material=sapien.render.RenderMaterial(
                            base_color=[0.95, 0.12, 0.06, 1]
                        ),
                    )
                builder.set_initial_pose(sapien.Pose(p=config["start"]))
                env.cube = builder.build(name="task_cube")
                builder = env.scene.create_actor_builder()
                builder.add_box_collision(
                    half_size=[0.055, 0.045, 0.003], material=material
                )
                if want_render:
                    builder.add_box_visual(
                        half_size=[0.055, 0.045, 0.003],
                        material=sapien.render.RenderMaterial(
                            base_color=[0.15, 0.7, 0.3, 1]
                        ),
                    )
                builder.set_initial_pose(
                    sapien.Pose(p=[config["goal"][0], config["goal"][1], 0.003])
                )
                env.tray = builder.build_static(name="task_tray")

            def _initialize_episode(env, env_idx, options):
                env.agent.reset(np.array(home + [0.04, 0.04], dtype=np.float32))
                env.agent.robot.set_pose(sapien.Pose())
                env.cube.set_pose(Pose.create_from_pq(config["start"]))

            @property
            def _default_human_render_camera_configs(env):
                return CameraConfig(
                    "render_camera",
                    sapien_utils.look_at([1.2, -1.1, 0.8], [0.3, 0.05, 0.18]),
                    640,
                    480,
                    1,
                    0.01,
                    100,
                )

        self.env = SharedPickPlaceEnv(
            robot_uids="panda",
            obs_mode="state",
            control_mode="pd_joint_pos",
            reward_mode="none",
            sim_backend="cpu",
            render_backend="cpu" if want_render else "none",
            render_mode="rgb_array" if want_render else None,
            sim_config=dict(sim_freq=500, control_freq=50),
        )
        self.env.reset(seed=self.seed)
        self.peak = config["start"][2]
        self.grasp_seen = False
        self.grip_target = 1.0
        self.media_root = None
        self.render = want_render
        self.record_dir = Path(hello["record_dir"]) if hello.get("record_dir") else None
        self.video_out = Path(hello["video_out"]) if hello.get("video_out") else None
        self.frame_seq = 0
        self.video_frame_seq = 0
        self.set_media_root(hello.get("media_root"))
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
        self._sync_reference()
        tcp = self.env.agent.tcp.pose.p.cpu().numpy()[0]
        if np.linalg.norm(tcp - self.data.site_xpos[self.ik.site]) > 0.003:
            raise RuntimeError(
                f"URDF/reference TCP mismatch: {tcp} vs {self.data.site_xpos[self.ik.site]}"
            )

    def _sync_reference(self):
        import mujoco

        q = self.env.agent.robot.qpos.cpu().numpy()[0]
        self.data.qpos[self.ik.qadr] = q[:7]
        for name, value in zip(self.profile["gripper"]["joint_names"], q[7:]):
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
            self.data.qpos[self.model.jnt_qposadr[jid]] = value
        bid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "task_cube")
        j = int(self.model.body_jntadr[bid])
        a = int(self.model.jnt_qposadr[j])
        pose = self.env.cube.pose.raw_pose.cpu().numpy()[0]
        self.data.qpos[a : a + 7] = pose
        mujoco.mj_forward(self.model, self.data)

    def _measured_gripper_open(self):
        return float(
            self.env.agent.robot.qpos[0, 7:].mean().clamp(0, 0.04).item() / 0.04
        )

    def _joint_positions(self):
        return self.env.agent.robot.qpos[0, :7].cpu().tolist()

    def task_annotations(self):
        import numpy as np
        from pick_place import measured_verdict

        pos = self.env.cube.pose.p.cpu().numpy()[0]
        self.peak = max(self.peak, float(pos[2]))
        self.grasp_seen |= bool(self.env.agent.is_grasping(self.env.cube).item())
        speed = float(self.env.cube.linear_velocity.norm().item())
        state = measured_verdict(
            self.config,
            pos,
            speed,
            self._measured_gripper_open(),
            self.peak,
            self.grasp_seen,
        )
        state["runtime_asset"] = self.runtime_asset
        return [dict(kind="task_state", stamp_us=now_us(), payload=state)]

    def observation(self):
        modalities = []
        if self.render and self.media_root:
            from protocol import write_ppm

            pixels = self.env.render_rgb_array()[0].cpu().numpy()
            self.frame_seq += 1
            h, w, _ = pixels.shape
            path = (
                self.media_root / "media/images.primary" / f"{self.frame_seq:06d}.ppm"
            )
            write_ppm(str(path), w, h, pixels.tobytes())
            modalities.append(
                dict(
                    key="images.primary",
                    stamp_us=now_us(),
                    frame_id="overview",
                    encoding="rgb8",
                    width=w,
                    height=h,
                    uri=str(path),
                )
            )
        return dict(
            type="observation",
            stamp_us=now_us(),
            proprio=dict(
                joint_names=self.joint_names,
                positions=self._joint_positions(),
                gripper_open=self._measured_gripper_open(),
            ),
            modalities=modalities,
            annotations=self.task_annotations(),
        )

    def solve_ik(self, target, down=True):
        self._sync_reference()
        q = self.ik.solve(self.data.qpos, target, down)
        self.guard.path(self.data.qpos, self.ik.qadr, q)
        return dict(type="ik_solution", joint_names=self.joint_names, positions=q)

    def command(self, msg):
        import numpy as np

        q = np.array(msg.get("positions"), dtype=float)
        g = msg.get("gripper_open", self.grip_target)
        if (
            msg.get("names") != self.joint_names
            or q.shape != (7,)
            or not np.isfinite(q).all()
        ):
            raise ValueError("invalid command layout/values")
        if (
            np.any(q < self.profile["lower"])
            or np.any(q > self.profile["upper"])
            or not np.isfinite(g)
            or not 0 <= g <= 1
        ):
            raise ValueError("command outside profile limits")
        self._sync_reference()
        self.guard.path(self.data.qpos, self.ik.qadr, q)
        self.grip_target = g
        # Map normalized openness to mimic controller [-0.01,0.04] physical target.
        grip_action = 2 * (0.04 * g + 0.01) / 0.05 - 1
        self.env.step(np.r_[q, grip_action].astype(np.float32))
        self._sync_reference()
        self.guard.check(self.data)
        self._check_contacts()
        if self.render and self.record_dir:
            from protocol import write_ppm

            pixels = self.env.render_rgb_array()[0].cpu().numpy()
            h, w, _ = pixels.shape
            self.video_frame_seq += 1
            write_ppm(
                str(self.record_dir / f"frame_{self.video_frame_seq:06d}.ppm"),
                w,
                h,
                pixels.tobytes(),
            )
        return self.observation()

    def _check_contacts(self):
        # Check actual SAPIEN contacts too; shared proxy check is conservative planning only.
        for contact in self.env.scene.get_contacts():
            a, b = contact.bodies
            names = {a.entity.name, b.entity.name}
            if not any(point.separation < -0.0005 for point in contact.points):
                continue
            if "task_cube" in names and names.issubset(
                {
                    "task_cube",
                    "task_floor",
                    "task_tray",
                    "panda_leftfinger",
                    "panda_rightfinger",
                }
            ):
                continue
            if names.issubset({"task_floor", "panda_link0"}):
                continue
            raise ValueError(f"SAPIEN collision rejected: {sorted(names)}")

    def reset(self):
        self.env.reset(seed=self.seed)
        self.peak = self.config["start"][2]
        self.grasp_seen = False
        self.grip_target = 1.0
        self._sync_reference()
        return self.observation()

    def apply_estop(self):
        import numpy as np

        q = self.env.agent.robot.qpos.cpu().numpy()[0]
        self.env.agent.robot.set_drive_target(q)

    def set_media_root(self, root):
        self.media_root = Path(root) if root else None
        if self.media_root:
            (self.media_root / "media/images.primary").mkdir(
                parents=True, exist_ok=True
            )

    def shutdown(self):
        import subprocess

        if self.video_out and self.record_dir:
            self.video_out.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-framerate",
                    "50",
                    "-i",
                    str(self.record_dir / "frame_%06d.ppm"),
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(self.video_out),
                ],
                check=True,
                capture_output=True,
            )
        self.env.close()


def main():
    import os

    # Native Vulkan/PhysX diagnostics bypass Python redirect_stdout.
    wire_stdout = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = wire_stdout
    session = ManiSkillSession()
    try:
        hello = read_msg()
        if (
            not hello
            or hello.get("type") != "hello"
            or hello.get("platform") != "maniskill"
        ):
            raise ValueError("expected ManiSkill hello")
        with contextlib.redirect_stdout(sys.stderr):
            session.load(hello)
        write_msg(
            dict(
                type="hello_ok",
                protocol_version=PROTOCOL_VERSION,
                platform="maniskill",
                joint_names=session.joint_names,
                dof=session.dof,
                model_path=str(session.model_path),
            )
        )
        while True:
            msg = read_msg()
            if msg is None:
                break
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    kind = msg["type"]
                    if kind == "observe":
                        response = session.observation()
                    elif kind == "reset":
                        response = session.reset()
                    elif kind == "solve_ik":
                        response = session.solve_ik(
                            msg["target"], msg.get("down", True)
                        )
                    elif kind == "command":
                        response = session.command(msg)
                    elif kind == "estop":
                        session.apply_estop()
                        response = dict(type="ack")
                    elif kind == "set_media_root":
                        session.set_media_root(msg.get("media_root"))
                        response = dict(type="ack")
                    elif kind == "shutdown":
                        session.shutdown()
                        response = dict(type="ack")
                    else:
                        raise ValueError(f"unknown type: {kind}")
                write_msg(response)
                if kind == "shutdown":
                    break
            except (ValueError, RuntimeError) as error:
                session.apply_estop()
                write_error(str(error))
        return 0
    except Exception as error:
        write_error(f"{error}\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
