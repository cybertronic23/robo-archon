"""Shared physical pick/place scene, numerical IK and measured task evaluation."""

from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
import mujoco

TASK_VERSION = "pick_place.v1"


def scene_model(model_path, profile, seed=0):
    rid = profile["robot_id"]
    folder = Path(model_path).parent
    robot_file = "panda.xml" if rid == "franka_panda" else "so101_new_calib.xml"
    root = ET.parse(folder / robot_file).getroot()
    root.find("compiler").set("meshdir", str((folder / "assets").resolve()))
    world = root.find("worldbody")
    cube_half = 0.02 if rid == "franka_panda" else 0.012
    x = 0.45 if rid == "franka_panda" else 0.2
    y = (seed % 3 - 1) * 0.015
    start = [x, y, cube_half + 0.002]
    goal = [x, 0.15 if rid == "franka_panda" else 0.06, cube_half + 0.008]
    ET.SubElement(world, "light", pos="0 -1 2", dir="0 0 -1")
    ET.SubElement(
        world,
        "geom",
        name="task_floor",
        type="plane",
        size="2 2 .1",
        rgba=".17 .21 .25 1",
    )
    ET.SubElement(
        world,
        "geom",
        name="task_tray",
        type="box",
        pos=f"{goal[0]} {goal[1]} .003",
        size=".055 .025 .003" if rid == "so101" else ".055 .045 .003",
        rgba=".15 .7 .3 1",
        friction="2 .01 .0001",
    )
    cube = ET.SubElement(world, "body", name="task_cube", pos=" ".join(map(str, start)))
    ET.SubElement(cube, "freejoint", name="task_cube_joint")
    ET.SubElement(
        cube,
        "geom",
        name="task_cube_geom",
        type="box",
        size=" ".join([str(cube_half)] * 3),
        mass=".04" if rid == "franka_panda" else ".008",
        rgba=".95 .12 .06 1",
        friction="3 .01 .0001",
        condim="4",
        solref=".005 1",
    )
    if rid == "franka_panda":
        hand = root.find(".//body[@name='hand']")
        ET.SubElement(
            hand,
            "site",
            name="archon_tcp",
            pos="0 0 .1034",
            size=".003",
            rgba="0 1 1 1",
        )
        axis = 2
        fingers = ["left_finger", "right_finger"]
        for key in root.findall(".//key"):
            key.set(
                "qpos", key.get("qpos") + " " + " ".join(map(str, start)) + " 1 0 0 0"
            )
    else:
        # Retain upstream geometry; add contact pads to the physical jaws.
        gripper = root.find(".//body[@name='gripper']")
        site = gripper.find("site[@name='gripperframe']")
        tcp = np.fromstring(site.get("pos"), sep=" ")
        tcp[0] += 0.0125
        ET.SubElement(
            gripper,
            "site",
            name="archon_tcp",
            pos=" ".join(map(str, tcp)),
            quat=site.get("quat"),
            size=".003",
            rgba="0 1 1 1",
        )
        axis = 0
        fingers = ["gripper", "moving_jaw_so101_v1"]
    # Avoid implicit Euler instability during contact tasks.
    option = root.find("option")
    if option is None:
        option = ET.SubElement(root, "option")
    option.set("timestep", ".002")
    option.set("integrator", "implicitfast")
    option.set("cone", "elliptic")
    if rid == "so101":
        # Convex mesh hulls fill jaw cavities. Use explicit flat fingertip collision pads.
        reference = mujoco.MjModel.from_xml_string(
            ET.tostring(root, encoding="unicode")
        )
        state = mujoco.MjData(reference)
        mujoco.mj_forward(reference, state)
        site_id = mujoco.mj_name2id(reference, mujoco.mjtObj.mjOBJ_SITE, "archon_tcp")
        center = state.site_xpos[site_id]
        rotation = state.site_xmat[site_id].reshape(3, 3)
        for name, sign in [("gripper", -1), ("moving_jaw_so101_v1", 1)]:
            node = root.find(f".//body[@name='{name}']")
            for geom in node.findall("geom[@class='collision']"):
                geom.set("contype", "0")
                geom.set("conaffinity", "0")
            bid = mujoco.mj_name2id(reference, mujoco.mjtObj.mjOBJ_BODY, name)
            local_rotation = state.xmat[bid].reshape(3, 3).T
            position = local_rotation @ (
                center + sign * 0.014 * rotation[:, 2] - state.xpos[bid]
            )
            quat = np.zeros(4)
            mujoco.mju_mat2Quat(quat, (local_rotation @ rotation).flatten())
            ET.SubElement(
                node,
                "geom",
                name=f"{name}_contact_pad",
                type="box",
                pos=" ".join(map(str, position)),
                quat=" ".join(map(str, quat)),
                size=".015 .010 .004",
                contype="1",
                conaffinity="1",
                friction="3 .01 .0001",
                condim="4",
                rgba=".08 .08 .08 1",
                solref=".005 1",
            )
    model = mujoco.MjModel.from_xml_string(ET.tostring(root, encoding="unicode"))
    model.vis.headlight.ambient[:] = 0.4
    model.vis.headlight.diffuse[:] = 0.8
    return model, dict(
        version=TASK_VERSION,
        start=start,
        goal=goal,
        cube_half=cube_half,
        axis=axis,
        fingers=fingers,
        seed=seed,
    )


