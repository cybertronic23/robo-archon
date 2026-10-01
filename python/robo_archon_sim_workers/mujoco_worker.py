#!/usr/bin/env python3
"""MuJoCo NDJSON worker for RoboArchon BridgedSimBackend (protocol v1)."""

from __future__ import annotations

import os
import select
import sys
import time
import traceback
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from asset_integrity import tree_hash
from protocol import (
    PROTOCOL_VERSION,
    now_us,
    read_msg,
    write_error,
    write_msg,
    write_ppm,
)  # noqa: E402


def _default_model_path() -> Path:
    return _HERE.parent / "models" / "desktop_arm_red_blob.xml"


def discover_actuated_joints(model) -> Tuple[List[str], List[int]]:
    """Return (joint_names, actuator_ids) for position/motor actuators with joints."""
    import mujoco

    names: List[str] = []
    acts: List[int] = []
    for i in range(model.nu):
        # trnid[i, 0] is joint id for joint actuators
        jid = int(model.actuator_trnid[i][0])
        if jid < 0:
            continue
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, jid)
        if not name:
            name = f"joint_{len(names) + 1}"
        names.append(name)
        acts.append(i)
    if not names:
        # Fall back to all hinge joints
        for j in range(model.njnt):
            if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_HINGE:
                name = (
                    mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, j)
                    or f"joint_{j}"
                )
                names.append(name)
    return names, acts