class CartesianIK:
    def __init__(self, model, profile):
        self.model = model
        self.profile = profile
        self.site = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "archon_tcp")
        if self.site < 0:
            raise ValueError("task TCP missing")
        self.jids = [
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, n)
            for n in profile["joint_names"]
        ]
        self.qadr = model.jnt_qposadr[self.jids]
        self.dadr = model.jnt_dofadr[self.jids]
        self.axis = 2 if profile["robot_id"] == "franka_panda" else 0
        self.lower = np.array(profile["lower"])
        self.upper = np.array(profile["upper"])

    def solve(self, qpos, target, down=True):
        target = np.asarray(target, dtype=float)
        if target.shape != (3,) or not np.isfinite(target).all():
            raise ValueError("invalid Cartesian target")
        data = mujoco.MjData(self.model)
        data.qpos[:] = qpos
        jp = np.zeros((3, self.model.nv))
        jr = np.zeros_like(jp)
        desired = np.array([0, 0, -1.0])
        for iteration in range(400):
            mujoco.mj_forward(self.model, data)
            error = target - data.site_xpos[self.site]
            axis = data.site_xmat[self.site].reshape(3, 3)[:, self.axis]
            rot_error = np.cross(axis, desired)
            if np.linalg.norm(error) < 0.001 and (
                not down or np.linalg.norm(axis - desired) < 0.015
            ):
                return data.qpos[self.qadr].copy().tolist()
            mujoco.mj_jacSite(self.model, data, jp, jr, self.site)
            jac = jp[:, self.dadr]
            residual = error
            if down:
                # Only constrain the tool direction; allow rotation around it for 5-DoF arms.
                project = np.eye(3) - np.outer(axis, axis)
                jac = np.vstack([jac, 0.15 * project @ jr[:, self.dadr]])
                residual = np.r_[error, 0.15 * rot_error]
            delta = jac.T @ np.linalg.solve(
                jac @ jac.T + 0.0001 * np.eye(len(residual)), residual
            )
            data.qpos[self.qadr] = np.clip(
                data.qpos[self.qadr] + np.clip(delta, -0.08, 0.08),
                self.lower + 0.0001,
                self.upper - 0.0001,
            )
        raise ValueError(
            f"IK target unreachable: {target.tolist()} residual={np.linalg.norm(error):.5f}"
        )


class TaskEvaluator:
    def __init__(self, model, config):
        self.config = config
        self.cube = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "task_cube")
        self.cube_geom = mujoco.mj_name2id(
            model, mujoco.mjtObj.mjOBJ_GEOM, "task_cube_geom"
        )
        self.fingers = {
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, n)
            for n in config["fingers"]
        }
        self.peak = config["start"][2]
        self.grasp_seen = False

    def update(self, model, data):
        self.peak = max(self.peak, float(data.xpos[self.cube, 2]))
        touched = set()
        for contact in data.contact:
            if self.cube_geom == contact.geom1:
                touched.add(int(model.geom_bodyid[contact.geom2]))
            elif self.cube_geom == contact.geom2:
                touched.add(int(model.geom_bodyid[contact.geom1]))
        self.grasp_seen |= self.fingers.issubset(touched)

    def state(self, model, data, openness):
        self.update(model, data)
        pos = data.xpos[self.cube].copy()
        speed = float(np.linalg.norm(data.cvel[self.cube, 3:]))
        return measured_verdict(
            self.config, pos, speed, openness, self.peak, self.grasp_seen
        )


def measured_verdict(config, pos, speed, openness, peak, grasp_seen):
    lifted = peak > config["start"][2] + 0.05
    placed = (
        np.linalg.norm(np.asarray(pos)[:2] - np.array(config["goal"][:2])) < 0.025
        and abs(pos[2] - config["goal"][2]) < 0.015
    )
    return dict(
        version=TASK_VERSION,
        object_position=np.asarray(pos).tolist(),
        goal_position=config["goal"],
        peak_height=float(peak),
        grasp_seen=bool(grasp_seen),
        lifted=bool(lifted),
        released=bool(openness > 0.8),
        speed=float(speed),
        success=bool(
            grasp_seen and lifted and placed and openness > 0.8 and speed < 0.05
        ),
    )


class CollisionGuard:
    """Task contact whitelist, not a general obstacle-avoiding motion planner."""

    def __init__(self, model, config):
        self.model = model
        self.cube = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "task_cube")
        self.fingers = {
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, n)
            for n in config["fingers"]
        }
        self.fixed = {0}
        for bid in range(1, model.nbody):
            if (
                int(model.body_parentid[bid]) in self.fixed
                and model.body_dofnum[bid] == 0
            ):
                self.fixed.add(bid)

    def check(self, data):
        for contact in data.contact:
            if contact.dist > -0.0005:
                continue  # solver/contact skin tolerance: 0.5 mm
            a, b = (
                int(self.model.geom_bodyid[g]) for g in (contact.geom1, contact.geom2)
            )
            if self.cube in (a, b):
                other = b if a == self.cube else a
                if other == 0 or other in self.fingers:
                    continue
            elif a in self.fixed and b in self.fixed:
                continue
            elif a in self.fingers and b in self.fingers:
                continue
            elif (
                int(self.model.body_parentid[a]) == b
                or int(self.model.body_parentid[b]) == a
            ):
                continue
            names = [
                mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, g)
                or f"geom#{g}"
                for g in (contact.geom1, contact.geom2)
            ]
            raise ValueError(
                f"collision rejected: {names}, penetration={-contact.dist:.6f}m"
            )

    def path(self, qpos, qadr, target):
        data = mujoco.MjData(self.model)
        data.qpos[:] = qpos
        start = data.qpos[qadr].copy()
        target = np.asarray(target)
        mujoco.mj_forward(self.model, data)
        touched = set()
        for c in data.contact:
            a, b = (int(self.model.geom_bodyid[g]) for g in (c.geom1, c.geom2))
            if a == self.cube:
                touched.add(b)
            if b == self.cube:
                touched.add(a)
        carried = self.fingers.issubset(touched)
        site = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "archon_tcp")
        rotation = data.site_xmat[site].reshape(3, 3).copy()
        offset = rotation.T @ (data.xpos[self.cube] - data.site_xpos[site])
        relative_rotation = rotation.T @ data.xmat[self.cube].reshape(3, 3)
        cube_adr = int(self.model.jnt_qposadr[int(self.model.body_jntadr[self.cube])])
        # Limit joint sampling interval to 0.02 rad; execution also checks every 2ms.
        count = max(2, int(np.ceil(np.max(np.abs(target - start)) / 0.02)) + 1)
        for fraction in np.linspace(0, 1, count):
            data.qpos[qadr] = start + (target - start) * fraction
            mujoco.mj_forward(self.model, data)
            if carried:
                # Predict held-object pose only in scratch planning data, never in physics.
                rotation = data.site_xmat[site].reshape(3, 3)
                data.qpos[cube_adr : cube_adr + 3] = (
                    data.site_xpos[site] + rotation @ offset
                )
                quat = np.zeros(4)
                mujoco.mju_mat2Quat(quat, (rotation @ relative_rotation).flatten())
                data.qpos[cube_adr + 3 : cube_adr + 7] = quat
                mujoco.mj_forward(self.model, data)
            self.check(data)