class MujocoSession:
    def __init__(self) -> None:
        self.model = None
        self.data = None
        self.renderer = None
        self.joint_names: List[str] = []
        self.actuator_ids: List[int] = []
        self.dof = 6
        self.render = True
        self.media_root: Optional[Path] = None
        self.frame_seq = 0
        self.gripper_open = 0.5
        self.camera_name = "scene"
        self.render_camera = None
        self.width = 64
        self.height = 48
        self.model_path: Optional[Path] = None
        self.viewer = None
        self.want_viewer = False
        self.record_dir: Optional[Path] = None
        self.video_out: Optional[Path] = None
        self.video_frame_seq = 0
        self.record_every_n = 2  # subsample for smoother file size
        self.hold_viewer_on_shutdown = True
        # Estop consumed mid-command already applied; next estop only needs ack.
        self._estop_applied = False
        self.arm_profile = None
        self.gripper_actuator_id = None
        self.task_config = None
        self.task_evaluator = None
        self.ik = None
        self.collision_guard = None

    def load(self, hello: Dict[str, Any]) -> None:
        try:
            import mujoco
        except ImportError as e:
            raise RuntimeError(
                "mujoco Python package not installed. "
                "pip install -r python/requirements-mujoco.txt"
            ) from e

        self.render = bool(hello.get("render", True))
        self.want_viewer = bool(hello.get("viewer", False))
        # Default true for one-shot demos; TUI sets false so /quit is non-blocking.
        self.hold_viewer_on_shutdown = bool(hello.get("hold_viewer_on_shutdown", True))
        media = hello.get("media_root")
        self.media_root = Path(media) if media else None
        if hello.get("record_dir"):
            self.record_dir = Path(hello["record_dir"])
            self.record_dir.mkdir(parents=True, exist_ok=True)
        if hello.get("video_out"):
            self.video_out = Path(hello["video_out"])
            if self.record_dir is None:
                self.record_dir = (
                    self.video_out.parent / f".frames_{self.video_out.stem}"
                )
                self.record_dir.mkdir(parents=True, exist_ok=True)
        if hello.get("camera"):
            self.camera_name = str(hello["camera"])
        # High-res only when recording MP4; episode thumbnails stay cheap
        if self.record_dir is not None:
            self.width = int(hello.get("record_width") or 1280)
            self.height = int(hello.get("record_height") or 720)

        model_path = (
            hello.get("model_path")
            or os.environ.get("ROBO_ARCHON_MUJOCO_MODEL")
            or os.environ.get("ARCHON_MUJOCO_MODEL")
        )
        if model_path:
            self.model_path = Path(model_path)
        else:
            self.model_path = _default_model_path()
        if not self.model_path.exists():
            raise RuntimeError(f"MJCF not found: {self.model_path}")

        # Load relative mesh assets from model directory
        if hello.get("task") == "pick_place":
            if hello.get("arm_profile") is None:
                raise RuntimeError("pick_place requires arm profile")
            from pick_place import (
                scene_model,
                CartesianIK,
                TaskEvaluator,
                CollisionGuard,
            )

            self.model, self.task_config = scene_model(
                self.model_path, hello["arm_profile"], hello.get("seed", 0)
            )
            self.ik = CartesianIK(self.model, hello["arm_profile"])
            self.collision_guard = CollisionGuard(self.model, self.task_config)
            self.task_evaluator = TaskEvaluator(self.model, self.task_config)
        else:
            self.model = mujoco.MjModel.from_xml_path(str(self.model_path))
        self.data = mujoco.MjData(self.model)
        mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)

        auto = bool(hello.get("auto_joints", True))
        requested = list(hello.get("joint_names") or [])
        if auto or not requested:
            self.joint_names, self.actuator_ids = discover_actuated_joints(self.model)
        else:
            self.joint_names = requested
            self.actuator_ids = list(range(min(len(requested), self.model.nu)))
        self.arm_profile = hello.get("arm_profile")
        if self.arm_profile is not None:
            self._configure_arm_profile(self.arm_profile)
        self.dof = len(self.joint_names)
        if self.dof == 0:
            raise RuntimeError("no actuated joints found in model")

        is_car = any(n.startswith("root_") for n in self.joint_names)

        # Offscreen renderer before viewer — more reliable on macOS when both are needed.
        need_renderer = self.render or self.record_dir is not None
        if need_renderer:
            self._init_renderer()

        if self.want_viewer:
            try:
                import mujoco.viewer

                self.viewer = mujoco.viewer.launch_passive(self.model, self.data)
                if is_car:
                    self.viewer.cam.lookat[:] = [0.4, 0.0, 0.1]
                    self.viewer.cam.distance = 2.4
                    self.viewer.cam.azimuth = 140
                    self.viewer.cam.elevation = -25
                else:
                    self.viewer.cam.lookat[:] = [0.12, 0.0, 0.28]
                    self.viewer.cam.distance = 1.15
                    self.viewer.cam.azimuth = 140
                    self.viewer.cam.elevation = -20
                sys.stderr.write(
                    "[mujoco_worker] interactive viewer opened "
                    "(window stays open until you close it after motion)\n"
                )
            except Exception as e:
                sys.stderr.write(f"[mujoco_worker] viewer failed to open: {e}\n")
                self.viewer = None
                self.want_viewer = False

        if self.arm_profile is not None:
            self.render_camera = mujoco.MjvCamera()
            self.render_camera.lookat[:] = (
                [0.25, 0, 0.4]
                if self.arm_profile["robot_id"] == "franka_panda"
                else [0.13, 0.025, 0.13]
            )
            self.render_camera.distance = (
                1.8 if self.arm_profile["robot_id"] == "franka_panda" else 0.65
            )
            self.render_camera.azimuth = 140
            self.render_camera.elevation = -25
            if self.viewer is not None:
                self.viewer.cam.lookat[:] = self.render_camera.lookat
                self.viewer.cam.distance = self.render_camera.distance

        # Pick a usable camera if configured one is missing
        if self.renderer is not None:
            cid = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_CAMERA, self.camera_name
            )
            if cid < 0 and self.model.ncam > 0:
                alt = mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_CAMERA, 0)
                if alt:
                    self.camera_name = alt

    def _init_renderer(self) -> None:
        """Create offscreen Renderer, trying GL backends that work on CI / macOS."""
        import mujoco

        assert self.model is not None
        self.model.vis.global_.offwidth = max(
            self.width, self.model.vis.global_.offwidth
        )
        self.model.vis.global_.offheight = max(
            self.height, self.model.vis.global_.offheight
        )
        sizes = [(self.height, self.width)]
        # Fallback smaller size if high-res offscreen fails (common on constrained CI).
        if self.width > 320 or self.height > 240:
            sizes.append((240, 320))

        preferred = os.environ.get("MUJOCO_GL", "").strip().lower()
        # On Linux CI without display, egl/osmesa usually work; glfw needs a display.
        if preferred:
            gl_candidates = [preferred]
        elif sys.platform == "darwin":
            gl_candidates = ["glfw", "egl", ""]
        else:
            gl_candidates = ["egl", "osmesa", "glfw", ""]

        last_err: Optional[BaseException] = None
        for gl in gl_candidates:
            if gl:
                os.environ["MUJOCO_GL"] = gl
            for h, w in sizes:
                try:
                    self.renderer = mujoco.Renderer(self.model, height=h, width=w)
                    self.width = w
                    self.height = h
                    self.render = True
                    if gl or (h, w) != (self.height, self.width):
                        sys.stderr.write(
                            f"[mujoco_worker] offscreen renderer ok "
                            f"(MUJOCO_GL={os.environ.get('MUJOCO_GL', '')!r} {w}x{h})\n"
                        )
                    return
                except Exception as e:
                    last_err = e
                    self.renderer = None

        sys.stderr.write(f"[mujoco_worker] offscreen render disabled: {last_err}\n")
        self.renderer = None
        if self.record_dir is not None:
            sys.stderr.write(
                "[mujoco_worker] WARNING: cannot record MP4 without offscreen renderer. "
                "Try MUJOCO_GL=egl (Linux) or screen-capture the viewer on macOS "
                "(Cmd+Shift+5).\n"
            )

    def set_media_root(self, media_root: Optional[str]) -> None:
        self.media_root = Path(media_root) if media_root else None
        self.frame_seq = 0
        if self.media_root is not None:
            (self.media_root / "media" / "images.primary").mkdir(
                parents=True, exist_ok=True
            )
            # Ensure renderer exists once media is requested mid-session.
            if self.renderer is None and self.model is not None:
                self.render = True
                self._init_renderer()

    def _sync_viewer(self) -> None:
        if self.viewer is None:
            return
        try:
            if not self.viewer.is_running():
                self.viewer = None
                return
            self.viewer.sync()
        except Exception as e:
            sys.stderr.write(f"[mujoco_worker] viewer sync error: {e}\n")
            self.viewer = None

    def close_viewer(self) -> None:
        if self.viewer is None:
            return
        try:
            self.viewer.close()
        except Exception:
            pass
        self.viewer = None

    def hold_viewer_until_closed(self) -> None:
        """Keep window open after the run so the user can inspect the scene."""
        if self.viewer is None:
            return
        sys.stderr.write(
            "[mujoco_worker] motion finished — close the MuJoCo window to exit\n"
        )
        try:
            while self.viewer is not None and self.viewer.is_running():
                self.viewer.sync()
                time.sleep(0.03)
        except Exception:
            pass
        self.viewer = None

    def _configure_arm_profile(self, profile):
        import mujoco
        import math

        stamp = self.model_path.parent / ".robo-archon-install.json"
        metadata = json.loads(stamp.read_text())
        if metadata["sha256"] != tree_hash(self.model_path.parent):
            raise RuntimeError("installed asset checksum mismatch; reinstall assets")
        if (
            metadata["revision"] != profile["source_revision"]
            or metadata["robot"] != profile["robot_id"]
        ):
            raise RuntimeError(
                "installed model does not match profile revision/identity"
            )
        names = profile["joint_names"]
        arrays = [profile[k] for k in ("actuator_names", "home", "lower", "upper")]
        if not names or any(len(v) != len(names) for v in arrays):
            raise RuntimeError("profile dimension mismatch")
        self.joint_names = names
        self.actuator_ids = []
        for i, (joint, actuator) in enumerate(zip(names, profile["actuator_names"])):
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, joint)
            aid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, actuator)
            if jid < 0 or aid < 0 or self.model.actuator_trnid[aid, 0] != jid:
                raise RuntimeError(
                    "profile joint/actuator mapping missing or mismatched"
                )
            lo, hi = self.model.jnt_range[jid]
            if (
                abs(lo - profile["lower"][i]) > 1e-6
                or abs(hi - profile["upper"][i]) > 1e-6
            ):
                raise RuntimeError("profile limits differ from compiled model")
            value = profile["home"][i]
            if not math.isfinite(value) or not lo <= value <= hi:
                raise RuntimeError("invalid profile home")
            self.actuator_ids.append(aid)
        g = profile["gripper"]
        self.gripper_actuator_id = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, g["actuator_name"]
        )
        if self.gripper_actuator_id < 0:
            raise RuntimeError("gripper actuator missing")
        if not (
            len(g["joint_names"])
            == len(g["open_positions"])
            == len(g["closed_positions"])
        ):
            raise RuntimeError("gripper dimension mismatch")
        for name, opened, closed in zip(
            g["joint_names"], g["open_positions"], g["closed_positions"]
        ):
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
            if (
                jid < 0
                or not all(math.isfinite(v) for v in (opened, closed))
                or opened == closed
            ):
                raise RuntimeError("invalid gripper joint mapping")
            lo, hi = self.model.jnt_range[jid]
            if not lo <= opened <= hi or not lo <= closed <= hi:
                raise RuntimeError("gripper positions outside model limits")
        for value in (g["open_ctrl"], g["closed_ctrl"]):
            lo, hi = self.model.actuator_ctrlrange[self.gripper_actuator_id]
            if not math.isfinite(value) or not lo <= value <= hi:
                raise RuntimeError("gripper control outside actuator limits")
        if profile.get("keyframe"):
            kid = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_KEY, profile["keyframe"]
            )
            if kid < 0:
                raise RuntimeError("profile keyframe missing")
            mujoco.mj_resetDataKeyframe(self.model, self.data, kid)
        else:
            for name, value in zip(names, profile["home"]):
                jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
                self.data.qpos[self.model.jnt_qposadr[jid]] = value
            for name, value in zip(g["joint_names"], g["open_positions"]):
                jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
                self.data.qpos[self.model.jnt_qposadr[jid]] = value
        for aid, value in zip(self.actuator_ids, profile["home"]):
            self.data.ctrl[aid] = value
        self.data.ctrl[self.gripper_actuator_id] = g["open_ctrl"]
        self.gripper_open = 1.0
        mujoco.mj_forward(self.model, self.data)

    def _measured_gripper_open(self):
        if self.arm_profile is None:
            return self.gripper_open
        import mujoco

        g = self.arm_profile["gripper"]
        fractions = []
        for name, opened, closed in zip(
            g["joint_names"], g["open_positions"], g["closed_positions"]
        ):
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
            value = self.data.qpos[self.model.jnt_qposadr[jid]]
            fractions.append((value - closed) / (opened - closed))
        return max(0.0, min(1.0, sum(fractions) / len(fractions)))

    def _joint_positions(self) -> List[float]:
        import mujoco

        assert self.model is not None and self.data is not None
        q = []
        for name in self.joint_names:
            jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)
            if jid < 0:
                q.append(0.0)
                continue
            qadr = self.model.jnt_qposadr[jid]
            q.append(float(self.data.qpos[qadr]))
        return q

    def apply_estop(self) -> None:
        """Stop motion: hold pose for arm joints, zero planar/base controls."""
        import mujoco

        if self.data is None or self.model is None:
            return
        try:
            if self.arm_profile is not None:
                for aid, value in zip(self.actuator_ids, self._joint_positions()):
                    self.data.ctrl[aid] = value
                g = self.arm_profile["gripper"]
                self.gripper_open = self._measured_gripper_open()
                self.data.ctrl[self.gripper_actuator_id] = g[
                    "closed_ctrl"
                ] + self.gripper_open * (g["open_ctrl"] - g["closed_ctrl"])
                self._sync_viewer()
                self._estop_applied = True
                return
            for i, jname in enumerate(self.joint_names):
                if i >= len(self.actuator_ids):
                    break
                aid = self.actuator_ids[i]
                jid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, jname)
                if jid < 0:
                    self.data.ctrl[aid] = 0.0
                    continue
                jtype = self.model.jnt_type[jid]
                # Planar base / slide / free joints: zero ctrl stops translation.
                if jname.startswith("root_") or jtype == mujoco.mjtJoint.mjJNT_SLIDE:
                    self.data.ctrl[aid] = 0.0
                else:
                    qadr = self.model.jnt_qposadr[jid]
                    self.data.ctrl[aid] = float(self.data.qpos[qadr])
            # Zero any unused actuators.
            for aid in range(self.model.nu):
                if aid not in self.actuator_ids:
                    self.data.ctrl[aid] = 0.0
            mujoco.mj_forward(self.model, self.data)
            self._sync_viewer()
            self._estop_applied = True
        except Exception as e:
            sys.stderr.write(f"[mujoco_worker] estop failed: {e}\n")

    def _poll_estop(self) -> bool:
        """Non-blocking check for an estop line on stdin during mj_step bursts."""
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
        except (ValueError, OSError):
            return False
        if not ready:
            return False
        line = sys.stdin.readline()
        if not line:
            return False
        line = line.strip()
        if not line:
            return False
        try:
            import json

            msg = json.loads(line)
        except Exception:
            sys.stderr.write(
                f"[mujoco_worker] ignored non-JSON while stepping: {line[:80]}\n"
            )
            return False
        if msg.get("type") == "estop":
            self.apply_estop()
            return True
        sys.stderr.write(
            f"[mujoco_worker] unexpected mid-command msg type={msg.get('type')!r}; ignored\n"
        )
        return False

    def _maybe_record_frame(self) -> None:
        if self.record_dir is None or self.renderer is None:
            return
        assert self.model is not None and self.data is not None
        self.video_frame_seq += 1
        self.record_every_n = max(1, round(1 / (30 * self.model.opt.timestep)))
        if self.video_frame_seq % self.record_every_n != 0:
            return
        try:
            self.renderer.update_scene(
                self.data,
                camera=(
                    self.render_camera
                    if self.render_camera is not None
                    else self.camera_name
                ),
            )
            pixels = self.renderer.render()
        except Exception as e:
            sys.stderr.write(f"[mujoco_worker] record frame skipped: {e}\n")
            return
        h, w, _ = pixels.shape
        idx = self.video_frame_seq // self.record_every_n
        path = self.record_dir / f"frame_{idx:06d}.ppm"
        # Atomic write avoids partial frames if the process is interrupted.
        tmp = path.with_suffix(".ppm.tmp")
        write_ppm(str(tmp), w, h, pixels.tobytes())
        os.replace(tmp, path)

    def encode_video(self) -> None:
        if self.video_out is None or self.record_dir is None:
            return
        frames = sorted(self.record_dir.glob("frame_*.ppm"))
        if not frames:
            raise RuntimeError("video requested but no rendered frames available")
        self.video_out.parent.mkdir(parents=True, exist_ok=True)
        pattern = str(self.record_dir / "frame_%06d.ppm")
        cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(1 / (self.record_every_n * self.model.opt.timestep)),
            "-i",
            pattern,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(self.video_out),
        ]
        import subprocess

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            sys.stderr.write(f"[mujoco_worker] video saved: {self.video_out}\n")
        except FileNotFoundError:
            raise RuntimeError("ffmpeg is required to encode the requested video")
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"video encoding failed: {e.stderr.decode(errors='replace')}"
            ) from e

    def _maybe_render(self) -> Optional[Dict[str, Any]]:
        if not self.render or self.renderer is None or self.media_root is None:
            return None
        assert self.model is not None and self.data is not None
        try:
            self.renderer.update_scene(
                self.data,
                camera=(
                    self.render_camera
                    if self.render_camera is not None
                    else self.camera_name
                ),
            )
            pixels = self.renderer.render()
        except Exception as e:
            sys.stderr.write(f"[mujoco_worker] render frame skipped: {e}\n")
            return None
        h, w, _ = pixels.shape
        self.frame_seq += 1
        rel = f"media/images.primary/{self.frame_seq:06d}.ppm"
        path = self.media_root / rel
        tmp = path.with_suffix(".ppm.tmp")
        write_ppm(str(tmp), w, h, pixels.tobytes())
        os.replace(tmp, path)
        return {
            "key": "images.primary",
            "stamp_us": now_us(),
            "frame_id": (
                "overview" if self.render_camera is not None else self.camera_name
            ),
            "encoding": "rgb8",
            "width": w,
            "height": h,
            "uri": str(path),
        }

    def observation(self) -> Dict[str, Any]:
        modalities = []
        mod = self._maybe_render()
        if mod is not None:
            modalities.append(mod)
        return {
            "type": "observation",
            "stamp_us": now_us(),
            "proprio": {
                "joint_names": self.joint_names,
                "positions": self._joint_positions(),
                "gripper_open": self._measured_gripper_open(),
            },
            "modalities": modalities,
            "annotations": self.task_annotations(),
        }

    def task_annotations(self):
        if self.task_evaluator is None:
            return []
        return [
            {
                "kind": "task_state",
                "stamp_us": now_us(),
                "payload": self.task_evaluator.state(
                    self.model, self.data, self._measured_gripper_open()
                ),
            }
        ]

    def solve_ik(self, target, down=True):
        if self.ik is None:
            raise RuntimeError("Cartesian IK requires pick_place scene")
        result = self.ik.solve(self.data.qpos, target, down)
        self.collision_guard.path(self.data.qpos, self.ik.qadr, result)
        return {
            "type": "ik_solution",
            "joint_names": self.joint_names,
            "positions": result,
        }

    def reset(self) -> Dict[str, Any]:
        import mujoco

        assert self.model is not None and self.data is not None
        mujoco.mj_resetData(self.model, self.data)
        mujoco.mj_forward(self.model, self.data)
        self.gripper_open = 0.5
        if self.arm_profile is not None:
            self._configure_arm_profile(self.arm_profile)
        self._estop_applied = False
        if self.task_config is not None:
            from pick_place import TaskEvaluator

            self.task_evaluator = TaskEvaluator(self.model, self.task_config)
        self._sync_viewer()
        self._maybe_record_frame()
        return self.observation()

    def command(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        import mujoco

        assert self.model is not None and self.data is not None
        self._estop_applied = False
        positions = list(msg.get("positions") or [])
        names = list(msg.get("names") or [])

        if self.arm_profile is not None:
            import math

            if names != self.joint_names or len(positions) != len(names):
                raise RuntimeError("command joint layout differs from profile")
            for value, lo, hi in zip(
                positions, self.arm_profile["lower"], self.arm_profile["upper"]
            ):
                if not math.isfinite(value) or not lo <= value <= hi:
                    raise RuntimeError("command position outside profile limits")
            if (
                msg.get("gripper_open") is not None
                and not 0 <= msg["gripper_open"] <= 1
            ):
                raise RuntimeError("gripper openness outside [0,1]")

        if self.collision_guard is not None:
            self.collision_guard.path(self.data.qpos, self.ik.qadr, positions)

        # Map by name when possible
        name_to_pos = {}
        if names and len(names) == len(positions):
            name_to_pos = {n: float(p) for n, p in zip(names, positions)}

        for i, jname in enumerate(self.joint_names):
            if jname in name_to_pos:
                val = name_to_pos[jname]
            elif i < len(positions):
                val = float(positions[i])
            else:
                continue
            if i < len(self.actuator_ids):
                aid = self.actuator_ids[i]
                self.data.ctrl[aid] = val
            elif i < self.model.nu:
                self.data.ctrl[i] = val

        if msg.get("gripper_open") is not None:
            self.gripper_open = float(msg["gripper_open"])
            if self.arm_profile is not None:
                if not 0.0 <= self.gripper_open <= 1.0:
                    raise RuntimeError("gripper openness outside [0,1]")
                g = self.arm_profile["gripper"]
                self.data.ctrl[self.gripper_actuator_id] = g[
                    "closed_ctrl"
                ] + self.gripper_open * (g["open_ctrl"] - g["closed_ctrl"])

        # With a viewer: step near real-time so motion is visible.
        # Headless / video-only: burst steps and capture frames.
        n_sub = 10
        dt = float(self.model.opt.timestep)
        cancelled = False
        for _ in range(n_sub):
            if self._poll_estop():
                cancelled = True
                break
            mujoco.mj_step(self.model, self.data)
            if self.task_evaluator is not None:
                self.task_evaluator.update(self.model, self.data)
            if self.collision_guard is not None:
                try:
                    self.collision_guard.check(self.data)
                except ValueError:
                    self.apply_estop()
                    raise
            if self.viewer is not None:
                self._sync_viewer()
                time.sleep(dt)
            self._maybe_record_frame()
        if self.viewer is None and not cancelled:
            self._sync_viewer()
        return self.observation()


def main() -> int:
    session = MujocoSession()
    try:
        hello = read_msg()
        if hello is None or hello.get("type") != "hello":
            write_error("expected hello")
            return 1
        if hello.get("platform") not in (None, "mujoco"):
            write_error(f"this worker is mujoco, got platform={hello.get('platform')}")
            return 1
        session.load(hello)
        write_msg(
            {
                "type": "hello_ok",
                "protocol_version": PROTOCOL_VERSION,
                "platform": "mujoco",
                "joint_names": session.joint_names,
                "dof": session.dof,
                "model_path": str(session.model_path) if session.model_path else None,
            }
        )

        while True:
            msg = read_msg()
            if msg is None:
                break
            t = msg.get("type")
            if t == "reset":
                write_msg(session.reset())
            elif t == "observe":
                write_msg(session.observation())
            elif t == "solve_ik":
                try:
                    write_msg(session.solve_ik(msg["target"], msg.get("down", True)))
                except (ValueError, RuntimeError) as error:
                    write_error(str(error))
            elif t == "command":
                try:
                    obs = session.command(msg)
                except (ValueError, RuntimeError) as error:
                    session.apply_estop()
                    write_error(str(error))
                    continue
                write_msg(obs)
                # If estop was consumed mid-burst, ack it after the command observation
                # so Rust can drain Obs then Ack in order.
                if session._estop_applied:
                    write_msg({"type": "ack"})
                    session._estop_applied = False
            elif t == "set_media_root":
                root = msg.get("media_root")
                session.set_media_root(root if root else None)
                write_msg({"type": "ack"})
            elif t == "estop":
                if not session._estop_applied:
                    session.apply_estop()
                session._estop_applied = False
                write_msg({"type": "ack"})
            elif t == "shutdown":
                session.encode_video()
                if session.viewer is not None and session.hold_viewer_on_shutdown:
                    session.hold_viewer_until_closed()
                else:
                    session.close_viewer()
                write_msg({"type": "ack"})
                break
            else:
                write_error(f"unknown type: {t}")
                return 1
    except Exception as e:
        write_error(f"{e}\n{traceback.format_exc()}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
